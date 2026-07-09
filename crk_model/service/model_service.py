"""ModelService — 외부 계약(C4/C5) 파사드. 원본 api/routes의 프레임워크 중립 대응.

HTTP 바인딩(FastAPI 등)은 이 파사드를 감싸는 얇은 어댑터로 둔다 —
계약·불변식은 전부 여기서 끝나므로 어댑터에는 로직이 없다.

- handle_trigger  ← POST /trigger      (202 {status: queued} 의미론)
- handle_multi_zone ← POST /api/judge/multi-zone (OPEN/CLOSE 폴링)
- process_pending ← 워커 drain (장치에서는 전용 스레드가 호출)

기동 fail-fast (이관 리뷰 #1): startup_probe_frame을 주면 생성 시 detector를
1회 실행해 로드 실패를 기동 실패로 만든다 (무증상 기동 금지).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from crk_model.core.config import Settings
from crk_model.core.profiles import FREEZER, REFRIGERATOR, SensorProfile
from crk_model.core.types import ActiveProduct, InterimSummary
from crk_model.gateway.state_machine import (
    DoorState,
    MultiZoneGateway,
    build_payment_payload,
)
from crk_model.ingest.idempotency import IdempotencyRegistry
from crk_model.ledger.archive import SessionArchive
from crk_model.ledger.events import EventLog
from crk_model.ledger.journal import EventJournal
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detector
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore
from crk_model.service.worker import SerialTriggerWorker

logger = logging.getLogger(__name__)
ops_logger = logging.getLogger("crk_model.ops")


def _default_profile_from_settings(settings: Settings) -> SensorProfile:
    """MODEL__MACHINE__CABINET_TYPE 이식 — 기기 단위 기본 프로파일.

    존 미지정(zone이 freezer_zones/profiles dict에 없음) 시에도 냉동 기기는
    기본으로 FREEZER가 적용돼야 한다 (이슈 #6 공동 원인: cabinet_type 미이식으로
    미설정 시 전 존이 REFRIGERATOR ±3g로 판정됨)."""
    return FREEZER if settings.cabinet_type == "freezer" else REFRIGERATOR


def _profiles_from_settings(settings: Settings) -> dict[int, SensorProfile]:
    # MODEL__ZONES__FREEZER는 기본 프로파일에 대한 존 단위 오버라이드로만
    # 동작한다 — 예: refrigerated 기기에서 특정 존만 FREEZER로 지정.
    profiles: dict[int, SensorProfile] = {}
    for zone in settings.freezer_zones:
        profiles[zone] = FREEZER
    return profiles


class ModelService:
    def __init__(
        self,
        detector: Detector,
        *,
        settings: Settings | None = None,
        profiles: Mapping[int, SensorProfile] | None = None,
        journal: EventJournal | None = None,
        archive: SessionArchive | None = None,
        clock: Callable[[], float] = time.monotonic,
        startup_probe_frame=None,
    ):
        if startup_probe_frame is not None:
            # 이관 리뷰 #1: YOLO 로드 실패 = 기동 실패 (예외 전파, 무증상 기동 금지)
            detector.detect(startup_probe_frame)

        self.settings = settings or Settings()
        self._profiles = (
            dict(profiles) if profiles is not None else _profiles_from_settings(self.settings)
        )
        self._default_profile = _default_profile_from_settings(self.settings)
        logger.info(
            "[CONFIG] cabinet_type=%s default_profile=%s freezer_zones=%s",
            self.settings.cabinet_type, self._default_profile.name, self.settings.freezer_zones,
        )
        self.snapshots = ActiveProductStore()
        self.event_log = EventLog()
        # 판정(pipeline)·정산(settler)·잠정 집계(gateway interim)의 tolerance
        # 단일 소스 원칙: 존 미지정 시 폴백 프로파일을 세 경로 모두 같은 값으로
        # 주입한다 (cabinet_type=freezer인데 정산만 냉장 ±3g로 계산되는 불일치 방지).
        self.settler = CloseSettler(
            self.settings.error_policy, default_profile=self._default_profile
        )
        # journal과 동일한 하위호환 원칙: archive를 명시적으로 주지 않으면
        # 비활성(SessionArchive("")) — 기본 생성자로 settings.session_archive_dir
        # 를 자동 활성화하면 테스트/임시 ModelService가 실제 저장소(data/sessions)
        # 에 부작용을 남기게 된다. 운영 진입점(adapters/serve.py)이 Settings.
        # from_env() 값으로 명시적으로 SessionArchive를 만들어 주입한다.
        self.archive = archive if archive is not None else SessionArchive("")
        self._clock = clock
        self.gateway = MultiZoneGateway(
            self.settler,
            self.event_log,
            self._profiles,
            clock=clock,
            close_timeout_s=self.settings.close_timeout_s,
            worker_stall_timeout_s=self.settings.worker_stall_timeout_s,
            on_finalize=self._on_session_finalize,
            default_profile=self._default_profile,
        )
        self.pipeline = TriggerPipeline(
            detector, self._profiles, self.snapshots, default_profile=self._default_profile
        )
        # 동시성: FastAPI sync 엔드포인트(threadpool)와 워커 스레드가 게이트웨이·
        # 이벤트로그·스냅샷을 동시에 건드릴 수 있어 단일 RLock으로 코스 그레인
        # 보호한다. 폴링은 10초/1회, 트리거는 초당 1건 미만이라 경합은 사실상
        # 0에 가깝다 — 세분화된 락 대신 단순하고 검증 가능한 락 하나를 쓴다.
        self._lock = threading.RLock()
        self.worker = SerialTriggerWorker(
            self.pipeline, self.gateway, journal,
            lock=self._lock, outcomes_keep=self.settings.outcomes_keep,
        )
        self._idempotency = IdempotencyRegistry(self.settings.idempotency_ttl_s, clock)
        self._trigger_counter = 0
        self._session_counter = 0
        self._last_close_log_key: tuple | None = None
        # 무한 성장 방지: EventLog/settler 멱등 캐시 prune 대상 판단용 최근
        # 세션 ID 목록 (I11: 현재+직전 세션은 항상 보존해야 하므로 K개 유지).
        self._recent_session_ids: deque[str] = deque(maxlen=max(self.settings.keep_sessions, 1))

    # ---- POST /trigger (C4) ----
    def handle_trigger(self, payload: dict) -> dict:
        zone = payload["zone"]
        video_paths = payload.get("video_paths") or {"_ts": str(payload.get("ts", ""))}
        key = IdempotencyRegistry.key_for(zone, video_paths)
        self._trigger_counter += 1
        trigger_id = f"trg-{self._trigger_counter}"
        reg = self._idempotency.register(key, trigger_id)
        if reg.duplicate:
            return {"status": "duplicate", "trigger_id": reg.session_id}  # I7 드롭

        req = TriggerRequest(
            zone=zone,
            frames=payload.get("frames", {}),
            loadcells=payload.get("loadcells", ()),
            ts=payload.get("ts", 0.0),
            seq=payload.get("seq"),
            video_paths=payload.get("video_paths") or {},
        )
        with self._lock:
            # session_id 읽기 + note_seq(배리어 갱신) + submit(enqueue)을 하나의
            # 임계구역으로 묶는다 — 동시 handle_multi_zone(OPEN 신규 세션 발급 등)과
            # 겹치면 session_id/배리어가 불일치할 수 있다. worker.submit도 자체
            # 락을 잡지만(RLock이라 재진입 가능), 여기서 함께 묶어야 note_seq→submit
            # 사이에 다른 스레드가 끼어들지 않는다.
            session_id = self.gateway.session_id or "no-session"
            if req.seq is not None:
                self.gateway.barrier.note_seq(zone, req.seq)  # D2
            self.worker.submit(session_id, req)
        return {"status": "queued", "trigger_id": trigger_id}  # 202 의미론

    def _next_session_id(self) -> str:
        """문 세션 ID 발급 — EventLog 확정 거부(I11)·settler 멱등 캐시가
        session_id 키이므로 세션마다 유일해야 한다 (원본 global_session_id 대응)."""
        self._session_counter += 1
        return f"ses-{self._session_counter}-{int(time.time())}"

    # ---- POST /api/judge/multi-zone (C5) ----
    def handle_multi_zone(self, payload: dict) -> dict:
        # 계약(REFERENCE.md): 문 상태는 별도 필드가 아니라 세션 신호로 들어온다.
        # 어댑터가 wire(session_id="OPEN"|"CLOSE"|null)를 state로 번역해 전달한다.
        state = payload.get("state")  # "OPEN" | "CLOSE" | None(폴링)
        if state == "OPEN":
            products = tuple(
                ActiveProduct(**p) for p in payload.get("active_products", ())
            )
            with self._lock:
                if products:
                    self.snapshots.update(products)  # OPEN마다 스냅샷 갱신 (I2)
                    # 빈 목록은 재고 스냅샷을 덮어쓰지 않는다 (폴링성 OPEN 보호)
                if self.gateway.state in (DoorState.IDLE, DoorState.FINALIZED, DoorState.ERROR):
                    # 새 문 세션 시작 — ERROR/FINALIZED에서 복구는 여기서만 일어난다
                    session_id = self._next_session_id()
                    logger.info(
                        "[MULTI-ZONE OPEN] new session %s (prev_state=%s, products=%d)",
                        session_id, self.gateway.state.value, len(products),
                    )
                    # issue #6: class_id==-1(미매핑, http_app._active_product_fields
                    # 참고)인 상품이 있으면 vision_candidates가 비어 weight_only 오청구
                    # 재발 위험 — OPEN마다 매핑 성공률을 즉시 로그로 남긴다.
                    unmapped = [p.name for p in products if p.class_id == -1]
                    if unmapped:
                        logger.warning(
                            "[MULTI-ZONE OPEN] mapped=%d/%d unmapped=%s",
                            len(products) - len(unmapped), len(products), unmapped,
                        )
                    else:
                        logger.info(
                            "[MULTI-ZONE OPEN] mapped=%d/%d unmapped=[]",
                            len(products), len(products),
                        )
                    self._prune_ledger(session_id)
                else:
                    # 반복 OPEN — 진행 중 세션 유지 (원본 get_or_start 의미론)
                    session_id = self.gateway.session_id or self._next_session_id()
                resp = self.gateway.handle_open(session_id)
            return self._to_response(resp)
        if state == "CLOSE":
            with self._lock:
                if self.gateway.state is DoorState.ACTIVE:
                    logger.info(
                        "[MULTI-ZONE CLOSE] session=%s queue_pending=%d",
                        self.gateway.session_id, self.worker.pending,
                    )
                    resp = self.gateway.handle_close(payload.get("seq_watermark"))
                else:
                    resp = self.gateway.poll()  # PENDING_CLOSE 재폴링 / 확정 후 IDLE
                if resp.state in (DoorState.FINALIZED, DoorState.ERROR):
                    # ERROR는 다음 OPEN까지 지속돼 재폴링마다 동일 응답이 반복된다
                    # (issue #5) — 결과가 실제로 바뀔 때만 로그. FINALIZED는 확정
                    # 게이트웨이가 1회만 반환하므로 자연히 1회 기록된다.
                    log_key = (self.gateway.session_id, resp.state, resp.detail)
                    if log_key != self._last_close_log_key:
                        self._last_close_log_key = log_key
                        logger.info(
                            "[MULTI-ZONE CLOSE] session=%s -> %s detail=%s",
                            self.gateway.session_id, resp.state.value, resp.detail or "-",
                        )
            if resp.state is DoorState.IDLE:
                # 확정 결과가 이미 전달됐거나(게이트웨이가 finalize 직후 idle 복귀)
                # 애초에 열린 세션이 없는 CLOSE — 원본 wire 계약(_handle_door_close의
                # store-None 분기)대로 "활성 세션 없음"을 알린다. 에지는 이 응답으로
                # device busy를 해제한다 (complete를 반복 주면 busy가 안 풀림 — 실기).
                return {
                    "success": True,
                    "status": "success",
                    "message": "No active door session to close",
                    "zones": [],
                    "products": [],
                    "totalPrice": 0,
                    "totalProductCount": 0,
                    "productCount": 0,
                    "globalSessionInfo": None,
                }
            return self._to_response(resp)
        # 폴링(session_id=null): 현재 상태만 반환, 상태 전이 없음
        with self._lock:
            resp = self.gateway.poll()
        return self._to_response(resp)

    def _on_session_finalize(self, session_id, state: DoorState, settlement) -> None:
        """세션 아카이브 훅 (issue #6) — gateway가 FINALIZED/ERROR로 "최초"
        전이하는 시점에 정확히 1회 호출된다 (state_machine.poll() 참고).
        호출 시점은 이미 self._lock 보유 구간(handle_multi_zone) 내부이므로
        RLock 재진입으로 안전하게 EventLog/worker.outcomes를 조회할 수 있다.

        저장 실패는 SessionArchive.save() 내부에서 흡수하므로(부가 기능 원칙)
        여기서 추가 방어는 하지 않는다."""
        if session_id is None:
            return
        events = self.event_log.events_for(session_id)
        outcomes = self.worker.outcomes_for(session_id)
        traces = {o.event: o.trace for o in outcomes}
        processing_times_ms = {o.event: o.processing_time_ms for o in outcomes}
        status = "finalized" if state is DoorState.FINALIZED else "error"
        error_detail = "" if settlement is None else settlement.block_reason
        if state is DoorState.ERROR and settlement is None:
            error_detail = "barrier_timeout"
        path = self.archive.save(
            session_id,
            status,
            events,
            settlement,
            traces,
            processing_times_ms,
            error_detail=error_detail,
            finalized_at=self._clock(),
        )
        if path is not None:
            ops_logger.info("[OPS][SESSION_ARCHIVE] path=%s", path)

    def _prune_ledger(self, new_session_id: str) -> None:
        """무한 성장 방지 (24h+ soak): 새 세션 OPEN 시점에 EventLog/settler의
        세션별 캐시를 최근 K개(MODEL__LEDGER__KEEP_SESSIONS, 기본 4)만 남기고
        정리한다. 호출자(handle_multi_zone)가 이미 락을 잡은 상태에서만 불러야
        한다.

        I11 주의: 직전 세션들의 멱등 캐시를 성급히 지우면 새 OPEN 직후 섞여
        들어오는 직전 세션 CLOSE 재폴링이 재계산되어 다른 결과를 낼 위험이
        있다 — K개를 유지해 현재+최근 세션을 항상 보존한다. 현재(new_session_id)
        세션은 아직 이벤트가 없어도 목록에 넣어 prune 대상에서 절대 빠지지
        않게 한다.
        """
        self._recent_session_ids.append(new_session_id)
        keep = set(self._recent_session_ids)
        self.event_log.prune(keep)
        self.settler.prune(keep)

    def process_pending(self) -> int:
        """워커 drain — 장치에서는 전용 스레드/태스크가 주기 호출.

        drain() 자체는 이벤트 1건 단위로 락을 잡으므로(worker.py 참고) 여기서
        추가로 감싸지 않는다 — 감싸면 추론 구간까지 다시 락 안에 들어가
        coarse lock의 목적(폴링 블록 방지)이 무효화된다.
        """
        return self.worker.drain()

    @staticmethod
    def _to_response(resp) -> dict:
        if resp.state is DoorState.FINALIZED:
            payload = build_payment_payload(resp.payload)  # I10: 확정 타입만 통과
            return {"status": "complete", **payload}
        if resp.state is DoorState.ERROR:
            # I13: 에러 세션은 결제 필드 없이 에러로 응답 (무성 확정 금지)
            return {"status": "error", "detail": resp.detail}
        body: dict = {"status": "processing", "provisional": True}  # I10: 잠정 명시
        if isinstance(resp.payload, InterimSummary):
            body["zones"] = [
                {
                    "zone": z.zone,
                    "products": [
                        {
                            "product_id": pc.product.product_id,
                            "name": pc.product.name,
                            "count": pc.count,
                        }
                        for pc in z.products
                    ],
                }
                for z in resp.payload.zones
            ]
        if resp.detail:
            body["detail"] = resp.detail
        return body
