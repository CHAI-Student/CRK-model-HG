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

    head_votes/span_ratio/first_pos_ratio: held-object A-1 계측
    (docs/0713_held_object_demotion.md §3) — "프리롤 첫 프레임부터 영상 전
    구간 등장"이라는 carried-in 시간 구조의 신호. 판정에는 미사용(계측 전용,
    기본값 하위호환), 아카이브로 임계 확정 후 A-2(soft 강등)에서 소비한다.
    """

    class_id: int
    confidence: float
    vote_count: int
    vote_ratio: float
    head_votes: int = 0  # 스트림 첫 head_frames 내 득표 수
    span_ratio: float = 0.0  # (last_pos − first_pos + 1) / 디코드 프레임 수
    first_pos_ratio: float = 0.0  # 최초 등장 위치 / 디코드 프레임 수


@dataclass(frozen=True)
class WeightSegment:
    """ingest에서 정규화된 로드셀 변화 구간 (D4). delta_grams는 부호 유지."""

    start_ts: float
    end_ts: float
    delta_grams: float


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
