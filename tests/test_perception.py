"""perception: 투표 분모 단일 정의, I4, 조기 종료 한정(I15)·단일 tolerance(D7)."""
import pytest
from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import ActiveProduct
from crk_model.perception import (
    Detection,
    DetectionFilterChain,
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

    def test_held_object_position_signals(self):
        # held-object A-1 계측 (0713 §3): carried-in 후보(프리롤부터 전 구간
        # 등장)는 head_votes↑·span_ratio≈1, 진짜 취출 후보(후반 국소 등장)는
        # head=0·span 낮음 — 판정은 무변경, 신호만 후보에 실린다.
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0, head_frames=30)
        for pos in range(100):
            dets = [Detection(40, 0.9)]  # carried-in: 매 프레임
            if 60 <= pos < 75:
                dets.append(Detection(13, 0.9))  # 진짜 취출: 후반 15프레임
            v.add_frame("top", dets, pos=pos)
        by_id = {c.class_id: c for c in v.combine()}
        held, real = by_id[40], by_id[13]
        assert held.head_votes == 30 and held.span_ratio == 1.0
        assert held.first_pos_ratio == 0.0
        assert real.head_votes == 0 and real.span_ratio == 0.15
        assert abs(real.first_pos_ratio - 0.6) < 1e-6

    def test_position_signals_default_zero_without_pos(self):
        # pos 미제공(직접 생성 하위호환) — 계측 필드는 기본값 0 유지
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.add_frame("top", [Detection(1, 0.9)])
        (c,) = v.combine()
        assert c.head_votes == 0 and c.span_ratio == 0.0

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

    def test_weighted_conf_custom_camera_weights(self):
        # 카메라 conf 결합 가중은 env로 조정 가능해야 한다
        # (MODEL__VISION__CONF_WEIGHT_* — Settings가 voting_params로 주입).
        v = VotingEnsemble(
            min_vote_count=1, conf_floor=0.0,
            top_weight=0.8, side_weight=0.1, common_class_bonus=0.05,
        )
        v.add_frame("top", [Detection(1, 0.8)])
        v.add_frame("side", [Detection(1, 0.6)])
        (c,) = v.combine()
        assert abs(c.confidence - (0.8 * 0.8 + 0.6 * 0.1 + 0.6 * 0.05)) < 1e-9

    def test_weighted_conf_custom_single_camera_weight(self):
        # 단일 카메라 검출은 전용 *_ONLY 가중 — 양카메라 가중과 독립 조정.
        v = VotingEnsemble(
            min_vote_count=1, conf_floor=0.0,
            top_weight=0.8, top_only_weight=0.95,
        )
        v.add_frame("top", [Detection(1, 0.6)])
        (c,) = v.combine()
        assert c.confidence == pytest.approx(0.6 * 0.95)

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

    def test_ambiguous_weight_blocks(self, cola):
        # 이슈 #22 0805 ses-38 z3 재구성: Δ-260을 진열 맛밤(86×3=258)과
        # 정답 박카스(261×1)가 모두 설명 — 전 재고에 해가 2개면 아직 안
        # 보인 정답이 있을 수 있으므로 완주한다 (구 동작: 후보 창 안의
        # 86×3 설명만 보고 종료 → 정답 표 0).
        chestnut = ActiveProduct(
            "P58", "단밤", class_id=58, unit_weight=86.0, unit_price=3500, stock_qty=5
        )
        bacchus = ActiveProduct(
            "P13", "박카스", class_id=13, unit_weight=261.0, unit_price=500, stock_qty=5
        )
        assert not self._terminator().should_stop(
            delta_weight=-260.0,
            candidates=[cand(58, votes=10)],
            active_products=[chestnut, bacchus],
            frames_since_hand_exit=6,
        )

    def test_combo_explanation_no_longer_stops(self):
        # 이슈 #22 0805 ses-46 재구성: 60×1+54×3+32×2=738이 Δ-735를 조합
        # 설명해도 종료 금지 — 단일 종 해가 없으면(정답 10×2는 아직 무투표)
        # 완주한다 (구 동작: 매처 조합 성립만으로 종료 → 정답 표 0).
        pool = [
            ActiveProduct("P60", "콜라", class_id=60, unit_weight=535.0,
                          unit_price=2300, stock_qty=5),
            ActiveProduct("P54", "단백질바", class_id=54, unit_weight=55.0,
                          unit_price=2500, stock_qty=5),
            ActiveProduct("P32", "컨디션스틱", class_id=32, unit_weight=19.0,
                          unit_price=3000, stock_qty=5),
        ]
        assert not self._terminator().should_stop(
            delta_weight=-735.0,
            candidates=[cand(54, votes=10), cand(32, votes=5)],
            active_products=pool,
            frames_since_hand_exit=6,
        )

    def test_lead_mismatch_blocks(self):
        # 유일해 상품(트레비 523g)이 득표 리드(빼빼로)가 아니면 종료 금지 —
        # 지금 보이는 증거와 무게 해가 어긋난다 (진열·오염 리드 신호)
        pepero = ActiveProduct(
            "P18", "빼빼로", class_id=18, unit_weight=66.0, unit_price=2500, stock_qty=5
        )
        trevi = ActiveProduct(
            "P11", "트레비", class_id=11, unit_weight=523.0, unit_price=1600, stock_qty=5
        )
        assert not self._terminator().should_stop(
            delta_weight=-523.0,
            candidates=[cand(18, votes=10), cand(11, votes=5)],
            active_products=[pepero, trevi],
            frames_since_hand_exit=6,
        )

    def test_lazy_candidates_not_evaluated_when_gated(self, cola):
        """T1-1 (0728 리서치): combine()은 값싼 가드 통과 후에만 평가된다 —
        냉동(I15)·반품·손 미퇴장 프레임에서 콜러블이 호출되면 회귀."""
        calls = []

        def lazy():
            calls.append(1)
            return [cand(1, votes=10)]

        # 냉동 금지 — 콜러블 미호출
        assert not self._terminator(FREEZER).should_stop(
            delta_weight=-100.0, candidates=lazy,
            active_products=[cola], frames_since_hand_exit=6,
        )
        # 손 미퇴장 — 콜러블 미호출
        assert not self._terminator().should_stop(
            delta_weight=-100.0, candidates=lazy,
            active_products=[cola], frames_since_hand_exit=2,
        )
        assert calls == []
        # 가드 전부 통과 — 콜러블 1회 평가 후 기존과 동일 판정
        assert self._terminator().should_stop(
            delta_weight=-100.0, candidates=lazy,
            active_products=[cola], frames_since_hand_exit=6,
        )
        assert calls == [1]



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

    def test_track_pos_stats_recorded_in_summary(self):
        # T1 계측 (docs/devdoc/design/0723_tracklet_cost_benefit.md §8): 트랙별 first/last/
        # head_obs가 summary().track_detail로 노출 — held 강등(0713 A-2)의
        # 트랙 단위 재구현과 단절률(G2) 실측 입력. 판정 경로 무영향.
        ev = MotionEvidence(floor_px=10.0, head_frames=3)
        for pos in range(6):
            dets = [self._moving(pos)]  # pos 0부터 계속 움직이는 트랙
            if pos >= 4:  # 뒤늦게 등장한 정지 트랙 (head 밖)
                dets.append(Detection(2, 0.9, bbox=(300.0, 300.0, 350.0, 350.0)))
            ev.observe("top", dets, pos=pos)
        s = ev.summary()["top"]
        (t1,) = s[1]["track_detail"]
        assert (t1["first"], t1["last"], t1["obs"], t1["head_obs"]) == (0, 5, 6, 3)
        assert t1["passed"] is True
        (t2,) = s[2]["track_detail"]
        assert (t2["first"], t2["head_obs"], t2["passed"]) == (4, 0, False)
        assert s[2]["tracks"] == 1

    def test_track_pos_stats_optional_backward_compat(self):
        # pos 미제공 호출(기존 라이브러리 사용)은 계측만 생략된다.
        ev = MotionEvidence(floor_px=10.0)
        ev.observe("top", [self._moving(0)])
        (t,) = ev.summary()["top"][1]["track_detail"]
        assert (t["first"], t["last"], t["head_obs"]) == (-1, -1, 0)

    def test_g2_reassociation_bridges_occlusion_gap(self):
        # G2 (0723 문서 §2): 손 가림으로 관측이 끊겼다가 기본 반경(150px)
        # 밖·완화 반경(225px) 안에서 재등장하면 같은 트랙으로 잇는다 —
        # first 승계로 변위가 이어져 진짜 상품 표가 no_motion으로 죽지 않는다.
        ev = MotionEvidence(floor_px=10.0)
        t0 = ev.observe("top", [self._moving(0)])[0]
        ev.observe("top", [self._moving(1)])
        for _ in range(3):
            ev.observe("top", [])  # 가림 — 관측 공백
        tid = ev.observe(
            "top", [Detection(1, 0.9, bbox=(262.0, 50.0, 312.0, 100.0))]
        )[0]
        assert tid == t0  # 200px 이동 — 완화 반경으로 재연관
        assert ev.track_qualifies(tid)

    def test_g2_gap_beyond_window_starts_new_track(self):
        ev = MotionEvidence(floor_px=10.0, reassoc_window=2)
        t0 = ev.observe("top", [self._moving(0)])[0]
        for _ in range(5):
            ev.observe("top", [])
        tid = ev.observe(
            "top", [Detection(1, 0.9, bbox=(250.0, 50.0, 300.0, 100.0))]
        )[0]
        assert tid != t0  # 창 밖 공백 — 기본 반경(150) 초과라 새 트랙

    def test_track_held_requires_head_and_stream_length(self):
        # T2 held 판정: head 지속 관측 + 프리롤 길이 가드 (0713 §3·§6)
        ev = MotionEvidence(floor_px=10.0, held_min_head=3, held_min_stream=10)
        tids = []
        for pos in range(5):
            tids = ev.observe("top", [self._moving(pos)], pos=pos)
        assert ev.track_held(tids[0]) is False  # 스트림 5 < 10 — 프리롤 부족
        for pos in range(5, 12):
            tids = ev.observe("top", [self._moving(pos)], pos=pos)
        assert ev.track_held(tids[0]) is True  # head 충족 + 스트림 충족
        late = ev.observe(
            "top",
            [self._moving(12), Detection(2, 0.9, bbox=(400.0, 400.0, 450.0, 450.0))],
            pos=12,
        )
        assert ev.track_held(late[1]) is False  # 늦게 등장 — head 없음
        (d,) = ev.summary()["top"][2]["track_detail"]
        assert d["held"] is False

    def _held_scene(self, mode):
        # S2 재현: 같은 클래스가 carried-in(0번 위치부터)과 진짜 취출(5번
        # 위치부터, 별개 트랙)로 공존 — 트랙 단위 강등의 핵심 개선점.
        ev = MotionEvidence(
            floor_px=10.0, head_frames=3, held_min_head=2, held_min_stream=5
        )
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0, held_demotion=mode)
        v.attach_motion_evidence(ev)
        for pos in range(8):
            dets = [self._moving(pos, cid=1)]  # carried-in — head 3표
            if pos >= 5:
                off = 12.0 * (pos - 5)
                dets.append(
                    Detection(1, 0.95, bbox=(400.0 + off, 400.0, 450.0 + off, 450.0))
                )
            tids = ev.observe("top", dets, pos=pos)
            v.add_frame("top", dets, track_ids=tids, pos=pos)
        return ev, v

    def test_held_active_confiscates_carried_track_votes_only(self):
        _, v = self._held_scene("active")
        (c,) = v.combine()
        assert c.vote_count == 3  # carried 8표 몰수, 취출 트랙 3표만 생존
        assert v.held_summary() == {"top": {1: [8, 11]}}  # 원 득표는 관측 보존

    def test_held_shadow_keeps_votes_and_reports(self):
        _, v = self._held_scene("shadow")
        (c,) = v.combine()
        assert c.vote_count == 11  # 판정 무변경
        assert v.held_summary() == {"top": {1: [8, 11]}}

    def test_held_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            VotingEnsemble(held_demotion="activ")


    def test_display_then_taken_track_not_held(self):
        # 10차 정정 (ses-6 z1 c40 held 60/61 오플래그): 진열 상품은 프리롤
        # 0프레임부터 관측되지만 head 구간에는 정지 — 취출로 움직여도
        # carried-in이 아니다. head 구간 내 이동 요건이 이를 가른다.
        ev = MotionEvidence(
            floor_px=10.0, head_frames=5, held_min_head=3, held_min_stream=8
        )
        tids = []
        for pos in range(12):
            if pos < 7:  # 진열: head 구간 포함 정지
                det = Detection(1, 0.9, bbox=(50.0, 50.0, 100.0, 100.0))
            else:  # 취출: 이후 큰 이동
                off = 30.0 * (pos - 6)
                det = Detection(1, 0.9, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0))
            tids = ev.observe("top", [det], pos=pos)
        assert ev.track_qualifies(tids[0]) is True  # 변위는 통과 (취출)
        assert ev.track_held(tids[0]) is False  # head 정지 — carried-in 아님
        # 대조: head 구간부터 움직이는 carried-in 트랙은 여전히 held
        ev2 = MotionEvidence(
            floor_px=10.0, head_frames=5, held_min_head=3, held_min_stream=8
        )
        for pos in range(12):
            tids = ev2.observe("top", [self._moving(pos)], pos=pos)
        assert ev2.track_held(tids[0]) is True


class TestTubeDiagnostics:
    """튜브 층 계측 (판정 영향 0) — 의류 산탄("한 궤적, 깜빡이는 클래스")을
    궤적 단위로 관측한다. 이 판정으로 표를 몰수하던 경로(구 TUBE_IDENTITY=
    active)와 저신뢰 표 회수·probation·트랙 소멸은 냉동 실측 열세/무발동으로
    2026-07-30 폐기 — 남은 계약은 "관측만 하고 표는 건드리지 않는다"다
    (docs/07-rejected-and-retired.md)."""

    @staticmethod
    def _moving(i, cid=1, conf=0.9, lane=0.0):
        off = 12.0 * i
        return Detection(
            cid, conf, bbox=(50.0 + off, 50.0 + lane, 100.0 + off, 100.0 + lane)
        )

    @staticmethod
    def _wire(**voting_kwargs):
        ev = MotionEvidence(floor_px=10.0)
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0, **voting_kwargs)
        v.attach_motion_evidence(ev)
        return ev, v

    def _flicker_scene(self):
        # 의류 산탄 시그니처: 한 궤적 위에서 주 클래스 1, 5프레임에 1번
        # 클래스 2로 깜빡임 — 튜브 히스토그램 {1:10, 2:2}, 2는 결정적 소수.
        ev, v = self._wire()
        for i in range(12):
            cid = 2 if i % 5 == 4 else 1
            dets = [self._moving(i, cid=cid)]
            tids = ev.observe("top", dets)
            v.add_frame("top", dets, track_ids=tids)
        return ev, v

    def test_minority_is_measured_but_never_forfeits(self):
        _, v = self._flicker_scene()
        assert {c.class_id for c in v.combine()} == {1, 2}  # 판정 무변경 (계약)
        s = v.tube_summary()
        assert s["by_class"][2]["minority"] == 2
        assert s["by_class"][2]["votes"] == 2  # 유효표는 그대로
        assert s["by_class"][1]["tube_conf"] > 0

    def test_near_tie_not_counted_as_minority(self):
        # 48:52류 근소 열세는 소수가 아니다 (다수결 문턱 0.3)
        ev, v = self._wire()
        for i in range(12):
            cid = 2 if i % 2 else 1  # 6:6
            dets = [self._moving(i, cid=cid)]
            tids = ev.observe("top", dets)
            v.add_frame("top", dets, track_ids=tids)
        s = v.tube_summary()
        assert s["by_class"][1]["minority"] == 0
        assert s["by_class"][2]["minority"] == 0

    def test_separate_objects_keep_separate_tubes(self):
        # 인접한 별개 상품(다른 궤적)은 튜브가 갈라져 서로 소수가 아니다
        ev, v = self._wire()
        for i in range(10):
            dets = [self._moving(i, cid=1), self._moving(i, cid=2, lane=300.0)]
            tids = ev.observe("top", dets)
            v.add_frame("top", dets, track_ids=tids)
        s = v.tube_summary()
        assert s["by_class"][1]["minority"] == 0
        assert s["by_class"][2]["minority"] == 0

    def test_same_frame_dual_box_absorbed_into_one_tube(self):
        # 한 물체에 같은 프레임 두 클래스 박스 — 중심 근접이면 같은 튜브
        ev = MotionEvidence(floor_px=10.0)
        for i in range(6):
            ev.observe(
                "top", [self._moving(i, cid=1), self._moving(i, cid=2, lane=4.0)]
            )
        (tube,) = ev.tube_detail()["top"]
        assert tube["classes"] == {1: 6, 2: 6}

    def test_summary_none_without_motion_evidence(self):
        v = VotingEnsemble(min_vote_count=1)
        v.add_frame("top", [self._moving(0)])
        assert v.tube_summary() is None


class TestSideHandConfFloor:
    """side 전용 손 conf 하한 + hand_path 자동 무장 (이슈 #18 side hand)."""

    def test_side_floor_overrides_global_on_side_only(self):
        f = DetectionFilterChain(hand_conf_floor=0.3, side_hand_conf_floor=0.6)
        weak = Detection(0, 0.45, is_hand=True, bbox=(10.0, 10.0, 50.0, 50.0))
        assert f.apply("top", [weak])  # top: 0.3 하한 → 생존
        assert not f.apply("side", [weak])  # side: 0.6 하한 → 제거
        assert f.drop_stats["hand_conf"]["side"] == 1

    def test_side_floor_unset_inherits_global(self):
        f = DetectionFilterChain(hand_conf_floor=0.3)
        weak = Detection(0, 0.45, is_hand=True, bbox=(10.0, 10.0, 50.0, 50.0))
        assert f.apply("side", [weak])

    def test_side_hand_arms_hand_path_on_side(self):
        # side 손 1건이 들어오는 순간부터 side 상품은 손 궤적 ±마진과
        # 교차해야 생존 — side hand 활성화의 핵심 효과(정지 진열 오투표
        # 제거)이자, 손 오탐 시 recall이 죽는 방아쇠이기도 하다.
        f = DetectionFilterChain(hand_conf_floor=0.0, hand_margin_px=40.0)
        hand = Detection(0, 0.9, is_hand=True, bbox=(100.0, 100.0, 150.0, 150.0))
        near = Detection(7, 0.9, bbox=(160.0, 160.0, 200.0, 200.0))  # ±40 교차
        far = Detection(8, 0.9, bbox=(300.0, 300.0, 350.0, 350.0))  # 궤적 밖
        out = f.apply("side", [hand, near, far])
        assert near in out and far not in out
        assert f.drop_stats["hand_path"]["side"] == 1


class TestHandWindowRatio:
    """vote_ratio 분모 hand_window 모드 (이슈 #18 후속 — 정답 클래스 ratio
    희석 대응). 기본 gate 모드의 분모 의미는 TestVoting의
    test_denominator_is_gate_passed_frames가 계약한다."""

    def test_denominator_counts_hand_active_frames_only(self):
        v = VotingEnsemble(
            min_vote_count=1, conf_floor=0.0, ratio_denominator="hand_window"
        )
        for _ in range(20):  # 프리롤/포스트롤 — 손 없음, 분모 제외
            v.add_frame("top", [], hand_active=False)
        for _ in range(4):
            v.add_frame("top", [Detection(1, 0.9)], hand_active=True)
        (c,) = v.combine()
        assert c.vote_ratio == 4 / 4  # gate 모드였다면 4/24

    def test_camera_without_hand_falls_back_to_its_gate_frames(self):
        # side hand 비활성 구성: side는 손을 못 보지만 표는 낼 수 있다 —
        # 분모 0으로 ratio가 부풀지 않게 그 카메라는 게이트 통과 수 폴백.
        v = VotingEnsemble(
            min_vote_count=1, conf_floor=0.0, ratio_denominator="hand_window"
        )
        for _ in range(4):
            v.add_frame("top", [Detection(1, 0.9)], hand_active=True)
        for _ in range(6):
            v.add_frame("side", [Detection(1, 0.9)], hand_active=False)
        (c,) = v.combine()
        assert c.vote_ratio == 10 / (4 + 6)

    def test_gate_mode_ignores_hand_active(self):
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)  # 기본 gate
        for _ in range(8):
            v.add_frame("top", [], hand_active=True)
        v.add_frame("top", [Detection(1, 0.9)], hand_active=True)
        (c,) = v.combine()
        assert c.vote_ratio == 1 / 9  # 분모 = 게이트 통과 프레임 전체

    def test_invalid_denominator_mode_raises(self):
        with pytest.raises(ValueError):
            VotingEnsemble(ratio_denominator="hand")


class TestMotionUnmeasurable:
    """no_motion "측정 불가" 정책 (이슈 #18 후속) — n=1 트랙은 path=0이라
    passes() 통과가 구조적으로 불가능한 결함의 면제 경로."""

    def test_single_obs_track_forfeits_by_default(self):
        ev = MotionEvidence(floor_px=10.0)  # forfeit (현행 계약)
        tids = ev.observe("top", [Detection(7, 0.9, bbox=(100.0, 100.0, 140.0, 140.0))])
        assert ev.class_motion("top", 7) is False
        assert ev.track_qualifies(tids[0]) is False
        assert ev.class_unmeasurable("top", 7) is True  # 진단 마킹은 양 모드 공통

    def test_exempt_preserves_unmeasurable_class(self):
        ev = MotionEvidence(floor_px=10.0, unmeasurable_policy="exempt")
        tids = ev.observe("top", [Detection(7, 0.9, bbox=(100.0, 100.0, 140.0, 140.0))])
        assert ev.class_motion("top", 7) is True
        assert ev.track_qualifies(tids[0]) is True

    def test_exempt_still_forfeits_measured_still_class(self):
        # 5관측 정지 트랙 = "측정된 정지"(진열) — exempt에서도 몰수 유지.
        ev = MotionEvidence(floor_px=10.0, unmeasurable_policy="exempt")
        for _ in range(5):
            tid = ev.observe(
                "top", [Detection(7, 0.9, bbox=(100.0, 100.0, 140.0, 140.0))]
            )[0]
        assert ev.class_motion("top", 7) is False
        assert ev.track_qualifies(tid) is False

    def test_exempt_fragment_of_measurable_class_stays_forfeited(self):
        # 진열(측정 가능 트랙)과 단편이 같은 클래스로 공존 — 클래스 전체가
        # 측정 불가일 때만 면제한다 (진열+취출 동시 케이스의 트랙 정밀성).
        ev = MotionEvidence(floor_px=10.0, unmeasurable_policy="exempt")
        for _ in range(5):
            ev.observe("top", [Detection(7, 0.9, bbox=(100.0, 100.0, 140.0, 140.0))])
        frag = ev.observe(
            "top", [Detection(7, 0.9, bbox=(400.0, 400.0, 440.0, 440.0))]
        )[0]
        assert ev.track_qualifies(frag) is False

    def test_debug_summary_labels_unmeasurable_forfeit(self):
        # forfeit 모드에서 몰수 사유를 "측정된 정지"와 구분 — exempt 승격
        # 판단의 실측 재료 (rejected_by: no_motion_unmeasurable).
        ev = MotionEvidence(floor_px=10.0)
        v = VotingEnsemble(min_vote_count=1, conf_floor=0.0)
        v.attach_motion_evidence(ev)
        det = Detection(7, 0.9, bbox=(100.0, 100.0, 140.0, 140.0))
        tids = ev.observe("top", [det])
        v.add_frame("top", [det], track_ids=tids)
        assert v.combine() == ()
        assert v.debug_summary()[7]["rejected_by"] == "no_motion_unmeasurable"

    def test_invalid_policy_raises(self):
        with pytest.raises(ValueError):
            MotionEvidence(unmeasurable_policy="open")
