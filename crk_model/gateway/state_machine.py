"""Multi-Zone OPEN/CLOSE 상태기계 (다이어그램 10의 재설계, D1).

현행과의 차이: PendingClose → Finalizing 전이가 "20s/3s 경과"(time-paced)가
아니라 인과 배리어 충족(I17)이다. 고정 대기는 카메라 무응답 대비 상한
타임아웃으로 강등 — 만료 시 배리어 미충족이면 에러 세션(I13, D9 fail-closed).

효과: 큐 적체 시 late-trigger 유실 race 제거 + 큐가 비면 대기 없이 즉시 확정
(지연 단축은 부수 효과).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import FinalizedSettlement, InterimSummary
from crk_model.ledger.barrier import CausalBarrier
from crk_model.ledger.events import EventLog, TriggerEvent
from crk_model.ledger.settler import CloseSettler, interim_summary


class DoorState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    PENDING_CLOSE = "pending_close"
    FINALIZED = "finalized"
    ERROR = "error"


@dataclass(frozen=True)
class GatewayResponse:
    state: DoorState
    payload: FinalizedSettlement | InterimSummary | None
    detail: str = ""


class MultiZoneGateway:
    def __init__(
        self,
        settler: CloseSettler,
        event_log: EventLog,
        profiles: Mapping[int, SensorProfile],
        *,
        clock: Callable[[], float] = time.monotonic,
        close_timeout_s: float = 10.0,  # I17: 상한 타임아웃 (정상 경로 아님)
    ):
        self._settler = settler
        self._event_log = event_log
        self._profiles = dict(profiles)
        self._clock = clock
        self._close_timeout = close_timeout_s
        self.state = DoorState.IDLE
        self.session_id: str | None = None
        self.barrier = CausalBarrier()
        self._close_ts: float | None = None

    # -- OPEN --
    def handle_open(self, session_id: str) -> GatewayResponse:
        if self.session_id != session_id:
            self.session_id = session_id
            self.barrier = CausalBarrier()  # 세션당 새 배리어
            self._close_ts = None
        self.state = DoorState.ACTIVE
        return GatewayResponse(DoorState.ACTIVE, self.interim())

    # -- 트리거 수명주기 → 배리어 공급 (I17 ①) --
    def notify_enqueued(self, zone: int) -> None:
        self.barrier.notify_enqueued(zone)

    def notify_processed(self, zone: int) -> None:
        self.barrier.notify_processed(zone)

    def record_trigger(self, event: TriggerEvent) -> bool:
        return self._event_log.append(event)

    # -- CLOSE --
    def handle_close(self, seq_watermark: dict[int, int] | None = None) -> GatewayResponse:
        self.state = DoorState.PENDING_CLOSE
        self._close_ts = self._clock()
        if seq_watermark:  # D2: 카메라 seq 도입 시에만 (I17 ③)
            self.barrier.set_close_watermark(seq_watermark)
        return self.poll()

    def poll(self) -> GatewayResponse:
        if self.state is DoorState.ACTIVE:
            return GatewayResponse(DoorState.ACTIVE, self.interim(), "processing")
        if self.state is DoorState.FINALIZED:
            # I11: 재폴링에도 동일 결과 (settler 멱등 캐시)
            return GatewayResponse(DoorState.FINALIZED, self._settle())
        if self.state is not DoorState.PENDING_CLOSE:
            return GatewayResponse(self.state, None)

        status = self.barrier.status()
        if status.satisfied:
            settlement = self._settle()
            if settlement.blocked:
                self.state = DoorState.ERROR  # I13: 무성 확정 금지
                return GatewayResponse(DoorState.ERROR, settlement, settlement.block_reason)
            self.state = DoorState.FINALIZED
            return GatewayResponse(DoorState.FINALIZED, settlement)

        assert self._close_ts is not None
        if self._clock() - self._close_ts >= self._close_timeout:
            # I17: 상한 타임아웃 만료 + 배리어 미충족 = 에러 세션 (D9 fail-closed).
            # "시간이 지나서 확정"은 정상 경로가 아니다 — late trigger 유실
            # = 매출 누락 또는 이중 과금이므로 결제로 보내지 않는다.
            self.state = DoorState.ERROR
            return GatewayResponse(
                DoorState.ERROR, None, "barrier_timeout:" + ";".join(status.pending)
            )
        return GatewayResponse(
            DoorState.PENDING_CLOSE, self.interim(), "barrier_pending:" + ";".join(status.pending)
        )

    def interim(self) -> InterimSummary:
        assert self.session_id is not None
        return interim_summary(
            self.session_id, self._event_log.events_for(self.session_id), self._profiles
        )

    def _settle(self) -> FinalizedSettlement:
        assert self.session_id is not None
        return self._settler.settle(
            self.session_id,
            self._event_log.events_for(self.session_id),
            self._profiles,
            self._event_log,
        )


def build_payment_payload(settlement: FinalizedSettlement) -> dict:
    """결제 페이로드 빌더 — I10을 타입으로 강제.

    InterimSummary(잠정)를 넘기면 TypeError. blocked settlement은 ValueError (I13).
    """
    if not isinstance(settlement, FinalizedSettlement):
        raise TypeError(
            "I10: 결제 입력은 FinalizedSettlement만 허용 — interim 결과 전달 금지"
        )
    if settlement.blocked:
        raise ValueError(f"I13: blocked settlement은 결제 불가 — {settlement.block_reason}")
    return {
        "zones": [
            {
                "zone": z.zone,
                "products": [
                    {
                        "product_id": pc.product.product_id,
                        "name": pc.product.name,
                        "count": pc.count,
                        "unit_price": pc.product.unit_price,
                        "total_price": pc.total_price,
                    }
                    for pc in z.products
                ],
                "totalPrice": z.total_price,
            }
            for z in settlement.zones
        ],
        "totalPrice": settlement.total_price,
        "productCount": settlement.product_count,
        "globalSessionInfo": {"session_id": settlement.session_id, "status": "complete"},
    }
