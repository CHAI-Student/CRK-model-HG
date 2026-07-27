"""ledger: 배리어(I17), 정산기(I11·I13·I14), 교차존, freezer close, I10."""
import pytest
from conftest import cand

from crk_model.core.policy import ErrorSessionPolicy
from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
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

    def test_freezer_close_resolve_gate_scales_with_count(self, cola):
        # 설계 3a (issue #16): close 재solve의 I3 게이트도 개수 비례 —
        # 100g×5, net −533(개당 편차 누적 33g)은 flat ±15로는 실패하지만
        # gate_n(5)=15+5×4=35로 5개 확정된다 (판정층 gate_n과 동일 산식).
        s = CloseSettler()
        events = [removal("s1", 9, 1.0, cola, count=4, delta=-533.0)]
        result = s.settle("s1", events, PROFILES)
        assert any("freezer_close_resolve" in n for n in result.notes)
        assert result.total_price == 5 * cola.unit_price


class TestVisionComboResolve:
    """0723 이슈 #17: freezer close 재solve의 단일 종 ×N 스냅 vs 비전 조합 중재.

    실사고 7회 반복: z5에서 3(224g)+44(77.5g) 취출 → Δ가 44×4(310g)와 겹쳐
    무게 잔차만으로 44×4 스냅 (c3의 자격 8표 무시). 게이트 안 동률의 선택은
    vision — 자격 표를 받은 2종 조합을 우선한다."""

    P44 = ActiveProduct(
        "P44", "츄러스44", class_id=44, unit_weight=77.5, unit_price=1200, stock_qty=10
    )
    P3 = ActiveProduct(
        "P3", "만두3", class_id=3, unit_weight=224.0, unit_price=3500, stock_qty=10
    )
    P27 = ActiveProduct(
        "P27", "베이글27", class_id=27, unit_weight=156.0, unit_price=2000, stock_qty=10
    )
    P30 = ActiveProduct(
        "P30", "브리또30", class_id=30, unit_weight=100.0, unit_price=1800, stock_qty=10
    )

    @staticmethod
    def removal_with_cands(sid, zone, ts, product, count, delta, cands, conf=0.9):
        j = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(product, count),), conf, "strict"
        )
        return TriggerEvent(sid, zone, ts, delta, (), j, vision_candidates=tuple(cands))

    def settler(self, *products):
        return CloseSettler(active_products_provider=lambda: products)

    def test_combo_beats_multiple_snap(self):
        # 3+44 취출: 잔차는 44×4(0g) < 3+44(8.5g)지만 둘 다 게이트 안 —
        # c3의 자격 8표가 조합을 고른다 (ses-8-1784812080 재구성).
        s = self.settler(self.P44, self.P3)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 1, -310.0,
            [cand(44, conf=0.95, votes=14), cand(3, conf=0.53, votes=8)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 1, "P3": 1}
        assert any("freezer_close_resolve_combo:zone9" in n for n in result.notes)

    def test_combo_fires_when_trigger_already_inflated(self):
        # 11차 ses-1 재구성: freezer 트리거 판정의 count는 무게 산정(I12)이라
        # 판정이 이미 44×4로 부풀린 경우(증분==스냅) 독립 증거가 아니다 —
        # 초기 구현의 "count > 증분" 가드가 조합 탐색을 막아 44×4가 재발했다.
        # N≥2 스냅이면 증분과 무관하게 탐색해야 한다.
        s = self.settler(self.P44, self.P3)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 4, -305.0,
            [cand(44, conf=0.72, votes=46), cand(3, conf=0.89, votes=8)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 1, "P3": 1}
        assert any("freezer_close_resolve_combo:zone9" in n for n in result.notes)

    def test_combo_requires_vote_floor(self):
        # 2번째 클래스가 자격 표 미달(<3표)이면 조합 자체가 성립 안 함 —
        # 유령 스파이크로 진짜 ×N 스냅이 무너지지 않는다.
        s = self.settler(self.P44, self.P3)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 1, -310.0,
            [cand(44, conf=0.95, votes=14), cand(3, conf=0.4, votes=2)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 4}
        assert any("freezer_close_resolve:zone9:P44=4" in n for n in result.notes)

    def test_combo_rescues_gate_failure(self):
        # ses-9-1784800090 재구성: 27×3+30 취출 net −560, 단일 종 27×4는
        # 잔차 64로 게이트 실패(증분 27×3 유지가 기존 동작) — 조합 27×3+30×1
        # (잔차 8)이 구제한다.
        s = self.settler(self.P27, self.P30)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P27, 3, -560.0,
            [cand(27, conf=0.9, votes=30), cand(30, conf=0.8, votes=12)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P27": 3, "P30": 1}
        assert any("freezer_close_resolve_combo:zone9" in n for n in result.notes)

    def test_single_count_snap_untouched(self):
        # N=1 정상 스냅은 조합 탐색 자체를 하지 않는다 (기존 동작 보존).
        s = self.settler(self.P44, self.P3)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 1, -77.0,
            [cand(44, conf=0.95, votes=14), cand(3, conf=0.53, votes=8)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 1}
        assert not any("freezer_close_resolve_combo" in n for n in result.notes)

    # --- 12차 자격 강화 (실사고 4건: 콤보가 정답 판정을 뒤집던 반대 방향) ---

    P13 = ActiveProduct(
        "P13", "만두13", class_id=13, unit_weight=189.0, unit_price=2100, stock_qty=10
    )
    P24 = ActiveProduct(
        "P24", "핫도그24", class_id=24, unit_weight=165.0, unit_price=2200, stock_qty=10
    )
    P23 = ActiveProduct(
        "P23", "북엇국23", class_id=23, unit_weight=180.0, unit_price=2500, stock_qty=10
    )

    def test_combo_low_evidence_minor_keeps_snap(self):
        # 12차 ses-11 재구성: 13x2 취출(Δ-365), 스냅 잔차 13g로 게이트 안인데
        # 이동 중 오분류 플리커 c24(9표, conf .45)가 조합 13+24(잔차 11g)를
        # 만들어 정답 13x2를 쪼갰다. 실존 증거 하한(top 50% 또는 conf .8)이
        # c24를 자격에서 제외해 스냅이 유지되고, 억제 관측 note가 남는다.
        s = self.settler(self.P13, self.P24)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P13, 2, -365.0,
            [cand(13, conf=1.0, votes=44), cand(24, conf=0.45, votes=9)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P13": 2}
        assert any("freezer_close_resolve:zone9:P13=2" in n for n in result.notes)
        assert any(
            "freezer_combo_suppressed:zone9:" in n and "class24(low_evidence)" in n
            for n in result.notes
        )

    def test_combo_other_zone_backed_excluded(self):
        # 12차 ses-5 재구성: 양손 멀티존 동시 취출은 연장 병합된 같은 에피소드
        # 영상을 공유해 두 존의 후보 풀이 동일하다 — z8에서 취출한 23의 표가
        # z9 콤보 재료로, z9의 27이 z8 콤보 재료로 유입되어 양쪽 정답 스냅을
        # 모두 쪼갰다. "다른 존 무게 뒷받침이 이미 설명한 정체성"은 콤보
        # 자격에서 제외된다.
        profiles = {8: FREEZER, 9: FREEZER}
        p27 = ActiveProduct(
            "P27", "베이글27", class_id=27, unit_weight=178.0, unit_price=2000,
            stock_qty=10,
        )
        s = self.settler(self.P23, p27)
        shared_cands = [cand(23, conf=1.0, votes=21), cand(27, conf=1.0, votes=47)]
        e_z8 = self.removal_with_cands(
            "s1", 8, 1.0, self.P23, 2, -360.0, shared_cands
        )
        e_z9 = self.removal_with_cands(
            "s1", 9, 1.5, p27, 2, -356.0, shared_cands
        )
        result = s.settle("s1", [e_z8, e_z9], profiles)
        billed = {
            (z.zone, pc.product.product_id): pc.count
            for z in result.zones
            for pc in z.products
        }
        assert billed == {(8, "P23"): 2, (9, "P27"): 2}
        assert any(
            "freezer_combo_suppressed:zone8:" in n and "class27(other_zone_backed)" in n
            for n in result.notes
        )
        assert any(
            "freezer_combo_suppressed:zone9:" in n and "class23(other_zone_backed)" in n
            for n in result.notes
        )

    def test_combo_respects_judgment_rejection_of_vote_top(self):
        # 13차 ses-20 재구성: c13(옷 프린트 유령, 28표 conf .84)이 득표 1위지만
        # 판정층이 conf 중재로 기각하고 c23(9표 conf .96)x4를 과금(COMPLETE).
        # 득표 1위는 실존 하한(③)을 정의상 통과하므로, "판정이 보고도 기각한
        # 득표 1위는 콤보 재료 금지"(④)가 13x1+23x3(잔차 2g < 스냅 11g)의
        # 되살림을 막는다.
        p23 = ActiveProduct(
            "P23", "북엇국23", class_id=23, unit_weight=176.0, unit_price=2700,
            stock_qty=10,
        )
        s = self.settler(self.P13, p23)
        e = self.removal_with_cands(
            "s1", 9, 1.0, p23, 4, -715.0,
            [cand(13, conf=0.84, votes=28), cand(23, conf=0.96, votes=9)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P23": 4}
        assert any("freezer_close_resolve:zone9:P23=4" in n for n in result.notes)
        assert any(
            "freezer_combo_suppressed:zone9:" in n
            and "class13(rejected_by_judgment)" in n
            for n in result.notes
        )

    def test_combo_rejected_runnerup_excluded(self):
        # 14차 ses-1 재구성(④ 일반화): 판정이 c13(137표)·c30(48표)을 모두
        # 기각하고 c3(27표, conf 1.0)x4를 확정(arbitrated). ④가 득표 1위만
        # 막으면 차순위 c30(48표, conf .94 — ③ 실존 하한 통과)이 콤보
        # 3x3+30x3(잔차 13g)으로 스냅 3x4(잔차 4g)를 뒤집는다. 과금 클래스
        # (27표) 이상 득표한 기각 클래스는 전부 콤보 재료 금지.
        p3 = ActiveProduct(
            "P3", "만두3", class_id=3, unit_weight=229.0, unit_price=3500,
            stock_qty=10,
        )
        p30 = ActiveProduct(
            "P30", "요맘때30", class_id=30, unit_weight=82.0, unit_price=1500,
            stock_qty=10,
        )
        s = self.settler(self.P13, p3, p30)
        e = self.removal_with_cands(
            "s1", 9, 1.0, p3, 4, -920.0,
            [
                cand(13, conf=0.89, votes=137),
                cand(30, conf=0.94, votes=48),
                cand(3, conf=1.0, votes=27),
            ],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P3": 4}
        assert any("freezer_close_resolve:zone9:P3=4" in n for n in result.notes)
        assert any(
            "freezer_combo_suppressed:zone9:" in n
            and "class13(rejected_by_judgment)" in n
            and "class30(rejected_by_judgment)" in n
            for n in result.notes
        )

    def test_combo_cannot_override_confident_snap(self):
        # 14차 ses-2 재구성(⑤): 판정이 conf 1.0으로 13x4를 확정(우도 일치),
        # 스냅 잔차 13.5g로 게이트 안. c24(18표, conf .84)가 실존 하한의
        # conf 분기를 통과해 조합 13x3+24x1(잔차 10.5g)이 성립하지만,
        # 확신(≥0.95) 스냅은 콤보가 뒤집을 수 없다 — 콤보는 판정이 자신
        # 없는 앨리어싱 스냅(보호 케이스 conf 0.9)과 게이트 실패 구제 전용.
        s = self.settler(self.P13, self.P24)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P13, 4, -742.5,
            [cand(13, conf=1.0, votes=74), cand(24, conf=0.84, votes=18)],
            conf=1.0,
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P13": 4}
        assert any("freezer_close_resolve:zone9:P13=4" in n for n in result.notes)
        assert any(
            "freezer_combo_rejected_confident_snap:zone9:" in n and "conf=1.00" in n
            for n in result.notes
        )

    def test_combo_vote_top_billed_stays_eligible(self):
        # ④의 반대 방향 보존: 판정이 득표 1위(44)를 과금한 경우(보호 케이스
        # 시그니처)는 제외가 걸리지 않아 조합 구제가 그대로 동작한다 —
        # test_combo_beats_multiple_snap과 동일 데이터로 ④ 활성 하에 재확인.
        s = self.settler(self.P44, self.P3)
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 1, -310.0,
            [cand(44, conf=0.95, votes=14), cand(3, conf=0.53, votes=8)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 1, "P3": 1}

    def test_combo_ghost_class_excluded(self):
        # 세션 유령(ghost_ledger 검출: ≥2존·≥2에피소드 자격 표 + 무게 뒷받침
        # 없음)은 콤보 자격에서 제외된다 — shadow 모드여도 검출은 순수 관측이라
        # 자격 판단에 사용한다. 실패 방향은 콤보 미형성 = 비전 판정 유지.
        profiles = {8: FREEZER, 9: FREEZER}
        p27 = ActiveProduct(
            "P27", "베이글27", class_id=27, unit_weight=178.0, unit_price=2000,
            stock_qty=10,
        )
        s = self.settler(self.P3, self.P44, p27)
        # c44가 두 존·두 에피소드에서 자격 표(conf .9 — 실존 하한은 통과)를
        # 얻지만 어느 존에서도 무게 뒷받침 과금이 없음 → ghost.
        e_z8 = self.removal_with_cands(
            "s1", 8, 1.0, p27, 2, -356.0,
            [cand(27, conf=1.0, votes=30), cand(44, conf=0.9, votes=8)],
        )
        e_z9 = self.removal_with_cands(
            "s1", 9, 5.0, self.P3, 2, -448.0,
            [cand(3, conf=1.0, votes=40), cand(44, conf=0.9, votes=8)],
        )
        result = s.settle("s1", [e_z8, e_z9], profiles)
        billed = {
            (z.zone, pc.product.product_id): pc.count
            for z in result.zones
            for pc in z.products
        }
        # ghost 제외가 없으면 z9는 조합 3x1+44x3(잔차 8.5)이 스냅 3x2(잔차 0)를
        # 뒤집는다.
        assert billed == {(8, "P27"): 2, (9, "P3"): 2}
        assert any(
            "freezer_combo_suppressed:zone9:" in n and "class44(ghost)" in n
            for n in result.notes
        )

    def test_kill_switch(self):
        s = CloseSettler(
            active_products_provider=lambda: (self.P44, self.P3), vision_combo=False
        )
        e = self.removal_with_cands(
            "s1", 9, 1.0, self.P44, 1, -310.0,
            [cand(44, conf=0.95, votes=14), cand(3, conf=0.53, votes=8)],
        )
        result = s.settle("s1", [e], PROFILES)
        billed = {pc.product.product_id: pc.count for z in result.zones for pc in z.products}
        assert billed == {"P44": 4}


class TestInterim:
    def test_interim_is_distinct_type_and_rejected_by_payment(self, cola):
        # I10: 잠정 타입은 결제 빌더가 TypeError로 거부
        summary = interim_summary("s1", [removal("s1", 1, 1.0, cola)], PROFILES)
        assert isinstance(summary, InterimSummary)
        with pytest.raises(TypeError):
            build_payment_payload(summary)
