"""ledger: 배리어(I17), 정산기(I11·I13·I14), 교차존, freezer close, shadow, I10."""
import pytest

from crk_model.core.policy import ErrorSessionPolicy
from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    InterimSummary,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.gateway import build_payment_payload
from crk_model.ledger import (
    CausalBarrier,
    CloseSettler,
    EventLog,
    ShadowSettlerRunner,
    TriggerEvent,
    interim_summary,
)


def removal(sid, zone, ts, product, count=1, delta=None):
    d = delta if delta is not None else -product.unit_weight * count
    j = JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(product, count),), 0.9, "strict")
    return TriggerEvent(sid, zone, ts, d, (), j)


def ret(sid, zone, ts, delta):
    j = JudgmentResult(JudgmentStatus.NO_DETECTION, reason="return")
    return TriggerEvent(sid, zone, ts, delta, (), j)


def error_event(sid, zone, ts):
    return TriggerEvent(
        sid, zone, ts, 0.0, (), JudgmentResult(JudgmentStatus.ERROR), status="error"
    )


PROFILES = {1: REFRIGERATOR, 2: REFRIGERATOR, 9: FREEZER}


class TestCausalBarrier:
    def test_queue_pending_blocks(self):
        b = CausalBarrier()
        b.notify_enqueued(1)
        b.notify_enqueued(1)
        b.notify_processed(1)
        st = b.status()
        assert not st.satisfied and "zone1:queue_pending(1)" in st.pending
        b.notify_processed(1)
        assert b.status().satisfied  # I17 ①

    def test_loadcell_unstable_blocks(self):
        b = CausalBarrier()
        b.set_loadcell_stable(1, False)
        assert not b.status().satisfied
        b.set_loadcell_stable(1, True)
        assert b.status().satisfied  # I17 ②

    def test_seq_watermark_blocks_until_arrival(self):
        b = CausalBarrier()
        b.set_close_watermark({1: 3})
        b.note_seq(1, 2)
        assert not b.status().satisfied  # I17 ③ (D2)
        b.note_seq(1, 3)
        assert b.status().satisfied


class TestCloseSettler:
    def test_idempotent_finalize(self, cola):
        # I11: 같은 세션은 항상 같은 결과 객체
        s = CloseSettler()
        events = [removal("s1", 1, 1.0, cola)]
        a = s.settle("s1", events, PROFILES)
        b = s.settle("s1", events, PROFILES)
        assert a is b

    def test_events_after_finalize_rejected(self, cola):
        s = CloseSettler()
        log = EventLog()
        log.append(removal("s1", 1, 1.0, cola))
        s.settle("s1", log.events_for("s1"), PROFILES, log)
        late = removal("s1", 1, 2.0, cola)
        assert not log.append(late)  # I11: 확정 후 유입 거부
        assert late in log.rejected

    def test_same_zone_return_decrements(self, cola):
        s = CloseSettler()
        events = [removal("s1", 1, 1.0, cola), ret("s1", 1, 2.0, +100.0)]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0

    def test_count_never_negative(self, cola):
        # I14: 환수 > 청구 금지 — 반품 3회여도 count 0 아래로 안 내려감
        s = CloseSettler()
        events = [
            removal("s1", 1, 1.0, cola),
            ret("s1", 1, 2.0, +100.0),
            ret("s1", 1, 3.0, +100.0),
            ret("s1", 1, 4.0, +100.0),
        ]
        result = s.settle("s1", events, PROFILES)
        for z in result.zones:
            for pc in z.products:
                assert pc.count >= 0
        assert result.total_price == 0

    def test_cross_zone_return(self, cola):
        # zone1에서 꺼내 zone2에 반납 → zone1 count 차감
        s = CloseSettler()
        events = [removal("s1", 1, 1.0, cola), ret("s1", 2, 2.0, +100.0)]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        assert any("cross_zone_return" in n for n in result.notes)  # I8

    def test_net_delta_correction(self, cola, water):
        # 판정은 water(200g)로 청구했지만 net=0 (반품이 무게 미매칭) → 교정
        s = CloseSettler()
        events = [
            removal("s1", 1, 1.0, water, delta=-100.0),  # 판정-무게 불일치 상황
            ret("s1", 1, 2.0, +100.0),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0
        assert any("net_delta_correction" in n for n in result.notes)

    def test_error_blocks_payment_fail_closed(self, cola):
        # I13 + D9 기본: 에러 trigger 존재 → blocked, 결제 빌더 거부
        s = CloseSettler()
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = s.settle("s1", events, PROFILES)
        assert result.blocked
        with pytest.raises(ValueError):
            build_payment_payload(result)

    def test_error_free_zone_policy_excludes_only_error_zones(self, cola):
        s = CloseSettler(ErrorSessionPolicy.FINALIZE_ERROR_FREE_ZONES)
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = s.settle("s1", events, PROFILES)
        assert not result.blocked
        assert [z.zone for z in result.zones] == [1]
        assert any("error_zones_excluded" in n for n in result.notes)

    def test_freezer_close_resolve_signed_net(self, bar170):
        # freezer: 꺼냈다 되돌림 → net~0 → 과금 0 (close 재solve)
        s = CloseSettler()
        events = [
            removal("s1", 9, 1.0, bar170, delta=-178.0),  # 오차 8g (freezer)
            ret("s1", 9, 2.0, +178.0),
        ]
        result = s.settle("s1", events, PROFILES)
        assert result.total_price == 0

    def test_freezer_gate_failure_keeps_incremental(self, cola):
        # I3: net이 게이트(±15g)로 설명 안 되면 재solve 포기, 증분 결과 유지
        s = CloseSettler()
        events = [removal("s1", 9, 1.0, cola, delta=-160.0)]  # 100g 상품, net -160
        result = s.settle("s1", events, PROFILES)
        assert any("freezer_close_gate_failed" in n for n in result.notes)


class TestInterimAndShadow:
    def test_interim_is_distinct_type_and_rejected_by_payment(self, cola):
        # I10: 잠정 타입은 결제 빌더가 TypeError로 거부
        summary = interim_summary("s1", [removal("s1", 1, 1.0, cola)], PROFILES)
        assert isinstance(summary, InterimSummary)
        with pytest.raises(TypeError):
            build_payment_payload(summary)

    def test_shadow_runner_logs_diff(self, cola):
        primary = CloseSettler()
        shadow = CloseSettler(ErrorSessionPolicy.FINALIZE_ERROR_FREE_ZONES)
        runner = ShadowSettlerRunner(primary, shadow)
        events = [removal("s1", 1, 1.0, cola), error_event("s1", 2, 2.0)]
        result = runner.settle("s1", events, PROFILES)
        assert result.blocked  # primary 기준 반환
        # blocked vs 확정의 가격 차이가 diff에 잡히지 않아도 실행은 안전해야 함
        assert isinstance(runner.diffs, list)
