"""판정 계층 인터페이스 (D3).

핵심 구분 (L5 승인 조건 ①): 후속 분기의 입력을 변형하는 단계는 Strategy로
표현이 안 됨 → Stage(입력 변환기)와 Strategy(결정자)를 인터페이스에서 구분.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    VisionCandidate,
    WeightSegment,
)


@dataclass(frozen=True)
class JudgmentContext:
    zone: int
    profile: SensorProfile
    delta_weight: float
    segments: tuple[WeightSegment, ...]
    vision_candidates: tuple[VisionCandidate, ...]
    active_products: tuple[ActiveProduct, ...]
    vision_only: bool = False
    stage_hints: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Stage(Protocol):
    """입력 변환기 — 판정하지 않고 컨텍스트만 바꾼다."""

    name: str

    def apply(self, ctx: JudgmentContext) -> JudgmentContext: ...


@runtime_checkable
class Strategy(Protocol):
    """결정자 — precondition을 만족하면 solve를 시도, None이면 다음 전략으로."""

    name: str

    def precondition(self, ctx: JudgmentContext) -> bool: ...

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None: ...
