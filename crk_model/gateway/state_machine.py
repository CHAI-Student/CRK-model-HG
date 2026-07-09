"""Multi-Zone OPEN/CLOSE 상태기계 (다이어그램 10의 재설계, D1).

현행과의 차이: PendingClose → Finalizing 전이가 "20s/3s 경과"(time-paced)가
아니라 인과 배리어 충족(I17)이다. 고정 대기는 카메라 무응답 대비 상한
타임아웃으로 강등 — 만료 시 배리어 미충족이면 에러 세션(I13, D9 fail-closed).

효과: 큐 적체 시 late-trigger 유실 race 제거 + 큐가 비면 대기 없이 즉시 확정
(지연 단축은 부수 효과).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import FinalizedSettlement, InterimSummary
from crk_model.ledger.barrier import CausalBarrier
from crk_model.ledger.events import EventLog, TriggerEvent
from crk_model.ledger.settler import CloseSettler, interim_summary

logger = logging.getLogger(__name__)
ops_logger = logging.getLogger("crk_model.ops")


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


def _format_products(zb) -> str:
    if not zb.products:
        return "none"
    return ", ".join(f"{pc.product.name}x{pc.count}" for pc in zb.products)


def _format_judgments(events: Sequence[TriggerEvent]) -> str:
    """issue #6 진단 보강: 존별 트리거의 전략·사유·신뢰도 요약.
    `judgments=strategy:reason(conf=0.62)` 형태를 트리거별로 쉼표 연결."""
    parts = []
    for e in events:
        j = e.judgment
        strategy = j.strategy or "-"
        reason = j.reason or "-"
        parts.append(f"{strategy}:{reason}(conf={j.confidence:.2f})")
    return ", ".join(parts)


def _format_runner_up(events: Sequence[TriggerEvent]) -> str:
    """issue #6 진단 보강: 채택되지 않은 톱 경쟁 후보 1~2개 — 오판정 즉시 의심
    가능하게. 채택된 class_id(judgment.products)를 제외한 vision_candidates 중
    vote_count 상위 항목을 고른다."""
    parts = []
    for e in events:
        adopted_ids = {pc.product.class_id for pc in e.judgment.products}
        runner_ups = sorted(
            (c for c in e.vision_candidates if c.class_id not in adopted_ids),
            key=lambda c: c.vote_count,
            reverse=True,
        )[:2]
        for c in runner_ups:
            parts.append(
                f"class{c.class_id}(conf={c.confidence:.2f},votes={c.vote_count})"
            )
    return ", ".join(parts)


def _log_ops_close(
    session_id: str,
    settlement: FinalizedSettlement,
    events: Sequence[TriggerEvent] = (),
) -> None:
    """[OPS][CLOSE] 존별 확정 요약 로그 (세션당 1회, 호출측이 보장).

    issue #6 실기 피드백("현재 [OPS][CLOSE]는 별로 도움이 안 된다") 대응:
    존별 줄에 판정 근거(judgments)와 채택되지 않은 톱 경쟁 후보(runner_up)를
    추가해 오판정을 즉시 의심할 수 있게 한다."""
    total_weight_delta = sum(zb.weight_delta for zb in settlement.zones)
    ops_logger.info(
        "[OPS][CLOSE] session_id=%s total_weight_delta=%+.1fg total_products=%d total_price=%d",
        session_id, total_weight_delta, settlement.product_count, settlement.total_price,
    )
    events_by_zone: dict[int, list[TriggerEvent]] = {}
    for e in events:
        events_by_zone.setdefault(e.zone, []).append(e)
    for zb in settlement.zones:
        note_part = f" notes={', '.join(zb.notes)}" if zb.notes else ""
        zone_events = events_by_zone.get(zb.zone, ())
        judgments = _format_judgments(zone_events)
        runner_up = _format_runner_up(zone_events)
        judgments_part = f" judgments={judgments}" if judgments else ""
        runner_up_part = f" runner_up={runner_up}" if runner_up else ""
        ops_logger.info(
            "[OPS][CLOSE] zone=%d weight_delta=%+.1fg products=%s triggers=%d%s%s%s",
            zb.zone, zb.weight_delta, _format_products(zb), zb.trigger_count,
            note_part, judgments_part, runner_up_part,
        )


class MultiZoneGateway:
    def __init__(
        self,
        settler: CloseSettler,
        event_log: EventLog,
        profiles: Mapping[int, SensorProfile],
        *,
        clock: Callable[[], float] = time.monotonic,
        close_timeout_s: float = 10.0,  # I17: 상한 타임아웃 (정상 경로 아님)
        worker_stall_timeout_s: float = 120.0,  # queue_pending 전용 상한 (처리 지연 ≠ 유실)
        on_finalize: Callable[[str, DoorState, FinalizedSettlement], None] | None = None,
        default_profile: SensorProfile = REFRIGERATOR,
        # zone이 profiles dict에 없을 때의 폴백 프로파일 (cabinet_type 이식) —
        # interim 집계의 tolerance가 판정(pipeline)·정산(settler)과 같은 기본
        # 프로파일을 쓰게 한다. 기본값은 기존 동작(REFRIGERATOR)과 동일.
    ):
        self._settler = settler
        self._event_log = event_log
        self._profiles = dict(profiles)
        self._default_profile = default_profile
        self._clock = clock
        self._close_timeout = close_timeout_s
        self._stall_timeout = max(worker_stall_timeout_s, close_timeout_s)
        # 세션 아카이브 훅 (issue #6): FINALIZED/ERROR로 "최초" 전이하는 시점에
        # 정확히 1회 호출된다 (재폴링 시 반복 안 됨 — I11과 동일한 멱등 요구).
        # None이면 무동작(하위호환 — 기존 테스트는 콜백 없이 게이트웨이 생성).
        self._on_finalize = on_finalize
        self.state = DoorState.IDLE
        self.session_id: str | None = None
        self.barrier = CausalBarrier()
        self._close_ts: float | None = None
        self._progress_ts: float | None = None  # 마지막 트리거 처리 완료 시각

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
        self._progress_ts = self._clock()  # stall 판정 기준점 (진행 = 살아있음)

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
        # FINALIZED는 지속 상태가 아니다 — 아래 확정 분기가 결제 결과를 응답에
        # 실어 보낸 "그 호출"에서 즉시 IDLE로 복귀한다 (원본 finalize_global_session이
        # 확정 직후 _global_session=None으로 비우는 것과 동형). 이후 CLOSE 재폴링은
        # IDLE로 떨어져 "활성 세션 없음" 응답을 받는다 — 에지(Edge_Environment)는
        # 이 응답으로 device busy를 해제한다 (실기 검증: complete를 반복 응답하면
        # busy가 영구 유지됨). I11(이중 과금 불가)은 wire 반복 전달이 아니라
        # settler의 세션 키 멱등 캐시가 보장한다.
        if self.state is not DoorState.PENDING_CLOSE:
            return GatewayResponse(self.state, None)

        status = self.barrier.status()
        if status.satisfied:
            settlement = self._settle()
            if settlement.blocked:
                self.state = DoorState.ERROR  # I13: 무성 확정 금지
                logger.error(
                    "[GATEWAY] session=%s ERROR (blocked settlement): %s",
                    self.session_id, settlement.block_reason,
                )
                ops_logger.error(
                    "[OPS][CLOSE_ERROR] session_id=%s reason=%s",
                    self.session_id, settlement.block_reason,
                )
                self._notify_finalize(DoorState.ERROR, settlement)
                return GatewayResponse(DoorState.ERROR, settlement, settlement.block_reason)
            logger.info(
                "[GATEWAY] session=%s FINALIZED: totalPrice=%d products=%d notes=%s",
                self.session_id, settlement.total_price,
                settlement.product_count, list(settlement.notes),
            )
            _log_ops_close(
                self.session_id, settlement, self._event_log.events_for(self.session_id)
            )
            self._notify_finalize(DoorState.FINALIZED, settlement)
            # 확정 결과는 이 응답으로 1회 전달 — 상태는 즉시 idle 복귀 (원본 동형).
            # session_id는 유지: late trigger의 세션 귀속·사후 로그 추적용이며,
            # 다음 OPEN이 새 ID를 발급한다. 응답의 state는 FINALIZED로 나가
            # 어댑터가 결제 페이로드를 빌드한다 (I10).
            self.state = DoorState.IDLE
            return GatewayResponse(DoorState.FINALIZED, settlement)

        assert self._close_ts is not None
        # queue_pending(워커가 처리 중)은 유실이 아니라 진행 중 — 원본
        # handle_close_signal이 pending trigger를 기다리듯 소진까지 대기한다
        # (Jetson 디코드+추론이 close_timeout보다 길 수 있음). 워커 사망/행
        # 대비 상한은 별도의 넉넉한 stall_timeout으로 유지 (I17 fail-closed).
        in_flight = any("queue_pending" in p for p in status.pending)
        timeout = self._stall_timeout if in_flight else self._close_timeout
        anchor = self._close_ts
        if in_flight and self._progress_ts is not None:
            anchor = max(anchor, self._progress_ts)  # 트리거 처리 완료 = 진행 증거
        if self._clock() - anchor >= timeout:
            # I17: 상한 타임아웃 만료 + 배리어 미충족 = 에러 세션 (D9 fail-closed).
            # "시간이 지나서 확정"은 정상 경로가 아니다 — late trigger 유실
            # = 매출 누락 또는 이중 과금이므로 결제로 보내지 않는다.
            self.state = DoorState.ERROR
            logger.error(
                "[GATEWAY] session=%s ERROR (barrier_timeout after %.1fs): %s",
                self.session_id, timeout, list(status.pending),
            )
            barrier_reason = "barrier_timeout:" + ";".join(status.pending)
            ops_logger.error(
                "[OPS][CLOSE_ERROR] session_id=%s reason=%s",
                self.session_id, barrier_reason,
            )
            self._notify_finalize(DoorState.ERROR, None)
            return GatewayResponse(DoorState.ERROR, None, barrier_reason)
        return GatewayResponse(
            DoorState.PENDING_CLOSE, self.interim(), "barrier_pending:" + ";".join(status.pending)
        )

    def _notify_finalize(
        self, state: DoorState, settlement: FinalizedSettlement | None
    ) -> None:
        """세션 아카이브 훅 호출 (issue #6) — 실패해도 게이트웨이 상태 전이는
        이미 완료된 뒤이므로 서비스 경로에 영향 없음. 콜백 자체의 예외 안전은
        호출측(ModelService/SessionArchive)이 책임진다 — 여기서는 콜백이 없는
        경우만 방어한다."""
        if self._on_finalize is not None:
            self._on_finalize(self.session_id, state, settlement)

    def interim(self) -> InterimSummary:
        assert self.session_id is not None
        return interim_summary(
            self.session_id,
            self._event_log.events_for(self.session_id),
            self._profiles,
            self._default_profile,
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
    """결제 페이로드 빌더 — I10을 타입으로 강제, wire 형식은 원본 finalize 응답과 동형.

    InterimSummary(잠정)를 넘기면 TypeError. blocked settlement은 ValueError (I13).

    형식 근거 (issue #6 4차 — 추론·정산이 정상인데 Node가 "결제할 내역이
    없습니다"를 표시): 원본 multi_zone.py의 finalize 응답(1108-1128행)은
    `success`/`status="success"`/평탄화된 `products` 배열("Node.js 하위 호환"
    주석 명시)/상품 항목 키 productIdx·productId·name·count·price를 쓴다.
    우리 구버전은 status="complete"에 zones 내부 product_id/unit_price만 보내
    Node가 결제 항목을 찾지 못했다. 상품 항목의 productIdx는 Node IF11 문자열
    ID(우리 ActiveProduct.product_id), productId는 YOLO class id(하위 호환,
    unmapped면 -1)다. confidence는 정산 결과(ZoneBasket)에 per-product 값이
    없어 0.0 고정 — 결제에는 쓰이지 않는 표시용 필드.
    """
    if not isinstance(settlement, FinalizedSettlement):
        raise TypeError(
            "I10: 결제 입력은 FinalizedSettlement만 허용 — interim 결과 전달 금지"
        )
    if settlement.blocked:
        raise ValueError(f"I13: blocked settlement은 결제 불가 — {settlement.block_reason}")

    zones = []
    all_products: list[dict] = []
    for z in settlement.zones:
        products = [
            {
                "productIdx": pc.product.product_id,
                "productId": pc.product.class_id,
                "name": pc.product.name,
                "count": pc.count,
                "price": pc.product.unit_price,
                "confidence": 0.0,
            }
            for pc in z.products
        ]
        zones.append(
            {
                "zone": z.zone,
                "products": products,
                "productNames": [p["name"] for p in products],
                "productCounts": [p["count"] for p in products],
                "totalPrice": z.total_price,
                "productCount": sum(p["count"] for p in products),
                "weightDelta": round(z.weight_delta, 1),
            }
        )
        all_products.extend(products)

    has_products = settlement.product_count > 0
    return {
        "success": True,
        "status": "success" if has_products else "complete_no_products",
        "has_products": has_products,
        "global_session_id": settlement.session_id,
        "zones": zones,
        "products": all_products,  # Node.js 하위 호환 — 결제 항목 소스 (원본 동형)
        "totalPrice": settlement.total_price,
        "totalProductCount": settlement.product_count,
        "productCount": settlement.product_count,
        "globalSessionInfo": {"session_id": settlement.session_id, "status": "complete"},
    }
