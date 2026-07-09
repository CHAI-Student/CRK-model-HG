"""service: 연결단 검증 — E2E 흐름, I2 fail-closed, 저무게 스킵, I1 에러 전파,
멱등성(I7), 기동 fail-fast(리뷰 #1), 저널 replay(G2.5 훅), 필터 체인."""
from dataclasses import asdict

import pytest

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import JudgmentStatus
from crk_model.ingest.loadcell import LoadcellAnalyzer, LoadcellSample
from crk_model.ledger.journal import EventJournal
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detection
from crk_model.perception.filters import DetectionFilterChain
from crk_model.service import (
    ActiveProductStore,
    ModelService,
    TriggerPipeline,
    TriggerRequest,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeDetector:
    """항상 class_id=1 (콜라) 검출. 호출 횟수 추적."""

    def __init__(self, detections=None, error=None):
        self.calls = 0
        self.error = error
        self._detections = detections if detections is not None else [
            Detection(1, 0.8, bbox=(50.0, 50.0, 100.0, 100.0))
        ]

    def detect(self, frame):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self._detections)


def frame(value):
    return [[value] * 4 for _ in range(4)]


def moving_frames(n):
    return [frame(10 if i % 2 == 0 else 200) for i in range(n)]


def samples(start, end, n=10, dt=0.1):
    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value / 2, value / 2)))
        ts += dt
    return out


def make_service(detector=None, clock=None, journal=None):
    # close_grace_s=0: 유예 창은 test_gateway의 전용 테스트에서 검증 —
    # 여기서는 FakeClock(t=0) 기반 E2E 흐름을 즉시 확정으로 단순화한다.
    return ModelService(
        detector or FakeDetector(),
        profiles={1: REFRIGERATOR},
        clock=clock or FakeClock(),
        journal=journal,
        settings=Settings(close_grace_s=0.0),
    )


def open_payload(cola):
    return {"session_id": "s1", "state": "OPEN", "active_products": [asdict(cola)]}


def trigger_payload(zone=1, n_frames=8):
    return {
        "zone": zone,
        "frames": {"top": moving_frames(n_frames), "side": moving_frames(n_frames)},
        "loadcells": samples(500, 400),  # delta -100
        "ts": 1.0,
        "video_paths": {"top": "/t.avi", "side": "/s.avi"},
    }


class TestEndToEnd:
    def test_open_trigger_close_complete(self, cola):
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        resp = svc.handle_trigger(trigger_payload())
        assert resp["status"] == "queued"

        # 큐 미소진 상태의 CLOSE → 배리어가 확정을 막음 (I17)
        close1 = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert close1["status"] == "processing" and close1["provisional"] is True
        assert "queue_pending" in close1["detail"]

        svc.process_pending()
        close2 = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert close2["status"] == "success"
        assert close2["totalPrice"] == 1500
        assert close2["productCount"] == 1

    def test_early_termination_saves_yolo_calls(self, cola):
        detector = FakeDetector()
        svc = make_service(detector)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload(n_frames=10))
        svc.process_pending()
        trace = svc.worker.outcomes[0].trace
        assert trace.early_terminated  # L2: 증거 수렴 후 추론 중단
        assert detector.calls < 20  # 전 프레임(10×2) 미만

    def test_close_after_delivery_reports_no_active_session(self, cola):
        # 확정 결과는 1회 전달, 이후 CLOSE 재폴링은 원본 wire 계약
        # "No active door session to close"(success) — 에지는 이 응답으로
        # device busy를 해제한다 (complete 반복 응답 시 busy 영구 유지, 실기 확인).
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        first = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        second = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert first["status"] == "success" and first["totalPrice"] == 1500
        assert second["success"] is True and second["status"] == "success"
        assert second["totalPrice"] == 0 and second["zones"] == []
        assert second["message"] == "No active door session to close"

    def test_repoll_after_complete_does_not_spam_log(self, cola, caplog):
        # issue #5: CLOSE는 문이 닫혀있는 동안 계속 재폴링되는 level-triggered
        # 신호라 결과가 바뀌지 않는 한 [MULTI-ZONE CLOSE] 로그를 반복하면 안 된다
        # (응답 자체는 매번 그대로 반환하되, 동일 로그가 몇 분씩 반복 찍히는 것만 억제).
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        with caplog.at_level("INFO", logger="crk_model.service.model_service"):
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        close_logs = [
            r
            for r in caplog.records
            if "[MULTI-ZONE CLOSE]" in r.message and "-> finalized" in r.message
        ]
        assert len(close_logs) == 1

    def test_consecutive_door_sessions_are_independent(self, cola):
        # 이슈 #3 회귀: 세션 ID가 고정("global")이면 EventLog 확정 거부(I11)와
        # settler 멱등 캐시가 세션을 관통 → 두 번째 세션이 첫 결과를 재탕했다.
        clock = FakeClock()
        svc = make_service(clock=clock)
        # 세션 1: 트리거 1건 → 1500원
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        first = svc.handle_multi_zone({"state": "CLOSE"})
        assert first["status"] == "success" and first["totalPrice"] == 1500

        # 세션 2: 트리거 2건 → 3000원 (캐시 재탕이면 1500, 이벤트 누적이면 4500)
        clock.t += 100.0  # 멱등성 TTL 경과
        svc.handle_multi_zone(open_payload(cola))
        for cam in ("a", "b"):
            p = trigger_payload()
            p["video_paths"] = {"top": f"/{cam}.avi"}
            assert svc.handle_trigger(p)["status"] == "queued"  # 멱등 드롭 아님
        svc.process_pending()
        second = svc.handle_multi_zone({"state": "CLOSE"})
        assert second["status"] == "success"
        assert second["totalPrice"] == 3000

    def test_error_session_recovers_on_next_open(self, cola):
        # 이슈 #3: ERROR 세션 후 door_state가 영구 error로 고착 —
        # 다음 OPEN이 새 세션을 발급해 복구해야 한다.
        clock = FakeClock()
        detector = FakeDetector(error=RuntimeError("gpu"))
        svc = make_service(detector, clock=clock)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()  # I1: 처리 예외 → error 이벤트
        err = svc.handle_multi_zone({"state": "CLOSE"})
        assert err["status"] == "error"  # I13: blocked settlement

        # 다음 손님: 문 열림 → 정상 세션으로 복구
        detector.error = None
        clock.t += 100.0
        opened = svc.handle_multi_zone(open_payload(cola))
        assert opened["status"] == "processing"  # error 고착 아님
        p = trigger_payload()
        p["video_paths"] = {"top": "/t2.avi"}
        svc.handle_trigger(p)
        svc.process_pending()
        done = svc.handle_multi_zone({"state": "CLOSE"})
        assert done["status"] == "success"
        assert done["totalPrice"] == 1500


class TestGuards:
    def test_empty_allowlist_fail_closed(self):
        # I2: OPEN 스냅샷 없음 → 추론 차단, YOLO 호출 0
        detector = FakeDetector()
        store = ActiveProductStore()
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(1, {"top": moving_frames(4)}, samples(500, 400), 1.0),
        )
        assert outcome.event.judgment.reason == "empty_allowlist_fail_closed"
        assert detector.calls == 0

    def test_last_valid_snapshot_fallback(self, cola):
        # I2 폴백: 스냅샷 상실 시 last_valid로 추론 지속 + 사유 기록
        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        store.update([])  # 컨텍스트 상실
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(1, {"top": moving_frames(4)}, samples(500, 400), 1.0),
        )
        assert "snapshot_source=last_valid" in outcome.trace.reason_codes
        assert outcome.event.judgment.status is not JudgmentStatus.ERROR

    def test_low_weight_skip_avoids_yolo(self, cola):
        # QA Q8: |delta| < 5g → vision 전체 생략 (YOLO 호출 0)
        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(
            detector,
            {1: REFRIGERATOR},
            store,
            analyzer_factory=lambda p: LoadcellAnalyzer(p, stability_threshold_grams=0.5),
        )
        outcome = pipe.process(
            "s1",
            TriggerRequest(1, {"top": moving_frames(4)}, samples(500, 496), 1.0),
        )
        assert outcome.event.judgment.reason == "below_min_weight_change"
        assert outcome.event.judgment.strategy == "low_weight_skip"
        assert detector.calls == 0

    def test_processing_error_propagates_as_error_event(self, cola):
        # I1 + I13: 검출기 예외 → error 이벤트 → close 시 결제 차단
        svc = make_service(FakeDetector(error=RuntimeError("engine crash")))
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        event = svc.worker.outcomes[0].event
        assert event.status == "error"
        assert event.judgment.status is JudgmentStatus.ERROR
        close = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert close["status"] == "error"
        assert "totalPrice" not in close  # 결제 필드 자체가 없음

    def test_duplicate_trigger_dropped(self, cola):
        # I7: 동일 zone+video_paths 5s 내 재전송 → 드롭
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        first = svc.handle_trigger(trigger_payload())
        second = svc.handle_trigger(trigger_payload())
        assert second["status"] == "duplicate"
        assert second["trigger_id"] == first["trigger_id"]
        assert svc.worker.pending == 1

    def test_startup_probe_fail_fast(self):
        # 이관 리뷰 #1: 검출기 로드 실패 = 기동 실패 (무증상 기동 금지)
        with pytest.raises(RuntimeError):
            ModelService(
                FakeDetector(error=RuntimeError("no engine")),
                profiles={1: REFRIGERATOR},
                startup_probe_frame=frame(0),
            )


class TestJournalReplay:
    def test_replay_settlement_equivalence(self, cola, tmp_path):
        # G2.5 훅: 저널 재생 → 신규 정산기 결과가 운영 결과와 동일
        journal = EventJournal(tmp_path / "events.jsonl")
        svc = make_service(journal=journal)
        svc.handle_multi_zone(open_payload(cola))
        sid = svc.gateway.session_id  # 세션 ID는 서비스가 발급 (세션마다 유일)
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        live = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})

        replayed = journal.replay(sid)
        assert len(replayed) == 1
        result = CloseSettler().settle(sid, replayed, {1: REFRIGERATOR})
        assert result.total_price == live["totalPrice"]


class TestFilterChain:
    def test_side_roi_drops_out_of_zone(self):
        f = DetectionFilterChain(side_roi_max_center_x=240.0)
        inside = Detection(1, 0.8, bbox=(100, 100, 200, 200))   # cx=150
        outside = Detection(2, 0.8, bbox=(260, 100, 360, 200))  # cx=310
        assert f.apply("side", [inside, outside]) == [inside]
        assert f.apply("top", [outside]) == [outside]  # top은 ROI 미적용

    def test_hand_path_proximity(self):
        f = DetectionFilterChain(hand_margin_px=40.0)
        hand = Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))
        near = Detection(1, 0.8, bbox=(30, 30, 60, 60))
        far = Detection(2, 0.8, bbox=(300, 300, 340, 340))
        out = f.apply("top", [hand, near, far])
        assert hand in out and near in out and far not in out

    def test_no_hand_history_keeps_detections(self):
        # 손 이력 없음 → fail-open (증거 보존 방향)
        f = DetectionFilterChain()
        d = Detection(1, 0.8, bbox=(300, 300, 340, 340))
        assert f.apply("top", [d]) == [d]

    def test_unknown_bbox_passes_spatial_filters(self):
        f = DetectionFilterChain()
        f.apply("side", [Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))])
        no_bbox = Detection(1, 0.8)  # 공간 정보 없음
        assert no_bbox in f.apply("side", [no_bbox])
