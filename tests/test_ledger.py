"""ledger: 배리어, 정산기 v2 (셀 net 우선 → 오배치 반품 교정), 멱등, 에러 정책.

설계 전제 3개(상품 ≤10 · 5존 × 좌/우 셀 · 셀당 한 상품 종류)만 사용한다.
"""
import pytest

from crk_model.core.policy import ErrorSessionPolicy
from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    CellOutcome,
    InterimSummary,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.gateway import build_payment_payload
from crk_model.ledger import (
    CausalBarrier,
    CellBeliefStore,
    CloseSettler,
    EventLog,
    ShadowSettlerRunner,
    TriggerEvent,
    interim_summary,
)

PROFILES = {1: REFRIGERATOR, 2: REFRIGERATOR, 9: FREEZER}


def cellout(channel, delta, product=None, count=0, resolved=False, reason=""):
    return CellOutcome(
        channel=channel,
        delta_weight=delta,
        resolved=resolved,
        product_id=product.product_id if product is not None else "",
        count=count,
        reason=reason,
    )


def ev(sid, zone, ts, cells, charged=()):
    """트리거 이벤트 — charged: 판정이 과금한 (product, count) 목록."""
    pcs = tuple(ProductCount(p, c) for p, c in charged)
    status = JudgmentStatus.COMPLETE if pcs else JudgmentStatus.NO_DETECTION
    j = JudgmentResult(status, pcs, 0.9, "cell_delta", "cell_delta")
    return TriggerEvent(
        sid, zone, ts, sum(c.delta_weight for c in cells), (), j, cells=tuple(cells)
    )


def removal(sid, zone, ts, product, count=1, channel=0, delta=None):
    d = delta if delta is not None else -product.unit_weight * count
    return ev(
        sid, zone, ts,
        [cellout(channel, d, product, count, resolved=True)],
        charged=[(product, count)],
    )


def ret(sid, zone, ts, delta, product=None, count=0, channel=0):
    return ev(
        sid, zone, ts,
        [cellout(channel, delta, product, count, resolved=product is not None)],
    )


def error_event(sid, zone, ts):
    return TriggerEvent(
        sid, zone, ts, 0.0, (), JudgmentResult(JudgmentStatus.ERROR), status="error"
    )


def settler(catalog=(), **kw):
    return CloseSettler(catalog=(lambda: tuple(catalog)) if catalog else None, **kw)


class TestCausalBarrier:
    def test_queue_pending_blocks(self):
        b = CausalBarrier()
        b.notify_enqueued(1)
        b.notify_enqueued(1)
        b.notify_processed(1)
        st = b.status()
        assert not st.satisfied and "zone1:queue_pending(1)" in st.pending
        b.notify_processed(1)
        assert b.status().satisfied

    def test_loadcell_unstable_blocks(self):
        b = CausalBarrier()
        b.set_loadcell_stable(1, False)
        assert not b.status().satisfied
        b.set_loadcell_stable(1, True)
        assert b.status().satisfied

    def test_seq_watermark_blocks_until_arrival(self):
        b = CausalBarrier()
        b.set_close_watermark({1: 3})
        b.note_seq(1, 2)
        assert not b.status().satisfied
        b.note_seq(1, 3)
        assert b.status().satisfied


class TestSettlerContracts:
    def test_idempotent_finalize(self, cola):
        s = settler()
        events = [removal("s1", 1, 1.0, cola)]
        a = s.settle("s1", events, PROFILES)
        b = s.settle("s1", events, PROFILES)
        assert a is b  # 멱등 — 이중 과금 불가

    def test_events_after_finalize_rejected(self, cola):
        s = settler()
        log = EventLog()
        log.append(removal("s1", 1, 1.0, cola))
        s.settle("s1", log.events_for("s1"), PROFILES, log)
        late = removal("s1", 1, 2.0, cola)
        assert not log.append(late)  # 확정 후 유입 거부
        assert late in log.rejected

    def test_error_blocks_payment_fail_closed(self, cola):
        # 에러 trigger 존재 → blocked, 결제 빌더 거부 (무성 확정 금지)
        s = settler()
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = s.settle("s1", events, PROFILES)
        assert result.blocked
        with pytest.raises(ValueError):
            build_payment_payload(result)

    def test_error_free_zone_policy_excludes_only_error_zones(self, cola):
        s = settler(error_policy=ErrorSessionPolicy.FINALIZE_ERROR_FREE_ZONES)
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = s.settle("s1", events, PROFILES)
        assert not result.blocked
        assert [z.zone for z in result.zones] == [1]
        assert any("error_zones_excluded" in n for n in result.notes)


class TestCellNetPass:
    """① 셀 net 우선 — 세션 순변화가 트리거 증분을 덮어쓴다."""

    def test_take_and_put_back_clears_charge(self, cola):
        # 제거는 과금됐는데 반품이 미확정이어도 net~0이 청구를 클리어한다
        s = settler()
        events = [
            removal("s1", 1, 1.0, cola),
            ret("s1", 1, 2.0, +100.0),  # 미확정 반품 (resolved=False)
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        assert any("cell_net_clear" in n for n in result.notes)

    def test_pending_cells_resolved_by_net(self, cola):
        # 트리거마다 개수 보류(count_pending) — close net -300이 3개로 확정
        s = settler(catalog=[cola])
        events = [
            ev("s1", 1, 1.0, [cellout(0, -100.0, cola, reason="count_pending")]),
            ev("s1", 1, 2.0, [cellout(0, -200.0, cola, reason="count_pending")]),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price * 3
        assert any("cell_net_resolve:zone1:ch0:P001=3" in n for n in result.notes)

    def test_net_overrides_wrong_incremental(self, cola):
        # 증분은 2개 과금인데 미확정 반품 +100 → net -100 → 1개로 교정
        s = settler()
        events = [
            removal("s1", 1, 1.0, cola),
            removal("s1", 1, 2.0, cola),
            ret("s1", 1, 3.0, +100.0),  # 미확정 반품 (resolved=False)
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price * 1
        assert any("cell_net_resolve:zone1:ch0:P001=1" in n for n in result.notes)

    def test_gate_failure_keeps_incremental(self, cola):
        # net -160이 100g 상품으로 설명 불가(냉장 ±3g) → 증분(1개) 유지 (fail-closed)
        s = settler()
        events = [
            removal("s1", 1, 1.0, cola),
            ev("s1", 1, 2.0, [cellout(0, -60.0, reason="identity_ambiguous")]),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price * 1
        assert any("cell_net_gate_failed" in n for n in result.notes)

    def test_belief_identity_resolves_unknown_cell(self, cola):
        beliefs = CellBeliefStore()
        for _ in range(3):
            beliefs.observe(1, 0, "P001", strong=True)
        s = settler(catalog=[cola], beliefs=beliefs)
        events = [ev("s1", 1, 1.0, [cellout(0, -200.0, reason="identity_ambiguous")])]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price * 2

    def test_unknown_identity_weight_unique_refrigerator(self, cola, water):
        # 미지 셀 net -300: cola 3개만 설명 (water 1.5개 비정수) → 냉장 채택
        s = settler(catalog=[cola, water])
        events = [ev("s1", 1, 1.0, [cellout(0, -300.0)])]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price * 3
        assert any("cell_net_weight_unique" in n for n in result.notes)

    def test_unknown_identity_freezer_fails_closed(self, bar170):
        # 냉동 미지 셀은 무게 단독 채택 보류 → 과금 0 + 기록 (매출 누락 방향)
        s = settler(catalog=[bar170])
        events = [ev("s1", 9, 1.0, [cellout(0, -170.0)])]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        assert any("cell_identity_unknown" in n for n in result.notes)

    def test_count_ambiguous_takes_floor(self):
        # w=20 ≤ 2×15: net -30은 1개/2개 모두 게이트 이내 → 작은 n (보수 청구)
        light = ActiveProduct(
            "P020", "젤리", class_id=5, unit_weight=20.0, unit_price=500, stock_qty=10
        )
        beliefs = CellBeliefStore()
        for _ in range(3):
            beliefs.observe(9, 0, "P020", strong=True)
        s = settler(catalog=[light], beliefs=beliefs)
        events = [ev("s1", 9, 1.0, [cellout(0, -30.0, light, reason="count_pending")])]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 500 * 1
        assert any("count_ambiguous_floor" in n for n in result.notes)

    def test_surplus_return_never_negative(self, cola):
        # 반품이 제거보다 많아 net > 0 — count ≥ 0 (환수 > 청구 금지)
        s = settler()
        events = [
            removal("s1", 1, 1.0, cola),
            ret("s1", 1, 2.0, +100.0, cola, 1),
            ret("s1", 1, 3.0, +100.0, cola, 1),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        for z in result.zones:
            for pc in z.products:
                assert pc.count >= 0


class TestCrossCellReturns:
    """② 오배치 반품 교정."""

    def test_return_to_other_zone_deducts(self, cola):
        # zone1에서 꺼내 zone2 셀에 반납 → zone1 청구 차감
        s = settler(catalog=[cola])
        events = [
            removal("s1", 1, 1.0, cola),
            ev("s1", 2, 2.0, [cellout(0, +100.0)]),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        assert any("cross_cell_return" in n for n in result.notes)

    def test_return_to_sibling_cell_deducts(self, cola, water):
        # 같은 존의 반대쪽 셀에 반납해도 교정된다 (셀 단위 정밀화)
        s = settler(catalog=[cola, water])
        events = [
            removal("s1", 1, 1.0, cola, channel=0),
            ev("s1", 1, 2.0, [cellout(1, +100.0)]),  # water 셀에 cola 반납
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0

    def test_exact_tie_withholds_deduction(self, cola):
        # 동일 무게 두 상품이 각각 청구됨 + 반품 무게가 완전 동률 → 감산 보류
        cola2 = ActiveProduct(
            "P00X", "콜라제로", class_id=6, unit_weight=100.0, unit_price=1700, stock_qty=5
        )
        s = settler(catalog=[cola, cola2])
        events = [
            removal("s1", 1, 1.0, cola, channel=0),
            removal("s1", 1, 2.0, cola2, channel=1),
            ev("s1", 2, 3.0, [cellout(0, +100.0)]),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price + cola2.unit_price  # 감산 없음
        assert any("cross_cell_return_ambiguous" in n for n in result.notes)

    def test_unmatched_return_recorded_only(self, cola):
        # 어느 장바구니와도 안 맞는 반품 — 기록만 (과소청구 방향 안전)
        s = settler(catalog=[cola])
        events = [
            removal("s1", 1, 1.0, cola),
            ev("s1", 2, 2.0, [cellout(0, +47.0)]),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == cola.unit_price
        assert any("unmatched_return" in n for n in result.notes)


class TestInterimAndShadow:
    def test_interim_is_distinct_type_and_rejected_by_payment(self, cola):
        # 잠정 타입은 결제 빌더가 TypeError로 거부
        summary = interim_summary("s1", [removal("s1", 1, 1.0, cola)], PROFILES)
        assert isinstance(summary, InterimSummary)
        assert summary.zones[0].products[0].count == 1
        with pytest.raises(TypeError):
            build_payment_payload(summary)

    def test_interim_nets_resolved_return(self, cola):
        summary = interim_summary(
            "s1",
            [removal("s1", 1, 1.0, cola), ret("s1", 1, 2.0, +100.0, cola, 1)],
            PROFILES,
        )
        assert all(pc.count == 0 or not z.products for z in summary.zones for pc in z.products)

    def test_shadow_runner_logs_diff(self, cola):
        primary = settler()
        shadow = settler(error_policy=ErrorSessionPolicy.FINALIZE_ERROR_FREE_ZONES)
        runner = ShadowSettlerRunner(primary, shadow)
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = runner.settle("s1", events, PROFILES)
        assert result.blocked  # primary 기준 반환
        assert isinstance(runner.diffs, list)
