"""T2 (docs/devdoc/research/0728_freezer_latency_research.md) — 마이크로배치·프리페치 검증.

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
        # ET는 기본 off (이슈 #22 0805) — 배치 중간 발동 규칙 자체를 고정하는
        # 테스트라 명시적으로 켠다 (단일 재고라 유일해 게이트 통과).
        base = _pipe(
            cola, FakeBatchDetector(), profile=REFRIGERATOR, zone=1,
            early_termination_enabled=True,
        ).process("s1", _request(zone=1))
        batched = _pipe(
            cola, FakeBatchDetector(), profile=REFRIGERATOR, zone=1, batch_size=4,
            early_termination_enabled=True,
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
            cola, FakeDetector(), profile=REFRIGERATOR, zone=1, prefetch_depth=2,
            early_termination_enabled=True,  # 기본 off (이슈 #22) — finally 계약 검증용 opt-in
        ).process("s1", req)
        assert outcome.trace.early_terminated
        assert closed["side"] is True


class TestSettingsWiring:
    def test_prefetch_env(self, monkeypatch):
        from crk_model.core.config import Settings

        assert Settings().prefetch_depth == 0
        monkeypatch.setenv("MODEL__VIDEO__PREFETCH", "4")
        assert Settings.from_env().prefetch_depth == 4


class TestStaticBatchEngineAdapter:
    """정적 batch 엔진 계약 (2026-07-29 실기 기동 실패 회귀 고정):
    ultralytics TRT backend는 정적 엔진에서 모든 predict가 정확히 batch
    크기여야 한다 ("input size ... not equal to max model size").
    → batch>1이면 단일 프레임 detect도 detect_batch(패딩)로 위임한다."""

    def _bare(self, batch):
        from crk_model.adapters.yolo_detector import UltralyticsEngineDetector

        det = UltralyticsEngineDetector.__new__(UltralyticsEngineDetector)
        det._imgsz = 480
        det._conf = 0.01
        det._max_det = 20
        det._hand_class = 0
        det._device = 0
        det._batch = batch
        det._model = None  # predict에 도달하면 그 자체가 실패 신호
        return det

    def test_single_detect_delegates_to_batch_path(self, monkeypatch):
        det = self._bare(batch=4)
        seen = {}

        def fake_batch(frames, allowed_class_ids=None):
            seen["n"] = len(frames)
            seen["allowed"] = allowed_class_ids
            return [["sentinel-detections"]]

        monkeypatch.setattr(det, "detect_batch", fake_batch)
        out = det.detect("frame", allowed_class_ids=(1, 2))
        assert out == ["sentinel-detections"]
        assert seen == {"n": 1, "allowed": (1, 2)}

    def test_non_square_with_static_batch_raises(self):
        np = pytest.importorskip("numpy")  # 비정방 입력 픽스처 생성용

        det = self._bare(batch=4)
        with pytest.raises(ValueError, match="static batch engine"):
            det.detect_batch([np.zeros((240, 480, 3), dtype=np.uint8)])

    def test_non_square_with_batch1_falls_back_per_frame(self, monkeypatch):
        np = pytest.importorskip("numpy")  # 비정방 입력 픽스처 생성용

        det = self._bare(batch=1)
        calls = []
        monkeypatch.setattr(
            det, "detect", lambda f, allowed_class_ids=None: calls.append(1) or []
        )
        out = det.detect_batch(
            [np.zeros((240, 480, 3), dtype=np.uint8)] * 2
        )
        assert out == [[], []] and calls == [1, 1]

    def test_empty_allowlist_fail_closed_without_predict(self):
        det = self._bare(batch=4)
        assert det.detect_batch(["f1", "f2"], allowed_class_ids=()) == [[], []]
        assert det.detect("f1", allowed_class_ids=()) == []


class TestStreamOpenTiming:
    """스트림 오픈 시점 계약: 기본값(PREFETCH=0)은 현행과 동일하게 카메라
    차례에 열고, 프리페치 활성 시에만 트리거 시작 시 전 카메라를 함께 연다
    — LazyAviFrames.__getitem__ == ffmpeg spawn이라 오픈 시점이 곧 동작이다."""

    class _RecordingFrames:
        def __init__(self, data, events):
            self._d = data
            self._events = events

        def get(self, camera):
            if camera not in self._d:
                return None
            self._events.append(("open", camera))
            return self._d[camera]

    def test_default_opens_side_only_after_top_consumed(self, cola):
        events = []

        def top_stream():
            for f in moving_frames(12):
                events.append(("frame", "top"))
                yield f

        req = TriggerRequest(
            2,
            self._RecordingFrames(
                {"top": top_stream(), "side": moving_frames(12)}, events
            ),
            samples(500, 400),
            1.0,
        )
        _pipe(cola, FakeDetector()).process("s1", req)
        assert events[0] == ("open", "top")
        assert events[-1] == ("open", "side")  # top 소비가 끝난 뒤에야 오픈
        assert ("frame", "top") in events

    def test_prefetch_opens_all_cameras_at_start(self, cola):
        events = []
        req = TriggerRequest(
            2,
            self._RecordingFrames(
                {"top": moving_frames(12), "side": moving_frames(12)}, events
            ),
            samples(500, 400),
            1.0,
        )
        _pipe(cola, FakeDetector(), prefetch_depth=2).process("s1", req)
        assert events[:2] == [("open", "top"), ("open", "side")]


class TestTensorInputSwitch:
    """T2-1 단독 스위치 (MODEL__VISION__TENSOR_INPUT): batch_size=1이어도
    detect_batch(1프레임) 경로로 — GPU 전처리 효과를 배치와 분리 측정."""

    def test_routes_through_batch_path_with_batch1(self, cola):
        detector = FakeBatchDetector()
        outcome = _pipe(cola, detector, tensor_input=True).process(
            "s1", _request()
        )
        # 배치 경로가 쓰였고, 전부 1프레임 배치다 (batch-1 엔진 호환)
        assert detector.batch_sizes and set(detector.batch_sizes) == {1}
        # 판정은 프레임별 경로와 동일
        base = _pipe(cola, FakeBatchDetector()).process("s1", _request())
        assert outcome.event.judgment == base.event.judgment
        assert outcome.trace.yolo_calls == base.trace.yolo_calls

    def test_ignored_without_detect_batch(self, cola):
        outcome = _pipe(cola, FakeDetector(), tensor_input=True).process(
            "s1", _request()
        )
        base = _pipe(cola, FakeDetector()).process("s1", _request())
        assert outcome.event.judgment == base.event.judgment

    def test_env_wiring(self, monkeypatch):
        from crk_model.core.config import Settings

        assert Settings().tensor_input is False
        monkeypatch.setenv("MODEL__VISION__TENSOR_INPUT", "1")
        assert Settings.from_env().tensor_input is True
