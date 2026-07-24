"""교차존 비전 오염 페널티 (docs/cross_zone_penalty.md) — CLOSE 2차 패스.

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
        # (구제는 Phase 2 likelihood/tray prior 소관) 관측 note가 남아야 한다.
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
