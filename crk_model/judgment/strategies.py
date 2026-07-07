"""판정 전략들 — 다이어그램 5의 분기 순서를 보존한 선언적 구현 (D3).

순서 원칙 (QA Q2 전문가 보정): "누적 + 특이도 우선" — 특수한 전제를 가진
전략이 앞, 일반 폴백이 뒤. freezer 1순위(센서 물리)와 segment>aggregate
(시계열 정보 보존)는 필연적 순서.

모든 성공 결과는 라우터에서 enforce_full_delta_match(I6)를 거친다.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.strict import StrictWeightMatcher


def enforce_full_delta_match(
    result: JudgmentResult, delta_weight: float, tolerance: float
) -> JudgmentResult:
    """I6: delta 전량 설명 못 하면 COMPLETE 금지 → PARTIAL 강등 (부분 설명 과금 금지)."""
    if result.status is not JudgmentStatus.COMPLETE:
        return result
    if abs(result.explained_weight - abs(delta_weight)) <= tolerance:
        return result
    return replace(
        result,
        status=JudgmentStatus.PARTIAL,
        reason=result.reason + "+full_delta_unexplained",
    )


def _product_by_class(ctx: JudgmentContext) -> dict[int, ActiveProduct]:
    return {p.class_id: p for p in ctx.active_products if p.stock_qty > 0}  # I5


class VisionOnlyStrategy:
    """순위 0: 로드셀 없음/강제 vision — count=1, conf×0.7."""

    name = "vision_only"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return ctx.vision_only

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        by_class = _product_by_class(ctx)
        ranked = sorted(ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence))
        for cand in ranked:
            if cand.class_id in by_class:
                p = by_class[cand.class_id]
                return JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    (ProductCount(p, 1),),
                    confidence=cand.confidence * 0.7,
                    reason="vision_only",
                )
        return JudgmentResult(JudgmentStatus.NO_DETECTION, reason="no_vision_candidates")


class FreezerVisionFirstStrategy:
    """순위 1: 냉동고 — vision 정체성 우선, 무게는 개수 게이트(±15g, I3)로만.

    178g 사건 재발 방지: 근접 단일 후보가 있으면 후보들을 합쳐 청구하지 않는다.
    """

    name = "freezer_vision_first"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return (
            not ctx.profile.weight_is_discriminative
            and bool(ctx.vision_candidates)
            and ctx.delta_weight < 0
        )

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        target = abs(ctx.delta_weight)
        gate = ctx.profile.count_gate
        by_class = _product_by_class(ctx)
        ranked = sorted(ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence))
        identities = [(by_class[c.class_id], c) for c in ranked if c.class_id in by_class]
        if not identities:
            return None

        # 단일 정체성 우선 (I3 게이트)
        p, cand = identities[0]
        if p.unit_weight > 0:
            count = min(max(1, round(target / p.unit_weight)), p.stock_qty)  # I12
            if abs(target - count * p.unit_weight) <= gate:
                return JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    (ProductCount(p, count),),
                    confidence=cand.confidence,
                    reason="freezer_vision_first_single",
                )

        # 2정체성 조합 (여전히 게이트 통과 필수 — I3)
        if len(identities) >= 2:
            q, cand_q = identities[1]
            for c1 in range(1, p.stock_qty + 1):
                rem = target - c1 * p.unit_weight
                if rem <= -gate:
                    break
                if q.unit_weight <= 0:
                    continue
                c2 = round(rem / q.unit_weight)
                if c2 < 1 or c2 > q.stock_qty:
                    continue
                if abs(target - (c1 * p.unit_weight + c2 * q.unit_weight)) <= gate:
                    return JudgmentResult(
                        JudgmentStatus.COMPLETE,
                        (ProductCount(p, c1), ProductCount(q, c2)),
                        confidence=(cand.confidence + cand_q.confidence) / 2,
                        reason="freezer_vision_first_combo",
                    )
        return None


class AugmentStageWeightGateStage:
    """순위 2 (Stage — 결정자 아님): 세그먼트별 목표 무게를 힌트로 주입 (D3 구분 예시)."""

    name = "augment_stage_weight_gate"

    def apply(self, ctx: JudgmentContext) -> JudgmentContext:
        removal = [s.delta_grams for s in ctx.segments if s.delta_grams < 0]
        if not removal:
            return ctx
        hints = dict(ctx.stage_hints)
        hints["segment_targets"] = tuple(abs(d) for d in removal)
        return replace(ctx, stage_hints=hints)


class SegmentWeightMatchingStrategy:
    """순위 3: 분리 가능한 로드셀 제거 구간을 개별 매칭 (QA Q3 — 시계열 정보 보존).

    합계 348g은 조합이 모호해도, 구간(-170 → -178)은 각각 유일해진다.
    """

    name = "segment_weight_matching"

    def __init__(self, matcher: StrictWeightMatcher | None = None):
        self._matcher = matcher or StrictWeightMatcher()

    def precondition(self, ctx: JudgmentContext) -> bool:
        removal = [s for s in ctx.segments if s.delta_grams < 0]
        return len(removal) >= 2 and bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        removal = [s for s in ctx.segments if s.delta_grams < 0]
        merged: dict[str, ProductCount] = {}
        scores: list[float] = []
        for seg in removal:
            best = self._matcher.best(
                ctx.vision_candidates, seg.delta_grams, ctx.active_products,
                ctx.profile.tolerance_grams,
            )
            if best is None:
                return None  # 한 구간이라도 실패 → aggregate 경로로 폴백
            scores.append(best.match_score)
            for pc in best.products:
                pid = pc.product.product_id
                prev = merged.get(pid)
                merged[pid] = ProductCount(pc.product, (prev.count if prev else 0) + pc.count)
        # I12: 구간 합산 count가 stock을 넘으면 무효
        for pc in merged.values():
            if pc.count > pc.product.stock_qty:
                return None
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            tuple(sorted(merged.values(), key=lambda pc: pc.product.product_id)),
            confidence=sum(scores) / len(scores),
            reason="segment_weight_matching",
        )


class NoCandidateFallbackStrategy:
    """순위 4 (후보 없음 체인): weight_only → forced_final. I6이 과잉 과금을 차단."""

    name = "no_candidate_fallback"

    def __init__(self, matcher: StrictWeightMatcher | None = None):
        self._matcher = matcher or StrictWeightMatcher()

    def precondition(self, ctx: JudgmentContext) -> bool:
        return not ctx.vision_candidates and not ctx.vision_only

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        # weight_only: vision 필터 없이 전 재고 대상 (낮은 신뢰도)
        pool = [p for p in ctx.active_products if p.stock_qty > 0]
        # vision 후보가 없으므로 전 재고를 conf=0 후보로 취급 (무게만으로 탐색)
        pseudo = tuple(VisionCandidate(p.class_id, 0.0, 0, 0.0) for p in pool)
        best = self._matcher.best(
            pseudo, ctx.delta_weight, pool, ctx.profile.tolerance_grams
        )
        if best is not None:
            return JudgmentResult(
                JudgmentStatus.COMPLETE,
                best.products,
                confidence=0.3,
                reason="weight_only",
            )
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="no_candidates_forced_final"
        )


class MinWeightGateStrategy:
    """순위 5: 무게 변화 미미 → NO_DETECTION (존 타입별 게이트, QA Q8)."""

    name = "min_weight_gate"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return (
            bool(ctx.vision_candidates)
            and abs(ctx.delta_weight) < ctx.profile.min_weight_change_grams
        )

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="below_min_weight_change"
        )


class SameWeightCollisionGuardStrategy:
    """순위 6: 동일 무게 후보 충돌 시 vision confidence 우위로 방어 (178g 사건 계열)."""

    name = "same_weight_collision_guard"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates) and ctx.delta_weight < 0

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        target = abs(ctx.delta_weight)
        tol = ctx.profile.tolerance_grams
        by_class = _product_by_class(ctx)
        conf = {c.class_id: c.confidence for c in ctx.vision_candidates}
        singles = [
            p for cid, p in by_class.items()
            if cid in conf and abs(target - p.unit_weight) <= tol
        ]
        if len(singles) < 2:
            return None
        # 충돌: 동일 무게대 후보 ≥ 2 → 가장 높은 vision confidence 채택
        singles.sort(key=lambda p: -conf[p.class_id])
        if abs(singles[0].unit_weight - singles[1].unit_weight) > tol:
            return None
        p = singles[0]
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            (ProductCount(p, 1),),
            confidence=conf[p.class_id],
            reason="same_weight_collision_guard",
        )


class StrictStrategy:
    """순위 7: 무게 우선 백트래킹 조합 (기본 경로, 다이어그램 6)."""

    name = "strict"

    def __init__(self, matcher: StrictWeightMatcher | None = None):
        self._matcher = matcher or StrictWeightMatcher()

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        best = self._matcher.best(
            ctx.vision_candidates, ctx.delta_weight, ctx.active_products,
            ctx.profile.tolerance_grams,
        )
        if best is None:
            return None  # strict_mismatch → 다음 전략 (I8 사유는 라우터 미스 로그)
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            best.products,
            confidence=best.match_score,
            reason="strict",
        )


class SameProductCountStrategy:
    """순위 8: strict 실패 시 동일 품목 n개 조합 (freezer repeat-count 계열)."""

    name = "same_product_count"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        target = abs(ctx.delta_weight)
        tol = ctx.profile.tolerance_grams
        by_class = _product_by_class(ctx)
        conf = {c.class_id: c.confidence for c in ctx.vision_candidates}
        best: tuple[float, ActiveProduct, int] | None = None
        for cid, p in by_class.items():
            if cid not in conf or p.unit_weight <= 0:
                continue
            n = round(target / p.unit_weight)
            if n < 2 or n > p.stock_qty:  # I12
                continue
            err = abs(target - n * p.unit_weight)
            if err <= tol and (best is None or err < best[0]):
                best = (err, p, n)
        if best is None:
            return None
        _, p, n = best
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            (ProductCount(p, n),),
            confidence=conf[p.class_id],
            reason="same_product_count",
        )


class RelaxedStrategy:
    """순위 9: single → combination(tolerance×2) → partial."""

    name = "relaxed"

    def __init__(self, matcher: StrictWeightMatcher | None = None, relax_factor: float = 2.0):
        self._matcher = matcher or StrictWeightMatcher()
        self._relax = relax_factor

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        relaxed_tol = ctx.profile.tolerance_grams * self._relax
        best = self._matcher.best(
            ctx.vision_candidates, ctx.delta_weight, ctx.active_products, relaxed_tol
        )
        if best is not None:
            # 주의: I6은 원래 tolerance로 강제되므로, relaxed 결과가 전량 설명이
            # 안 되면 라우터에서 PARTIAL로 강등된다 (부분 설명 과금 금지).
            return JudgmentResult(
                JudgmentStatus.COMPLETE,
                best.products,
                confidence=best.match_score * 0.8,
                reason="relaxed_combination",
            )
        by_class = _product_by_class(ctx)
        ranked = sorted(ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence))
        for cand in ranked:
            if cand.class_id in by_class:
                p = by_class[cand.class_id]
                return JudgmentResult(
                    JudgmentStatus.PARTIAL,
                    (ProductCount(p, 1),),
                    confidence=cand.confidence * 0.5,
                    reason="relaxed_partial",
                )
        return None


class FinalFallbackStrategy:
    """순위 10: 최후 — 설명 불가 delta는 NO_DETECTION (사유 명시, I8)."""

    name = "forced_final"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return True

    def solve(self, ctx: JudgmentContext) -> Optional[JudgmentResult]:
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="forced_final_no_match"
        )
