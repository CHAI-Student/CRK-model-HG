"""perception: 투표 분모 단일 정의, I4, 조기 종료 한정(I15)·단일 tolerance(D7)."""
import pytest
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.perception import Detection, EarlyTerminationConfig, EarlyTerminator, VotingEnsemble


class TestVoting:
    def test_denominator_is_gate_passed_frames(self):
        v = VotingEnsemble(min_vote_count=2, conf_floor=0.0)
        for _ in range(8):
            v.add_frame("top", [])
        v.add_frame("top", [Detection(1, 0.9)])
        v.add_frame("side", [Detection(1, 0.9)])
        (c,) = v.combine()
        assert v.gate_passed_frames == 10
        assert c.vote_ratio == 2 / 10  # 분모 = 게이트 통과 프레임 수

    def test_low_conf_votes_preserved_until_combine(self):
        # I4: conf 0.05 감지도 투표 누적 — 결합 후 weighted_conf로만 필터
        v = VotingEnsemble(conf_floor=0.4)
        for _ in range(5):
            v.add_frame("top", [Detection(1, 0.05)])
            v.add_frame("side", [Detection(1, 0.05)])
        # weighted = 0.05*0.6 + 0.05*0.4 + 0.05*0.2 = 0.06 < 0.4 → 탈락
        assert v.combine() == ()

    def test_weighted_conf_formula(self):
        # 원본 voting_ensemble.py combine() 427-458행: 양쪽 검출 시
        # top*top_weight(0.60) + side*side_weight(0.40) + min(top,side)*bonus(0.2)
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.8)])
        v.add_frame("side", [Detection(1, 0.6)])
        (c,) = v.combine()
        assert abs(c.confidence - (0.8 * 0.6 + 0.6 * 0.4 + 0.6 * 0.2)) < 1e-9

    def test_hand_detections_not_voted(self):
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(0, 0.9, is_hand=True)])
        assert v.combine() == ()

    def test_single_camera_high_conf_survives_as_candidate(self):
        # 이슈 #6: 구버전은 단일 카메라 검출도 공용 0.5/0.5 가중치를 써서
        # top conf=0.7 다수 프레임이 weighted=0.35로 conf_floor(0.4) 미만
        # 탈락했다 — 실기에서 vision_candidates가 전멸한 유력 원인.
        # 원본은 top_only_weight(0.60) 전용 가중치를 써서 0.42로 생존한다.
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.4)
        for _ in range(10):
            v.add_frame("top", [Detection(1, 0.7)])
            v.add_frame("side", [])
        (c,) = v.combine()
        assert abs(c.confidence - (0.7 * 0.60)) < 1e-9
        assert c.confidence >= 0.4  # conf_floor를 넘어 후보로 생존

    def test_side_only_uses_side_only_weight(self):
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("side", [Detection(1, 0.9)])
        (c,) = v.combine()
        assert abs(c.confidence - (0.9 * 0.40)) < 1e-9

    def test_common_class_bonus_both_cameras_detected(self):
        # 원본 439행: dynamic_bonus = min(top_conf, side_conf) * common_class_bonus
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.9)])
        v.add_frame("side", [Detection(1, 0.9)])
        (c,) = v.combine()
        expected = min(0.9 * 0.60 + 0.9 * 0.40 + min(0.9, 0.9) * 0.2, 1.0)
        assert abs(c.confidence - expected) < 1e-9
        assert c.confidence == pytest.approx(1.0)  # 상한 clamp 확인 (0.9*1.0+0.18=1.08→1.0)

    def test_top_only_weight_exceeds_side_only_weight_for_equal_confidence(self):
        # 원본 test_default_top_only_weight_is_higher_than_side_only_weight와 동형
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.8)])
        v.add_frame("side", [Detection(2, 0.8)])
        results = {c.class_id: c for c in v.combine()}
        assert results[1].confidence == pytest.approx(0.8 * 0.60)
        assert results[2].confidence == pytest.approx(0.8 * 0.40)
        assert results[1].confidence > results[2].confidence


class TestEarlyTermination:
    def _terminator(self, profile=REFRIGERATOR):
        return EarlyTerminator(
            profile, EarlyTerminationConfig(min_lead_votes=5, lead_margin=3, hand_exit_frames=5)
        )

    def test_converged_removal_stops(self, cola):
        assert self._terminator().should_stop(
            delta_weight=-100.0,
            candidates=[cand(1, votes=10)],
            active_products=[cola],
            frames_since_hand_exit=6,
        )

    def test_freezer_never_stops(self, cola):
        # I15: freezer 금지
        assert not self._terminator(FREEZER).should_stop(
            delta_weight=-100.0, candidates=[cand(1, votes=10)],
            active_products=[cola], frames_since_hand_exit=6,
        )

    def test_return_never_stops(self, cola):
        # I15: +delta(반품) 금지
        assert not self._terminator().should_stop(
            delta_weight=100.0, candidates=[cand(1, votes=10)],
            active_products=[cola], frames_since_hand_exit=6,
        )

    def test_hand_still_present_blocks(self, cola):
        assert not self._terminator().should_stop(
            delta_weight=-100.0, candidates=[cand(1, votes=10)],
            active_products=[cola], frames_since_hand_exit=2,
        )

    def test_unexplained_delta_blocks(self, cola):
        # D7: judge()와 동일 tolerance(±3g) 단일 소스 — 50g 오차는 설명 불가
        assert not self._terminator().should_stop(
            delta_weight=-150.0, candidates=[cand(1, votes=10)],
            active_products=[cola], frames_since_hand_exit=6,
        )

    def test_no_margin_blocks(self, cola, water):
        assert not self._terminator().should_stop(
            delta_weight=-100.0,
            candidates=[cand(1, votes=6), cand(2, votes=5)],  # 마진 1 < 3
            active_products=[cola, water],
            frames_since_hand_exit=6,
        )
