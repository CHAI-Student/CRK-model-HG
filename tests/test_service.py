"""service: 연결단 검증 — E2E 흐름, I2 fail-closed, 저무게 스킵, I1 에러 전파,
멱등성(I7), 기동 fail-fast(리뷰 #1), 저널 replay(G2.5 훅), 필터 체인."""
from dataclasses import asdict

import pytest

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import ActiveProduct, JudgmentStatus
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

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        self.last_allowed = allowed_class_ids
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
        out.append(LoadcellSample(ts, (value, 0.0)))  # 트레이 분리: 하중은 단일 채널에
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


def dual_tray_samples(ch0_step, ch1_step, n=10, dt=0.1):
    """두 트레이 동시 이벤트: 채널별 계단형 시계열 (2단계 검증용)."""
    out, ts = [], 0.0
    (a0, a1), (b0, b1) = ch0_step, ch1_step
    for k in range(2 * n):
        v0 = a0 if k < n else a1
        v1 = b0 if k < n else b1
        out.append(LoadcellSample(ts, (v0, v1)))
        ts += dt
    return out


class TestMultiTrayEvents:
    """2단계: 트레이별 동시 이벤트를 이벤트당 개별 판정 후 병합."""

    def _service(self, cola, second):
        detector = FakeDetector(detections=[
            Detection(1, 0.85, bbox=(50.0, 50.0, 100.0, 100.0)),
            Detection(second.class_id, 0.80, bbox=(150.0, 50.0, 200.0, 100.0)),
        ])
        svc = make_service(detector)
        svc.handle_multi_zone({
            "session_id": "s1", "state": "OPEN",
            "active_products": [asdict(cola), asdict(second)],
        })
        return svc

    def test_simultaneous_two_tray_pickup_charges_both(self, cola, bar170):
        # 트레이0에서 콜라(-100), 트레이1에서 아이스바(-170) 동시 취출:
        # 합산 -270의 조합 탐색 없이 이벤트별 단품 매칭으로 분해된다
        svc = self._service(cola, bar170)
        payload = trigger_payload()
        payload["loadcells"] = dual_tray_samples((500, 400), (400, 230))
        svc.handle_trigger(payload)
        svc.process_pending()
        close = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert close["status"] == "success"
        assert close["totalPrice"] == 1500 + 2000
        assert close["productCount"] == 2

    def test_same_product_from_both_trays_merges_count(self, cola, bar170):
        # 두 트레이에서 같은 상품(콜라 100g)을 하나씩 → count 2로 병합
        svc = self._service(cola, bar170)
        payload = trigger_payload()
        payload["loadcells"] = dual_tray_samples((500, 400), (300, 200))
        svc.handle_trigger(payload)
        svc.process_pending()
        close = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert close["status"] == "success"
        assert close["totalPrice"] == 1500 * 2

    def test_single_tray_event_keeps_legacy_path(self, cola, bar170):
        # 이벤트 1개면 기존 단일 판정 경로 그대로 (multi_tray 미발동)
        svc = self._service(cola, bar170)
        payload = trigger_payload()
        payload["loadcells"] = dual_tray_samples((500, 400), (300, 300))
        svc.handle_trigger(payload)
        svc.process_pending()
        outcome = svc.worker.outcomes[0]
        assert not outcome.event.judgment.strategy.startswith("multi_tray")
        assert abs(outcome.event.delta_weight - (-100)) < 1.0


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


class TestAllowedClassIds:
    """P0-2 (perf-gap 보고서): 판매중 상품 class 허용목록의 카메라별 전달."""

    class RecordingDetector(FakeDetector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.allowed_seen = []

        def detect(self, frame, allowed_class_ids=None):
            self.allowed_seen.append(tuple(allowed_class_ids or ()))
            return super().detect(frame, allowed_class_ids)

    def test_top_gets_products_plus_hand_side_products_only(self, cola):
        # 원본 _inference_allowed_class_ids 동형: top = 상품 + hand(0),
        # side = 상품만 (side는 hand를 추론하지 않는다).
        detector = self.RecordingDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        pipe.process(
            "s1",
            TriggerRequest(
                1,
                {"top": moving_frames(4), "side": moving_frames(4)},
                samples(500, 400),
                1.0,
            ),
        )
        assert (1, 0) in detector.allowed_seen  # top: 콜라 + hand
        assert (1,) in detector.allowed_seen  # side: 상품만
        assert all(s in ((1, 0), (1,)) for s in detector.allowed_seen)

    def test_unmapped_sentinel_excluded_from_allowlist(self, cola):
        # 미매핑 상품(class_id=-1 센티널, issue #6)은 허용목록에서 제외 —
        # -1이 predict classes로 흘러가면 안 된다.
        unmapped = ActiveProduct(
            "P099", "미매핑", class_id=-1, unit_weight=333.0, unit_price=2000, stock_qty=3
        )
        detector = self.RecordingDetector()
        store = ActiveProductStore()
        store.update([cola, unmapped])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        pipe.process(
            "s1",
            TriggerRequest(1, {"top": moving_frames(4)}, samples(500, 400), 1.0),
        )
        assert detector.allowed_seen  # 추론은 일어났고
        assert all(-1 not in s for s in detector.allowed_seen)


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


class TestVisionTopObservability:
    def test_flags_when_top_candidate_not_billed(self, cola):
        # 이슈 #15: 65표 1위가 미매핑/게이트 탈락으로 소멸하고 하위 후보가
        # 과금될 때, 파이프라인이 순위 역전 사실을 reason_codes로 남긴다
        from crk_model.core.types import JudgmentResult, ProductCount, VisionCandidate
        from crk_model.service.pipeline import _vision_top_not_billed

        j = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(cola, 1),), 0.5, "strict"
        )
        top23 = VisionCandidate(23, 0.9, 65, 0.2)
        mine = VisionCandidate(cola.class_id, 0.6, 16, 0.05)
        assert _vision_top_not_billed((top23, mine), j) == "vision_top_not_billed:class23"
        assert _vision_top_not_billed((mine,), j) is None  # 1위가 곧 과금 품목
        assert _vision_top_not_billed((), j) is None  # 후보 없음
        empty = JudgmentResult(JudgmentStatus.NO_DETECTION, reason="x")
        assert _vision_top_not_billed((top23,), empty) is None  # 무과금은 대상 아님


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

    def test_static_track_suppresses_protruding_display_item(self):
        # 이슈 #10: 전시 영역 밖 돌출 상품 — 같은 자리에 정지한 검출은
        # min_frames 이후 투표에서 제거, 그 전까지의 표는 보존(fail-open 방향)
        f = DetectionFilterChain(static_track_min_frames=10, static_track_iou=0.85)
        static = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        passed = 0
        for _ in range(30):
            passed += static in f.apply("top", [static])
        assert passed == 9  # 10번째 관측부터 억제 (count >= min_frames)
        assert f.drop_stats["static_track"]["top"] == 21

    def test_static_track_releases_when_item_moves(self):
        # 손님이 바로 그 돌출 상품을 집으면(bbox 이동) 억제가 즉시 풀린다
        f = DetectionFilterChain(static_track_min_frames=10, static_track_iou=0.85)
        for _ in range(15):
            f.apply("top", [Detection(27, 0.9, bbox=(100, 100, 160, 200))])
        moved = Detection(27, 0.9, bbox=(180, 150, 240, 250))  # 집어서 이동
        assert moved in f.apply("top", [moved])

    def test_baseline_active_suppresses_prehand_fixture(self):
        # 이슈 #14 후속: 손 등장 전부터 있던 고정 물체 — 첫 관측은 등록만
        # 하고 통과(증거 보존), 같은 자리 재검출부터 억제. bbox가 출렁여
        # 정지 트랙(IoU 0.85 연속)이 성립하지 않아도 잡아야 한다.
        f = DetectionFilterChain(
            baseline_suppress_mode="active", baseline_suppress_iou=0.5
        )
        first = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        assert first in f.apply("top", [first])  # 첫 관측 = 등록 + 통과
        jittered = Detection(27, 0.9, bbox=(108, 104, 170, 210))  # IoU ~0.75
        assert jittered not in f.apply("top", [jittered])
        assert f.drop_stats["baseline"]["top"] == 1

    def test_baseline_shadow_counts_without_dropping(self):
        f = DetectionFilterChain(
            baseline_suppress_mode="shadow", baseline_suppress_iou=0.5
        )
        d = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        f.apply("top", [d])
        assert d in f.apply("top", [d])  # shadow: 드랍 없음
        assert f.drop_stats["baseline"]["top"] == 1  # 계수만
        # 클래스별 세부 계수 — shadow 검증에서 "어떤 클래스가" 억제 대상인지
        assert f.baseline_drops_by_class["top"] == {27: 1}
        f.reset_trigger_state()
        assert f.baseline_drops_by_class["top"] == {}

    def test_baseline_registration_stops_after_hand(self):
        # 손 등장 이후 새 위치에 나타난 물체(손이 옮긴 상품 등)는 배경으로
        # 등록되지 않는다 → 이후에도 억제되지 않음
        f = DetectionFilterChain(
            baseline_suppress_mode="active", baseline_suppress_iou=0.5
        )
        f.apply("top", [Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))])
        late = Detection(13, 0.8, bbox=(300, 300, 360, 400))
        for _ in range(3):
            out = f.apply(
                "top",
                [Detection(0, 0.9, is_hand=True, bbox=(290, 290, 320, 320)), late],
            )
            assert late in out
        assert f.drop_stats["baseline"]["top"] == 0

    def test_baseline_released_when_item_moves(self):
        # 등록된 돌출 상품이라도 손에 들려 위치를 벗어나면 통과
        f = DetectionFilterChain(
            baseline_suppress_mode="active", baseline_suppress_iou=0.5
        )
        f.apply("top", [Detection(27, 0.9, bbox=(100, 100, 160, 200))])
        moved = Detection(27, 0.9, bbox=(220, 180, 280, 280))
        assert moved in f.apply("top", [moved])

    def test_baseline_state_resets_per_trigger(self):
        f = DetectionFilterChain(
            baseline_suppress_mode="active", baseline_suppress_iou=0.5
        )
        d = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        f.apply("top", [d])
        assert d not in f.apply("top", [d])
        f.reset_trigger_state()
        assert d in f.apply("top", [d])  # 이전 영상의 anchor가 새어오면 안 됨

    def test_baseline_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            DetectionFilterChain(baseline_suppress_mode="on")

    def test_moving_item_never_suppressed(self):
        # 손에 든/이동 중 상품은 bbox가 계속 변해 정지 트랙이 성립하지 않음
        f = DetectionFilterChain(static_track_min_frames=10, static_track_iou=0.85)
        for i in range(30):
            d = Detection(13, 0.8, bbox=(100 + i * 15, 100, 160 + i * 15, 200))
            assert d in f.apply("top", [d])

    def test_static_track_disabled_with_zero_min_frames(self):
        f = DetectionFilterChain(static_track_min_frames=0)
        static = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        for _ in range(50):
            assert static in f.apply("top", [static])

    def test_trigger_state_reset_clears_hand_history_and_tracks(self):
        # 결함 수정: 손 궤적·정지 트랙은 영상(트리거) 단위 상태 — 이전 영상
        # 좌표가 다음 트리거의 필터 기준으로 새면 안 된다
        f = DetectionFilterChain(static_track_min_frames=10, hand_margin_px=40.0)
        hand = Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))
        static = Detection(27, 0.9, bbox=(100, 100, 160, 200))
        for _ in range(15):
            f.apply("top", [hand, static])
        far = Detection(2, 0.8, bbox=(300, 300, 340, 340))
        assert far not in f.apply("top", [far])  # 이전 영상 손 궤적에 걸러짐
        assert static not in f.apply("top", [static])  # 정지 억제 중

        f.reset_trigger_state()  # 새 트리거(다른 영상) 시작
        assert far in f.apply("top", [far])  # 손 이력 없음 → fail-open
        assert static in f.apply("top", [static])  # 정지 카운트 리셋


class TestSegmentTargetRetry:
    """이슈 #10 세션 3 트리거 1 재현 — 오염 delta 이중 타깃 재시도.

    접촉 하중이 delta(−241.77)를 부풀려 진짜 상품(비비고 224g)이 count_gate
    (±15)를 놓치는 케이스: 세그먼트 합(−233.77)을 타깃으로 재판정하면
    오차 9.8로 통과한다. 깨끗한 트리거(delta == seg합)는 발동하지 않는다.
    """

    BIBIGO = ActiveProduct(
        "P175", "비비고만두", class_id=3, unit_weight=224.0, unit_price=3700, stock_qty=35
    )
    COOZ = ActiveProduct(
        "P173", "쿠즈락만두", class_id=13, unit_weight=189.0, unit_price=2100, stock_qty=40
    )

    @staticmethod
    def contaminated_samples():
        """plateau 0 → −8(접촉, sub-threshold) → −241.77: delta=−241.77,
        세그먼트는 −233.77 하나만 방출 (freezer segment_step 20g 미만인
        −8 스텝은 delta에만 반영 — 실기 오염 서명과 동형)."""
        out, ts = [], 0.0
        for v in [0.0] * 10 + [-8.0] * 8 + [-241.77] * 12:
            out.append(LoadcellSample(ts, (v, 0.0)))  # 트레이 분리: 하중은 단일 채널에
            ts += 0.1
        return out

    def make_pipe(self, products):
        from crk_model.core.profiles import FREEZER

        detector = FakeDetector(
            detections=[Detection(3, 0.9), Detection(13, 0.7)]
        )
        store = ActiveProductStore()
        store.update(products)
        return TriggerPipeline(detector, {1: FREEZER}, store)

    def test_retry_recovers_true_product(self):
        pipe = self.make_pipe([self.BIBIGO, self.COOZ])
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                1, {"top": moving_frames(8)}, self.contaminated_samples(), 1.0
            ),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.COMPLETE
        assert [(pc.product.class_id, pc.count) for pc in j.products] == [(3, 1)]
        assert j.reason.endswith("+segment_target_retry")
        assert "segment_target_retry" in outcome.trace.reason_codes
        # 이벤트의 delta_weight는 원본(끝-끝) 유지 — 정산 net-delta 계약 불변
        assert outcome.event.delta_weight == pytest.approx(-241.77)

    def test_clean_trigger_does_not_retry(self):
        pipe = self.make_pipe([self.BIBIGO, self.COOZ])
        outcome = pipe.process(
            "s1",
            TriggerRequest(1, {"top": moving_frames(8)}, samples(500, 276), 1.0),
        )  # delta −224 = 단일 스텝, seg합과 일치
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.COMPLETE
        assert "segment_target_retry" not in outcome.trace.reason_codes
        assert "+segment_target_retry" not in j.reason

    def test_retry_failure_keeps_original_judgment(self):
        # 재시도 타깃(−233.77)도 설명 불가(쿠즈락 189뿐) → 원 판정 유지 (악화 금지)
        pipe = self.make_pipe([self.COOZ])
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                1, {"top": moving_frames(8)}, self.contaminated_samples(), 1.0
            ),
        )
        j = outcome.event.judgment
        assert "segment_target_retry" in outcome.trace.reason_codes  # 시도는 기록
        assert "+segment_target_retry" not in j.reason  # 채택은 안 됨
        assert j.status is not JudgmentStatus.COMPLETE
