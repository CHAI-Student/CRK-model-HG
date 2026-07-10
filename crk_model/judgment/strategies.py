"""판정 전략들 — 다이어그램 5의 분기 순서를 보존한 선언적 구현 (D3).

순서 원칙 (QA Q2 전문가 보정): "누적 + 특이도 우선" — 특수한 전제를 가진
전략이 앞, 일반 폴백이 뒤. freezer 1순위(센서 물리)와 segment>aggregate
(시계열 정보 보존)는 필연적 순서.

모든 성공 결과는 라우터에서 enforce_full_delta_match(I6)를 거친다.
"""
from __future__ import annotations

import itertools
from dataclasses import replace

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
    # issue #6 결함 수정: class_id<=0은 hand(0) 또는 미매핑 센티널(-1) —
    # 정체성 조회 딕셔너리에 절대 들어오면 안 된다(여러 미매핑 상품이 -1로
    # 뭉쳐 하나로 충돌하는 것도 방지).
    return {
        p.class_id: p for p in ctx.active_products if p.stock_qty > 0 and p.class_id > 0
    }  # I5


class VisionOnlyStrategy:
    """순위 0: 로드셀 없음/강제 vision — count=1, conf×0.7."""

    name = "vision_only"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return ctx.vision_only

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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

    한 트리거 다품종 (카메라가 연속 동작을 한 녹화로 합치는 실기 특성):
    - 단일 설명은 최상위 후보만이 아니라 **득표순으로 전 정체성**에 시도한다 —
      최상위가 오검출(반사 등, 이슈 #8의 메로나)이어도 실제 상품의 단일 설명이
      조합보다 앞선다.
    - 조합은 2종 상한이 아니라 k=2..max_kinds(기본 4)종까지, vision이 실제로 본
      상위 identity_pool개 안에서 탐색한다. 종류 수가 적은 설명 우선(특이도),
      같은 k에서는 무게 오차 최소 → 득표 합 최대 순으로 채택 (전부 I3 게이트
      통과 필수, I12 stock 상한).
    """

    name = "freezer_vision_first"

    def __init__(self, max_kinds: int = 4, identity_pool: int = 6, max_total_items: int = 12):
        self._max_kinds = max_kinds
        self._identity_pool = identity_pool
        self._max_total_items = max_total_items

    def precondition(self, ctx: JudgmentContext) -> bool:
        return (
            not ctx.profile.weight_is_discriminative
            and bool(ctx.vision_candidates)
            and ctx.delta_weight < 0
        )

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        target = abs(ctx.delta_weight)
        gate = ctx.profile.count_gate
        by_class = _product_by_class(ctx)
        ranked = sorted(ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence))
        identities = [(by_class[c.class_id], c) for c in ranked if c.class_id in by_class]
        identities = identities[: self._identity_pool]
        if not identities:
            return None

        # ① 단일 정체성 우선 (I3 게이트, 178g 원칙) — 득표순 전 정체성 시도
        for p, cand in identities:
            if p.unit_weight <= 0:
                continue
            count = min(max(1, round(target / p.unit_weight)), p.stock_qty)  # I12
            if abs(target - count * p.unit_weight) <= gate:
                return JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    (ProductCount(p, count),),
                    confidence=cand.confidence,
                    reason="freezer_vision_first_single",
                )

        # ② k정체성 조합 (k=2..max_kinds, 종류 적은 설명 우선 — 여전히 I3 필수)
        pool = [(p, c) for p, c in identities if p.unit_weight > 0]
        for k in range(2, min(self._max_kinds, len(pool)) + 1):
            best: tuple[tuple[float, int], tuple, tuple] | None = None
            for subset in itertools.combinations(pool, k):
                for alloc in self._allocations([p for p, _ in subset], target, gate):
                    weight = sum(c * p.unit_weight for (p, _), c in zip(subset, alloc, strict=True))
                    key = (
                        abs(target - weight),
                        -sum(cand.vote_count for _, cand in subset),
                    )
                    if best is None or key < best[0]:
                        best = (key, subset, alloc)
            if best is not None:
                _, subset, alloc = best
                return JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    tuple(ProductCount(p, c) for (p, _), c in zip(subset, alloc, strict=True)),
                    confidence=sum(cand.confidence for _, cand in subset) / len(subset),
                    reason="freezer_vision_first_combo",
                )
        return None

    def _allocations(self, products: list[ActiveProduct], target: float, gate: float):
        """각 종류 최소 1개(부분집합 크기가 곧 종류 수)로 target±gate를 설명하는
        개수 배분을 백트래킹으로 열거. I12(stock)·총 개수 상한·무게 초과 가지치기."""

        def rec(i: int, counts: list[int], weight: float, items: int):
            if i == len(products):
                if abs(target - weight) <= gate:  # I3
                    yield tuple(counts)
                return
            p = products[i]
            for c in range(1, p.stock_qty + 1):  # I12
                w = weight + c * p.unit_weight
                if w > target + gate or items + c > self._max_total_items:
                    break
                counts.append(c)
                yield from rec(i + 1, counts, w, items + c)
                counts.pop()

        yield from rec(0, [], 0.0, 0)


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

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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


class StageCountCombinationStrategy:
    """stage(세그먼트) 단위 개수 조합 매칭 (원본 `_try_stage_count_combination_match`).

    AugmentStageWeightGateStage가 넣는 stage_hints["segment_targets"]
    (세그먼트별 |delta| 목표 무게 목록)를 소비하는 유일한 전략 — 넣기만 하고
    아무도 안 쓰던 힌트를 실제로 사용한다.

    원본 핵심 차별점: 병합 후보 풀로 strict를 돌리되 **total_count>=2인 조합만
    채택**한다 (단일 매치는 strict/relaxed가 이미 담당 — 이 전략은 "여러 개
    반복 구매"를 stage 증거로 구제하는 자리, freezer repeat-count 실패 계열
    커밋 07518f7/d86e879/b923e16).

    다이어그램 5 기준 두 자리 (docstring 근거) — 라우터에 두 인스턴스로 배치해
    `require_no_vision`로 각 인스턴스의 유효 구간을 분리, 원본 순서를 보존한다:
    - `require_no_vision=True` → 후보 없음 체인의 첫 단계
      (다이어그램 5 순위 4의 SC1, NoCandidateFallback 앞).
    - `require_no_vision=False`(기본) → strict 실패 후 폴백
      (다이어그램 5 원본 4780행 stage_strict_result 호출 지점, strict 뒤).
    두 인스턴스는 상태 없이 같은 클래스를 공유하며 name도 동일 — 텔레메트리는
    "stage_count_combo" 히트 총합으로 집계된다(어느 자리에서 맞았는지는
    miss_log 부재로 유추 가능).
    """

    name = "stage_count_combo"

    def __init__(
        self, matcher: StrictWeightMatcher | None = None, require_no_vision: bool = False
    ):
        self._matcher = matcher or StrictWeightMatcher()
        self._require_no_vision = require_no_vision

    def precondition(self, ctx: JudgmentContext) -> bool:
        if self._require_no_vision and ctx.vision_candidates:
            return False
        targets = ctx.stage_hints.get("segment_targets")
        return bool(targets) and len(targets) >= 2

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        targets = ctx.stage_hints["segment_targets"]
        pool = [p for p in ctx.active_products if p.stock_qty > 0]
        candidates = ctx.vision_candidates or tuple(
            VisionCandidate(p.class_id, 0.0, 0, 0.0) for p in pool
        )
        merged: dict[str, ProductCount] = {}
        scores: list[float] = []
        for target in targets:
            best = self._matcher.best(
                candidates, -abs(target), ctx.active_products, ctx.profile.tolerance_grams
            )
            if best is None:
                return None
            scores.append(best.match_score)
            for pc in best.products:
                pid = pc.product.product_id
                prev = merged.get(pid)
                merged[pid] = ProductCount(pc.product, (prev.count if prev else 0) + pc.count)
        total_count = sum(pc.count for pc in merged.values())
        if total_count < 2:  # 원본: 단일 매치는 이 전략의 몫이 아님
            return None
        for pc in merged.values():
            if pc.count > pc.product.stock_qty:  # I12
                return None
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            tuple(sorted(merged.values(), key=lambda pc: pc.product.product_id)),
            confidence=sum(scores) / len(scores),
            reason="stage_count_combo",
        )


class NoCandidateFallbackStrategy:
    """순위 4 (후보 없음 체인): weight_only → forced_final. I6이 과잉 과금을 차단.

    결함 수정 (원본 `_is_vision_first_identity_policy()` 분기 이식): freezer
    (profile.weight_is_discriminative=False)는 로드셀 오차가 5~15g라 무게가
    정체성 판별자 자격이 없다 (QA Q1, 178g 사건). vision 후보가 아예 없는데
    weight_only로 "식별"해 버리면 178g 사건과 동일한 오청구 위험이 재발한다.
    → freezer는 여기서 품목 식별을 포기하고 loadcell_identity_suppressed로
    NO_DETECTION 반환 (원본 `_create_loadcell_identity_suppressed_result`).
    냉장고(weight_is_discriminative=True)는 기존 weight_only 그대로 유지.

    issue #6 결함 수정 (오청구 재발 방지): weight_only는 더 이상 다품목 조합
    탐색(StrictWeightMatcher.best, 최대 6개/3종)을 쓰지 않는다 — vision 증거가
    전혀 없는 상태에서 다품목 조합이 우연히 무게 합을 맞추는 것은 사실상 우연의
    일치이고, 이것이 issue #6 실제 오청구(2품목 우연 일치, confidence 0.3, 둘 다
    오판)의 원인이었다. 원본 `judge_by_weight_only`/`_try_loadcell_nearest_single`
    (engine/decision_engine.py)처럼 단일 품목 최근접 매칭만 시도하고, 서로 다른
    품목이 섞인 조합은 여전히 금지한다 — 과청구가 미청구보다 나쁘다는 fail-closed
    방향(I13/D9)을 여기도 적용한다.

    후속 수정 (동일 상품 다수 개수 지원): "단일 품목"을 count=1로 고정하면
    동일 상품 n개 제거(delta = n × unit_weight)가 no_detection으로 빠지는
    과잉 제약이었다. 각 상품 p에 대해 n ∈ 1..min(stock, max_items)에서
    |target − n×unit_weight| ≤ tolerance인 (p, n) 쌍을 전수 수집해, 정확히
    1쌍이면 채택하고 2쌍 이상(서로 다른 product_id가 걸리는 경우)이면 여전히
    weight_only_ambiguous로 거부한다. 다품목 조합(서로 다른 상품 섞기)은
    계속 금지 — 이번 확장은 "동일 상품 n개"만 구제한다.
    """

    name = "no_candidate_fallback"

    def __init__(self, matcher: StrictWeightMatcher | None = None):
        self._matcher = matcher or StrictWeightMatcher()

    def precondition(self, ctx: JudgmentContext) -> bool:
        return not ctx.vision_candidates and not ctx.vision_only

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        if not ctx.profile.weight_is_discriminative:
            return JudgmentResult(
                JudgmentStatus.NO_DETECTION, reason="loadcell_identity_suppressed"
            )
        # weight_only: vision 필터 없이 전 재고 대상. 각 상품 p에 대해
        # n=1..min(stock, matcher.max_items)를 전수 탐색해 (p, n) 쌍을 모은다
        # (동일 상품 n개 제거 지원). 서로 다른 상품을 섞는 조합은 여전히 금지
        # (same_product_count/segment/strict 등 vision-backed 전략의 몫).
        target = abs(ctx.delta_weight)
        tol = ctx.profile.tolerance_grams
        pool = [p for p in ctx.active_products if p.stock_qty > 0 and p.unit_weight > 0]
        max_n = self._matcher.max_items
        matches: list[tuple[float, ActiveProduct, int]] = []
        for p in pool:
            upper = min(p.stock_qty, max_n)  # I12
            for n in range(1, upper + 1):
                err = abs(target - n * p.unit_weight)
                if err <= tol:
                    matches.append((err, p, n))
        if not matches:
            return JudgmentResult(
                JudgmentStatus.NO_DETECTION, reason="no_candidates_forced_final"
            )
        if len(matches) >= 2:
            # 모호: 허용오차 내에 그럴듯한 (품목, 개수) 쌍이 2개 이상 — 서로
            # 다른 품목이 걸리는 경우뿐 아니라 같은 품목에 대해 서로 다른 n이
            # 동시에 허용오차를 통과하는 경우도 포함한다(원본의 best/second-best
            # 근접-동률 거부를 tolerance 창 스타일로 적용) — 과청구가 미청구보다
            # 나쁘다(I13/D9) → 청구하지 않는다.
            return JudgmentResult(
                JudgmentStatus.NO_DETECTION, reason="weight_only_ambiguous"
            )
        _, p, n = matches[0]
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            (ProductCount(p, n),),
            confidence=0.3,
            reason="weight_only",
        )


class MinWeightGateStrategy:
    """순위 5: 무게 변화 미미 → NO_DETECTION (존 타입별 게이트, QA Q8)."""

    name = "min_weight_gate"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return (
            bool(ctx.vision_candidates)
            and abs(ctx.delta_weight) < ctx.profile.min_weight_change_grams
        )

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="below_min_weight_change"
        )


class SameWeightCollisionGuardStrategy:
    """순위 6: 동일 무게 후보 충돌 시 vision confidence 우위로 방어 (178g 사건 계열)."""

    name = "same_weight_collision_guard"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates) and ctx.delta_weight < 0

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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
    """순위 9: combination(tolerance×2) 전용 — 무게 조합 재시도.

    원본은 이 자리에서 combination 실패 시 곧장 count=1 partial까지
    반환했지만(`_judge_relaxed`의 `_create_partial_result`), 그러면 원본의
    `result.is_success`(COMPLETE·PARTIAL 둘 다 성공 취급) 판정 때문에
    `_try_detected_single_item_fallback`·`_try_vision_first_identity_partial`이
    사실상 도달 못 하는 사문화된 코드가 된다(원본도 실제로 이런 특성을
    가짐 — `judge_by_weight_only`가 `UNCERTAIN`을 내는 좁은 코너케이스에서만
    도달). CRK-model-HG는 결제 정확도상 "무게 미검증 count=1"보다 "무게로
    뒷받침된 count 격상"을 먼저 시도하는 편이 낫다고 판단해 **의도적으로
    다르게** 함: partial(count=1, 무검증)은 RelaxedIdentityPartialStrategy로
    분리해 combination/DetectedSingle/VisionFirstIdentityPartial 다음의
    "최후 수단"으로 순서를 낮췄다.
    """

    name = "relaxed"

    def __init__(self, matcher: StrictWeightMatcher | None = None, relax_factor: float = 2.0):
        self._matcher = matcher or StrictWeightMatcher()
        self._relax = relax_factor

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        relaxed_tol = ctx.profile.tolerance_grams * self._relax
        best = self._matcher.best(
            ctx.vision_candidates, ctx.delta_weight, ctx.active_products, relaxed_tol
        )
        if best is None:
            return None
        # 주의: I6은 원래 tolerance로 강제되므로, relaxed 결과가 전량 설명이
        # 안 되면 라우터에서 PARTIAL로 강등된다 (부분 설명 과금 금지).
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            best.products,
            confidence=best.match_score * 0.8,
            reason="relaxed_combination",
        )


class RelaxedIdentityPartialStrategy:
    """일반(무게가 정체성 판별자인) 프로파일의 최종 정체성 보존 폴백
    (원본 `_judge_relaxed`의 `_create_partial_result` 계열, reason="relaxed_partial").

    detected_single·vision-first identity partial까지 실패한 뒤에야 오는
    "정말 마지막 수단" — count=1, 무게 무검증. freezer는 VisionFirstIdentity
    PartialStrategy가 이미 이 역할(더 보수적인 버전, 무게검증 1회 시도)을
    전담하므로 여기서는 제외한다.
    """

    name = "relaxed_partial"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return bool(ctx.vision_candidates) and ctx.profile.weight_is_discriminative

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
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


class VisionFirstIdentityPartialStrategy:
    """relaxed 실패 + vision-first(freezer) 프로파일 전용 (원본
    `_try_vision_first_identity_partial`).

    최상위 vision 후보의 "정체성"만 보존한다 — 무게가 판별자 자격이 없으므로
    개수는 함부로 청구하지 않는 보수적 partial. 원본과 동일하게 무게검증을
    한 번 시도해서:
    - count*unit_weight가 tolerance 내로 delta를 설명하면 → COMPLETE
      (reason="vision_identity_weight_validated")
    - 설명 못 하면 → count=1로 강등한 PARTIAL
      (reason="vision_first_identity_partial", I8: 원본 계열 보존)
    """

    name = "vision_first_identity_partial"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return (
            not ctx.profile.weight_is_discriminative
            and bool(ctx.vision_candidates)
            and ctx.delta_weight < 0
        )

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        target = abs(ctx.delta_weight)
        tol = ctx.profile.tolerance_grams
        by_class = _product_by_class(ctx)
        ranked = sorted(ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence))
        for cand in ranked:
            p = by_class.get(cand.class_id)
            if p is None or p.unit_weight <= 0:
                continue
            count = min(max(1, round(target / p.unit_weight)), p.stock_qty)  # I12
            expected = count * p.unit_weight
            if abs(target - expected) <= tol:
                return JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    (ProductCount(p, count),),
                    confidence=cand.confidence,
                    reason="vision_identity_weight_validated",
                )
            # 개수 확정 실패 → 정체성만 보존 (count=1), 신뢰도 보수적으로 강등
            return JudgmentResult(
                JudgmentStatus.PARTIAL,
                (ProductCount(p, 1),),
                confidence=cand.confidence * 0.5,
                reason="vision_first_identity_partial",
            )
        return None


class RelaxedLoadcellOnlyStrategy:
    """다이어그램 5 순위 8의 4단계: relaxed의 마지막 시도 loadcell_only
    (원본 `judge_by_weight_only` — vision 후보와 무관하게 전 재고에서
    nearest-single 탐색, 낮은 신뢰도, count=1 고정).

    vision 후보는 있었지만 그 후보들의 class_id가 active_products(allowlist)
    어디에도 매칭되지 않는 경우("allowlist 매칭 전부 실패")의 최후 시도 —
    vision 후보가 allowlist와 매칭되는 경우는 DetectedSingleItemFallback·
    RelaxedIdentityPartialStrategy가 더 정교하게(정체성 보존) 담당하므로,
    이 전략의 실질 영역은 vision-allowlist 불일치 케이스로 좁힌다(안 그러면
    vision 후보가 하나라도 있으면 항상 먼저 발동해 뒤의 정체성 보존 전략들을
    가려버림).

    freezer는 여기서도 억제한다 — NoCandidateFallback의 결함 수정과 동일
    원리(무게가 정체성 판별자 자격이 없으므로 loadcell만으로 "이 상품이다"라고
    청구하면 178g 사건이 재발한다).
    """

    name = "relaxed_loadcell_only"

    def precondition(self, ctx: JudgmentContext) -> bool:
        if not ctx.vision_candidates or not ctx.profile.weight_is_discriminative:
            return False
        by_class = _product_by_class(ctx)
        return all(c.class_id not in by_class for c in ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        target = abs(ctx.delta_weight)
        pool = [p for p in ctx.active_products if p.stock_qty > 0]
        if not pool:
            return None
        best_err, best_p = min(
            ((abs(target - p.unit_weight), p) for p in pool), key=lambda t: t[0]
        )
        if best_err >= 5.0:  # 원본: nearest-single 거부 임계 5g
            return None
        match_score = max(0.0, 1.0 - best_err / 5.0)
        return JudgmentResult(
            JudgmentStatus.PARTIAL,
            (ProductCount(best_p, 1),),
            confidence=match_score * 0.7,
            reason="relaxed_loadcell_only",
        )


class DetectedSingleItemFallbackStrategy:
    """relaxed 실패 후 (다이어그램 5 순위 9와 10 사이) — 원본
    `_try_detected_single_item_fallback` (커밋 3a5c306 "strict 미스지만 단일
    감지 품목이 무게 허용치에 맞는 경우 구제").

    strict/relaxed가 모두 실패했지만, vision이 감지한 품목이 사실상 1종뿐이고
    그 unit_weight × n이 |delta|를 tolerance 내로 설명하면 (n ≤ stock, I12)
    구제한다. "사실상 1종뿐"의 조작적 정의: 최상위 vision 후보(가장 표가 많고
    confidence가 높은 것) 하나만 확인 — 2종 이상이 유의미하게 감지된 경우는
    이 전략의 대상이 아니다(모호성 방지, strict/same_product_count의 몫).

    자체 tolerance는 원래 tolerance보다 완화한다 (원본도
    `detected_single_fallback_tolerance_grams`로 strict/relaxed와 다른 별도
    소스를 씀). 우리 StrictWeightMatcher는 완전 탐색(exhaustive)이라 원래
    tolerance·relaxed(×2 tolerance) 범위 내에서 성립하는 단일 품목 매치는
    strict/relaxed가 이미 찾아내므로, 이 전략이 relaxed보다 더 나아가 구제할
    수 있는 영역은 그 바깥(원래 tolerance의 ×3)뿐이다 — "정말 strict/relaxed가
    다 놓친 마지막 한 번의 관대한 재시도"라는 원본 취지를 그대로 반영.

    신뢰도는 원본처럼 보수적으로 상한을 둔다 (원본 `min(0.65, ...)` 계수
    0.45×weight_score + 0.25×vision_conf + 0.10 이식).
    """

    name = "detected_single_item_fallback"

    def __init__(self, tolerance_factor: float = 3.0):
        self._tolerance_factor = tolerance_factor

    def precondition(self, ctx: JudgmentContext) -> bool:
        return ctx.delta_weight < 0 and bool(ctx.vision_candidates)

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        target = abs(ctx.delta_weight)
        tol = ctx.profile.tolerance_grams * self._tolerance_factor
        by_class = _product_by_class(ctx)
        ranked = sorted(
            ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence)
        )
        top = ranked[0]
        p = by_class.get(top.class_id)
        if p is None or p.unit_weight <= 0:
            return None
        n = min(max(1, round(target / p.unit_weight)), p.stock_qty)  # I12
        residual = abs(target - n * p.unit_weight)
        if residual > tol:
            return None
        weight_score = max(0.0, 1.0 - residual / tol) if tol > 0 else 0.0
        confidence = min(0.65, max(0.05, 0.45 * weight_score + 0.25 * top.confidence + 0.10))
        return JudgmentResult(
            JudgmentStatus.COMPLETE,
            (ProductCount(p, n),),
            confidence=confidence,
            reason="detected_single_item_fallback",
        )


class FinalFallbackStrategy:
    """순위 10: 최후 — 설명 불가 delta는 NO_DETECTION (사유 명시, I8)."""

    name = "forced_final"

    def precondition(self, ctx: JudgmentContext) -> bool:
        return True

    def solve(self, ctx: JudgmentContext) -> JudgmentResult | None:
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="forced_final_no_match"
        )
