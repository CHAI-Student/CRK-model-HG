"""perception: 투표 분모 단일 정의, I4, 조기 종료 한정(I15)·단일 tolerance(D7)."""
import pytest
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.perception import (
    Detection,
    EarlyTerminationConfig,
    EarlyTerminator,
    MotionEvidence,
    VotingEnsemble,
)


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
        # 진입자(0.7)만 결합에 반영 → weighted = 0.7 * top_only(0.6) = 0.42
        assert c.confidence == pytest.approx(0.7 * 0.6)
        assert c.vote_count == 10  # 노이즈 투표는 카운트에도 미포함
        assert v.entry_dropped == {"top": 10, "side": 0}  # 진단 카운터

    def test_min_vote_share_drops_relative_noise(self):
        # 이슈 #10: 절대 count 게이트(3)는 긴 영상에서 8표짜리 노이즈도
        # 통과시킨다 — 1위 득표 대비 상대 하한이 제거한다.
        v = VotingEnsemble(
            min_vote_count=3, min_vote_ratio=0.05, min_vote_share=0.1, conf_floor=0.0
        )
        for i in range(100):
            dets = [Detection(13, 0.7)]
            if i < 30:
                dets.append(Detection(3, 0.9))
            if i < 8:
                dets.append(Detection(44, 0.67))
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {13, 3}  # 44(1위의 8%) 제거
        assert v.debug_summary()[44]["rejected_by"] == "share"

    def test_min_vote_share_zero_is_backward_compatible(self):
        v = VotingEnsemble(min_vote_count=3, conf_floor=0.0)  # 기본 share=0.0
        for i in range(100):
            dets = [Detection(13, 0.7)] + ([Detection(44, 0.67)] if i < 8 else [])
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {13, 44}

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
        assert old.combine() == ()  # 구 운영: max 0.55 ×0.6 = 0.33 < floor 0.4 → 전멸
        survivors = new.combine()
        assert [c.class_id for c in survivors] == [3]  # 원본 의미론: 상품 생존
        assert survivors[0].confidence == pytest.approx(0.55 * 0.6)  # max 결합 (P1-4)

    def test_weighted_conf_formula(self):
        # 원본 voting_ensemble.py combine() 427-458행: 양쪽 검출 시
        # top*top_weight(0.60) + side*side_weight(0.40) + min(top,side)*bonus(0.2)
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.8)])
        v.add_frame("side", [Detection(1, 0.6)])
        (c,) = v.combine()
        assert abs(c.confidence - (0.8 * 0.6 + 0.6 * 0.4 + 0.6 * 0.2)) < 1e-9

    def test_weighted_conf_uses_camera_max_not_mean(self):
        # P1-4 (perf-gap 보고서): 원본 combine()은 카메라별 최대 conf
        # (top/side_max_confidence)로 결합한다. 구버전의 평균 결합은
        # 0.72 한 번 + 0.45 스무 번 → 0.46×0.6으로 원본(0.72×0.6)보다
        # 항상 낮게 나와 후단 신뢰도 비교가 열세였다.
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.72)])
        for _ in range(20):
            v.add_frame("top", [Detection(1, 0.45)])
        (c,) = v.combine()
        assert c.confidence == pytest.approx(0.72 * 0.60)
        assert c.vote_count == 21

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


class TestMotionEvidence:
    """모션 변위 증거 (issue #16 후속, 원본 변위 필터 이식): "집어간 상품은
    움직이고 진열 상품은 안 움직인다"의 직접 검사 — static_track(연속 정지)·
    baseline(손 타이밍)이 대리 신호로 쫓던 물리의 일반해."""

    @staticmethod
    def _moving(i, cid=1, conf=0.9):
        off = 12.0 * i
        return Detection(cid, conf, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0))

    @staticmethod
    def _wire(**voting_kwargs):
        ev = MotionEvidence(floor_px=10.0)
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0, **voting_kwargs)
        v.attach_motion_evidence(ev)
        return ev, v

    def test_static_class_vetoed_moving_class_passes(self):
        ev, v = self._wire()
        for i in range(10):
            dets = [self._moving(i), Detection(2, 0.95, bbox=(300.0, 300.0, 350.0, 350.0))]
            ev.observe("top", dets)
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {1}
        assert v.debug_summary()[2]["rejected_by"] == "no_motion"

    def test_flickering_static_object_vetoed(self):
        # baseline이 잡으려던 "깜빡이는 고정 물체": 관측에 공백이 있어도
        # 변위 ~0이면 몰수 — static_track(연속 IoU 요건)과의 결정적 차이.
        ev, v = self._wire()
        for i in range(20):
            dets = [self._moving(i)]
            if i % 4 == 0:  # 4프레임에 1번만 깜빡임
                dets.append(Detection(2, 0.9, bbox=(300.0, 300.0, 350.0, 350.0)))
            ev.observe("top", dets)
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {1}

    def test_zero_bbox_exempt_fail_open(self):
        # bbox 없는 검출은 변위를 잴 수 없다 — filters.py와 동일한
        # "실패 방향 = 증거 보존" 원칙으로 면제.
        ev, v = self._wire()
        for _ in range(5):
            dets = [Detection(3, 0.9)]
            ev.observe("top", dets)
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {3}

    def test_per_camera_veto_independent(self):
        # top에서는 정지(진열 각도), side에서는 움직임 → side 표만 유효
        ev, v = self._wire()
        for i in range(10):
            top_dets = [Detection(1, 0.9, bbox=(50.0, 50.0, 100.0, 100.0))]
            side_dets = [self._moving(i)]
            ev.observe("top", top_dets)
            v.add_frame("top", top_dets)
            ev.observe("side", side_dets)
            v.add_frame("side", side_dets)
        (c,) = v.combine()
        assert c.vote_count == 10  # top 10표 몰수, side 10표만
        assert c.confidence == pytest.approx(0.9 * 0.40)  # side-only 가중

    def test_vetoed_top_class_does_not_poison_share_floor(self):
        # 몰수된 배경 1위가 min_vote_share의 기준(top_votes)을 오염시키면
        # 진짜 상품이 상대 하한에 걸린다 — 몰수 반영 후 기준이어야 한다.
        ev, v = self._wire(min_vote_share=0.5)
        for i in range(20):
            dets = [Detection(9, 0.9, bbox=(300.0, 300.0, 350.0, 350.0))]  # 정지 20표
            if i % 4 == 0:
                dets.append(self._moving(i, cid=1))  # 움직임 5표 (정지 1위의 25%)
            ev.observe("top", dets)
            v.add_frame("top", dets)
        assert {c.class_id for c in v.combine()} == {1}

    def test_same_class_display_instance_votes_dropped_track_level(self):
        # 트랙릿 투표 (research §3 적용): 같은 클래스가 진열(정지)+취출(이동)로
        # 동시에 있으면, 클래스 단위 판정으로는 진열 인스턴스 표까지 전부
        # 살아남는다 — 트랙 귀속 투표는 움직인 트랙의 표만 남긴다.
        ev, v = self._wire()
        for i in range(10):
            dets = [
                self._moving(i),  # 취출 인스턴스
                Detection(1, 0.9, bbox=(300.0, 300.0, 350.0, 350.0)),  # 진열 인스턴스
            ]
            tids = ev.observe("top", dets)
            v.add_frame("top", dets, track_ids=tids)
        (c,) = v.combine()
        assert c.vote_count == 10  # 클래스 단위였다면 20 — 진열 트랙 10표 몰수
