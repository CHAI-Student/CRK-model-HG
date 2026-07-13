"""교차존 비전 오염 페널티 (docs/0711_idea.md) — CLOSE 2차 패스.

시나리오 (§1): zone1에서 A 취출 → 세션 유지 중 zone2에서 B 취출 → zone1
연장창 내 A 재취출. zone2 AVI 프리롤/라이브에 A 장면이 섞여 A가 vision
후보로 진입, B가 A로 오판 → CLOSE에서 soft 페널티로 보정.
"""
import pytest

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.ledger import CloseSettler, CrossZonePenaltyConfig, TriggerEvent
from crk_model.ledger.cross_zone import (
    apply_cross_zone_penalty,
    contamination_window,
    sub_event_anchors,
)
from crk_model.ledger.journal import event_from_dict, event_to_dict

from conftest import cand

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
        # W(E) = [min−4−0.3, max+3+0.3] (§4.2 ②)
        e = event("s", 1, 0.0, judged(cola), -100.0, change_ts=(100.0, 102.5))
        lo, hi = contamination_window(e, CFG)
        assert lo == pytest.approx(95.7)
        assert hi == pytest.approx(105.8)


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
        # ③ 소스 신뢰도 게이트 (R1): confidence < θ 소스는 오판 전파 차단
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
        assert out[1] is z2 and not notes

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
