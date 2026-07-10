"""핵심 도메인 타입.

설계 원칙 (GREENFIELD_DESIGN_GUIDE §7 함정 #5): 불변식은 예외 처리가 아니라
타입으로 표현한다 — InterimSummary(잠정)와 FinalizedSettlement(확정)는 서로 다른
타입이며, 결제 페이로드 빌더는 후자만 받는다 (I10).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JudgmentStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_DETECTION = "no_detection"
    SUPPRESSED = "suppressed"
    ERROR = "error"


@dataclass(frozen=True)
class ActiveProduct:
    """Node가 주는 재고 스냅샷 항목 — 매칭의 유일한 권위 소스 (제약 C7)."""

    product_id: str
    name: str
    class_id: int
    unit_weight: float
    unit_price: int
    stock_qty: int


@dataclass(frozen=True)
class VisionCandidate:
    """투표 집계를 통과한 비전 후보.

    vote_ratio의 분모는 항상 "게이트 통과 프레임 수"다 (단일 정의 — 함정 #4).
    L1 모션 게이트·L2 조기 종료 어느 조합에서도 분모 의미가 바뀌지 않는다.
    """

    class_id: int
    confidence: float
    vote_count: int
    vote_ratio: float


@dataclass(frozen=True)
class WeightSegment:
    """ingest에서 정규화된 로드셀 변화 구간 (D4). delta_grams는 부호 유지."""

    start_ts: float
    end_ts: float
    delta_grams: float


@dataclass(frozen=True)
class CellOutcome:
    """셀(존 내 좌/우 로드셀 채널 하나)의 트리거 단위 관측+판정 (설계 v2).

    전제 3(한 로드셀에 한 상품 종류)에 의해 셀 delta는 단일 품종의 정수배 —
    resolved=True면 product_id/count가 그 설명이다. resolved=False(pending)는
    close 시점 셀 net으로 이월된다. delta_weight 부호가 방향(−제거/+반품)이다.
    """

    channel: int
    delta_weight: float
    segments: tuple[WeightSegment, ...] = ()
    stabilized: bool = True
    resolved: bool = False
    product_id: str = ""
    count: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ProductCount:
    product: ActiveProduct
    count: int

    @property
    def total_price(self) -> int:
        return self.product.unit_price * self.count

    @property
    def total_weight(self) -> float:
        return self.product.unit_weight * self.count


@dataclass(frozen=True)
class JudgmentResult:
    status: JudgmentStatus
    products: tuple[ProductCount, ...] = ()
    confidence: float = 0.0
    reason: str = ""  # I8: 판정 사유 코드 — 현장 디버깅 계약
    strategy: str = ""  # 어느 전략이 판정했는가 (텔레메트리)

    @property
    def explained_weight(self) -> float:
        return sum(pc.total_weight for pc in self.products)


@dataclass(frozen=True)
class ZoneBasket:
    zone: int
    products: tuple[ProductCount, ...]
    weight_delta: float = 0.0  # OPS 로그: 해당 zone 이벤트 delta_weight 합
    trigger_count: int = 0  # OPS 로그: 해당 zone에 도달한 트리거(이벤트) 수
    notes: tuple[str, ...] = ()  # OPS 로그: 해당 zone에 귀속되는 정산 사유(I8)

    @property
    def total_price(self) -> int:
        return sum(pc.total_price for pc in self.products)

    @property
    def product_count(self) -> int:
        return sum(pc.count for pc in self.products)


@dataclass(frozen=True)
class InterimSummary:
    """잠정 집계 (I10) — 절대 결제로 전달되지 않는다.

    build_payment_payload()가 이 타입을 TypeError로 거부한다.
    """

    session_id: str
    zones: tuple[ZoneBasket, ...]
    provisional: bool = True  # 항상 True — 문서화 목적


@dataclass(frozen=True)
class FinalizedSettlement:
    """close-time 정산기의 확정 출력 — 결제 입력의 유일한 타입 (I10)."""

    session_id: str
    zones: tuple[ZoneBasket, ...]
    blocked: bool = False  # I13: 에러 세션 무성 확정 금지
    block_reason: str = ""
    notes: tuple[str, ...] = ()  # I8: 정산 사유 로그

    @property
    def total_price(self) -> int:
        return sum(z.total_price for z in self.zones)

    @property
    def product_count(self) -> int:
        return sum(z.product_count for z in self.zones)
