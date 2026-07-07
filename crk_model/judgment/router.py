"""판정 라우터 — 우선순위가 데이터(리스트)로 선언됨 (D3, L5).

다이어그램 5의 분기 순서를 완전 보존: 순서를 바꾸려면 이 리스트의 diff 한 줄.
전략별 히트 텔레메트리 → 실전에서 안 맞는 전략은 데이터로 제거 근거 확보.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Sequence, Union

from crk_model.core.types import JudgmentResult, JudgmentStatus
from crk_model.judgment.interfaces import JudgmentContext, Stage, Strategy
from crk_model.judgment.strategies import (
    AugmentStageWeightGateStage,
    FinalFallbackStrategy,
    FreezerVisionFirstStrategy,
    MinWeightGateStrategy,
    NoCandidateFallbackStrategy,
    RelaxedStrategy,
    SameProductCountStrategy,
    SameWeightCollisionGuardStrategy,
    SegmentWeightMatchingStrategy,
    StrictStrategy,
    VisionOnlyStrategy,
    enforce_full_delta_match,
)

PipelineEntry = Union[Stage, Strategy]


def default_pipeline() -> list[PipelineEntry]:
    """다이어그램 5 순서 보존 — "누적 + 특이도 우선" (QA Q2)."""
    return [
        VisionOnlyStrategy(),                 # 0
        FreezerVisionFirstStrategy(),         # 1 — 센서 물리 (필연적 순서)
        AugmentStageWeightGateStage(),        # 2 — Stage (입력 변환기)
        SegmentWeightMatchingStrategy(),      # 3 — 시계열 정보 보존 (필연적 순서)
        NoCandidateFallbackStrategy(),        # 4
        MinWeightGateStrategy(),              # 5
        SameWeightCollisionGuardStrategy(),   # 6
        StrictStrategy(),                     # 7 — 기본 경로
        SameProductCountStrategy(),           # 8
        RelaxedStrategy(),                    # 9
        FinalFallbackStrategy(),              # 10
    ]


class JudgmentRouter:
    def __init__(self, pipeline: Sequence[PipelineEntry] | None = None):
        self.pipeline: list[PipelineEntry] = list(pipeline) if pipeline is not None else default_pipeline()
        self.telemetry: Counter[str] = Counter()  # 전략별 히트율
        self.miss_log: list[str] = []  # I8: solve=None인 전략 기록

    def judge(self, ctx: JudgmentContext) -> JudgmentResult:
        for entry in self.pipeline:
            if isinstance(entry, Stage):
                ctx = entry.apply(ctx)
                continue
            if not entry.precondition(ctx):
                continue
            result = entry.solve(ctx)
            if result is None:
                self.miss_log.append(f"{entry.name}_mismatch")
                continue
            if not ctx.vision_only:  # vision_only는 설명할 delta가 없음
                result = enforce_full_delta_match(
                    result, ctx.delta_weight, ctx.profile.tolerance_grams
                )
            self.telemetry[entry.name] += 1
            return replace(result, strategy=entry.name)
        # FinalFallback이 항상 잡지만 방어적으로:
        return JudgmentResult(JudgmentStatus.NO_DETECTION, reason="pipeline_exhausted")
