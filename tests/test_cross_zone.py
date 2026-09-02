"""교차존 비전 오염 페널티 (docs/devdoc/design/cross_zone_penalty.md) — CLOSE 2차 패스.

시나리오 (§1): zone1에서 A 취출 → 세션 유지 중 zone2에서 B 취출 → zone1
연장창 내 A 재취출. zone2 AVI 프리롤/라이브에 A 장면이 섞여 A가 vision
후보로 진입, B가 A로 오판 → CLOSE에서 soft 페널티로 보정.
"""
import pytest
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.ledger import CloseSettler, CrossZonePenaltyConfig, TriggerEvent
from crk_model.ledger.cross_zone import (
    apply_cross_zone_penalty,
    contamination_window,
    sub_event_anchors,
)
from crk_model.ledger.journal import event_from_dict, event_to_dict

PROFILES = {1: FREEZER, 2: FREEZER}
CFG = CrossZonePenaltyConfig(enabled=True)


def judged(product, count=1, conf=0.9, status=JudgmentStatus.COMPLETE):
    return JudgmentResult(status, (ProductCount(product, count),), conf, "strict")


def event(sid, zone, ts, judgment, delta, candidates=(), change_ts=()):
    return TriggerEvent(
        sid, zone, ts, delta, (), judgment,
        vision_candidates=tuple(candidates),
        change_timestamps=tuple(change_ts),
    )


class TestAnchors:
    def test_change_timestamps_first(self, cola):
        e = event("s", 1, 5.0, judged(cola), -100.0, change_ts=(10.0, 12.5))
        assert sub_event_anchors(e) == (10.0, 12.5)

    def test_segments_fallback(self, cola):
        from crk_model.core.types import WeightSegment

        e = TriggerEvent(
            "s", 1, 5.0, -100.0,
            (WeightSegment(7.0, 7.5, -50.0), WeightSegment(9.0, 9.5, -50.0)),
            judged(cola),
        )
        assert sub_event_anchors(e) == (7.0, 9.0)

    def test_ts_last_resort(self, cola):
        e = event("s", 1, 5.0, judged(cola), -100.0)
        assert sub_event_anchors(e) == (5.0,)

    def test_window_is_conservative(self, cola):
        # W(E) = [min−4−1.0, max+4+1.0] (§4.2 ②, trigger_s는 CAMERA 포스트롤
        # 4.0s와 단일 소스 — CRK-CAMERA 7c8395f. ε=1.0은 0.8s 폴링 전환에
        # 따른 재산정값)
        e = event("s", 1, 0.0, judged(cola), -100.0, change_ts=(100.0, 102.5))
        lo, hi = contamination_window(e, CFG)
        assert lo == pytest.approx(95.0)
        assert hi == pytest.approx(107.5)


class TestCrossZonePenalty:
    def zone_events(self, bar170, bar178):
        """§1.1 타임라인: zone1 A(178g)#1 t0=100.0, #2 t2=102.5 (병합 1건),
        zone2 B(170g) t1=101.5 — zone2 후보에 오염된 A가 다수표로 진입해
        A로 오판된 상태. freezer count_gate=15라 170/178은 무게로 못 가른다."""
        z1 = event(
            "s", 1, 100.0, judged(bar178, 2), -356.0,
            candidates=[cand(4, conf=0.9, votes=50)],
            change_ts=(100.0, 102.5),
        )
        z2 = event(
            "s", 2, 101.5,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(bar178, 1),), 0.85, "strict"
            ),
            -170.0,
            candidates=[cand(4, conf=0.85, votes=40), cand(3, conf=0.7, votes=30)],
            change_ts=(101.5,),
        )
        return z1, z2

    def test_doc_scenario_rejudges_zone2(self, bar170, bar178):
        z1, z2 = self.zone_events(bar170, bar178)
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (bar170, bar178), CFG, notes
        )
        # zone1은 그대로, zone2는 B(bar170)로 보정
        assert out[0] is z1
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P170", 1)
        ]
        assert "cross_zone_vision_penalty" in out[1].judgment.reason
        assert any("zone2:cross_zone_vision_penalty:demoted=P178" in n for n in notes)
        assert any("source=zone1@" in n for n in notes)

    def test_settler_integration(self, bar170, bar178):
        z1, z2 = self.zone_events(bar170, bar178)
        s = CloseSettler(
            default_profile=FREEZER,
            cross_zone=CFG,
            active_products_provider=lambda: (bar170, bar178),
        )
        result = s.settle("s", [z1, z2], PROFILES)
        by_zone = {z.zone: z for z in result.zones}
        assert [(pc.product.product_id, pc.count) for pc in by_zone[2].products] == [
            ("P170", 1)
        ]
        assert by_zone[1].products[0].product.product_id == "P178"
        assert any("cross_zone_vision_penalty" in n for n in result.notes)

    def test_ses_36_1788332797_keeps_class76_after_class69_return(self):
        """2026-09-02 freezer field session: simultaneous 69/76 pickup, 69 return."""
        bar69 = ActiveProduct(
            "P17854812077466827", "FROZEN_STICK_HAGENDAZS", class_id=69,
            unit_weight=95.0, unit_price=5000, stock_qty=10,
        )
        cone76 = ActiveProduct(
            "P17355177269742847", "BOX_LOTTE_WORLDCON_160ML", class_id=76,
            unit_weight=100.0, unit_price=1400, stock_qty=10,
        )
        candidates = [
            cand(77, conf=0.7063987255096436, votes=53, ratio=0.18213058419243985),
            cand(69, conf=1.0, votes=41, ratio=0.140893470790378),
            cand(76, conf=1.0, votes=15, ratio=0.05154639175257732),
        ]
        removal69 = event(
            "ses-36-1788332797", 2, 1788332798.222933,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(bar69, 1),), 1.0,
                "freezer_vision_first_single",
            ),
            -85.0, candidates=candidates, change_ts=(1788332801.399152,),
        )
        removal76 = event(
            "ses-36-1788332797", 4, 1788332798.222933,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(bar69, 1),), 1.0,
                "freezer_vision_first_single",
            ),
            -100.0, candidates=candidates, change_ts=(1788332801.399152,),
        )
        returned69 = event(
            "ses-36-1788332797", 2, 1788332806.173655,
            JudgmentResult(JudgmentStatus.NO_DETECTION, reason="forced_final_no_match"),
            85.0, candidates=[
                cand(77, conf=0.7169692039489747, votes=83, ratio=0.3192307692307692),
                cand(69, conf=1.0, votes=9, ratio=0.03461538461538462),
            ], change_ts=(1788332809.404019,),
        )

        result = CloseSettler(
            default_profile=FREEZER,
            cross_zone=CFG,
            active_products_provider=lambda: (bar69, cone76),
        ).settle(
            "ses-36-1788332797", [removal69, removal76, returned69],
            {2: FREEZER, 4: FREEZER},
        )

        by_zone = {zone.zone: zone for zone in result.zones}
        assert by_zone[2].products == ()
        assert [(pc.product.product_id, pc.count) for pc in by_zone[4].products] == [
            ("P17355177269742847", 1)
        ]
        assert result.total_price == 1400
        assert "zone2:cross_zone_mutual_exempt:class69" in result.notes
        assert any(
            note.startswith(
                "zone4:cross_zone_vision_penalty:demoted=P17854812077466827:"
                "adopted=P17355177269742847x1:source=zone2@1788332801.399"
            )
            for note in result.notes
        )

    def test_retrieves_competing_refrigerator_item_with_product_variance(self):
        bag = ActiveProduct("P43", "꽃게랑", class_id=43, unit_weight=79.0,
                            unit_price=500, stock_qty=20)
        porridge = ActiveProduct("P53", "소고기죽", class_id=53, unit_weight=312.0,
                                 unit_price=2000, stock_qty=20)
        z1 = event(
            "s", 1, 100.0,
            judged(bag, conf=0.88), -80.0,
            candidates=[cand(43, conf=1.0, votes=35), cand(53, conf=0.58, votes=22)],
            change_ts=(100.0,),
        )
        z2 = event(
            "s", 2, 101.0,
            judged(bag, count=4, conf=0.37), -325.0,
            candidates=[cand(43, conf=1.0, votes=69), cand(53, conf=1.0, votes=59)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], {1: REFRIGERATOR, 2: REFRIGERATOR},
            (bag, porridge), CFG, notes,
        )
        assert out[1].judgment.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in out[1].judgment.products] == [(53, 1)]
        assert any("cross_zone_robust_alternative" in note for note in notes)

    def test_retrieves_partial_relaxed_trigger_from_latest_log(self):
        bag = ActiveProduct("P43", "꽃게랑", class_id=43, unit_weight=79.0,
                            unit_price=500, stock_qty=20)
        porridge = ActiveProduct("P53", "소고기죽", class_id=53, unit_weight=312.0,
                                 unit_price=2000, stock_qty=20)
        z1 = event(
            "s", 1, 100.0, judged(bag, conf=0.52), -75.0,
            candidates=[cand(43, conf=1.0, votes=24), cand(53, conf=0.53, votes=7)],
            change_ts=(100.0,),
        )
        z2 = event(
            "s", 2, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bag, 4),), 0.2557,
                "relaxed_combination+full_delta_unexplained", "relaxed",
            ),
            -325.0,
            candidates=[cand(43, conf=0.532, votes=9), cand(53, conf=1.0, votes=7)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], {1: REFRIGERATOR, 2: REFRIGERATOR},
            (bag, porridge), CFG, notes,
        )
        assert out[1].judgment.status is JudgmentStatus.PARTIAL
        assert [(pc.product.class_id, pc.count) for pc in out[1].judgment.products] == [(53, 1)]

    def test_mutual_demotion_guard_keeps_better_residual_zone(self, bar170, bar178):
        # 8차 ses-3 실사고: 동시 멀티존 취출이 영상을 공유해 두 존 모두 X를
        # 판정 → 서로를 소스로 X를 강등 → X가 정산에서 통째로 소멸 (잔차 1로
        # 맞던 존까지 오답). 가드: 잔차가 정확한 존은 X의 진짜 소스로 보고
        # 페널티 면제, 잔차가 나쁜 존만 재판정된다.
        za = event(
            "s", 1, 100.0, judged(bar178), -178.0,  # 잔차 0 — 진짜 소스
            candidates=[cand(4, conf=0.9, votes=50), cand(3, conf=0.7, votes=30)],
            change_ts=(100.0,),
        )
        zb = event(
            "s", 2, 101.5, judged(bar178, conf=0.85), -170.0,  # 잔차 8 — 오염
            candidates=[cand(4, conf=0.85, votes=40), cand(3, conf=0.7, votes=30)],
            change_ts=(101.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [za, zb], PROFILES, (bar170, bar178), CFG, notes
        )
        # 잔차 0인 zone1은 X(bar178) 유지 — 상호 강등이었다면 둘 다 P170
        assert out[0] is za
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P170", 1)
        ]
        assert any("zone1:cross_zone_mutual_exempt:class4" in n for n in notes)

    def test_ses8_mutual_topology_field_fixture(self):
        # 9차 ses-8 실기 재구성 (GT z1:40, z2:46 — 동시 취출, 유사 복장으로
        # 13이 vision top 오염, 전 후보 3~9표 저득표·저conf): 가드는 잔차가
        # 정확한 z2(46, 잔차 1)를 면제하고, z1 재판정은 오염 top 13에 막혀
        # gate 실패 → 원 판정 유지 + note. 과금은 어느 쪽이든 46×2로 같지만
        # (구제 경로 없음) 관측 note가 남아야 한다.
        p46 = ActiveProduct("P46", "46", class_id=46, unit_weight=71.0,
                            unit_price=1000, stock_qty=20)
        p40 = ActiveProduct("P40", "40", class_id=40, unit_weight=131.0,
                            unit_price=2000, stock_qty=20)
        p13 = ActiveProduct("P13c", "13", class_id=13, unit_weight=185.0,
                            unit_price=2100, stock_qty=20)
        cands = [
            cand(13, conf=0.75, votes=9), cand(46, conf=0.39, votes=8),
            cand(40, conf=0.31, votes=3),
        ]
        z2 = event(
            "s", 2, 1784805686.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(p46, 1),), 0.393,
                "freezer_vision_first_single",
            ),
            -70.0, candidates=cands, change_ts=(1784805686.629,),
        )
        z1 = event(
            "s", 1, 1784805688.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(p46, 2),), 0.393,
                "freezer_vision_first_single",
            ),
            -135.0, candidates=cands,
            change_ts=(1784805686.629, 1784805688.230),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z2, z1], PROFILES, (p46, p40, p13), CFG, notes
        )
        assert any("zone2:cross_zone_mutual_exempt:class46" in n for n in notes)
        assert any("zone1:cross_zone_penalty_gate_failed" in n for n in notes)
        assert out[0] is z2 and out[1] is z1  # 과금 무변경 — 관측만

    def test_self_fit_strips_wrong_claimant(self):
        # 10차 ses-1 실기 재구성 (GT z2:23, z3:27 — 동일측 동시 취출로 27이
        # 양존 vision top): 존 간 잔차 비교만으로는 노이즈 낀 z3보다 z2가
        # X=27의 면제를 받아 27이 양존 중복 과금된다. self-fit 자격 검사가
        # "z2의 delta는 자기 후보 23을 5g 이상 명확히 더 잘 설명한다"로
        # z2의 claimant 자격을 박탈 → 면제는 z3, z2는 재판정된다.
        p27 = ActiveProduct("P27", "27", class_id=27, unit_weight=160.0,
                            unit_price=1500, stock_qty=20)
        p23 = ActiveProduct("P23", "23", class_id=23, unit_weight=176.0,
                            unit_price=1500, stock_qty=20)
        p13 = ActiveProduct("P13", "13", class_id=13, unit_weight=189.0,
                            unit_price=2100, stock_qty=20)
        z2 = event(
            "s", 2, 100.0, judged(p27, conf=0.8), -172.5,  # 잔차 12.5 — 그러나 23이 3.5
            candidates=[cand(27, conf=1.0, votes=22), cand(23, conf=0.8, votes=18),
                        cand(13, conf=0.76, votes=9)],
            change_ts=(100.0,),
        )
        z3 = event(
            "s", 3, 101.0, judged(p27, conf=0.9), -145.0,  # 잔차 15 (freezer 노이즈)
            candidates=[cand(27, conf=0.9, votes=30), cand(23, conf=0.3, votes=5)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z2, z3], {2: FREEZER, 3: FREEZER}, (p27, p23, p13), CFG, notes
        )
        # 잔차만 보면 z2(12.5) < z3(15)로 z2가 면제됐을 상황 — self-fit이 뒤집는다
        assert any("zone3:cross_zone_mutual_exempt:class27" in n for n in notes)
        assert out[1] is z3  # 진짜 소스 존은 원 판정 유지
        assert [(pc.product.product_id, pc.count) for pc in out[0].judgment.products] == [
            ("P23", 1)
        ]

    def test_mutual_demotion_tie_keeps_both(self, bar170, bar178):
        # 잔차 동률이면 무게가 판별하지 못하는 것 — 양쪽 다 면제(원 판정
        # 유지), ④ 무게 모호성 게이트와 같은 "개입하지 않는" 방향.
        za = event(
            "s", 1, 100.0, judged(bar178), -178.0,
            candidates=[cand(4, conf=0.9, votes=50), cand(3, conf=0.7, votes=30)],
            change_ts=(100.0,),
        )
        zb = event(
            "s", 2, 101.5, judged(bar178, conf=0.85), -178.0,
            candidates=[cand(4, conf=0.85, votes=40), cand(3, conf=0.7, votes=30)],
            change_ts=(101.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [za, zb], PROFILES, (bar170, bar178), CFG, notes
        )
        assert out[0] is za and out[1] is zb  # 둘 다 원 판정 유지

    def test_disabled_is_noop(self, bar170, bar178):
        z1, z2 = self.zone_events(bar170, bar178)
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (bar170, bar178),
            CrossZonePenaltyConfig(enabled=False), notes,
        )
        assert out == [z1, z2] and not notes

    def test_no_overlap_is_noop(self, bar170, bar178):
        z1, z2 = self.zone_events(bar170, bar178)
        z1_far = event(
            "s", 1, 200.0, judged(bar178, 2), -356.0,
            candidates=[cand(4)], change_ts=(200.0, 202.5),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1_far, z2], PROFILES, (bar170, bar178), CFG, notes
        )
        assert out[1] is z2 and not notes

    def test_weight_unambiguous_keeps_original(self, cola, bar178):
        # ④ 무게 모호성 게이트: cola(100g)만 delta를 설명 — 페널티 미발동.
        # bar178은 오염 창에서 왔지만 |100−178|=78 > count_gate(15).
        z1 = event(
            "s", 1, 100.0, judged(bar178, 2), -356.0,
            candidates=[cand(4)], change_ts=(100.0, 102.5),
        )
        z2 = event(
            "s", 2, 101.5, judged(cola), -100.0,
            candidates=[cand(4, votes=40), cand(1, votes=30)],
            change_ts=(101.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (cola, bar178), CFG, notes
        )
        assert out[1] is z2 and not notes

    def test_low_confidence_source_excluded(self, bar170, bar178):
        # ③ 소스 신뢰도 게이트 (R1): confidence < θ 소스는 오판 전파 차단.
        # 9차 ses-8 후속: 완전 침묵이던 이 경로가 "창은 겹쳤는데 conf 탈락"
        # 진단 note를 남긴다 — 동작(재판정 없음)은 그대로.
        z1, z2 = self.zone_events(bar170, bar178)
        z1_low = event(
            "s", 1, 100.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar178, 1),), 0.2, "relaxed"
            ),
            -178.0,
            candidates=[cand(4)],
            change_ts=(100.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1_low, z2], PROFILES, (bar170, bar178), CFG, notes
        )
        assert out[1] is z2  # 재판정 없음 — 게이트 동작 유지
        assert any("zone2:cross_zone_source_low_conf:zone1@0.20" in n for n in notes)

    def test_rejudge_gate_failure_keeps_original(self, bar170, bar178):
        # ⑥ 게이트 (R2): 재판정이 COMPLETE가 아니면 원 판정 유지 + 사유 기록
        class StubRouter:
            def judge(self, ctx):
                return JudgmentResult(JudgmentStatus.NO_DETECTION, reason="stub")

        z1, z2 = self.zone_events(bar170, bar178)
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (bar170, bar178), CFG, notes, router=StubRouter()
        )
        assert out[1] is z2
        assert any("cross_zone_penalty_gate_failed:keep_original" in n for n in notes)

    def test_rejudge_gate_failure_suppresses_contaminated_partial(self, bar178):
        """이슈 #27 ses-43/44: 이웃 존의 확정 상품이 PARTIAL 과금 상품을
        오염시켰고 재판정도 COMPLETE가 아니면, 이미 무게 미검증인 원 과금을
        보호하지 않고 오염 상품만 제거한다."""
        class StubRouter:
            def judge(self, ctx):
                return JudgmentResult(JudgmentStatus.NO_DETECTION, reason="stub")

        source = event(
            "s", 4, 100.0, judged(bar178), -178.0,
            candidates=[cand(4, conf=0.95, votes=20)], change_ts=(100.0,),
        )
        contaminated = event(
            "s", 3, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar178, 1),), 0.55,
                "freezer_vision_first_near_gate",
            ),
            -140.0, candidates=[cand(4, conf=0.91, votes=7)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [source, contaminated], {3: FREEZER, 4: FREEZER}, (bar178,), CFG, notes,
            router=StubRouter(),
        )
        assert out[0] is source
        assert out[1].judgment.status is JudgmentStatus.NO_DETECTION
        assert not out[1].judgment.products
        assert out[1].judgment.reason == "cross_zone_contaminated_partial_suppressed"
        assert any(
            "cross_zone_penalty_gate_failed:suppress_contaminated_partial=P178"
            in n for n in notes
        )

    def test_penalized_partial_winner_still_wins(self, bar178):
        """PARTIAL 성공 경로 보존: soft 페널티 후에도 같은 상품이 다시
        선택되면 인접 존의 실제 동일상품 취출일 수 있으므로 원 과금을 유지한다."""
        source = event(
            "s", 4, 100.0, judged(bar178), -178.0,
            candidates=[cand(4, conf=0.95, votes=20)], change_ts=(100.0,),
        )
        target = event(
            "s", 3, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar178, 1),), 0.55,
                "freezer_vision_first_near_gate",
            ),
            -140.0, candidates=[cand(4, conf=0.91, votes=7)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [source, target], {3: FREEZER, 4: FREEZER}, (bar178,), CFG, notes
        )
        assert out[1] is target
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P178", 1)
        ]
        assert any("cross_zone_penalty_gate_failed:keep_original" in n for n in notes)

    def test_session42_three_zone_bleed_suppresses_both_victims(self, bar178):
        """session42 실사고: 소바바치킨(bar178 대역)이 zone1(진짜)뿐 아니라
        FOV 유출로 zone2·zone3에도 유일 후보로 잡혀 3개 모두 과금됐다.
        test_penalized_partial_winner_still_wins(2-zone)와 달리 피해 zone이
        2곳 이상이면 "인접 존도 진짜 팔았을 수 있다"는 가정이 깨져 강제
        억제된다 — 소스 zone1만 유지."""
        source = event(
            "s", 1, 100.0, judged(bar178, conf=1.0), -178.0,
            candidates=[cand(4, conf=1.0, votes=122)], change_ts=(100.0,),
        )
        victim2 = event(
            "s", 2, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar178, 1),), 0.3,
                "vision_first_identity_partial",
            ),
            -90.0, candidates=[cand(4, conf=1.0, votes=122)], change_ts=(101.0,),
        )
        victim3 = event(
            "s", 3, 102.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar178, 1),), 0.3,
                "vision_first_identity_partial",
            ),
            -105.0, candidates=[cand(4, conf=1.0, votes=122)], change_ts=(102.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [source, victim2, victim3], {1: FREEZER, 2: FREEZER, 3: FREEZER},
            (bar178,), CFG, notes,
        )
        by_zone = {e.zone: e for e in out}
        assert [(pc.product.product_id, pc.count) for pc in by_zone[1].judgment.products] == [
            ("P178", 1)
        ]
        assert not by_zone[2].judgment.products
        assert not by_zone[3].judgment.products
        assert any(
            "zone2:cross_zone_penalty_gate_failed:suppress_contaminated_partial=P178"
            in n for n in notes
        )
        assert any(
            "zone3:cross_zone_penalty_gate_failed:suppress_contaminated_partial=P178"
            in n for n in notes
        )

    def test_contamination_unrelated_to_billed_products_keeps_multi_tray_result(self):
        """0826 2차 재테스트(ses-3 zone2 재구성): zone2가 68x1+70x1로 두 상품을
        정확히 나눠 잡았는데(multi_tray), 오염 후보가 실제로는 그중 어느
        것도 아닌 class75(옆 zone에서 진짜 판매)인 경우. 예전에는 재판정이
        zone 전체 무게를 통째로 다시 계산해 68이 사라지고 70x2로 뭉개졌다 —
        오염 클래스가 과금 상품과 무관하면 재판정 자체를 건너뛴다."""
        p68 = ActiveProduct(
            "P68", "하겐다즈68", class_id=68, unit_weight=95.0, unit_price=5000,
            stock_qty=10,
        )
        p70 = ActiveProduct(
            "P70", "브라보콘70", class_id=70, unit_weight=105.0, unit_price=2000,
            stock_qty=10,
        )
        p75 = ActiveProduct(
            "P75", "월드콘75", class_id=75, unit_weight=70.0, unit_price=1400,
            stock_qty=10,
        )
        source = event(
            "s", 4, 100.0, judged(p75, conf=0.98), -70.0,
            candidates=[cand(75, conf=0.98, votes=8)], change_ts=(100.0,),
        )
        target = event(
            "s", 2, 101.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE,
                (ProductCount(p68, 1), ProductCount(p70, 1)), 0.91,
                "multi_tray[freezer_vision_first,freezer_vision_first]",
            ),
            -193.75,
            candidates=[
                cand(70, conf=1.0, votes=35), cand(71, conf=0.58, votes=26),
                cand(68, conf=0.91, votes=18), cand(75, conf=0.98, votes=8),
            ],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [source, target], {2: FREEZER, 4: FREEZER}, (p68, p70, p75), CFG, notes,
        )
        assert out[1] is target
        assert sorted(
            (pc.product.product_id, pc.count) for pc in out[1].judgment.products
        ) == [("P68", 1), ("P70", 1)]

    def test_issue27_partial_contamination_is_not_billed_at_settlement(self):
        """ses-43/44 형태 통합 회귀: c75 소스가 있는 상태에서 zone3의 c75
        PARTIAL이 페널티 후 다른 PARTIAL로 바뀌면 원 c75 과금은 정산에서 빠진다."""
        p75 = ActiveProduct("P75", "월드콘", 75, 70.0, 1400, 20)
        p65 = ActiveProduct("P65", "오염대안", 65, 200.0, 1000, 20)
        p69 = ActiveProduct("P69", "제로바", 69, 70.0, 2500, 20)
        source = event(
            "s", 4, 100.0, judged(p75), -70.0,
            candidates=[cand(75, conf=0.95, votes=20)], change_ts=(100.0,),
        )
        contaminated = event(
            "s", 3, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(p75, 2),), 0.55,
                "freezer_vision_first_near_gate",
            ),
            -110.0,
            candidates=[
                cand(75, conf=0.914, votes=7),
                cand(65, conf=0.797, votes=6),
                cand(69, conf=0.147, votes=5),
            ],
            change_ts=(101.0,),
        )
        settler = CloseSettler(
            default_profile=FREEZER,
            cross_zone=CFG,
            active_products_provider=lambda: (p75, p65, p69),
        )
        result = settler.settle(
            "s", [source, contaminated], {3: FREEZER, 4: FREEZER}
        )
        by_zone = {z.zone: z for z in result.zones}
        assert not by_zone[3].products
        assert [(pc.product.product_id, pc.count) for pc in by_zone[4].products] == [
            ("P75", 1)
        ]
        assert any(
            "zone3:cross_zone_penalty_gate_failed:"
            "suppress_contaminated_partial=P75" in n
            for n in result.notes
        )

    def test_rejudge_gate_failure_keeps_uncontaminated_partial(self, bar170, bar178):
        """PARTIAL 전역 차단 방지: 페널티 클래스가 후보에만 있고 실제 과금
        상품과 다르면 기존 PARTIAL은 유지한다."""
        class StubRouter:
            def judge(self, ctx):
                return JudgmentResult(JudgmentStatus.NO_DETECTION, reason="stub")

        source = event(
            "s", 1, 100.0, judged(bar178), -178.0,
            candidates=[cand(4, conf=0.95, votes=20)], change_ts=(100.0,),
        )
        target = event(
            "s", 2, 101.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(bar170, 1),), 0.5,
                "freezer_vision_first_near_gate",
            ),
            -170.0,
            candidates=[cand(4, conf=0.9, votes=10), cand(3, conf=0.8, votes=8)],
            change_ts=(101.0,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [source, target], PROFILES, (bar170, bar178), CFG, notes,
            router=StubRouter(),
        )
        assert out[1] is target
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P170", 1)
        ]
        assert any("cross_zone_penalty_gate_failed:keep_original" in n for n in notes)

    def test_penalized_winner_still_wins(self, bar170, bar178):
        # ⑤ soft 페널티: 페널티 후에도 오염 후보가 이기면 그대로 인정
        # (인접 존이 실제로 같은 상품을 파는 배치 — P(E) 상품이 진짜 정답).
        z1 = event(
            "s", 1, 100.0, judged(bar178, 2), -356.0,
            candidates=[cand(4, conf=0.9, votes=50)],
            change_ts=(100.0, 102.5),
        )
        # zone2도 실제로 A(178g) 취출: delta=-178, A가 압도적 표
        z2 = event(
            "s", 2, 101.5, judged(bar178), -178.0,
            candidates=[cand(4, conf=0.9, votes=90), cand(3, conf=0.3, votes=5)],
            change_ts=(101.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (bar170, bar178), CFG, notes
        )
        # 판정 상품 불변 (교체 note 없음)
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P178", 1)
        ]
        assert not any("cross_zone_vision_penalty" in n for n in notes)

    def test_refrigerator_tight_tolerance_not_ambiguous(self, bar170, bar178):
        # 냉장 프로파일(±3g)에서는 170 vs 178이 무게로 갈린다 → 페널티 미발동
        z1 = event(
            "s", 1, 100.0, judged(bar178, 2), -356.0,
            candidates=[cand(4)], change_ts=(100.0, 102.5),
        )
        z2 = event(
            "s", 2, 101.5, judged(bar170), -170.0,
            candidates=[cand(4, votes=40), cand(3, votes=30)],
            change_ts=(101.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], {1: REFRIGERATOR, 2: REFRIGERATOR},
            (bar170, bar178), CFG, notes,
        )
        assert out[1] is z2 and not notes


class TestChangeTimestampsPersistence:
    def test_journal_roundtrip(self, cola):
        e = event("s", 1, 5.0, judged(cola), -100.0, change_ts=(10.0, 12.5))
        assert event_from_dict(event_to_dict(e)).change_timestamps == (10.0, 12.5)

    def test_journal_backward_compat(self, cola):
        d = event_to_dict(event("s", 1, 5.0, judged(cola), -100.0))
        d.pop("change_timestamps")  # 구버전 저널 라인
        assert event_from_dict(d).change_timestamps == ()


class TestIssue22PartialOriginalBypassesWeightGate:
    """이슈 #22 ses-4 z3 재구성: ④ KEEP의 전제("무게가 유일 해 → 기존 무게
    매칭이 이미 방어")는 원 판정이 COMPLETE일 때만 참이다. 무게 무검증
    relaxed_partial이 오염 후보(525g)를 Δ-80g에 과금한 경우 ④가 침묵
    KEEP하면 재판정 기회 자체가 사라진다 — PARTIAL 원 판정은 ④를 건너뛰고
    재판정한다 (⑥ COMPLETE 게이트는 그대로 방어)."""

    BARLEY = ActiveProduct(
        "P35", "보리차", class_id=35, unit_weight=525.0, unit_price=1800, stock_qty=5
    )
    TEA = ActiveProduct(
        "P28", "둥굴레차", class_id=28, unit_weight=80.0, unit_price=1500, stock_qty=5
    )

    def zone_events(self):
        # z5: 보리차(525g) 실취출 — PARTIAL이지만 conf 0.375 ≥ θ라 소스 자격.
        z5 = event(
            "s", 5, 100.0,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(self.BARLEY, 1),), 0.375,
                "relaxed_combination+full_delta_unexplained",
            ),
            -525.0, candidates=[cand(35, conf=0.83, votes=20)],
            change_ts=(100.0,),
        )
        # z3: Δ-80인데 오염 표로 c35가 득표 1위 → relaxed_partial이 35 과금
        # (conf 0.269 < θ라 z3은 상호 강등 가드의 valid에도 못 든다).
        z3 = event(
            "s", 3, 100.5,
            JudgmentResult(
                JudgmentStatus.PARTIAL, (ProductCount(self.BARLEY, 1),), 0.269,
                "relaxed_partial",
            ),
            -80.0,
            candidates=[cand(35, conf=0.54, votes=13), cand(28, conf=0.66, votes=5)],
            change_ts=(100.5,),
        )
        return z5, z3

    def test_partial_original_is_rejudged_to_weight_fit(self):
        z5, z3 = self.zone_events()
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z5, z3], {3: REFRIGERATOR, 5: REFRIGERATOR},
            (self.BARLEY, self.TEA), CFG, notes,
        )
        # 구 동작: ④가 "무게 유일 해(28만 80g 설명)"로 침묵 KEEP → 35x1 유지.
        # 수정 후: PARTIAL 원 판정은 재판정 — 페널티 먹은 35 대신 무게가
        # 뒷받침하는 28x1 COMPLETE 채택.
        assert [(pc.product.product_id, pc.count) for pc in out[1].judgment.products] == [
            ("P28", 1)
        ]
        assert out[1].judgment.reason.endswith("+cross_zone_vision_penalty")
        assert any(
            n.startswith("zone3:cross_zone_vision_penalty:demoted=P35:adopted=P28x1")
            for n in notes
        )

    def test_complete_original_still_kept_by_weight_gate(self):
        # ④ 보존 검증: 원 판정이 COMPLETE(무게 검증 통과)면 무게 유일 해
        # KEEP은 그대로다 — 재판정 안 함, 무기록.
        z5, z3 = self.zone_events()
        z3_complete = TriggerEvent(
            "s", 3, 100.5, -80.0, (),
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(self.TEA, 1),), 0.9, "strict"
            ),
            vision_candidates=z3.vision_candidates,
            change_timestamps=(100.5,),
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z5, z3_complete], {3: REFRIGERATOR, 5: REFRIGERATOR},
            (self.BARLEY, self.TEA), CFG, notes,
        )
        assert out[1] is z3_complete
        assert not any(n.startswith("zone3:cross_zone_vision_penalty") for n in notes)


class TestNoOverlapDiagnosis:
    """이슈 #23 0806 ses-28: 순차 취출 간격이 오염 창(앵커 ±5s)을 넘으면
    소스 겹침·상호 강등이 전부 불성립하는데 아카이브에 흔적이 없어
    "cross_zone이 안 돈다"로 보였다 — 근접(30s 이내) 무겹침을 노트로 남긴다.
    동작(판정)은 무변경."""

    def _pair(self, bar170, bar178, gap):
        z1 = event(
            "s", 1, 100.0, judged(bar178), -178.0,
            candidates=[cand(4, votes=20)], change_ts=(100.0,),
        )
        z2 = event(
            "s", 2, 100.0 + gap, judged(bar170), -170.0,
            candidates=[cand(3, votes=20), cand(4, votes=6)],
            change_ts=(100.0 + gap,),
        )
        return z1, z2

    def test_near_miss_gets_note_judgment_unchanged(self, bar170, bar178):
        z1, z2 = self._pair(bar170, bar178, gap=7.0)
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z1, z2], PROFILES, (bar170, bar178), CFG, notes
        )
        assert out[0] is z1 and out[1] is z2  # 판정 무변경
        assert any(n == "zone1:cross_zone_no_overlap:zone2@dt=7.0s" for n in notes)
        assert any(n == "zone2:cross_zone_no_overlap:zone1@dt=7.0s" for n in notes)

    def test_beyond_horizon_stays_silent(self, bar170, bar178):
        # 30s를 넘는 간격은 명백한 별개 에피소드 — 침묵이 정상
        z1, z2 = self._pair(bar170, bar178, gap=45.0)
        notes: list[str] = []
        apply_cross_zone_penalty([z1, z2], PROFILES, (bar170, bar178), CFG, notes)
        assert not notes

    def test_overlapping_source_takes_precedence(self, bar170, bar178):
        # 겹치는 소스가 있으면 기존 페널티 기제가 담당 — 무겹침 노트 없음
        z1, z2 = self._pair(bar170, bar178, gap=2.0)
        notes: list[str] = []
        apply_cross_zone_penalty([z1, z2], PROFILES, (bar170, bar178), CFG, notes)
        assert not any("cross_zone_no_overlap" in n for n in notes)


class TestFingerprintDuplicateSuppression:
    """2026-08-28 재테스트(ses-34/35, ses-36/37): 카메라 화각 겹침으로 같은
    클래스가 서로 다른 zone에서 개수·표·확신도까지 거의 완전히 동일하게
    청구되면, 대체 상품을 추정해 끼워넣지 않고 무게 잔차가 더 나쁜 쪽의
    중복 청구만 제거한다."""

    PROFILES_34 = {3: FREEZER, 4: FREEZER}

    def test_identical_fingerprint_across_zones_suppresses_worse_fit(
        self, hanmaek115, thirdclass75
    ):
        # ses-34/35: zone3·zone4 모두 71을 표36·확신도1.0으로 완전히 동일하게
        # 청구 — zone3(잔차0g)만 남기고 zone4(잔차15g)의 중복은 제거된다.
        z3 = event(
            "s", 3, 100.0, judged(hanmaek115, conf=1.0), -115.0,
            candidates=[cand(71, conf=1.0, votes=36), cand(75, conf=1.0, votes=11)],
        )
        z4 = event(
            "s", 4, 105.0, judged(hanmaek115, conf=1.0), -100.0,
            candidates=[cand(71, conf=1.0, votes=36), cand(75, conf=1.0, votes=11)],
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z3, z4], self.PROFILES_34, (hanmaek115, thirdclass75), CFG, notes
        )
        by_zone = {e.zone: e for e in out}
        assert [pc.product.class_id for pc in by_zone[3].judgment.products] == [71]
        assert by_zone[4].judgment.status is JudgmentStatus.NO_DETECTION
        assert any(
            "zone4:cross_zone_fingerprint_duplicate_suppressed:class71" in n
            for n in notes
        )

    def test_near_identical_confidence_with_vote_drift_still_suppresses(
        self, hagendaz95, bravocon105
    ):
        # ses-36/37: 표는 60 vs 58로 갈리지만(카메라별 프레임 수 차이) 확신도가
        # 소수점 13자리까지 동일 — 강한 유출 증거라 표 오차 10% 안에서 억제된다.
        z2 = event(
            "s", 2, 200.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE,
                (ProductCount(hagendaz95, 1), ProductCount(bravocon105, 1)),
                0.99, "multi_tray",
            ),
            -185.0,
            candidates=[
                cand(70, conf=0.9902377456426621, votes=60),
                cand(68, conf=0.9977415680885315, votes=57),
            ],
        )
        z4 = event(
            "s", 4, 205.0, judged(bravocon105, conf=0.99), -100.0,
            candidates=[
                cand(70, conf=0.9902377456426621, votes=58),
                cand(68, conf=0.9977415680885315, votes=51),
            ],
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z2, z4], self.PROFILES_34, (hagendaz95, bravocon105), CFG, notes
        )
        by_zone = {e.zone: e for e in out}
        assert [pc.product.class_id for pc in by_zone[2].judgment.products] == [68]
        assert [pc.product.class_id for pc in by_zone[4].judgment.products] == [70]

    def test_different_count_is_not_treated_as_duplicate(
        self, hanmaek115, thirdclass75
    ):
        # 개수가 다르면(1개 vs 2개) 지문이 같아도 손대지 않는다 — 서로 다른
        # 실제 판매였을 개연성을 배제할 수 없다.
        z3 = event(
            "s", 3, 100.0, judged(hanmaek115, count=1, conf=1.0), -115.0,
            candidates=[cand(71, conf=1.0, votes=36)],
        )
        z4 = event(
            "s", 4, 105.0, judged(hanmaek115, count=2, conf=1.0), -230.0,
            candidates=[cand(71, conf=1.0, votes=36)],
        )
        notes: list[str] = []
        out = apply_cross_zone_penalty(
            [z3, z4], self.PROFILES_34, (hanmaek115, thirdclass75), CFG, notes
        )
        assert not any("cross_zone_fingerprint_duplicate_suppressed" in n for n in notes)

    def test_duplicate_suppression_replaces_with_weight_fit_candidate(self):
        """중복 75 제거 뒤 zone3의 72(115g)를 COMPLETE로 복구한다."""
        yomamte = ActiveProduct("P17437536515731485", "요맘때", 75, 95.0, 1500, 10)
        candidate72 = ActiveProduct("P72", "class72", 72, 115.0, 1800, 10)
        candidates = [
            cand(77, conf=0.711431121826172, votes=37, ratio=0.13553113553113552),
            cand(75, conf=1.0, votes=15, ratio=0.054945054945054944),
            cand(72, conf=0.7813407719135284, votes=10, ratio=0.03663003663003663),
        ]
        zone3 = event(
            "ses-44-1788333361", 3, 100.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(yomamte, 1),), 1.0,
                "freezer_vision_first_single",
            ),
            -110.0, candidates=candidates,
        )
        zone4 = event(
            "ses-44-1788333361", 4, 200.0,
            JudgmentResult(
                JudgmentStatus.COMPLETE, (ProductCount(yomamte, 1),), 1.0,
                "freezer_vision_first_single",
            ),
            -100.0, candidates=candidates,
        )
        notes: list[str] = []

        out = apply_cross_zone_penalty(
            [zone3, zone4], self.PROFILES_34, (yomamte, candidate72), CFG, notes
        )

        by_zone = {event.zone: event for event in out}
        assert [(pc.product.class_id, pc.count) for pc in by_zone[3].judgment.products] == [
            (72, 1)
        ]
        assert [(pc.product.class_id, pc.count) for pc in by_zone[4].judgment.products] == [
            (75, 1)
        ]
        assert any(
            note == (
                "zone3:cross_zone_fingerprint_duplicate_replaced:"
                "removed=class75:adopted=P72x1"
            )
            for note in notes
        )
