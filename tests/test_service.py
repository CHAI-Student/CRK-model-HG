"""service: 연결단 검증 — E2E 흐름, I2 fail-closed, 저무게 스킵, I1 에러 전파,
멱등성(I7), 기동 fail-fast(리뷰 #1), 저널 replay(G2.5 훅), 필터 체인."""
from dataclasses import asdict
from dataclasses import replace as dc_replace

import pytest

from crk_model.core.config import Settings
from crk_model.core.profiles import FREEZER, REFRIGERATOR
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
        # 모션 변위 증거(perception/motion_evidence.py) 통과용 드리프트 —
        # 실물 취출 상품은 움직인다. 12px/프레임, %8 순환으로 side ROI(400)
        # 경계 안에 머문다 (점프 84px ≤ max_jump 150 → 같은 트랙으로 누적).
        off = 12.0 * (self.calls % 8)
        out = []
        for d in self._detections:
            x1, y1, x2, y2 = d.bbox
            if (x1, y1, x2, y2) == (0.0, 0.0, 0.0, 0.0):
                out.append(d)
            else:
                out.append(dc_replace(d, bbox=(x1 + off, y1, x2 + off, y2)))
        return out


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

    def test_issue16_vote_dominated_second_tray_recovered(self):
        # 이슈 #16 재현: 냉동, 동시 2트레이 취출 — ch0 베이글(155g, 다득표)
        # ch1 135g 상품(소득표, share 게이트 미달). 1차 판정에서 ch1이
        # 베이글 near-gate PARTIAL로 오염되지만, 2-pass 소진 재판정이
        # 베이글을 풀에서 빼고 진짜 상품을 COMPLETE로 복구해야 한다.
        bagel = ActiveProduct(
            "P27", "베이글", class_id=27, unit_weight=155.0, unit_price=2800, stock_qty=5
        )
        hotdog = ActiveProduct(
            "P40", "핫도그", class_id=40, unit_weight=135.0, unit_price=3000, stock_qty=5
        )

        class ImbalancedDetector(FakeDetector):
            """베이글은 매 프레임, 핫도그는 5프레임에 1번만 (12/62 사고 재현)."""

            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                dets = [Detection(27, 0.7, bbox=(50.0, 50.0, 100.0, 100.0))]
                if self.calls % 5 == 0:
                    dets.append(Detection(40, 0.95, bbox=(150.0, 50.0, 200.0, 100.0)))
                return dets

        detector = ImbalancedDetector()
        store = ActiveProductStore()
        store.update([bagel, hotdog])
        pipe = TriggerPipeline(detector, {2: FREEZER}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                2,
                {"top": moving_frames(20)},
                dual_tray_samples((500, 345), (420, 285)),  # ch0 −155 / ch1 −135
                1.0,
            ),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.COMPLETE
        billed = {pc.product.class_id: pc.count for pc in j.products}
        assert billed == {27: 1, 40: 1}  # 한쪽만 잡히던 사고의 반대 증명
        assert "+pool_exhaustion" in j.reason
        assert any(
            rc.startswith("multi_tray_pool_exhaustion_retry")
            for rc in outcome.trace.reason_codes
        )

    def test_issue16_retry_with_near_band_distractor_recovered(self):
        # 이슈 #16 재현 2차 (실기 코멘트 YAML): 재판정 풀에 168g 배경 후보
        # (다득표)와 115g near 밴드 교란 후보가 섞인 케이스. 구 ④는
        # "적합 2개(135g 잔차 0 + 115g 잔차 20)=모호"로 불발했다 — 하드
        # 게이트 유일 적합(135g)은 near 밴드 적합과 무관하게 채택돼야 한다.
        bagel = ActiveProduct(
            "P27", "베이글", class_id=27, unit_weight=155.0, unit_price=2800, stock_qty=5
        )
        dumpling = ActiveProduct(
            "P13", "만두", class_id=13, unit_weight=168.0, unit_price=3700, stock_qty=5
        )
        hotdog135 = ActiveProduct(
            "P40", "핫도그135", class_id=40, unit_weight=135.0, unit_price=3000, stock_qty=5
        )
        hotdog115 = ActiveProduct(
            "P24", "핫도그115", class_id=24, unit_weight=115.0, unit_price=2500, stock_qty=5
        )

        class CommentSceneDetector(FakeDetector):
            """27 매 프레임(20표), 13 절반(10표), 40·24 는 1/5(각 4표)."""

            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                dets = [Detection(27, 0.7, bbox=(50.0, 50.0, 100.0, 100.0))]
                if self.calls % 2 == 0:
                    dets.append(Detection(13, 0.85, bbox=(150.0, 150.0, 200.0, 200.0)))
                if self.calls % 5 == 0:
                    dets.append(Detection(40, 0.95, bbox=(150.0, 50.0, 200.0, 100.0)))
                    dets.append(Detection(24, 0.70, bbox=(250.0, 50.0, 300.0, 100.0)))
                return dets

        detector = CommentSceneDetector()
        store = ActiveProductStore()
        store.update([bagel, dumpling, hotdog135, hotdog115])
        pipe = TriggerPipeline(detector, {2: FREEZER}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                2,
                {"top": moving_frames(20)},
                dual_tray_samples((500, 345), (420, 285)),  # ch0 −155 / ch1 −135
                1.0,
            ),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.COMPLETE
        billed = {pc.product.class_id: pc.count for pc in j.products}
        assert billed == {27: 1, 40: 1}
        assert "freezer_vision_first_unique_refit+pool_exhaustion" in j.reason

    def test_partial_with_distinct_identity_billed_in_merge(self):
        # 설계 4 (issue #16 순차 취출): 단일 트리거라면 near-gate PARTIAL도
        # 정산기가 과금한다(#15 정답 경로) — 멀티트레이 병합도 형제 COMPLETE와
        # 정체성이 겹치지 않는 PARTIAL은 과금에 포함해야 한다.
        bagel = ActiveProduct(
            "P27", "베이글", class_id=27, unit_weight=155.0, unit_price=2800, stock_qty=5
        )
        prod130 = ActiveProduct(
            "P30", "요맘때", class_id=30, unit_weight=130.0, unit_price=2500, stock_qty=5
        )

        class SceneDetector(FakeDetector):
            """130g 상품은 매 프레임(다득표), 베이글은 1/3 프레임 — 단 양 카메라
            모두에서 잡혀 weighted conf 1.0 (conf_override 자격)."""

            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                dets = [Detection(30, 0.7, bbox=(50.0, 50.0, 100.0, 100.0))]
                if self.calls % 3 == 0:
                    dets.append(Detection(27, 0.9, bbox=(150.0, 50.0, 200.0, 100.0)))
                return dets

        detector = SceneDetector()
        store = ActiveProductStore()
        store.update([bagel, prod130])
        pipe = TriggerPipeline(detector, {2: FREEZER}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                2,
                {"top": moving_frames(20), "side": moving_frames(20)},
                dual_tray_samples((500, 345), (390, 285)),  # ch0 −155 / ch1 −105
                1.0,
            ),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.PARTIAL  # ch1은 near-gate PARTIAL
        billed = {pc.product.class_id: pc.count for pc in j.products}
        assert billed == {27: 1, 30: 1}
        assert "partial_billed:ch1" in j.reason
        assert "partial_billed:ch1" in outcome.trace.reason_codes

    def test_mutual_duplicate_partials_not_billed(self):
        # 가드 ②: PARTIAL끼리 같은 정체성이면 대칭 오염 가능성 — 과청구가
        # 미청구보다 나쁘다(I13/D9) → 전부 제외 (현행 보수 동작 유지).
        prod130 = ActiveProduct(
            "P30", "요맘때", class_id=30, unit_weight=130.0, unit_price=2500, stock_qty=5
        )
        detector = FakeDetector(
            detections=[Detection(30, 0.7, bbox=(50.0, 50.0, 100.0, 100.0))]
        )
        store = ActiveProductStore()
        store.update([prod130])
        pipe = TriggerPipeline(detector, {2: FREEZER}, store)
        outcome = pipe.process(
            "s1",
            TriggerRequest(
                2,
                {"top": moving_frames(20)},
                dual_tray_samples((500, 395), (400, 295)),  # ch0 −105 / ch1 −105
                1.0,
            ),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.NO_DETECTION
        assert j.products == ()

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


class TestMotionEvidencePipeline:
    """변위 필터 파이프라인 통합 (issue #16 후속): 진열 상품 표 몰수."""

    def test_static_display_class_removed_from_candidates(self):
        # 실기 로그 4의 근본 차단: 진열 만두가 매 프레임 잡혀 63표 1위가 돼도
        # 변위 증거가 없으면 후보에서 몰수 — 진짜 상품이 득표 1위가 된다.
        held = ActiveProduct(
            "P23", "취출상품", class_id=23, unit_weight=175.0, unit_price=3000, stock_qty=5
        )
        display = ActiveProduct(
            "P13", "진열만두", class_id=13, unit_weight=185.0, unit_price=2100, stock_qty=5
        )

        class Scene(FakeDetector):
            """진열(13)은 정지, 취출(23)은 12px/프레임 이동."""

            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                off = 12.0 * (self.calls % 8)
                return [
                    Detection(13, 0.79, bbox=(300.0, 300.0, 350.0, 350.0)),
                    Detection(23, 0.95, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0)),
                ]

        detector = Scene()
        store = ActiveProductStore()
        store.update([held, display])
        pipe = TriggerPipeline(detector, {2: FREEZER}, store, motion_evidence_enabled=True)
        outcome = pipe.process(
            "s1",
            TriggerRequest(2, {"top": moving_frames(12)}, samples(500, 325), 1.0),
        )
        j = outcome.event.judgment
        assert j.status is JudgmentStatus.COMPLETE
        assert [(pc.product.class_id, pc.count) for pc in j.products] == [(23, 1)]
        summary = outcome.trace.vote_summary
        assert summary["classes"][13]["rejected_by"] == "no_motion"
        assert summary["motion_evidence"]["top"][13]["passed"] is False
        assert summary["motion_evidence"]["top"][23]["passed"] is True

    def test_held_shadow_wired_into_vote_summary(self):
        # T2 배선 (0723 문서 §8): voting_params.held_demotion=shadow →
        # carried-in 트랙의 표가 vote_summary.held_shadow로 관측된다
        # (판정 무변경 — 몰수는 active 승격 후).
        held = ActiveProduct(
            "P44", "들고있던", class_id=44, unit_weight=79.0, unit_price=800,
            stock_qty=5,
        )
        taken = ActiveProduct(
            "P23", "취출상품", class_id=23, unit_weight=175.0, unit_price=3000,
            stock_qty=5,
        )

        class Scene(FakeDetector):
            """carried-in(44)은 0번 프레임부터, 취출(23)은 40번부터 — 둘 다 이동."""

            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                i = self.calls - 1
                off = 12.0 * (i % 8)
                dets = [
                    Detection(44, 0.9, bbox=(200.0 + off, 200.0, 250.0 + off, 250.0))
                ]
                if i >= 40:
                    dets.append(
                        Detection(23, 0.95, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0))
                    )
                return dets

        store = ActiveProductStore()
        store.update([held, taken])
        pipe = TriggerPipeline(
            Scene(), {2: FREEZER}, store, motion_evidence_enabled=True,
            voting_params={"held_demotion": "shadow"},
        )
        outcome = pipe.process(
            "s1",
            TriggerRequest(2, {"top": moving_frames(70)}, samples(500, 325), 1.0),
        )
        hs = outcome.trace.vote_summary["held_shadow"]
        assert 44 in hs.get("top", {})  # carried-in 표가 held로 관측
        held_votes, total = hs["top"][44]
        assert 0 < held_votes <= total


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

    def test_vertical_roi_upper_keeps_upper_half_on_both_cameras(self):
        # P1-5 (원본 freezer dual-top): 두 카메라 모두 center_y <= split 유지,
        # side x-ROI는 생략 (side 스트림도 top 뷰).
        f = DetectionFilterChain(
            vertical_roi_region="upper", vertical_roi_y_split=240.0,
            side_roi_max_center_x=240.0,
        )
        upper = Detection(1, 0.8, bbox=(300, 100, 400, 200))  # cy=150, cx=350
        lower = Detection(2, 0.8, bbox=(100, 300, 200, 400))  # cy=350
        for cam in ("top", "side"):
            out = f.apply(cam, [upper, lower])
            assert upper in out and lower not in out  # cx=350 > 240이어도 생존
            assert f.drop_stats["vertical_roi"][cam] == 1
            assert f.drop_stats["side_roi"][cam] == 0

    def test_vertical_roi_lower_region(self):
        f = DetectionFilterChain(vertical_roi_region="lower")
        upper = Detection(1, 0.8, bbox=(100, 100, 200, 200))
        lower = Detection(2, 0.8, bbox=(100, 300, 200, 400))
        assert f.apply("top", [upper, lower]) == [lower]

    def test_vertical_roi_invalid_region_rejected(self):
        with pytest.raises(ValueError):
            DetectionFilterChain(vertical_roi_region="uper")

    def test_top_roi_lower_half_only_when_delta_nonzero(self):
        # P1-5 (원본 top_roi): 냉장 레이아웃 top 카메라 — delta 있을 때
        # center_y >= split(하단 절반)만 유지. delta 없으면 미적용, side 무관.
        f = DetectionFilterChain(top_roi_enabled=True, top_roi_y_split=240.0)
        upper = Detection(1, 0.8, bbox=(100, 100, 200, 200))  # cy=150
        lower = Detection(2, 0.8, bbox=(100, 300, 200, 400))  # cy=350
        assert f.apply("top", [upper, lower]) == [upper, lower]  # delta 미주입
        f.set_trigger_delta(-100.0)
        assert f.apply("top", [upper, lower]) == [lower]
        assert upper in f.apply("side", [upper])  # side는 top ROI 대상 아님
        f.set_trigger_delta(0.0)
        assert f.apply("top", [upper]) == [upper]  # delta 0 → 미적용

    def test_hand_conf_floor_drops_ghost_hand(self):
        # P1-7 (원본 hand_confidence_threshold): 저신뢰 유령 손은 래치·궤적
        # 입력에서 제외 — hand_path 기준을 오염시키지 않는다.
        f = DetectionFilterChain(hand_conf_floor=0.3, hand_margin_px=40.0)
        ghost = Detection(0, 0.1, is_hand=True, bbox=(300, 300, 320, 320))
        real = Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))
        near_real = Detection(1, 0.8, bbox=(30, 30, 60, 60))
        near_ghost = Detection(2, 0.8, bbox=(310, 310, 340, 340))
        out = f.apply("top", [ghost, real, near_real, near_ghost])
        assert ghost not in out and real in out
        assert near_real in out and near_ghost not in out  # 유령 궤적 미등록
        assert f.drop_stats["hand_conf"]["top"] == 1

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

    def test_trigger_state_reset_clears_hand_history(self):
        # 결함 수정: 손 궤적은 영상(트리거) 단위 상태 — 이전 영상 좌표가
        # 다음 트리거의 필터 기준으로 새면 안 된다
        f = DetectionFilterChain(hand_margin_px=40.0)
        hand = Detection(0, 0.9, is_hand=True, bbox=(0, 0, 20, 20))
        for _ in range(15):
            f.apply("top", [hand])
        far = Detection(2, 0.8, bbox=(300, 300, 340, 340))
        assert far not in f.apply("top", [far])  # 이전 영상 손 궤적에 걸러짐

        f.reset_trigger_state()  # 새 트리거(다른 영상) 시작
        assert far in f.apply("top", [far])  # 손 이력 없음 → fail-open


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
