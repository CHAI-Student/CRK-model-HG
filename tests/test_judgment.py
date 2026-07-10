"""판정 라우터 v2 — 셀 단위 4경로 (설계 전제 3개만 사용).

전제: 상품 ≤10 (class_id ≤11, hand=0) · 5존 × 좌/우 셀 · 셀당 한 상품 종류.
그 외 어떤 가정도 테스트에 넣지 않는다 (예: "상품 무게는 서로 다르다" 금지 —
bar170/bar178은 freezer ±15g에서 서로 겹치는 실사고 케이스다).
"""
import pytest

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import ActiveProduct, CellOutcome, JudgmentStatus
from crk_model.judgment import JudgmentContext, JudgmentRouter
from tests.conftest import cand


def cell(channel, delta, stabilized=True, reason=""):
    return CellOutcome(
        channel=channel, delta_weight=delta, stabilized=stabilized, reason=reason
    )


def ctx(
    cells,
    products,
    candidates=(),
    identities=None,
    profile=REFRIGERATOR,
    vision_only=False,
    zone=1,
):
    return JudgmentContext(
        zone=zone,
        profile=profile,
        cells=tuple(cells),
        vision_candidates=tuple(candidates),
        active_products=tuple(products),
        identities=identities or {},
        vision_only=vision_only,
    )


@pytest.fixture
def router():
    return JudgmentRouter()


class TestCellDelta:
    """① 주 경로 — 정체성 p의 n×w 설명."""

    def test_known_identity_counts_from_weight(self, router, cola):
        d = router.judge(ctx([cell(0, -200.0)], [cola], identities={0: "P001"}))
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.result.strategy == "cell_delta"
        assert [(pc.product.product_id, pc.count) for pc in d.result.products] == [
            ("P001", 2)
        ]
        assert d.cells[0].resolved and d.cells[0].count == 2

    def test_known_identity_works_without_vision(self, router, cola):
        # 알려진 셀은 비전이 실패해도(김서림·가림) 무게만으로 판정된다
        d = router.judge(ctx([cell(0, -100.0)], [cola], identities={0: "P001"}))
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.result.products[0].count == 1

    def test_two_cells_resolve_independently(self, router, cola, water):
        # 한 트리거에 좌/우 셀 동시 동작 — v1 "다품종 조합"의 자연 분해
        d = router.judge(
            ctx(
                [cell(0, -100.0), cell(1, -400.0)],
                [cola, water],
                identities={0: "P001", 1: "P002"},
            )
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        got = {(pc.product.product_id, pc.count) for pc in d.result.products}
        assert got == {("P001", 1), ("P002", 2)}

    def test_unknown_cell_adopts_vision_weight_intersection(self, router, cola, water):
        # 미지 셀: V ∩ W 유일 → 채택 (신념 갱신 입력)
        d = router.judge(
            ctx([cell(0, -100.0)], [cola, water], candidates=[cand(1, conf=0.9)])
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.cells[0].product_id == "P001"
        assert d.cells[0].reason == "vision_weight_match"

    def test_unknown_cell_weight_unique_refrigerator(self, router, cola, water):
        # -200은 cola 2개로도 water 1개로도 설명 → 유일 아님 → 보류
        d = router.judge(ctx([cell(0, -200.0)], [cola, water]))
        assert d.result.status is not JudgmentStatus.COMPLETE
        # -300: cola 3개(300) = water 1.5개(비정수) → cola 유일 → 무게 단독 채택 (냉장)
        d2 = router.judge(ctx([cell(0, -300.0)], [cola, water]))
        assert d2.result.status is JudgmentStatus.COMPLETE
        assert d2.cells[0].reason == "weight_unique"
        assert d2.result.products[0].count == 3

    def test_return_resolved_charges_nothing(self, router, cola):
        d = router.judge(ctx([cell(0, +100.0)], [cola], identities={0: "P001"}))
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.result.products == ()
        assert d.result.reason == "return_resolved"
        assert d.cells[0].resolved and d.cells[0].count == 1

    def test_stock_cap_blocks_removal(self, router, cola):
        # 재고 5개인데 -600(6개) — 셀에 없는 것은 꺼낼 수 없음
        d = router.judge(ctx([cell(0, -600.0)], [cola], identities={0: "P001"}))
        assert d.result.status is not JudgmentStatus.COMPLETE
        assert not d.cells[0].resolved


class TestFreezerSuppression:
    """냉동(±15g)은 무게 단독 정체성 채택 보류 — 178g 사건·이슈 #6 억제 원리."""

    def test_weight_only_identity_suppressed_in_freezer(self, router, bar170):
        d = router.judge(ctx([cell(0, -170.0)], [bar170], profile=FREEZER))
        assert d.result.status is JudgmentStatus.NO_DETECTION
        assert d.cells[0].reason == "weight_identity_suppressed"
        assert not d.cells[0].product_id

    def test_freezer_vision_plus_weight_still_adopts(self, router, bar170, bar178):
        # ±15g에서 -170은 bar178(res 8)로도 설명되지만 비전이 bar170만 지목 → V∩W 유일
        d = router.judge(
            ctx(
                [cell(0, -170.0)],
                [bar170, bar178],
                candidates=[cand(3, conf=0.8)],
                profile=FREEZER,
            )
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.cells[0].product_id == "P170"

    def test_known_freezer_cell_resolves_without_vision(self, router, bar170):
        # 학습이 끝난 냉동 셀은 김서림으로 비전이 죽어도 무게로 판정
        d = router.judge(
            ctx([cell(0, -340.0)], [bar170], identities={0: "P170"}, profile=FREEZER)
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.result.products[0].count == 2


class TestCellPending:
    """② 모호 — 확실한 만큼만, 확정은 close 셀 net으로."""

    def test_identity_ambiguous_pends(self, router, bar170, bar178):
        # freezer ±15g에서 -174는 두 상품 모두 설명 + 비전도 둘 다 지목 → 보류
        d = router.judge(
            ctx(
                [cell(0, -174.0)],
                [bar170, bar178],
                candidates=[cand(3), cand(4)],
                profile=FREEZER,
            )
        )
        assert d.result.status is JudgmentStatus.NO_DETECTION
        assert d.result.strategy == "cell_pending"
        assert d.cells[0].reason == "identity_ambiguous"

    def test_count_ambiguous_confirms_identity_only(self, router):
        light = ActiveProduct(
            "P020", "젤리", class_id=5, unit_weight=20.0, unit_price=500, stock_qty=10
        )
        # w=20 ≤ 2×15 → freezer에서 -30은 n=1(res 10)과 n=2(res 10) 모두 tol 이내
        d = router.judge(
            ctx([cell(0, -30.0)], [light], candidates=[cand(5)], profile=FREEZER)
        )
        assert not d.cells[0].resolved
        assert d.cells[0].reason == "count_pending"
        assert d.cells[0].product_id == "P020"  # 정체성은 확정 (신념 증거)

    def test_partial_when_one_cell_pends(self, router, cola, bar170, bar178):
        d = router.judge(
            ctx(
                [cell(0, -100.0), cell(1, -174.0)],
                [cola, bar170, bar178],
                candidates=[cand(1), cand(3), cand(4)],
                identities={0: "P001"},
                profile=FREEZER,
            )
        )
        assert d.result.status is JudgmentStatus.PARTIAL
        assert d.result.strategy == "cell_pending"
        assert [(pc.product.product_id, pc.count) for pc in d.result.products] == [
            ("P001", 1)
        ]

    def test_unstable_cell_pends(self, router, cola):
        d = router.judge(
            ctx(
                [cell(0, -100.0, stabilized=False, reason="needs_return_stabilization")],
                [cola],
                identities={0: "P001"},
            )
        )
        assert d.result.status is JudgmentStatus.NO_DETECTION
        assert d.result.strategy == "cell_pending"


class TestVisionCrossCheck:
    def test_mismatch_noted_but_judgment_holds(self, router, cola, water):
        # 알려진 셀: 제거는 물리적으로 그 셀 상품 — 비전 불일치는 note만
        d = router.judge(
            ctx(
                [cell(0, -100.0)],
                [cola, water],
                candidates=[cand(2, conf=0.9)],  # water 지목 — 그러나 -100은 water로 설명 불가
                identities={0: "P001"},
            )
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        assert "vision_mismatch:P002" in d.cells[0].reason
        assert d.contradictions == ()  # 무게가 water를 지지하지 않음 — 강등 증거 아님

    def test_mismatch_with_weight_support_reports_contradiction(
        self, router, cola, water
    ):
        # 비전이 water를 지목하고 -200이 water 1개로도 설명됨 → 강등 증거
        d = router.judge(
            ctx(
                [cell(0, -200.0)],
                [cola, water],
                candidates=[cand(2, conf=0.9)],
                identities={0: "P001"},
            )
        )
        assert d.contradictions == ((0, "P002"),)


class TestNoSignalAndVisionOnly:
    def test_below_gate_is_no_detection(self, router, cola):
        d = router.judge(ctx([cell(0, -2.0), cell(1, 1.0)], [cola]))
        assert d.result.status is JudgmentStatus.NO_DETECTION
        assert d.result.strategy == "no_signal"

    def test_vision_only_top_candidate(self, router, cola, water):
        d = router.judge(
            ctx(
                [cell(0, 0.0, stabilized=False, reason="insufficient_samples")],
                [cola, water],
                candidates=[cand(2, conf=0.8, votes=12), cand(1, conf=0.9, votes=3)],
                vision_only=True,
            )
        )
        assert d.result.status is JudgmentStatus.COMPLETE
        assert d.result.strategy == "vision_only"
        assert d.result.products[0].product.product_id == "P002"  # 최다득표
        assert d.result.products[0].count == 1
        assert d.result.confidence == pytest.approx(0.8 * 0.7)

    def test_vision_only_without_candidates(self, router, cola):
        d = router.judge(ctx([cell(0, 0.0)], [cola], vision_only=True))
        assert d.result.status is JudgmentStatus.NO_DETECTION
        assert d.result.reason == "no_vision_candidates"

    def test_hand_class_never_matches(self, router):
        unmapped = ActiveProduct(
            "P999", "미매핑", class_id=-1, unit_weight=100.0, unit_price=1000, stock_qty=5
        )
        # hand(0)·미매핑(-1)은 비전 정체성 매칭에서 제외 (이슈 #6 승계)
        d = router.judge(
            ctx([cell(0, -100.0)], [unmapped], candidates=[cand(0), cand(-1)])
        )
        assert d.result.status is not JudgmentStatus.COMPLETE


class TestTelemetry:
    def test_path_hits_recorded(self, router, cola):
        router.judge(ctx([cell(0, -100.0)], [cola], identities={0: "P001"}))
        router.judge(ctx([cell(0, -1.0)], [cola]))
        assert router.telemetry["cell_delta"] == 1
        assert router.telemetry["no_signal"] == 1
