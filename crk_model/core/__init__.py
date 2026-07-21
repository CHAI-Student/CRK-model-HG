"""core — 도메인 타입·SensorProfile·정책·env 설정 (런타임 의존성 0)."""
from crk_model.core.policy import ErrorSessionPolicy
from crk_model.core.profiles import FREEZER, REFRIGERATOR, SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    FinalizedSettlement,
    InterimSummary,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
    WeightSegment,
    ZoneBasket,
)

__all__ = [
    "ActiveProduct",
    "ErrorSessionPolicy",
    "FinalizedSettlement",
    "FREEZER",
    "InterimSummary",
    "JudgmentResult",
    "JudgmentStatus",
    "ProductCount",
    "REFRIGERATOR",
    "SensorProfile",
    "VisionCandidate",
    "WeightSegment",
    "ZoneBasket",
]
