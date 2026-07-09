"""judgment: 라우터 순서(D3), strict(I5·I12), I6, freezer(I3), 세그먼트(D4), I8."""
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import JudgmentResult, JudgmentStatus, ProductCount, WeightSegment
from crk_model.judgment import (
    JudgmentContext,
    JudgmentRouter,
    StrictWeightMatcher,
    default_pipeline,
    enforce_full_delta_match,
)


def ctx(delta, products, candidates, profile=REFRIGERATOR, segments=(), vision_only=False):
    return JudgmentContext(
        zone=1, profile=profile, delta_weight=delta,
        segments=tuple(segments), vision_candidates=tuple(candidates),
        active_products=tuple(products), vision_only=vision_only,
    )


class TestPipelineOrder:
    def test_diagram5_order_preserved(self):
        names = [e.name for e in default_pipeline()]
        assert names == [
            "vision_only", "freezer_vision_first", "augment_stage_weight_gate",
            "segment_weight_matching", "stage_count_combo", "no_candidate_fallback",
            "min_weight_gate", "same_weight_collision_guard", "strict",
            "stage_count_combo", "same_product_count", "relaxed",
            "relaxed_loadcell_only", "vision_first_identity_partial",
            "detected_single_item_fallback", "relaxed_partial", "forced_final",
        ]


class TestStrictMatcher:
    def test_prefers_simpler_combination(self, cola, water):
        # simplicity_score: 같은 오차·conf면 종류 수가 적은 조합 우선
        m = StrictWeightMatcher()
        best = m.best([cand(1), cand(2)], -300.0, [cola, water], 3.0)
        counts = {pc.product.product_id: pc.count for pc in best.products}
        assert counts == {"P001": 3}

    def test_combination_across_kinds_when_stock_limits(self, cola, water):
        # I12: stock 제한으로 단일 종류가 막히면 종류 조합으로
        from dataclasses import replace

        cola1 = replace(cola, stock_qty=1)
        best = StrictWeightMatcher().best([cand(1), cand(2)], -300.0, [cola1, water], 3.0)
        counts = {pc.product.product_id: pc.count for pc in best.products}
        assert counts == {"P001": 1, "P002": 1}

    def test_stock_zero_excluded(self, cola):
        # I5: 품절 하드필터
        from dataclasses import replace

        sold_out = replace(cola, stock_qty=0)
        assert StrictWeightMatcher().best([cand(1)], -100.0, [sold_out], 3.0) is None

    def test_count_capped_by_stock(self, cola):
        # I12: count ≤ stock (stock=5, 600g은 6개 필요 → 매칭 불가)
        assert StrictWeightMatcher().best([cand(1)], -600.0, [cola], 3.0) is None

    def test_target_below_tolerance_empty(self, cola):
        assert StrictWeightMatcher().find_valid_combinations([cand(1)], -2.0, [cola], 3.0) == []

    def test_vision_unseen_excluded(self, cola, water):
        best = StrictWeightMatcher().best([cand(1)], -200.0, [cola, water], 3.0)
        # water(200g)는 vision 미검출 → cola×2로만 설명
        assert {pc.product.product_id: pc.count for pc in best.products} == {"P001": 2}


class TestFullDeltaMatch:
    def test_downgrades_partial_explanation(self, cola):
        # I6: 부분 설명으로 과금 금지
        r = JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(cola, 1),), 0.9, "strict")
        out = enforce_full_delta_match(r, -250.0, 3.0)
        assert out.status is JudgmentStatus.PARTIAL
        assert "full_delta_unexplained" in out.reason

    def test_relaxed_overreach_downgraded_by_router(self, cola):
        # relaxed(tol×2)가 200g 조합을 내도 delta -178과 22g 차이 → I6이 PARTIAL 강등
        router = JudgmentRouter()
        result = router.judge(ctx(-178.0, [cola], [cand(1)]))
        assert result.status is not JudgmentStatus.COMPLETE


class TestFreezer:
    def test_vision_first_single_not_summed(self, bar170, bar178, cola):
        # 178g 사건 재발 방지: 근접 단일 후보(170g, 오차 8g ≤ 15g)가 있으면
        # 후보들을 합쳐 청구하지 않는다 (I3 게이트)
        router = JudgmentRouter()
        result = router.judge(ctx(
            -178.0, [bar170, bar178, cola],
            [cand(3, conf=0.9, votes=10), cand(4, conf=0.5, votes=3), cand(1, conf=0.4, votes=2)],
            profile=FREEZER,
        ))
        assert result.strategy == "freezer_vision_first"
        assert len(result.products) == 1
        assert result.products[0].count == 1

    def test_gate_failure_falls_through(self, cola):
        # I3: 게이트(±15g) 실패 → freezer_vision_first 불발 → 폴백은 COMPLETE 금지
        router = JudgmentRouter()
        result = router.judge(ctx(-178.0, [cola], [cand(1)], profile=FREEZER))
        assert result.strategy != "freezer_vision_first"
        assert result.status is not JudgmentStatus.COMPLETE  # I6 방어


class TestSegmentMatching:
    def test_segments_resolve_aggregate_ambiguity(self, bar170, bar178):
        # 합계 348g은 모호해도 구간 -170/-178은 각각 유일 (QA Q3)
        segments = [WeightSegment(0, 1, -170.0), WeightSegment(1, 2, -178.0)]
        router = JudgmentRouter()
        result = router.judge(ctx(-348.0, [bar170, bar178], [cand(3), cand(4)], segments=segments))
        assert result.strategy == "segment_weight_matching"
        counts = {pc.product.product_id: pc.count for pc in result.products}
        assert counts == {"P170": 1, "P178": 1}

    def test_single_segment_falls_to_strict(self, cola):
        router = JudgmentRouter()
        result = router.judge(
            ctx(-100.0, [cola], [cand(1)], segments=[WeightSegment(0, 1, -100.0)])
        )
        assert result.strategy == "strict"


class TestGuards:
    def test_min_weight_gate(self, cola):
        router = JudgmentRouter()
        result = router.judge(ctx(-2.0, [cola], [cand(1)]))
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.reason == "below_min_weight_change"  # I8 사유 코드

    def test_same_weight_collision_prefers_confidence(self, cola):
        twin = cola.__class__(**{**cola.__dict__, "product_id": "P099", "class_id": 9})
        router = JudgmentRouter()
        result = router.judge(ctx(-100.0, [cola, twin], [cand(1, conf=0.6), cand(9, conf=0.9)]))
        assert result.strategy == "same_weight_collision_guard"
        assert result.products[0].product.product_id == "P099"

    def test_vision_only_count_one(self, cola):
        router = JudgmentRouter()
        result = router.judge(ctx(0.0, [cola], [cand(1, conf=0.8)], vision_only=True))
        assert result.strategy == "vision_only"
        assert result.products[0].count == 1
        assert abs(result.confidence - 0.8 * 0.7) < 1e-9

    def test_no_candidates_weight_only(self, cola):
        router = JudgmentRouter()
        result = router.judge(ctx(-100.0, [cola], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.reason == "weight_only"

    def test_telemetry_counts_hits(self, cola):
        router = JudgmentRouter()
        router.judge(ctx(-100.0, [cola], [cand(1)]))
        router.judge(ctx(-100.0, [cola], [cand(1)]))
        assert router.telemetry["strict"] == 2


class TestNoCandidateFreezerSuppression:
    """결함 수정: 후보 없음 상태에서 freezer는 weight_only로 "식별"하지 않는다."""

    def test_freezer_suppresses_identity(self, bar170):
        # freezer + vision 후보 없음 → loadcell_identity_suppressed (I3, QA Q1)
        router = JudgmentRouter()
        result = router.judge(ctx(-178.0, [bar170], [], profile=FREEZER))
        assert result.strategy == "no_candidate_fallback"
        assert result.reason == "loadcell_identity_suppressed"
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.products == ()

    def test_refrigerator_keeps_weight_only(self, cola):
        # 냉장고는 weight_is_discriminative=True → 기존 weight_only 유지 (회귀 방지)
        router = JudgmentRouter()
        result = router.judge(ctx(-100.0, [cola], [], profile=REFRIGERATOR))
        assert result.strategy == "no_candidate_fallback"
        assert result.reason == "weight_only"
        assert result.status is JudgmentStatus.COMPLETE


class TestStageCountCombination:
    def test_no_vision_uses_segment_targets(self, cola):
        # 후보없음 체인 SC1: vision 후보가 없어도 segment_targets로 개수 조합 성립
        segments = [WeightSegment(0, 1, -100.0), WeightSegment(1, 2, -100.0)]
        router = JudgmentRouter()
        result = router.judge(ctx(-200.0, [cola], [], segments=segments))
        assert result.strategy == "stage_count_combo"
        assert result.status is JudgmentStatus.COMPLETE
        assert result.products[0].count == 2

    def test_single_match_ignored_falls_through(self, cola):
        # 원본 차별점: total_count<2인 단일 매치는 이 전략의 몫이 아님 →
        # 세그먼트가 1개뿐이면 애초에 precondition 불충족(len>=2 요구) → strict로
        router = JudgmentRouter()
        result = router.judge(
            ctx(-100.0, [cola], [cand(1)], segments=[WeightSegment(0, 1, -100.0)])
        )
        assert result.strategy != "stage_count_combo"


class TestDetectedSingleItemFallback:
    def test_rescues_when_strict_and_relaxed_miss(self, cola):
        # strict(tol=3)·relaxed(tol*2=6) 둘 다 놓치는 잔차(8g)를 detected_single
        # (tol*3=9)이 구제 — 단, I6이 원래 tolerance로 재검증해 PARTIAL 강등
        router = JudgmentRouter()
        result = router.judge(ctx(-108.0, [cola], [cand(1, votes=10, conf=0.9)]))
        assert result.strategy == "detected_single_item_fallback"
        assert result.products[0].count == 1
        assert result.products[0].product.product_id == "P001"

    def test_two_detected_kinds_not_applied(self, cola, water):
        # "사실상 1종뿐"만 대상 — top 후보만 보므로 2종 감지에서도 동작 자체는
        # 하지만 same_weight 등 앞선 전략이 이미 처리 못 한 잔차만 넘어옴을 확인
        # (여기서는 top 후보가 명확한 상황에서도 다른 전략이 우선함을 검증)
        router = JudgmentRouter()
        result = router.judge(
            ctx(-300.0, [cola, water], [cand(1, votes=10, conf=0.9), cand(2, votes=1, conf=0.3)])
        )
        # cola*3=300은 strict가 정확히 잡음 → detected_single까지 갈 필요 없음
        assert result.strategy == "strict"


class TestRelaxedLoadcellOnly:
    def test_allowlist_mismatch_fridge_only(self, cola):
        # vision이 active_products에 없는 클래스를 감지 → allowlist 완전 불일치
        # → relaxed_loadcell_only가 전 재고에서 nearest-single 탐색 (냉장고만)
        router = JudgmentRouter()
        result = router.judge(ctx(-99.0, [cola], [cand(999, votes=10, conf=0.9)]))
        assert result.strategy == "relaxed_loadcell_only"
        assert result.status is JudgmentStatus.PARTIAL
        assert result.products[0].product.product_id == "P001"

    def test_freezer_suppressed(self, bar170):
        # freezer는 loadcell_only도 억제 (178g 사건 재발 방지 원리 동일 적용)
        router = JudgmentRouter()
        result = router.judge(
            ctx(-99.0, [bar170], [cand(999, votes=10, conf=0.9)], profile=FREEZER)
        )
        assert result.strategy != "relaxed_loadcell_only"


class TestVisionFirstIdentityPartial:
    def test_freezer_preserves_identity_after_relaxed_miss(self, bar170):
        # freezer_vision_first 게이트(±15g)도, relaxed(tol*2=30)도 실패하는
        # 잔차(50g) → 정체성만 보존한 PARTIAL(count=1)
        router = JudgmentRouter()
        result = router.judge(
            ctx(-220.0, [bar170], [cand(3, votes=5, conf=0.7)], profile=FREEZER)
        )
        assert result.strategy == "vision_first_identity_partial"
        assert result.status is JudgmentStatus.PARTIAL
        assert result.products[0].count == 1
        assert result.products[0].product.product_id == "P170"

    def test_weight_validated_upgrades_to_complete(self, bar170):
        # 무게검증이 tolerance 내로 통과하면 COMPLETE (개수 확정)
        router = JudgmentRouter()
        result = router.judge(
            ctx(-170.0, [bar170], [cand(3, votes=5, conf=0.7)], profile=FREEZER)
        )
        assert result.products[0].count == 1
        assert result.status is JudgmentStatus.COMPLETE
