"""judgment — 순수 판정 계층: Stage/Strategy 라우터(D3)·무게 조합 매칭."""
from crk_model.judgment.interfaces import JudgmentContext, Stage, Strategy
from crk_model.judgment.router import JudgmentRouter, default_pipeline
from crk_model.judgment.strategies import (
    DetectedSingleItemFallbackStrategy,
    RelaxedIdentityPartialStrategy,
    RelaxedLoadcellOnlyStrategy,
    StageCountCombinationStrategy,
    VisionFirstIdentityPartialStrategy,
    enforce_full_delta_match,
)
from crk_model.judgment.strict import Combination, StrictWeightMatcher

__all__ = [
    "Combination",
    "DetectedSingleItemFallbackStrategy",
    "JudgmentContext",
    "JudgmentRouter",
    "RelaxedIdentityPartialStrategy",
    "RelaxedLoadcellOnlyStrategy",
    "Stage",
    "StageCountCombinationStrategy",
    "Strategy",
    "StrictWeightMatcher",
    "VisionFirstIdentityPartialStrategy",
    "default_pipeline",
    "enforce_full_delta_match",
]
