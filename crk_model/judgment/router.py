"""판정 라우터 — 우선순위가 데이터(리스트)로 선언됨 (D3, L5).

다이어그램 5의 분기 순서를 완전 보존: 순서를 바꾸려면 이 리스트의 diff 한 줄.
전략별 히트 텔레메트리 → 실전에서 안 맞는 전략은 데이터로 제거 근거 확보.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import replace

from crk_model.core.types import JudgmentResult, JudgmentStatus
from crk_model.judgment.interfaces import JudgmentContext, Stage, Strategy
from crk_model.judgment.strategies import (
    AugmentStageWeightGateStage,
    DetectedSingleItemFallbackStrategy,
    FinalFallbackStrategy,
    FreezerVisionFirstStrategy,
    MinWeightGateStrategy,
    NoCandidateFallbackStrategy,
    RelaxedIdentityPartialStrategy,
    RelaxedLoadcellOnlyStrategy,
    RelaxedStrategy,
    SameProductCountStrategy,
    SameWeightCollisionGuardStrategy,
    SegmentWeightMatchingStrategy,
    StageCountCombinationStrategy,
    StrictStrategy,
    VisionFirstIdentityPartialStrategy,
    VisionOnlyStrategy,
    enforce_full_delta_match,
)

PipelineEntry = Stage | Strategy


def default_pipeline(
    freezer_strategy: FreezerVisionFirstStrategy | None = None,
) -> list[PipelineEntry]:
    """다이어그램 5 순서 보존 — "누적 + 특이도 우선" (QA Q2).

    freezer_strategy: I-V 노브(single/combo/refit_share, near_factor)를
    env(MODEL__JUDGMENT__*)로 튜닝한 인스턴스 주입점 — None이면 기본값.

    | 순위   | 전략 (name)                     | 다이어그램 5 대응                          |
    | ------ | -------------------------------- | ------------------------------------------ |
    | 0      | vision_only                      | vision_only                                 |
    | 1      | freezer_vision_first              | freezer_vision_first                        |
    | 2      | augment_stage_weight_gate (Stage) | (augment, 결정자 아님)                      |
    | 3      | segment_weight_matching           | segment_weight_matching                     |
    | 3.5    | stage_count_combo (require_no_vision=True) | 후보없음 체인 SC1 (순위 3 하위) |
    | 4      | no_candidate_fallback             | detected_single→vision-first?→weight_only |
    | 5      | min_weight_gate                   | min_weight_change 게이트                    |
    | 6      | same_weight_collision_guard        | same_weight_candidate_collision_guard       |
    | 7      | strict                            | strict                                      |
    | 7.5    | stage_count_combo (require_no_vision=False) | strict 실패 후 stage 조합 구제 |
    | 8      | same_product_count                | same_product_count_match                    |
    | 9      | relaxed                           | relaxed 1단계: combination(tolerance×2)     |
    | 9.1    | relaxed_loadcell_only              | relaxed 4단계: loadcell_only(allowlist 불일치) |
    | 9.2    | vision_first_identity_partial      | relaxed 실패 + vision-first → identity partial |
    | 9.3    | detected_single_item_fallback       | relaxed 실패 후 detected_single 구제        |
    | 9.4    | relaxed_partial                    | relaxed 최종 폴백: 정체성 보존 count=1(일반) |
    | 10     | forced_final                       | forced_final                                |

    순위 3.5/7.5의 stage_count_combo는 원본에서 동일 헬퍼(`_try_stage_count_
    combination_match`)가 호출되는 두 지점 — 인스턴스를 둘 둬서 유효 순서를
    보존한다(require_no_vision으로 각 인스턴스의 구간을 분리, 클래스
    docstring 참고). no_candidate_fallback 자체는 결코 None을 반환하지 않으므로
    (weight_only 또는 loadcell_identity_suppressed로 항상 확정) stage_count_
    combo는 반드시 그 앞에 와야 후보없음 체인의 첫 단계로 실제 작동한다.

    순위 9 계열 배치 근거 (원본과 의도적으로 다른 지점, 완료 보고 참고):
    원본은 `_judge_relaxed`가 자체 partial(count=1)까지 반환해 버리면
    `is_success=True`(COMPLETE·PARTIAL 모두 성공 취급)가 되어 그 뒤
    `detected_single`·`vision_first_identity_partial`이 사실상 도달 못 하는
    구조다. CRK-model-HG는 "무게로 뒷받침된 count 격상"이 "무검증 count=1"보다
    결제 정확도상 우선해야 한다고 보아, relaxed_partial(RelaxedIdentityPartial
    Strategy)을 9.4로 최종 폴백에 두고 9.1~9.3(loadcell_only→vision-first
    identity→detected_single)이 먼저 COMPLETE 격상을 시도하도록 순서를
    재배치했다. 각 전략의 precondition이 서로 겹치지 않게 좁혀져 있어
    (relaxed_loadcell_only는 allowlist 완전 불일치 전용, vision_first_identity_
    partial/relaxed_partial은 프로파일로 상호 배타) 실질적 우선순위 충돌은 없다.
    """
    return [
        VisionOnlyStrategy(),                                  # 0
        freezer_strategy or FreezerVisionFirstStrategy(),      # 1 — 센서 물리 (필연적 순서)
        AugmentStageWeightGateStage(),                         # 2 — Stage (입력 변환기)
        SegmentWeightMatchingStrategy(),                       # 3 — 시계열 정보 보존 (필연적 순서)
        StageCountCombinationStrategy(require_no_vision=True), # 3.5 — 후보없음 체인 SC1
        NoCandidateFallbackStrategy(),                         # 4
        MinWeightGateStrategy(),                               # 5
        SameWeightCollisionGuardStrategy(),                    # 6
        StrictStrategy(),                                      # 7 — 기본 경로
        StageCountCombinationStrategy(),                       # 7.5 — strict 실패 후 구제
        SameProductCountStrategy(),                            # 8
        RelaxedStrategy(),                                     # 9 — combination(tolerance×2)
        RelaxedLoadcellOnlyStrategy(),                         # 9.1 — allowlist 불일치 전용
        VisionFirstIdentityPartialStrategy(),                  # 9.2 — freezer 전용
        DetectedSingleItemFallbackStrategy(),                  # 9.3
        RelaxedIdentityPartialStrategy(),                      # 9.4 — 일반 최종 폴백
        FinalFallbackStrategy(),                               # 10
    ]


class JudgmentRouter:
    def __init__(self, pipeline: Sequence[PipelineEntry] | None = None):
        self.pipeline: list[PipelineEntry] = (
            list(pipeline) if pipeline is not None else default_pipeline()
        )
        self.telemetry: Counter[str] = Counter()  # 전략별 히트율
        # I8: solve=None인 전략 기록. 24h+ soak에서 무상한 성장 방지 (deque)
        self.miss_log: deque[str] = deque(maxlen=256)

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
