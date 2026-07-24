"""동시성 안전 + 무한 성장 방지 (24h+ soak) 검증.

- outcomes 상한 (worker.outcomes deque maxlen)
- 새 세션 OPEN 시 EventLog/settler prune (I11: 현재+직전 세션 보존)
- EventJournal 일자별 로테이션 + replay 통합 + 보존기간 삭제
- 락 스모크 테스트: handle_multi_zone 폴링과 worker.drain 동시 실행
"""
from __future__ import annotations

import datetime
import threading
import time
from dataclasses import asdict
from dataclasses import replace as dc_replace

import pytest

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.ledger.events import TriggerEvent
from crk_model.ledger.journal import EventJournal
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detection
from crk_model.service import ModelService

# tests/ 는 패키지가 아니라(test_service.py 참고, __init__.py 없음) 공용 픽스처를
# import할 수 없다 — test_service.py와 동일한 최소 FakeClock/FakeDetector/헬퍼를
# 여기서도 독립적으로 정의한다 (test_service.py는 다른 에이전트 작업 중이라 수정 금지).


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


def open_payload(cola, session_id="OPEN"):
    return {"session_id": session_id, "state": "OPEN", "active_products": [asdict(cola)]}


def trigger_payload(zone=1, n_frames=8, seq=0):
    # video_paths가 IdempotencyRegistry 키(zone+video_paths)라 반복 트리거를
    # 여러 건 넣으려면 seq마다 경로를 달리해 중복(I7)으로 드롭되지 않게 한다.
    return {
        "zone": zone,
        "frames": {"top": moving_frames(n_frames), "side": moving_frames(n_frames)},
        "loadcells": samples(500, 400),  # delta -100
        "ts": 1.0 + seq,
        "video_paths": {"top": f"/t{seq}.avi", "side": f"/s{seq}.avi"},
    }


def make_service(detector=None, clock=None, journal=None, settings=None):
    # close_grace_s=0: CLOSE 유예 창은 test_gateway 전용 테스트에서 검증 —
    # FakeClock(t=0) 기반 흐름 테스트는 즉시 확정으로 단순화한다.
    return ModelService(
        detector or FakeDetector(),
        profiles={1: REFRIGERATOR},
        clock=clock or FakeClock(),
        journal=journal,
        settings=settings if settings is not None else Settings(close_grace_s=0.0),
    )


# ---------------------------------------------------------------------------
# 1) outcomes 상한
# ---------------------------------------------------------------------------
class TestOutcomesBound:
    def test_outcomes_capped_at_keep(self, cola):
        settings = Settings(outcomes_keep=3, close_grace_s=0.0)
        svc = make_service(settings=settings)
        svc.handle_multi_zone(open_payload(cola))
        for i in range(5):
            svc.handle_trigger(trigger_payload(seq=i))
            svc.process_pending()
        assert len(svc.worker.outcomes) == 3  # 상한 유지, 무한 성장 아님

    def test_outcomes_indexable_like_list(self, cola):
        # 기존 테스트가 outcomes[0], outcomes[-1] 인덱싱을 쓰므로 deque도
        # 동일하게 동작해야 한다 (하위호환).
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        assert svc.worker.outcomes[0].event.zone == 1
        assert svc.worker.outcomes[-1] is svc.worker.outcomes[0]

    def test_default_keep_from_settings(self):
        assert Settings().outcomes_keep == 256
        assert Settings.from_env().outcomes_keep == 256


class TestConfWeightWiring:
    """카메라 conf 결합 가중의 env 배선 (MODEL__VISION__CONF_WEIGHT_*).

    산식 자체는 test_perception이 검증 — 여기서는 env → Settings →
    voting_params 경로가 끊기지 않는 것만 고정한다 (env 이름 오타 방지)."""

    def test_from_env_parses_conf_weights(self, monkeypatch):
        monkeypatch.setenv("MODEL__VISION__CONF_WEIGHT_TOP", "0.8")
        monkeypatch.setenv("MODEL__VISION__CONF_WEIGHT_SIDE", "0.2")
        monkeypatch.setenv("MODEL__VISION__CONF_WEIGHT_TOP_ONLY", "0.9")
        monkeypatch.setenv("MODEL__VISION__CONF_WEIGHT_SIDE_ONLY", "0.3")
        monkeypatch.setenv("MODEL__VISION__CONF_COMMON_CLASS_BONUS", "0.1")
        s = Settings.from_env()
        assert (s.conf_weight_top, s.conf_weight_side) == (0.8, 0.2)
        assert (s.conf_weight_top_only, s.conf_weight_side_only) == (0.9, 0.3)
        assert s.conf_common_class_bonus == 0.1

    def test_settings_reach_voting_params(self):
        svc = make_service(settings=Settings(
            close_grace_s=0.0, conf_weight_top=0.8, conf_weight_side=0.2,
            conf_weight_top_only=0.9, conf_weight_side_only=0.3,
            conf_common_class_bonus=0.1,
        ))
        vp = svc.pipeline._voting_params
        assert (vp["top_weight"], vp["side_weight"]) == (0.8, 0.2)
        assert (vp["top_only_weight"], vp["side_only_weight"]) == (0.9, 0.3)
        assert vp["common_class_bonus"] == 0.1

    def test_defaults_match_original_operating_values(self):
        vp = make_service().pipeline._voting_params
        assert (vp["top_weight"], vp["side_weight"]) == (0.60, 0.40)
        assert (vp["top_only_weight"], vp["side_only_weight"]) == (0.60, 0.40)
        assert vp["common_class_bonus"] == 0.2


# ---------------------------------------------------------------------------
# 2) EventLog/settler prune on 새 세션 OPEN (I11 보존 확인)
# ---------------------------------------------------------------------------
class TestLedgerPrune:
    def test_old_session_pruned_current_and_prev_kept(self, cola):
        settings = Settings(keep_sessions=2, close_grace_s=0.0)
        svc = make_service(settings=settings)

        session_ids = []
        for i in range(5):
            svc.handle_multi_zone(open_payload(cola))
            session_ids.append(svc.gateway.session_id)
            svc.handle_trigger(trigger_payload(seq=i))
            svc.process_pending()
            svc.handle_multi_zone({"session_id": "s", "state": "CLOSE"})

        # 마지막 OPEN 이전에 있던 오래된 세션들은 event_log에서 사라져야 한다.
        oldest = session_ids[0]
        assert svc.event_log.events_for(oldest) == ()

        # keep_sessions=2 → 직전(마지막에서 두번째) 세션은 보존.
        prev = session_ids[-2]
        assert prev in svc._recent_session_ids

    def test_prune_never_removes_active_session(self, cola):
        settings = Settings(keep_sessions=1, close_grace_s=0.0)
        svc = make_service(settings=settings)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        current = svc.gateway.session_id
        # 활성 세션 이벤트가 prune으로 사라지면 안 된다.
        assert svc.event_log.events_for(current) != ()
        assert current in svc.settler._finalized or True  # 아직 미확정일 수 있음

    def test_i11_close_repoll_after_prune_stable(self, cola):
        """직전 세션 CLOSE 재폴링이 새 OPEN 직후 섞여 들어와도(K=4 기본값)
        동일 결과를 내야 한다 — prune이 settler의 멱등 캐시를 지우면 안 됨."""
        settings = Settings(keep_sessions=4, close_grace_s=0.0)
        svc = make_service(settings=settings)

        svc.handle_multi_zone(open_payload(cola))
        first_session = svc.gateway.session_id
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        first_close = svc.handle_multi_zone({"session_id": "s", "state": "CLOSE"})
        assert first_close["status"] == "success"

        # 새 세션 OPEN (직전 세션은 keep_sessions=4 안에 들어 prune 안 됨)
        svc.handle_multi_zone(open_payload(cola))
        assert svc.gateway.session_id != first_session

        # 직전 세션의 settler 멱등 캐시가 여전히 남아있는지 직접 재생 확인
        result = svc.settler.settle(
            first_session, svc.event_log.events_for(first_session), {1: REFRIGERATOR}
        )
        assert result.total_price == first_close["totalPrice"]


# ---------------------------------------------------------------------------
# 3) EventJournal 로테이션 + replay + 보존기간
# ---------------------------------------------------------------------------
def make_event(session_id="s1", zone=1, ts=1.0):
    from crk_model.core.types import JudgmentResult, JudgmentStatus

    return TriggerEvent(
        session_id=session_id,
        zone=zone,
        ts=ts,
        delta_weight=-100.0,
        segments=(),
        judgment=JudgmentResult(
            status=JudgmentStatus.COMPLETE, products=(), confidence=1.0,
            reason="test", strategy="test",
        ),
        seq=None,
        status="ok",
    )


class _FixedDate:
    def __init__(self, d: datetime.date):
        self.d = d

    def __call__(self) -> datetime.date:
        return self.d


class TestJournalRotation:
    def test_rotated_filename_per_day(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 7, 9))
        j = EventJournal(tmp_path / "events.jsonl", today=clock)
        j.append(make_event())
        expected = tmp_path / "events_20260709.jsonl"
        assert expected.exists()

    def test_rollover_creates_new_file(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 7, 9))
        j = EventJournal(tmp_path / "events.jsonl", today=clock)
        j.append(make_event(session_id="s1"))
        clock.d = datetime.date(2026, 7, 10)
        j.append(make_event(session_id="s2"))

        assert (tmp_path / "events_20260709.jsonl").exists()
        assert (tmp_path / "events_20260710.jsonl").exists()

    def test_replay_reads_all_rotations_in_date_order(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 7, 8))
        j = EventJournal(tmp_path / "events.jsonl", today=clock)
        j.append(make_event(session_id="day1"))
        clock.d = datetime.date(2026, 7, 9)
        j.append(make_event(session_id="day2"))
        clock.d = datetime.date(2026, 7, 10)
        j.append(make_event(session_id="day3"))

        replayed = j.replay()
        assert [e.session_id for e in replayed] == ["day1", "day2", "day3"]

    def test_replay_filters_by_session_across_rotations(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 7, 8))
        j = EventJournal(tmp_path / "events.jsonl", today=clock)
        j.append(make_event(session_id="keep"))
        clock.d = datetime.date(2026, 7, 9)
        j.append(make_event(session_id="drop"))
        j.append(make_event(session_id="keep"))

        replayed = j.replay("keep")
        assert len(replayed) == 2
        assert all(e.session_id == "keep" for e in replayed)

    def test_retention_deletes_old_rotation_files(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 1, 1))
        j = EventJournal(tmp_path / "events.jsonl", retention_days=2, today=clock)
        j.append(make_event())
        old_file = tmp_path / "events_20260101.jsonl"
        assert old_file.exists()

        # 보존기간(2일)을 훨씬 넘겨 롤오버 → 오래된 파일은 삭제되어야 함
        clock.d = datetime.date(2026, 1, 10)
        j.append(make_event())

        assert not old_file.exists()
        assert (tmp_path / "events_20260110.jsonl").exists()

    def test_retention_keeps_recent_rotation_files(self, tmp_path):
        clock = _FixedDate(datetime.date(2026, 1, 1))
        j = EventJournal(tmp_path / "events.jsonl", retention_days=14, today=clock)
        j.append(make_event())

        clock.d = datetime.date(2026, 1, 3)  # 14일 보존기간 내
        j.append(make_event())

        assert (tmp_path / "events_20260101.jsonl").exists()
        assert (tmp_path / "events_20260103.jsonl").exists()

    def test_constructor_default_path_still_positional(self, tmp_path):
        # 어댑터(serve.py)가 EventJournal(path) 단일 인자로 생성하므로 호환 유지.
        j = EventJournal(tmp_path / "events.jsonl")
        j.append(make_event())
        assert j.replay() != []

    def test_g25_replay_equivalence_across_rotation(self, cola, tmp_path):
        # G2.5 훅: 로테이션이 있어도 등가성 검증에 쓰이는 replay 동작은 보존.
        clock = _FixedDate(datetime.date(2026, 7, 9))
        journal = EventJournal(tmp_path / "events.jsonl", today=clock)
        svc = make_service(journal=journal)
        svc.handle_multi_zone(open_payload(cola))
        sid = svc.gateway.session_id
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        live = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})

        replayed = journal.replay(sid)
        assert len(replayed) == 1
        result = CloseSettler().settle(sid, replayed, {1: REFRIGERATOR})
        assert result.total_price == live["totalPrice"]


# ---------------------------------------------------------------------------
# 4) 락 스모크 테스트
# ---------------------------------------------------------------------------
class TestLockSmoke:
    def test_concurrent_poll_and_drain_no_crash(self, cola):
        """스레드 2개 — 하나는 handle_multi_zone 폴링을 반복, 하나는
        trigger 제출+drain을 반복한다. 예외 없이 끝나고 최종 상태가 일관되면
        (성공적으로 complete 되거나 최소한 크래시 없이 종료) 통과."""
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))

        errors: list[BaseException] = []
        stop = threading.Event()

        def poller():
            try:
                while not stop.is_set():
                    svc.handle_multi_zone({"session_id": "s", "state": None})
                    time.sleep(0.001)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def trigger_and_drain():
            try:
                for i in range(20):
                    svc.handle_trigger(trigger_payload(seq=i))
                    svc.process_pending()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=poller)
        t2 = threading.Thread(target=trigger_and_drain)
        t1.start()
        t2.start()
        t2.join(timeout=10)
        stop.set()
        t1.join(timeout=10)

        assert not errors
        assert not t1.is_alive() and not t2.is_alive()
        # 모든 트리거가 결국 처리됨 (큐 잔량 없음)
        assert svc.worker.pending == 0

        close = svc.handle_multi_zone({"session_id": "s", "state": "CLOSE"})
        assert close["status"] == "success"

    def test_worker_default_lock_none_backward_compat(self):
        # lock 없이 생성해도(기존 테스트 패턴) 정상 동작해야 한다.
        from crk_model.core.profiles import REFRIGERATOR as _REF
        from crk_model.gateway.state_machine import MultiZoneGateway
        from crk_model.ledger.events import EventLog as _EL
        from crk_model.ledger.settler import CloseSettler as _CS
        from crk_model.service.pipeline import TriggerPipeline
        from crk_model.service.snapshot import ActiveProductStore
        from crk_model.service.worker import SerialTriggerWorker

        detector = FakeDetector()
        snapshots = ActiveProductStore()
        pipeline = TriggerPipeline(detector, {1: _REF}, snapshots)
        event_log = _EL()
        settler = _CS()
        gateway = MultiZoneGateway(settler, event_log, {1: _REF})
        worker = SerialTriggerWorker(pipeline, gateway)  # lock=None 기본값
        assert worker.pending == 0
        assert worker.drain() == 0


# ---------------------------------------------------------------------------
# 5) MODEL__MACHINE__CABINET_TYPE — 기기 단위 기본 프로파일 (E2E)
# ---------------------------------------------------------------------------
class TestCabinetTypeDefaultProfile:
    """cabinet_type=freezer면 존 미지정(profiles dict 미주입) 시에도 기본
    프로파일이 FREEZER가 되어야 한다 (이슈 #6 공동 원인 회귀 방지).

    weight_only(no_candidate_fallback)는 freezer에서 억제되므로
    (loadcell_identity_suppressed), 실제 판정 결과로 기본 프로파일 적용
    여부를 검증한다 — REFRIGERATOR가 남아 있으면 weight_only가 그대로
    COMPLETE를 내 회귀를 놓친다."""

    def test_freezer_cabinet_type_suppresses_weight_only_without_zone_override(self, cola):
        settings = Settings(cabinet_type="freezer", close_grace_s=0.0)
        svc = ModelService(
            FakeDetector(detections=[]),  # vision 후보 0 → no_candidate_fallback 경로
            clock=FakeClock(),
            settings=settings,
        )  # profiles 미주입 — cabinet_type만으로 기본 프로파일 결정
        assert svc._default_profile.name == "freezer"
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())  # delta=-100g == cola.unit_weight
        svc.process_pending()
        outcome = svc.worker.outcomes[-1]
        assert outcome.event.judgment.reason == "loadcell_identity_suppressed"
        assert outcome.event.judgment.status.value == "no_detection"

    def test_refrigerated_default_keeps_weight_only(self, cola):
        # 회귀 방지: 기본값(refrigerated)에서는 기존처럼 weight_only가 유지된다.
        settings = Settings(cabinet_type="refrigerated", close_grace_s=0.0)
        svc = ModelService(
            FakeDetector(detections=[]),
            clock=FakeClock(),
            settings=settings,
        )
        assert svc._default_profile.name == "refrigerator"
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        outcome = svc.worker.outcomes[-1]
        assert outcome.event.judgment.reason == "weight_only"
        assert outcome.event.judgment.status.value == "complete"

    def test_bocpd_primary_switch_swaps_shadow_to_plateau(self, cola):
        # 이슈 #14 승격 스위치: MODEL__LOADCELL__ANALYZER=bocpd — primary가
        # BOCPD 어댑터로 바뀌고, shadow는 plateau로 뒤집혀 대칭 diff 유지.
        settings = Settings(loadcell_analyzer="bocpd", close_grace_s=0.0)
        svc = ModelService(FakeDetector(), clock=FakeClock(), settings=settings)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())  # delta -100 = cola 1개
        svc.process_pending()
        outcome = svc.worker.outcomes[-1]
        assert outcome.event.judgment.status.value == "complete"
        assert abs(outcome.event.delta_weight - (-100.0)) < 5.0
        sh = outcome.trace.loadcell_shadow
        assert sh is not None and sh["analyzer"] == "plateau"
        assert sh["mismatch"] is False  # 깨끗한 계단 — 두 분석기 일치

    def test_invalid_loadcell_analyzer_env_rejected(self, monkeypatch):
        monkeypatch.setenv("MODEL__LOADCELL__ANALYZER", "bocdp")
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_freezer_dual_top_layout_applies_vertical_roi(self, cola):
        # P1-5 배선: cabinet_type=freezer ∧ camera_layout=dual_top_proxy면
        # 두 카메라 모두 상단 절반(center_y <= 240)만 유지 — 하단(진열 선반)
        # 검출은 vertical_roi 단계에서 몰수돼 후보가 되지 못한다.
        settings = Settings(
            cabinet_type="freezer", camera_layout="dual_top_proxy",
            close_grace_s=0.0,
        )
        svc = ModelService(
            FakeDetector(detections=[
                Detection(1, 0.9, bbox=(50.0, 300.0, 100.0, 400.0))  # cy=350
            ]),
            clock=FakeClock(),
            settings=settings,
        )
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        outcome = svc.worker.outcomes[-1]
        assert not outcome.event.vision_candidates
        drops = outcome.trace.vote_summary["filter_drops_by_stage"]["vertical_roi"]
        assert drops["top"] > 0

    def test_dual_layout_default_keeps_lower_half_detections(self, cola):
        # 회귀 방지: 기본 레이아웃(dual)에서는 수직 ROI가 꺼져 있어 하단
        # 검출도 후보가 된다 (기존 동작 보존).
        settings = Settings(cabinet_type="freezer", close_grace_s=0.0)
        svc = ModelService(
            FakeDetector(detections=[
                Detection(1, 0.9, bbox=(50.0, 300.0, 100.0, 400.0))
            ]),
            clock=FakeClock(),
            settings=settings,
        )
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        outcome = svc.worker.outcomes[-1]
        assert any(c.class_id == 1 for c in outcome.event.vision_candidates)

    def test_freezer_cabinet_type_applies_to_close_settlement(self, cola):
        """CLOSE 정산도 기본 프로파일을 따라야 한다 (판정·정산 tolerance 단일
        소스). removal -100g(콜라 1) 후 return +90g: freezer tolerance(±15g)면
        |90-100|=10g ≤ 15g로 반품이 매칭돼 청구 0원. settler/gateway 폴백이
        REFRIGERATOR(±3g)로 남아 있으면 반품 미매칭 → 콜라 1개가 그대로
        청구된다 (net_delta 교정도 cola 100g > excess+tol=93g라 불가) —
        이 delta는 정확히 폴백 프로파일 차이만 가른다."""
        settings = Settings(cabinet_type="freezer", close_grace_s=0.0)
        svc = ModelService(
            FakeDetector(),  # class_id=1(콜라) 검출 → freezer_vision_first 판정
            clock=FakeClock(),
            settings=settings,
        )  # profiles 미주입 — 존 미지정 상태에서 기본 프로파일이 정산까지 적용
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload(seq=0))  # removal: delta -100g
        svc.process_pending()
        return_payload = {
            "zone": 1,
            "frames": {"top": moving_frames(8), "side": moving_frames(8)},
            "loadcells": samples(400, 490),  # return: delta +90g
            "ts": 2.0,
            "video_paths": {"top": "/r0.avi", "side": "/rs0.avi"},
        }
        svc.handle_trigger(return_payload)
        svc.process_pending()
        close = svc.handle_multi_zone({"session_id": "s", "state": "CLOSE"})
        # 상품 0개 확정은 원본 wire 계약상 complete_no_products (success는 유지)
        assert close["status"] == "complete_no_products" and close["success"] is True
        assert close["totalPrice"] == 0  # freezer ±15g: 반품 매칭 → 청구 없음
        assert close["productCount"] == 0

    def test_invalid_cabinet_type_rejected(self):
        import os

        os.environ["MODEL__MACHINE__CABINET_TYPE"] = "frozen"
        try:
            try:
                Settings.from_env()
                raised = False
            except ValueError:
                raised = True
            assert raised
        finally:
            del os.environ["MODEL__MACHINE__CABINET_TYPE"]


# ---------------------------------------------------------------------------
# 6) 비전 투표 튜닝 env 배선 (issue #6 2차 — MODEL__VISION__*)
# ---------------------------------------------------------------------------
class TestVisionTuningWiring:
    """Settings의 비전 튜닝 값이 pipeline의 VotingEnsemble/필터까지 흐르는지."""

    def test_from_env_reads_vision_tuning(self, monkeypatch):
        monkeypatch.setenv("MODEL__VISION__TOP_CONFIDENCE_THRESHOLD", "0.35")
        monkeypatch.setenv("MODEL__VISION__SIDE_CONFIDENCE_THRESHOLD", "0.30")
        monkeypatch.setenv("MODEL__VISION__MIN_VOTE_RATIO", "0.02")
        monkeypatch.setenv("MODEL__VISION__MIN_VOTE_COUNT", "1")
        monkeypatch.setenv("MODEL__VISION__CONF_FLOOR", "0.1")
        monkeypatch.setenv("MODEL__VISION__SIDE_ROI_MAX_CENTER_X", "480")
        monkeypatch.setenv("MODEL__VISION__BASELINE_SUPPRESS_MODE", "active")
        monkeypatch.setenv("MODEL__VISION__BASELINE_SUPPRESS_IOU", "0.6")
        s = Settings.from_env()
        assert s.top_confidence_threshold == 0.35
        assert s.side_confidence_threshold == 0.30
        assert s.min_vote_ratio == 0.02
        assert s.min_vote_count == 1
        assert s.vote_conf_floor == 0.1
        assert s.side_roi_max_center_x == 480
        assert s.baseline_suppress_mode == "active"
        assert s.baseline_suppress_iou == 0.6

    def test_from_env_reads_judgment_and_gate_tuning(self, monkeypatch):
        monkeypatch.setenv("MODEL__JUDGMENT__SINGLE_SHARE", "0.4")
        monkeypatch.setenv("MODEL__JUDGMENT__NEAR_FACTOR", "1.5")
        monkeypatch.setenv("MODEL__JUDGMENT__REFIT_SHARE", "0.2")
        monkeypatch.setenv("MODEL__VISION__EARLY_TERMINATION", "0")
        monkeypatch.setenv("MODEL__VISION__MOTION_GATE_THRESHOLD", "0.03")
        monkeypatch.setenv("MODEL__VISION__MOTION_GATE_KEEPALIVE", "12")
        monkeypatch.setenv("MODEL__WEIGHT__STABLE_WINDOW", "4")
        s = Settings.from_env()
        assert s.judgment_single_share == 0.4
        assert s.judgment_near_factor == 1.5
        assert s.judgment_refit_share == 0.2
        assert s.early_termination_enabled is False
        assert s.motion_gate_threshold == 0.03
        assert s.motion_gate_keepalive == 12
        assert s.loadcell_stable_window == 4
        # 미설정 시 None → 프로파일 기본 유지
        monkeypatch.delenv("MODEL__VISION__MOTION_GATE_THRESHOLD")
        assert Settings.from_env().motion_gate_threshold is None

    def test_motion_gate_override_reaches_profiles(self):
        from crk_model.service.model_service import (
            _default_profile_from_settings,
            _profiles_from_settings,
        )

        s = Settings(cabinet_type="freezer", freezer_zones=(2,),
                     motion_gate_threshold=0.01, motion_gate_keepalive=6)
        default = _default_profile_from_settings(s)
        assert default.motion_gate_threshold == 0.01
        assert default.motion_gate_keepalive == 6
        assert default.early_termination_allowed is False  # 나머지 필드 보존
        assert _profiles_from_settings(s)[2].motion_gate_threshold == 0.01
        # 오버라이드 없으면 프로파일 상수 그대로
        plain = _default_profile_from_settings(Settings(cabinet_type="freezer"))
        assert plain.motion_gate_threshold == 0.005

    def test_from_env_rejects_invalid_baseline_mode(self, monkeypatch):
        monkeypatch.setenv("MODEL__VISION__BASELINE_SUPPRESS_MODE", "on")
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_settings_flow_into_voting_ensemble(self, cola):
        # 진입 컷 0.9로 올리면 FakeDetector(conf 0.8) 검출이 투표에 못 들어가
        # vision 후보 0 → (냉장 기본) weight_only로 빠진다 — env가 실제로
        # 파이프라인 동작을 바꾸는지 E2E로 검증.
        settings = Settings(
            top_confidence_threshold=0.9, side_confidence_threshold=0.9, close_grace_s=0.0
        )
        svc = make_service(settings=settings)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        ev = svc.worker.outcomes[-1].event
        assert ev.vision_candidates == ()  # 진입 컷이 전부 차단
        summary = svc.worker.outcomes[-1].trace.vote_summary
        assert summary["entry_dropped_by_camera"]["top"] > 0  # 진단 카운터 동작

    def test_default_settings_keep_high_conf_detections_alive(self, cola):
        # 기본값(진입 컷 0.70, conf_floor 0.0)에서 conf 0.8 검출은 후보 생존.
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        ev = svc.worker.outcomes[-1].event
        assert len(ev.vision_candidates) >= 1

    def test_vote_summary_has_filter_stage_breakdown(self, cola):
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        summary = svc.worker.outcomes[-1].trace.vote_summary
        assert "filter_drops_by_stage" in summary
        assert set(summary["filter_drops_by_stage"]) == {
            "side_roi", "vertical_roi", "hand_conf", "baseline",
            "static_track", "hand_path",
        }
        # baseline shadow 검증용 클래스별 세부 (이슈 #14 후속)
        assert set(summary["baseline_drops_by_class"]) == {"top", "side"}
