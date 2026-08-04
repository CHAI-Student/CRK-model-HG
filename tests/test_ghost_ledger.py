"""세션 고스트 원장 (ledger/ghost_ledger.py) — 0723 이슈 #17 P1.

시나리오: 옷 프린트 유령 c13이 z1·z2 트리거에서 자격 표를 얻지만 세션 내
무게 뒷받침 과금은 0 — CLOSE 2차 패스가 유령으로 검출·강등한다. 진짜 소수
표 후보(c23, 단일 존)와 무게 뒷받침 과금(c30)은 건드리지 않는다.
"""
from conftest import cand

from crk_model.core.profiles import FREEZER
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.ledger import CloseSettler, GhostLedgerConfig, TriggerEvent
from crk_model.ledger.ghost_ledger import apply_ghost_demotion, detect_ghosts

PROFILES = {1: FREEZER, 2: FREEZER}

G13 = ActiveProduct("P13", "유령13", class_id=13, unit_weight=189.0,
                    unit_price=2100, stock_qty=10)
R23 = ActiveProduct("P23", "실물23", class_id=23, unit_weight=176.0,
                    unit_price=1500, stock_qty=10)
R30 = ActiveProduct("P30", "실물30", class_id=30, unit_weight=100.0,
                    unit_price=1800, stock_qty=10)
PRODUCTS = (G13, R23, R30)


def event(zone, ts, judgment, delta, candidates):
    return TriggerEvent(
        "s", zone, ts, delta, (), judgment, vision_candidates=tuple(candidates)
    )


def ghost_billed_event():
    """z1: 유령 13이 identity_partial(무게 뒷받침 아님)로 과금된 이벤트 —
    실측 ses-4-1784807732 z1 위상 (진짜 23은 5표 소수)."""
    j = JudgmentResult(
        JudgmentStatus.PARTIAL, (ProductCount(G13, 1),), 0.45,
        "vision_first_identity_partial",
    )
    return event(1, 1.0, j, -176.0,
                 [cand(13, conf=0.74, votes=24), cand(23, conf=0.8, votes=5)])


def clean_backed_event():
    """z2: 30이 무게 뒷받침(COMPLETE)으로 과금 — 유령 13이 후보에 또 등장.

    ts=60.0: z1(ts=1.0)과 오염 창(±replay/trigger+ε ≈ ±5s)이 안 겹치는 별개
    에피소드 — settler 통합처럼 window_cfg가 전달되는 경로에서도 breadth가
    성립해야 하는 픽스처다 (같은 순간이면 이슈 #22 에피소드 병합으로 유령
    불성립이 옳다)."""
    j = JudgmentResult(
        JudgmentStatus.COMPLETE, (ProductCount(R30, 1),), 0.9, "strict"
    )
    return event(2, 60.0, j, -100.0,
                 [cand(30, conf=0.9, votes=20), cand(13, conf=0.7, votes=9)])


class StubRouter:
    def __init__(self, result):
        self._result = result

    def judge(self, ctx):
        return self._result


class TestDetection:
    def test_multi_zone_unbacked_is_ghost(self):
        ghosts = detect_ghosts(
            [ghost_billed_event(), clean_backed_event()], GhostLedgerConfig()
        )
        # 13: 2존 자격 + 뒷받침 0(PARTIAL은 뒷받침 아님) → 유령.
        # 30: COMPLETE 뒷받침 / 23: 단일 존 — 둘 다 아님.
        assert ghosts == {13: (1, 2)}

    def test_complete_billing_backs_class(self):
        # 13이 z2에서 COMPLETE(무게 뒷받침)로 과금되면 유령이 아니다 —
        # held 실물(존A 취출 후 들고 이동)이 이 조건으로 보호된다.
        backed = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(G13, 1),), 0.9, "strict"
        )
        e2 = event(2, 2.0, backed, -189.0,
                   [cand(13, conf=0.9, votes=30)])
        assert detect_ghosts([ghost_billed_event(), e2], GhostLedgerConfig()) == {}

    def test_near_gate_billing_is_not_backing(self):
        # COMPLETE라도 near_gate(무게가 고른 예외 경로)는 뒷받침이 아니다
        # (무게가 delta 전량을 설명한 판정만 뒷받침으로 인정한다).
        near = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(G13, 1),), 0.6,
            "freezer_vision_first_near_gate",
        )
        e2 = event(2, 2.0, near, -189.0, [cand(13, conf=0.9, votes=30)])
        assert detect_ghosts(
            [ghost_billed_event(), e2], GhostLedgerConfig()
        ) == {13: (1, 2)}

    def test_single_zone_is_not_ghost(self):
        assert detect_ghosts([ghost_billed_event()], GhostLedgerConfig()) == {}

    def test_shared_episode_video_is_single_appearance(self):
        # 11차 ses-2 재구성: 동시·연쇄 취출의 존 트리거들이 연장 병합된 같은
        # 에피소드 영상을 공유하면 후보 집합이 동일해 모든 클래스가 공짜로
        # "2존 등장"이 된다 (정답 27·30 오플래그 원인). 같은 video_paths는
        # 존 breadth 증거가 아니다 — 에피소드 ≥2 요건으로 걸러진다.
        shared = (("top", "/ep1_top.avi"), ("side", "/ep1_side.avi"))
        j5 = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(R30, 1),), 0.9, "strict"
        )
        j3 = JudgmentResult(
            JudgmentStatus.PARTIAL, (ProductCount(G13, 1),), 0.4,
            "vision_first_identity_partial",
        )
        cands = [cand(13, conf=0.7, votes=12), cand(27, conf=1.0, votes=24),
                 cand(30, conf=0.9, votes=20)]
        e5 = TriggerEvent("s", 5, 1.0, -79.0, (), j5,
                          vision_candidates=tuple(cands), video_paths=shared)
        e3 = TriggerEvent("s", 3, 2.0, -150.0, (), j3,
                          vision_candidates=tuple(cands), video_paths=shared)
        # 13·27 모두 2존(z3/z5) 등장 + 무게 뒷받침 0이지만 같은 에피소드 —
        # 유령 아님. (30은 COMPLETE 뒷받침으로 원래 제외)
        assert detect_ghosts([e5, e3], GhostLedgerConfig()) == {}

    def test_distinct_episode_videos_still_count(self):
        # 서로 다른 에피소드 영상에서 반복 등장하면 breadth 성립 (기존 정의).
        j = JudgmentResult(
            JudgmentStatus.PARTIAL, (ProductCount(G13, 1),), 0.4,
            "vision_first_identity_partial",
        )
        e1 = TriggerEvent(
            "s", 1, 1.0, -176.0, (), j,
            vision_candidates=(cand(13, conf=0.7, votes=12),),
            video_paths=(("top", "/ep1.avi"),),
        )
        e2 = TriggerEvent(
            "s", 2, 2.0, -100.0, (),
            JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(R30, 1),), 0.9,
                           "strict"),
            vision_candidates=(cand(13, conf=0.7, votes=9),
                               cand(30, conf=0.9, votes=20)),
            video_paths=(("top", "/ep2.avi"),),
        )
        assert detect_ghosts([e1, e2], GhostLedgerConfig()) == {13: (1, 2)}

    def test_vote_floor_filters_low_vote_appearance(self):
        j = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(R30, 1),), 0.9, "strict"
        )
        e2 = event(2, 2.0, j, -100.0,
                   [cand(30, conf=0.9, votes=20), cand(13, conf=0.7, votes=2)])
        assert detect_ghosts([ghost_billed_event(), e2], GhostLedgerConfig()) == {}


class TestIssue22EpisodeWindowMerge:
    """이슈 #22 ses-6 재구성: 동시 다존 취출(z4:11, z5:35, 양존 Δ-525)의 존별
    트리거는 **다른 녹화 파일**을 가져 video_paths 동일성 dedup이 뚫린다 —
    GT class35(10표 득표 1위)가 "2존 등장 + 무게 뒷받침 0"으로 유령 오플래그.
    오염 창이 양방향으로 겹치면(같은 물리적 순간의 같은 장면) 파일명과
    무관하게 같은 에피소드다 — window_cfg로 병합한다."""

    A11 = ActiveProduct("P11", "실물11", class_id=11, unit_weight=525.0,
                        unit_price=2000, stock_qty=5)
    CANDS = [cand(35, conf=0.72, votes=10), cand(11, conf=0.68, votes=6),
             cand(10, conf=0.72, votes=5)]

    def zone_events(self, ts4=100.0, ts5=100.2):
        j = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(self.A11, 1),), 0.664, "strict"
        )
        e4 = TriggerEvent(
            "s", 4, ts4, -525.0, (), j, vision_candidates=tuple(self.CANDS),
            video_paths=(("top", f"/z4_{ts4}.avi"),), change_timestamps=(ts4,),
        )
        e5 = TriggerEvent(
            "s", 5, ts5, -525.0, (), j, vision_candidates=tuple(self.CANDS),
            video_paths=(("top", f"/z5_{ts5}.avi"),), change_timestamps=(ts5,),
        )
        return e4, e5

    def test_same_moment_different_files_is_single_episode(self):
        from crk_model.ledger import CrossZonePenaltyConfig

        e4, e5 = self.zone_events()
        # 창 겹침 병합: 35·10 모두 에피소드 1개 → breadth 불성립 → 유령 아님
        assert detect_ghosts(
            [e4, e5], GhostLedgerConfig(), CrossZonePenaltyConfig()
        ) == {}

    def test_distinct_moments_still_count_as_breadth(self):
        from crk_model.ledger import CrossZonePenaltyConfig

        e4, e5 = self.zone_events(ts4=100.0, ts5=200.0)
        # 창이 안 겹치는 별개 에피소드 반복 등장 — 기존 유령 정의 그대로
        assert detect_ghosts(
            [e4, e5], GhostLedgerConfig(), CrossZonePenaltyConfig()
        ) == {35: (4, 5), 10: (4, 5)}

    def test_without_window_cfg_old_behavior(self):
        # window_cfg 미전달(구 호출) — 파일 동일성만으로는 못 걸러 유령 플래그
        # (이 케이스가 바로 ses-6의 오플래그 — settler는 이제 window를 넘긴다)
        e4, e5 = self.zone_events()
        assert detect_ghosts([e4, e5], GhostLedgerConfig()) == {
            35: (4, 5), 10: (4, 5)
        }


class TestShadow:
    def test_shadow_notes_without_change(self):
        events = [ghost_billed_event(), clean_backed_event()]
        notes: list[str] = []
        out = apply_ghost_demotion(
            events, PROFILES, PRODUCTS, GhostLedgerConfig(mode="shadow"), notes
        )
        assert out[0] is events[0] and out[1] is events[1]  # 동작 무변경
        assert any(n == "ghost_classes:class13@z1/2" for n in notes)
        assert any(
            n.startswith("zone1:ghost_shadow:billed=class13:would=") for n in notes
        )
        # 유령이 과금 안 된 z2는 shadow 무기록 (노이즈 억제)
        assert not any(n.startswith("zone2:ghost_shadow") for n in notes)

    def test_off_is_noop(self):
        events = [ghost_billed_event(), clean_backed_event()]
        notes: list[str] = []
        out = apply_ghost_demotion(
            events, PROFILES, PRODUCTS, GhostLedgerConfig(mode="off"), notes
        )
        assert out == events and not notes


class TestActive:
    CFG = GhostLedgerConfig(mode="active")

    def test_rejudges_ghost_billed_event(self):
        events = [ghost_billed_event(), clean_backed_event()]
        notes: list[str] = []
        rejudged = JudgmentResult(
            JudgmentStatus.COMPLETE, (ProductCount(R23, 1),), 0.8, "stub"
        )
        out = apply_ghost_demotion(
            events, PROFILES, PRODUCTS, self.CFG, notes,
            router=StubRouter(rejudged),
        )
        assert [(pc.product.product_id, pc.count) for pc in out[0].judgment.products] == [
            ("P23", 1)
        ]
        assert out[0].judgment.reason.endswith("+ghost_demotion")
        assert any(
            "zone1:ghost_demotion:billed=class13:adopted=class23x1" in n for n in notes
        )
        # 유령이 과금 안 된 이벤트도 후보는 강등 — 후속 cross_zone 채택 방어
        by_class = {c.class_id: c for c in out[1].vision_candidates}
        assert by_class[13].vote_count == 4  # 9 × α(0.5)
        assert by_class[30].vote_count == 20  # 비유령 무변경

    def test_gate_failure_keeps_original_billing(self):
        events = [ghost_billed_event(), clean_backed_event()]
        notes: list[str] = []
        out = apply_ghost_demotion(
            events, PROFILES, PRODUCTS, self.CFG, notes,
            router=StubRouter(JudgmentResult(JudgmentStatus.NO_DETECTION, reason="stub")),
        )
        assert out[0].judgment is events[0].judgment  # 원 판정 유지 (R2)
        assert any("zone1:ghost_demotion_gate_failed:keep_original" in n for n in notes)
        # 판정은 유지해도 후보 강등은 남는다
        assert {c.class_id: c.vote_count for c in out[0].vision_candidates}[13] == 12


class TestSettlerIntegration:
    def test_default_shadow_records_notes_only(self):
        s = CloseSettler(active_products_provider=lambda: PRODUCTS)
        result = s.settle("s", [ghost_billed_event(), clean_backed_event()], PROFILES)
        assert any(n.startswith("ghost_classes:class13") for n in result.notes)
        billed = {
            pc.product.product_id for z in result.zones for pc in z.products if pc.count
        }
        assert "P13" in billed  # shadow — 과금 무변경
