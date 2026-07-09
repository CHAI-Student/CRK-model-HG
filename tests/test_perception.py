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
        # 진입 컷 0(라이브러리 기본)이면 conf 0.05 감지도 투표 누적 —
        # 결합 후 weighted_conf로만 필터 (conf_floor 안전판 경로)
        v = VotingEnsemble(conf_floor=0.4)
        for _ in range(5):
            v.add_frame("top", [Detection(1, 0.05)])
            v.add_frame("side", [Detection(1, 0.05)])
        # weighted = 0.05*0.6 + 0.05*0.4 + 0.05*0.2 = 0.06 < 0.4 → 탈락
        assert v.combine() == ()

    def test_entry_conf_cut_blocks_noise_votes(self):
        # issue #6 2차: 원본의 노이즈 방어 지점(카메라별 진입 임계) — 저신뢰
        # 노이즈가 투표에 진입해 평균 conf를 희석하는 것을 원천 차단한다.
        v = VotingEnsemble(
            entry_conf_top=0.5, entry_conf_side=0.5, conf_floor=0.0, min_vote_count=1
        )
        for _ in range(10):
            v.add_frame("top", [Detection(1, 0.7), Detection(1, 0.05)])  # 노이즈 혼입
        (c,) = v.combine()
        # 진입자(0.7)만 평균에 반영 → weighted = 0.7 * top_only(0.6) = 0.42
        assert c.confidence == pytest.approx(0.7 * 0.6)
        assert c.vote_count == 10  # 노이즈 투표는 카운트에도 미포함
        assert v.entry_dropped == {"top": 10, "side": 0}  # 진단 카운터

    def test_entry_cut_reproduces_original_semantics_end_to_end(self):
        # 원본 재현 프리셋(진입 컷 0.5 + conf_floor 0.0): 실기 사고 패턴
        # (다수 중간 conf 투표)이 후보로 생존하는지 — 구버전(진입 0 + floor 0.4)
        # 에서는 평균 희석으로 전멸하던 케이스.
        old = VotingEnsemble(conf_floor=0.4)  # 구 운영 의미론
        new = VotingEnsemble(entry_conf_top=0.5, conf_floor=0.0)  # 원본 재현
        for _ in range(90):
            # 같은 클래스에 실검출(0.55)과 저신뢰 노이즈(0.05)가 섞임 — 실기
            # vote_summary의 패턴 (94표, weighted 0.157 = 평균 희석)
            frame = [Detection(3, 0.55), Detection(3, 0.05)]
            old.add_frame("top", frame)
            new.add_frame("top", frame)
        assert old.combine() == ()  # 구버전: avg(0.55,0.05)=0.30 ×0.6 < 0.4 → 전멸
        survivors = new.combine()
        assert [c.class_id for c in survivors] == [3]  # 원본 의미론: 상품 생존
        assert survivors[0].confidence == pytest.approx(0.55 * 0.6)  # 진입자만 평균

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
