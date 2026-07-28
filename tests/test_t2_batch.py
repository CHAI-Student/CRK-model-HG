"""T2 (docs/0728_freezer_latency_research.md) — 마이크로배치·프리페치 검증.

핵심 계약: batch_size/prefetch는 **속도 레버일 뿐 판정을 바꾸지 않는다**.
- 배치 경로는 프레임별 경로와 소비 순서·의미가 동일 (consume 단일 경로)
- 조기 종료가 배치 중간에 발동하면 잔여 결과 폐기 → 비배치와 투표 동등
- 프리페처는 순서 보존·예외 전파(I1)·close 전파(조기 종료 자원 해제)
"""
from __future__ import annotations

import pytest
from test_service import FakeDetector, moving_frames, samples

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.frames.prefetch import PrefetchFrames
from crk_model.service import ActiveProductStore, TriggerPipeline, TriggerRequest


class FakeBatchDetector(FakeDetector):
    """detect_batch = 프레임별 detect의 순차 합성 — 호출 순서 보존으로
    drift(calls 기반)까지 프레임별 경로와 동일한 출력을 낸다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch_sizes: list[int] = []

    def detect_batch(self, frames, allowed_class_ids=None):
        self.batch_sizes.append(len(frames))
        return [
            list(self.detect(f, allowed_class_ids=allowed_class_ids))
            for f in frames
        ]


def _pipe(cola, detector, profile=FREEZER, zone=2, **kwargs):
    store = ActiveProductStore()
    store.update([cola])
    return TriggerPipeline(detector, {zone: profile}, store, **kwargs)


def _request(zone=2, n=20):
    return TriggerRequest(
        zone,
        {"top": moving_frames(n), "side": moving_frames(n)},
        samples(500, 400),  # delta -100
        1.0,
    )


class TestBatchJudgmentParity:
    def test_freezer_batch4_equals_unbatched(self, cola):
        base = _pipe(cola, FakeBatchDetector()).process("s1", _request())
        batched_detector = FakeBatchDetector()
        batched = _pipe(
            cola, batched_detector, batch_size=4, save_detections=True
        ).process("s1", _request())

        assert batched.event.judgment == base.event.judgment
        assert batched.event.vision_candidates == base.event.vision_candidates
        assert batched.trace.yolo_calls == base.trace.yolo_calls
        assert batched.trace.processed_frames == base.trace.processed_frames
        # 배치 경로가 실제로 쓰였고, 요청 배치가 batch_size를 넘지 않는다
        assert batched_detector.batch_sizes
        assert max(batched_detector.batch_sizes) <= 4

    def test_remainder_batch_flushed(self, cola):
        """게이트 통과 수가 batch의 배수가 아니어도 잔여분이 추론된다."""
        detector = FakeBatchDetector()
        outcome = _pipe(cola, detector, batch_size=7).process("s1", _request())
        assert outcome.trace.yolo_calls == sum(detector.batch_sizes)
        assert outcome.trace.yolo_calls > 0

    def test_batch_ignored_without_detect_batch(self, cola):
        """detect_batch 미제공 검출기는 batch_size와 무관하게 프레임별 경로."""
        detector = FakeDetector()  # detect_batch 없음
        outcome = _pipe(cola, detector, batch_size=4).process("s1", _request())
        base = _pipe(cola, FakeDetector()).process("s1", _request())
        assert outcome.event.judgment == base.event.judgment

    def test_fridge_early_termination_mid_batch_parity(self, cola):
        """조기 종료(냉장)가 배치 중간에 발동해도 판정·투표가 비배치와 같다
        — 잔여 배치 결과 폐기 규칙의 회귀 고정."""
        base = _pipe(
            cola, FakeBatchDetector(), profile=REFRIGERATOR, zone=1
        ).process("s1", _request(zone=1))
        batched = _pipe(
            cola, FakeBatchDetector(), profile=REFRIGERATOR, zone=1, batch_size=4
        ).process("s1", _request(zone=1))

        assert base.trace.early_terminated  # 전제: ET가 실제로 발동하는 시나리오
        assert batched.trace.early_terminated
        assert batched.event.judgment == base.event.judgment
        assert batched.event.vision_candidates == base.event.vision_candidates
        assert batched.trace.yolo_calls == base.trace.yolo_calls  # 소비분만 집계


class TestPrefetchFrames:
    def test_preserves_order_and_exhausts(self):
        pf = PrefetchFrames(iter(range(50)), depth=4)
        assert list(pf) == list(range(50))
        with pytest.raises(StopIteration):
            next(pf)

    def test_propagates_source_error_after_frames(self):
        def gen():
            yield 1
            yield 2
            raise OSError("decode failed")

        pf = PrefetchFrames(gen(), depth=2)
        assert next(pf) == 1
        assert next(pf) == 2
        with pytest.raises(OSError):  # I1: 무검출로 삼키지 않는다
            next(pf)

    def test_close_stops_and_closes_source(self):
        closed = []

        def gen():
            try:
                yield from range(1000)
            finally:
                closed.append(True)

        pf = PrefetchFrames(gen(), depth=2)
        assert next(pf) == 0
        pf.close()
        pf.close()  # 멱등
        assert closed == [True]
        with pytest.raises(StopIteration):
            next(pf)

    def test_pipeline_with_prefetch_judgment_parity(self, cola):
        base = _pipe(cola, FakeDetector()).process("s1", _request())
        prefetched = _pipe(cola, FakeDetector(), prefetch_depth=3).process(
            "s1", _request()
        )
        assert prefetched.event.judgment == base.event.judgment
        assert prefetched.trace.yolo_calls == base.trace.yolo_calls

    def test_pipeline_prefetch_closes_unconsumed_side_on_early_stop(self, cola):
        """조기 종료(냉장)로 side를 순회하다 멈춰도 프리페처가 닫혀 백그라운드
        디코드가 남지 않는다 (finally 계약)."""
        closed = {"side": False}

        def side_stream():
            try:
                yield from moving_frames(300)
            finally:
                closed["side"] = True

        req = TriggerRequest(
            1,
            {"top": moving_frames(20), "side": side_stream()},
            samples(500, 400),
            1.0,
        )
        outcome = _pipe(
            cola, FakeDetector(), profile=REFRIGERATOR, zone=1, prefetch_depth=2
        ).process("s1", req)
        assert outcome.trace.early_terminated
        assert closed["side"] is True


class TestSettingsWiring:
    def test_prefetch_env(self, monkeypatch):
        from crk_model.core.config import Settings

        assert Settings().prefetch_depth == 0
        monkeypatch.setenv("MODEL__VIDEO__PREFETCH", "4")
        assert Settings.from_env().prefetch_depth == 4
