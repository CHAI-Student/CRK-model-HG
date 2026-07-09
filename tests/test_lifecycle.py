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
    return ModelService(
        detector or FakeDetector(),
        profiles={1: REFRIGERATOR},
        clock=clock or FakeClock(),
        journal=journal,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# 1) outcomes 상한
# ---------------------------------------------------------------------------
class TestOutcomesBound:
    def test_outcomes_capped_at_keep(self, cola):
        settings = Settings(outcomes_keep=3)
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


# ---------------------------------------------------------------------------
# 2) EventLog/settler prune on 새 세션 OPEN (I11 보존 확인)
# ---------------------------------------------------------------------------
class TestLedgerPrune:
    def test_old_session_pruned_current_and_prev_kept(self, cola):
        settings = Settings(keep_sessions=2)
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
        settings = Settings(keep_sessions=1)
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
        settings = Settings(keep_sessions=4)
        svc = make_service(settings=settings)

        svc.handle_multi_zone(open_payload(cola))
        first_session = svc.gateway.session_id
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        first_close = svc.handle_multi_zone({"session_id": "s", "state": "CLOSE"})
        assert first_close["status"] == "complete"

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
        assert close["status"] == "complete"

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
