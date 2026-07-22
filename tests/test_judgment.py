"""judgment: 라우터 순서(D3), strict(I5·I12), I6, freezer(I3), 세그먼트(D4), I8."""
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    WeightSegment,
)
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

    def test_gate_near_miss_keeps_identity_as_partial(self, cola):
        # I3 게이트(±15g)는 실패했지만 잔차(22g)가 오염 마진(2×gate=30) 내 —
        # I-V(이슈 #15): 정체성 교체 대신 top 정체성·개수를 보존한 PARTIAL.
        # COMPLETE 금지는 유지된다 (I6 방향).
        router = JudgmentRouter()
        result = router.judge(ctx(-178.0, [cola], [cand(1)], profile=FREEZER))
        assert result.reason == "freezer_vision_first_near_gate"
        assert result.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(1, 2)]

    # 다품종 조합 테스트용 커스텀 무게 — freezer 게이트(±15g)에서 1·2종
    # 조합으로는 우연 설명이 불가능하도록 서로 소인 큰 무게를 쓴다.
    @staticmethod
    def _multi_kind_products():
        pa = ActiveProduct("PA", "A", class_id=11, unit_weight=970.0, unit_price=1000, stock_qty=5)
        pb = ActiveProduct("PB", "B", class_id=12, unit_weight=610.0, unit_price=2000, stock_qty=5)
        pc_ = ActiveProduct("PC", "C", class_id=13, unit_weight=210.0, unit_price=3000, stock_qty=5)
        return pa, pb, pc_

    def test_three_kind_combo_in_single_trigger(self):
        # 한 트리거에 서로 다른 3종 (카메라가 연속 동작을 한 녹화로 합침):
        # 2종 상한이던 조합을 k=2..4종으로 일반화 — 970+610+210=1790g은
        # 1·2종 어떤 배분으로도 ±15g 내 설명 불가, 3종 1/1/1만 정답.
        pa, pb, pc_ = self._multi_kind_products()
        router = JudgmentRouter()
        result = router.judge(ctx(
            -1790.0, [pa, pb, pc_],
            [cand(11, conf=0.7, votes=30), cand(12, conf=0.6, votes=20),
             cand(13, conf=0.5, votes=10)],
            profile=FREEZER,
        ))
        assert result.strategy == "freezer_vision_first"
        assert result.reason == "freezer_vision_first_combo"
        counts = {p.product.product_id: p.count for p in result.products}
        assert counts == {"PA": 1, "PB": 1, "PC": 1}

    def test_unique_refit_rescues_when_top_decisively_refuted(self, water, bar178):
        # 이슈 #8 계열: 최상위 후보가 오검출(반사 등)이고 잔차가 결정적
        # (400 vs 178×2=356, 44g > 2×gate)일 때 — I-V의 유일한 예외인
        # 유일-적합 구제로 water×2를 잡는다. 밴드(50%) 밖 하위 정체성이라도
        # near(30g) 내 적합이 water 하나뿐이므로 무게 우연 채택이 아니다.
        router = JudgmentRouter()
        result = router.judge(ctx(
            -400.0, [water, bar178],
            [cand(4, conf=0.9, votes=50), cand(2, conf=0.6, votes=10)],  # bar178이 1위
            profile=FREEZER,
        ))
        assert result.reason == "freezer_vision_first_unique_refit"
        assert result.status is JudgmentStatus.COMPLETE  # 잔차 0 → I6 통과
        assert result.products[0].product.product_id == "P002"
        assert result.products[0].count == 2

    def test_ambiguous_refit_refused_and_chain_not_leaked(self):
        # 유일성 조건 + 체인 누수 방어: top(990g)이 결정적 반증(잔차 620)이고
        # near(30g) 내 적합이 2개(185×2=370, 120×3=360)면 무게로는 고를 수
        # 없다(I-V) → freezer_vision_first 불발. 이때 strict/relaxed가
        # freezer로 새면 같은 무게 산술로 오식별을 재생산하므로(이슈 #15
        # 누수) 배제되어야 하고, vision_first_identity_partial이 top 정체성만
        # count=1 PARTIAL로 보존한다.
        a = ActiveProduct("PA", "A", class_id=5, unit_weight=990.0, unit_price=1000, stock_qty=5)
        b = ActiveProduct("PB", "B", class_id=6, unit_weight=185.0, unit_price=1000, stock_qty=5)
        c = ActiveProduct("PC", "C", class_id=7, unit_weight=120.0, unit_price=1000, stock_qty=5)
        router = JudgmentRouter()
        result = router.judge(ctx(
            -370.0, [a, b, c],
            [cand(5, conf=0.9, votes=100), cand(6, conf=0.5, votes=15),
             cand(7, conf=0.5, votes=14)],
            profile=FREEZER,
        ))
        assert result.strategy == "vision_first_identity_partial"
        assert result.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(5, 1)]
        assert router.telemetry["strict"] == 0
        assert router.telemetry["relaxed"] == 0

    def test_combo_prefers_fewer_kinds(self):
        # 특이도: 2종으로 설명 가능하면(970+610=1580) 3종 조합을 만들지 않는다
        pa, pb, pc_ = self._multi_kind_products()
        router = JudgmentRouter()
        result = router.judge(ctx(
            -1580.0, [pa, pb, pc_],
            [cand(11, votes=30), cand(12, votes=20), cand(13, votes=10)],
            profile=FREEZER,
        ))
        assert result.reason == "freezer_vision_first_combo"
        counts = {p.product.product_id: p.count for p in result.products}
        assert counts == {"PA": 1, "PB": 1}


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

    def test_weight_only_single_match_with_nontrivial_pool(self, cola, water):
        # issue #6 결함 수정: 풀에 2개 품목이 있어도 tolerance 내에 하나만
        # 들어오면(cola=100g, water=200g, delta=-100g) 여전히 단일 매치로 확정된다.
        router = JudgmentRouter()
        result = router.judge(ctx(-100.0, [cola, water], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.reason == "weight_only"
        assert result.status is JudgmentStatus.COMPLETE
        assert result.products[0].product.product_id == "P001"
        assert result.products[0].count == 1

    def test_weight_only_no_longer_tries_multi_item_combination(self, cola, water):
        # issue #6 오청구 재발 방지: cola(100g)+water(200g)이 섞인 조합으로
        # 우연히 맞춰지던 delta(-290g)는, 동일 상품 n개 확장 이후에도 여전히
        # 청구하지 않는다 — cola×n(100,200,300,...)·water×n(200,400,...) 어느
        # 배수도 tolerance(3.0g) 내로 290g에 들어오지 않으므로(다품목 조합은
        # 여전히 탐색하지 않는다) no_candidates_forced_final로 빠진다.
        router = JudgmentRouter()
        result = router.judge(ctx(-290.0, [cola, water], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.reason == "no_candidates_forced_final"

    def test_weight_only_ambiguous_rejects_charge(self, cola):
        from dataclasses import replace

        # cola(100g)의 근접 쌍둥이(102g) — 둘 다 delta=-101g의 tolerance(3.0g) 내.
        twin = replace(cola, product_id="P099", class_id=9, unit_weight=102.0)
        router = JudgmentRouter()
        result = router.judge(ctx(-101.0, [cola, twin], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.reason == "weight_only_ambiguous"

    def test_telemetry_counts_hits(self, cola):
        router = JudgmentRouter()
        router.judge(ctx(-100.0, [cola], [cand(1)]))
        router.judge(ctx(-100.0, [cola], [cand(1)]))
        assert router.telemetry["strict"] == 2


class TestWeightOnlySameProductCount:
    """weight_only 확장: 동일 상품 n개 제거도 유일 매칭이면 채택한다
    (직전 수정이 count=1 유일 매칭으로 과도 제한했던 것을 완화)."""

    def test_same_product_two_units_unique_match(self):
        # 2 x 79g = 158g delta — 동일 상품 2개 제거가 유일하게 tolerance(3.0g)
        # 내로 들어오면 count=2로 채택한다.
        from crk_model.core.types import ActiveProduct

        snack = ActiveProduct(
            "P079", "스낵79", class_id=7, unit_weight=79.0, unit_price=1200, stock_qty=5
        )
        router = JudgmentRouter()
        result = router.judge(ctx(-158.0, [snack], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.reason == "weight_only"
        assert result.status is JudgmentStatus.COMPLETE
        assert result.products[0].product.product_id == "P079"
        assert result.products[0].count == 2

    def test_two_products_both_plausible_is_ambiguous(self, cola):
        # cola(100g) x2 = 200g와 water2(200g) x1 = 200g가 동시에 delta=-200g의
        # tolerance(3.0g) 내로 들어오면 — 서로 다른 (product, n) 쌍 2개가 모두
        # 그럴듯하므로 여전히 weight_only_ambiguous로 거부한다.
        from dataclasses import replace

        water2 = replace(cola, product_id="P200", class_id=8, unit_weight=200.0)
        router = JudgmentRouter()
        result = router.judge(ctx(-200.0, [cola, water2], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.reason == "weight_only_ambiguous"

    def test_count_exceeding_stock_excludes_candidate(self):
        # stock=2인 상품에 대해 n=3(=237g)이 필요한 delta는 후보에서 제외되고
        # (I12), 다른 매칭도 없으면 no_candidates_forced_final로 빠진다.
        from crk_model.core.types import ActiveProduct

        limited = ActiveProduct(
            "P079L", "스낵79한정", class_id=9, unit_weight=79.0, unit_price=1200, stock_qty=2
        )
        router = JudgmentRouter()
        result = router.judge(ctx(-237.0, [limited], []))
        assert result.strategy == "no_candidate_fallback"
        assert result.status is JudgmentStatus.NO_DETECTION
        assert result.reason == "no_candidates_forced_final"


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
        # strict(tol=5)·relaxed(tol*2=10) 둘 다 놓치는 잔차(12g)를 detected_single
        # (tol*3=15)이 구제 — 단, I6이 원래 tolerance로 재검증해 PARTIAL 강등
        # (tolerance 3→5 상향: 센서 보증 분해능 5g, profiles.py C3 참조)
        router = JudgmentRouter()
        result = router.judge(ctx(-112.0, [cola], [cand(1, votes=10, conf=0.9)]))
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


class TestIssue10MelonaFiller:
    """이슈 #10 세션 3(ses-1-1783926841) 트리거 1 재현 — 무게 filler 채택.

    press로 부풀려진 delta(−241.77, 실제 비비고 224g)에 비비고가 count_gate
    (±15)를 2.8g 차이로 놓치고, 8표(1위의 4%)짜리 메로나가 79×3=237로
    채택되던 사고. 방어는 voting의 min_vote_share가 담당하고(combine에서
    메로나 제거), 여기서는 제거 전/후 후보 셋에 대한 판정 경로를 고정한다.
    """

    BIBIGO = ActiveProduct("P175", "비비고만두", class_id=3, unit_weight=224.0,
                           unit_price=3700, stock_qty=35)
    COOZ = ActiveProduct("P173", "쿠즈락만두", class_id=13, unit_weight=189.0,
                         unit_price=2100, stock_qty=40)
    MELONA = ActiveProduct("P17M", "메로나", class_id=44, unit_weight=79.0,
                           unit_price=800, stock_qty=38)

    def test_low_share_filler_rejected_even_without_floor(self):
        # I-V (이슈 #15 개정): share 하한이 없어도 판정층 자체가 filler를
        # 거부하고 정답을 복원한다 — 메로나(8표, top의 4%)는 밴드(50%)·
        # 조합(30%)·구제(refit 10%) 전부 밖이라 적합/모호성 판단에서 제외.
        # top(쿠즈락) 결정적 반증 후 남는 적합은 비비고(잔차 17.77) 하나 →
        # 유일-적합 구제, I6이 PARTIAL 강등 — 품목·수량 정답.
        # (개정 전에는 이 후보 셋에서 79×3=237이 COMPLETE 채택되던 사고 경로.)
        result = JudgmentRouter().judge(ctx(
            -241.77, [self.BIBIGO, self.COOZ, self.MELONA],
            [cand(13, 0.72, 188, 0.61), cand(3, 0.93, 70, 0.23),
             cand(44, 0.67, 8, 0.026)],
            profile=FREEZER,
        ))
        assert result.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(3, 1)]

    def test_three_vote_filler_blocked_by_refit_floor(self):
        # 이슈 #10 ses-1-1783924418 재현: 비비고 1개(delta −231.4, DB 등록
        # 무게 200g → 잔차 31.4가 near(30) 밖)에서 3표(top 171의 1.75%)
        # 멜로나가 79×3=237(잔차 5.6)로 "유일 적합"이 되어 COMPLETE 채택되던
        # 사고. refit_share(10%)가 멜로나를 구제 대상에서 제외 → 멜로나
        # 미과금, top 정체성 count=1 PARTIAL 보존.
        bibigo200 = ActiveProduct("P3", "비비고200", class_id=3, unit_weight=200.0,
                                  unit_price=3700, stock_qty=35)
        bagel = ActiveProduct("P27", "베이글", class_id=27, unit_weight=140.0,
                              unit_price=2800, stock_qty=30)
        result = JudgmentRouter().judge(ctx(
            -231.4, [bibigo200, self.COOZ, bagel, self.MELONA],
            [cand(27, 0.65, 171), cand(13, 0.77, 88), cand(3, 0.82, 81),
             cand(44, 0.60, 3)],
            profile=FREEZER,
        ))
        assert all(pc.product.class_id != 44 for pc in result.products)
        assert result.status is JudgmentStatus.PARTIAL

    def test_share_floor_recovers_true_product(self):
        # min_vote_share=0.1이 combine에서 메로나(8표 < 188×0.1)를 제거한
        # 후보 셋이면: top(쿠즈락 189) 잔차 52.77로 결정적 반증 → near(30g)
        # 내 적합이 비비고(잔차 17.77) 하나뿐 → 유일-적합 구제 채택,
        # 잔차 17.77 > tol 15는 I6이 PARTIAL 강등 — 품목·수량이 정답 복원.
        result = JudgmentRouter().judge(ctx(
            -241.77, [self.BIBIGO, self.COOZ, self.MELONA],
            [cand(13, 0.72, 188, 0.61), cand(3, 0.93, 70, 0.23)],
            profile=FREEZER,
        ))
        assert result.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(3, 1)]


class TestIssue15IdentityConsistency:
    """이슈 #15 재현 — I-V: 무게 적합성이 정체성을 선택하지 못한다.

    실기 사고: class 23(176g 등록) ×2 취출, delta −370(접촉 오염 +18g).
    65표/0.86 1위(23)가 게이트를 3g 차이로 놓치자, 16표/0.66 배경 후보
    (만두 185g×2=370)가 freezer_vision_first_single COMPLETE로 과금됐다."""

    C23 = ActiveProduct("P23", "정답상품", class_id=23, unit_weight=176.0,
                        unit_price=3000, stock_qty=40)
    BAGEL = ActiveProduct("P27", "베이글", class_id=27, unit_weight=140.0,
                          unit_price=2800, stock_qty=30)
    DUMPLING = ActiveProduct("P13", "쿠즈락만두", class_id=13, unit_weight=185.0,
                             unit_price=2100, stock_qty=40)

    def test_near_gate_keeps_top_identity_and_count(self):
        result = JudgmentRouter().judge(ctx(
            -370.0, [self.C23, self.BAGEL, self.DUMPLING],
            [cand(23, 0.86, 65), cand(27, 0.73, 27), cand(13, 0.66, 16)],
            profile=FREEZER,
        ))
        # 370 vs 176×2=352: 잔차 18 ≤ gate_n(2)=20 (설계 3a n-스케일) → 이제
        # ①에서 COMPLETE로 격상 (구 동작: near-gate PARTIAL — 과금 동일).
        # 핵심 불변: 만두(185×2=370, 잔차 0!)는 share 25%·conf 0.66으로 자격
        # 양문(single_share 50% / conf_override 0.9) 모두 미달 — 무게
        # 갈아타기는 여전히 금지된다.
        assert result.reason == "freezer_vision_first_single"
        assert result.status is JudgmentStatus.COMPLETE
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(23, 2)]


class TestIssue16WeightArbitration:
    """이슈 #16 설계 (docs/0722_issue16_arbitration_design.md): 무게=거부권,
    선택권=vision(득표+conf). n-스케일 게이트 + ① 선착 폐지 + conf 자격."""

    BAGEL = ActiveProduct("P27", "베이글", class_id=27, unit_weight=155.0,
                          unit_price=2800, stock_qty=30)
    DUMPLING = ActiveProduct("P13", "쿠즈락만두", class_id=13, unit_weight=185.0,
                             unit_price=2100, stock_qty=40)
    C175 = ActiveProduct("P23", "정답175", class_id=23, unit_weight=175.0,
                         unit_price=3000, stock_qty=40)

    def test_case_c_vote_top_survives_coincidental_runner_fit(self):
        # 실사고 (베이글 5개 연속 → 만두 4개 오과금): −743에서 베이글
        # 5×155=775(잔차 32)는 gate_n(5)=35로 적합, 만두 4×185=740(잔차 3)도
        # 적합. 구 선착 규칙은 1위(베이글) 실패 후 2위 만두를 확정했다 —
        # 중재 기준은 잔차가 아니라 vision 증거(득표·conf 모두 베이글 우세).
        result = JudgmentRouter().judge(ctx(
            -743.0, [self.BAGEL, self.DUMPLING],
            [cand(27, 1.0, 34), cand(13, 0.80, 25)],
            profile=FREEZER,
        ))
        assert result.status is JudgmentStatus.COMPLETE  # I6도 gate_n 정합 (35≥32)
        assert result.reason == "freezer_vision_first_single"
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(27, 5)]

    def test_case_d_conf_override_and_margin_arbitration(self):
        # 실사고 (진열 오염): 진열 만두 63표(conf 0.79)가 득표 1위 + 잔차 10
        # 적합, 진짜 상품(conf 1.0)은 19표로 single_share(50%) 미달 —
        # conf_override(0.9)로 자격을 얻고 conf_margin(0.15) 중재로 승리.
        result = JudgmentRouter().judge(ctx(
            -175.0, [self.DUMPLING, self.C175],
            [cand(13, 0.79, 63), cand(23, 1.0, 19)],
            profile=FREEZER,
        ))
        assert result.status is JudgmentStatus.COMPLETE
        assert result.reason == "freezer_vision_first_single_arbitrated"
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(23, 1)]

    def test_ambiguous_fits_without_conf_dominance_fall_through(self):
        # 전역 top 미적합 + 적합 2개(conf 격차 < margin) → ①은 결정하지 않고
        # 폴스루, ④도 하드 게이트 2적합 모호 → 9.2가 top 정체성만 PARTIAL 보존.
        a = ActiveProduct("PA", "A", class_id=5, unit_weight=990.0, unit_price=1000, stock_qty=5)
        b = ActiveProduct("PB", "B", class_id=6, unit_weight=185.0, unit_price=1000, stock_qty=5)
        c = ActiveProduct("PC", "C", class_id=7, unit_weight=120.0, unit_price=1000, stock_qty=5)
        result = JudgmentRouter().judge(ctx(
            -370.0, [a, b, c],
            [cand(5, 0.9, 100), cand(6, 0.75, 60), cand(7, 0.8, 55)],
            profile=FREEZER,
        ))
        assert result.strategy == "vision_first_identity_partial"
        assert result.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(5, 1)]

    def test_rollback_knobs_restore_legacy_first_fit(self):
        # env 롤백 스토리 (설계 §6): slack=0 + override/margin 비활성 →
        # 구 동작(1위 적합 실패 → 2위 우연 적합 채택)이 재현된다.
        from crk_model.judgment.strategies import FreezerVisionFirstStrategy
        legacy = FreezerVisionFirstStrategy(
            count_unit_slack=0.0, conf_override=2.0, conf_margin=2.0
        )
        result = legacy.solve(ctx(
            -743.0, [self.BAGEL, self.DUMPLING],
            [cand(27, 1.0, 34), cand(13, 0.80, 25)],
            profile=FREEZER,
        ))
        assert result is not None
        assert [(pc.product.class_id, pc.count) for pc in result.products] == [(13, 4)]
