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
import time
from typing import Callable, Mapping

from crk_model.core.config import Settings
from crk_model.core.profiles import FREEZER, REFRIGERATOR, SensorProfile
from crk_model.core.types import ActiveProduct, InterimSummary
from crk_model.gateway.state_machine import (
    DoorState,
    MultiZoneGateway,
    build_payment_payload,
)
from crk_model.ingest.idempotency import IdempotencyRegistry
from crk_model.ledger.events import EventLog
from crk_model.ledger.journal import EventJournal
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detector
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore
from crk_model.service.worker import SerialTriggerWorker

logger = logging.getLogger(__name__)


def _profiles_from_settings(settings: Settings) -> dict[int, SensorProfile]:
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
        clock: Callable[[], float] = time.monotonic,
        startup_probe_frame=None,
    ):
        if startup_probe_frame is not None:
            # 이관 리뷰 #1: YOLO 로드 실패 = 기동 실패 (예외 전파, 무증상 기동 금지)
            detector.detect(startup_probe_frame)

        self.settings = settings or Settings()
        self._profiles = dict(profiles) if profiles is not None else _profiles_from_settings(self.settings)
        self.snapshots = ActiveProductStore()
        self.event_log = EventLog()
        self.settler = CloseSettler(self.settings.error_policy)
        self.gateway = MultiZoneGateway(
            self.settler,
            self.event_log,
            self._profiles,
            clock=clock,
            close_timeout_s=self.settings.close_timeout_s,
            worker_stall_timeout_s=self.settings.worker_stall_timeout_s,
        )
        self.pipeline = TriggerPipeline(detector, self._profiles, self.snapshots)
        self.worker = SerialTriggerWorker(self.pipeline, self.gateway, journal)
        self._idempotency = IdempotencyRegistry(self.settings.idempotency_ttl_s, clock)
        self._trigger_counter = 0
        self._session_counter = 0
        self._last_close_log_key: tuple | None = None

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

        session_id = self.gateway.session_id or "no-session"
        req = TriggerRequest(
            zone=zone,
            frames=payload.get("frames", {}),
            loadcells=payload.get("loadcells", ()),
            ts=payload.get("ts", 0.0),
            seq=payload.get("seq"),
        )
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
            else:
                # 반복 OPEN — 진행 중 세션 유지 (원본 get_or_start 의미론)
                session_id = self.gateway.session_id or self._next_session_id()
            resp = self.gateway.handle_open(session_id)
            return self._to_response(resp)
        if state == "CLOSE":
            if self.gateway.state is DoorState.ACTIVE:
                logger.info(
                    "[MULTI-ZONE CLOSE] session=%s queue_pending=%d",
                    self.gateway.session_id, self.worker.pending,
                )
                resp = self.gateway.handle_close(payload.get("seq_watermark"))
            else:
                resp = self.gateway.poll()  # 재폴링 (I11: 확정 후에도 동일 응답)
            if resp.state in (DoorState.FINALIZED, DoorState.ERROR):
                # CLOSE는 문이 닫혀있는 동안 계속 재폴링되는 level-triggered 신호라
                # (I11), FINALIZED/ERROR로 확정된 뒤에도 동일 응답이 반복된다. 매번
                # 로그를 남기면 몇 분씩 같은 줄이 반복돼 "멈춘 것처럼" 보이므로
                # (issue #5) 결과가 실제로 바뀔 때만 기록한다 — 응답 자체는 그대로
                # 매 호출 반환한다.
                log_key = (self.gateway.session_id, resp.state, resp.detail)
                if log_key != self._last_close_log_key:
                    self._last_close_log_key = log_key
                    logger.info(
                        "[MULTI-ZONE CLOSE] session=%s -> %s detail=%s",
                        self.gateway.session_id, resp.state.value, resp.detail or "-",
                    )
            return self._to_response(resp)
        # 폴링(session_id=null): 현재 상태만 반환, 상태 전이 없음
        return self._to_response(self.gateway.poll())

    def process_pending(self) -> int:
        """워커 drain — 장치에서는 전용 스레드/태스크가 주기 호출."""
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
