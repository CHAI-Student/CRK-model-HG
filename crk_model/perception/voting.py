"""Voting Ensemble — vote_ratio 분모 = 게이트 통과 프레임 수 (단일 정의).

함정 #4: 모션 게이트(D6)·조기 종료(D7) 어느 조합에서도 분모 의미가 바뀌지
않도록, add_frame()은 "게이트를 통과해 추론된 프레임"에서만 호출한다.

이슈 #6 조사 결과 (원본 reference/CRK-model/services/model/model_service/
video/voting_ensemble.py 327-458행 대조):
- 원본 combine()은 양 카메라 검출 시
  `top_conf*top_weight(0.60) + side_conf*side_weight(0.40)
   + min(top_conf,side_conf)*common_class_bonus(0.2)`,
  단일 카메라 검출 시 `conf*top_only_weight(0.60)` 또는
  `conf*side_only_weight(0.40)` 전용 가중치를 쓴다 (config.py 245-264행).
  우리 구버전은 단일 카메라도 공용 0.5/0.5 가중치를 써서 한쪽이 0이 되어
  conf가 반토막났다 — top conf=0.7 단일 검출이 0.35로 죽는 회귀.
  top_weight/side_weight도 원본 기본값 0.60/0.40에 정렬한다(구버전 0.5/0.5).
- 원본 combine()에는 결합 후 weighted_confidence 하한 필터가 없다
  (video_processor.py 3079-3087행: 필터는 vote_ratio/vote_count만).
  원본이 conf 노이즈를 거르는 지점은 프레임을 투표에 올리기 *전*의
  카메라별 임계값(_threshold_for_camera, 기본 top/side 각 0.70,
  실배포 jetson-stride2.env 0.70 · .env.example 0.50)이다.
  2차 실기(issue #6 재현, vote_summary)에서 이 격차가 실측으로 확정됐다:
  conf 0.01 노이즈 투표까지 평균에 섞여 클래스별 weighted가 0.10~0.16에
  머물고, 94~96표를 받은 실제 상품까지 conf_floor(0.4)에서 전멸했다.
  → 원본의 노이즈 방어 지점(카메라별 진입 임계)을 entry_conf_top/side로
  이식한다. 운영 기본(Settings)은 진입 컷 0.70(원본 코드 기본) +
  conf_floor 0.0(원본 동형)이며, 전부 MODEL__VISION__* env로 조정
  가능하다 (.env.example 참조).
- 결합 입력은 카메라별 **최대 conf** (perf-gap 보고서 P1-4, 원본
  voting_ensemble.py combine()의 top_max_confidence/side_max_confidence
  동형). 구버전은 진입 통과 표들의 평균을 썼는데, 같은 장면에서 최종
  confidence가 원본보다 항상 낮게 나와(0.72 한 번 + 0.45 스무 번 → 원본
  0.72 vs 평균 0.46) 후단 판정의 신뢰도 비교 전반이 열세였다.

weighted_conf:
  양쪽 검출 — max_top*top_weight + max_side*side_weight
              + min(max_top, max_side)*common_class_bonus
  단일 검출 — max_conf * (top_only_weight | side_only_weight)
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from crk_model.core.types import VisionCandidate
from crk_model.perception.detector import Detection


class VotingEnsemble:
    def __init__(
        self,
        *,
        conf_floor: float = 0.4,
        min_vote_ratio: float = 0.05,
        min_vote_count: int = 3,
        top_weight: float = 0.60,
        side_weight: float = 0.40,
        common_class_bonus: float = 0.2,
        top_only_weight: float = 0.60,
        side_only_weight: float = 0.40,
        entry_conf_top: float = 0.0,
        entry_conf_side: float = 0.0,
        # 카메라별 투표 진입 임계 (원본 _threshold_for_camera 대응,
        # MODEL__VISION__TOP/SIDE_CONFIDENCE_THRESHOLD). 생성자 기본값 0.0은
        # 라이브러리 하위호환용 — 운영값은 Settings가 주입한다.
        min_vote_share: float = 0.0,
        # 1위 후보 득표 대비 상대 하한 (이슈 #10): 절대 min_vote_count(3)는
        # 400프레임+ 영상에서 노이즈도 통과시킨다 — 8표(1위의 4%)짜리 후보가
        # 판정에 진입해 "무게 filler"(예: 메로나 79g×n)로 채택되는 사고의
        # 원인. votes < top_votes×share 후보를 제거한다. 0.0 = 비활성
        # (하위호환) — 운영값은 Settings(MODEL__VISION__MIN_VOTE_SHARE) 주입.
        head_frames: int = 30,
        # held-object A-1 계측 (0713 §3): 스트림 첫 head_frames(프리롤 첫
        # 1초)의 득표를 head_votes로 센다. pos 미제공 호출(하위호환)은 계측 0.
        held_demotion: str = "off",
        # T2 held 트랙 강등 (0713 A-2의 트랙 단위 재구현, 0723 문서 §8):
        # "off"(라이브러리 기본, 하위호환) | "shadow"(held_summary 관측만) |
        # "active"(carried-in 트랙의 표를 combine에서 몰수 — 같은 클래스의
        # 취출 트랙 표는 유지된다, S2 해소). 운영값은 Settings 주입
        # (MODEL__VISION__HELD_TRACK_DEMOTION). held 판정 자체는
        # MotionEvidence.track_held (head_obs 임계 + 프리롤 가드).
    ):
        if held_demotion not in ("off", "shadow", "active"):
            # baseline_suppress_mode와 동일한 fail-closed — 오타가 조용히
            # off가 되면 강등 없이 운영 중임을 알 수 없다.
            raise ValueError(f"Invalid held_demotion: {held_demotion}")
        self._conf_floor = conf_floor
        self._min_ratio = min_vote_ratio
        self._min_count = min_vote_count
        self._top_weight = top_weight
        self._side_weight = side_weight
        self._common_class_bonus = common_class_bonus
        self._top_only_weight = top_only_weight
        self._side_only_weight = side_only_weight
        self._min_share = min_vote_share
        self._held_mode = held_demotion
        self._entry_conf = {"top": entry_conf_top, "side": entry_conf_side}
        # (conf, track_id|None) — 트랙 귀속 표 (트랙릿 투표, motion_evidence 참조)
        self._votes: dict[str, dict[int, list[tuple[float, int | None]]]] = {
            "top": defaultdict(list),
            "side": defaultdict(list),
        }
        self.gate_passed_frames = 0  # 분모 (단일 정의)
        self.entry_dropped = {"top": 0, "side": 0}  # 진단: 진입 컷 탈락 수
        # held-object A-1 계측: (camera, class)별 [first_pos, last_pos,
        # head_votes] + 카메라별 관측 프레임 수(게이트 스킵 포함 디코드 위치).
        self._head_frames = head_frames
        self._pos_stats: dict[str, dict[int, list[int]]] = {
            "top": defaultdict(lambda: [-1, -1, 0]),
            "side": defaultdict(lambda: [-1, -1, 0]),
        }
        self._frames_seen = {"top": 0, "side": 0}
        # 모션 변위 증거 (perception/motion_evidence.py): attach되면 combine
        # 시점에 "변위 없는 카메라×클래스"의 표를 몰수한다 — 진열/배경 검출이
        # 득표 순위를 오염시키는 것을 투표 진입 전이 아니라 결합 시점에
        # 사후 일괄 차단 (원본 _apply_motion_filter_and_votes 대응). None이면
        # 무효 (라이브러리 하위호환 — 운영 배선은 pipeline이 한다).
        self._motion_evidence = None

    def add_frame(
        self, camera: str, detections: Sequence[Detection], track_ids=None,
        pos: int | None = None,
    ) -> None:
        """게이트 통과(=추론된) 프레임에서만 호출.

        track_ids: MotionEvidence.observe()가 반환한 검출별 트랙 귀속
        (detections와 정렬 동일). 주어지면 트랙릿 투표 — 표가 트랙에
        귀속되어 combine 시점에 트랙 단위로 변위 검증된다. None이면
        표는 클래스 단위 변위 판정으로 폴백 (하위호환).

        pos: 이 프레임의 디코드 스트림 내 위치 (게이트 스킵 **포함** 카운트,
        0-기반) — held-object A-1 계측 입력 (0713 §3, F6 비저촉: 벽시계
        환산 없이 스트림 상대 위치만 쓴다). None이면 계측 생략 (하위호환)."""
        self.gate_passed_frames += 1
        entry = self._entry_conf.get(camera, 0.0)
        tids = track_ids if track_ids is not None else [None] * len(detections)
        if pos is not None:
            self._frames_seen[camera] = max(self._frames_seen[camera], pos + 1)
        for d, tid in zip(detections, tids, strict=True):
            if d.is_hand:
                continue
            if d.confidence < entry:
                self.entry_dropped[camera] += 1  # 진단용 — 어디서 죽었는지 추적
                continue
            self._votes[camera][d.class_id].append((d.confidence, tid))
            if pos is not None:
                st = self._pos_stats[camera][d.class_id]
                if st[0] < 0:
                    st[0] = pos
                st[1] = pos
                if pos < self._head_frames:
                    st[2] += 1

    def attach_motion_evidence(self, evidence) -> None:
        self._motion_evidence = evidence

    def _effective_votes(self, camera: str, cid: int) -> list[float]:
        """변위 증거 반영 후의 유효 표(conf 리스트).

        트랙 귀속이 있는 표는 **트랙 단위**로 검증한다 (트랙릿 투표 — 같은
        클래스의 진열 인스턴스 표는 몰수되고 움직인 트랙의 표만 남는다).
        귀속 없는 표(tid None: zero-bbox 면제 또는 직접 사용)는 클래스 단위
        판정으로 폴백."""
        votes = self._votes[camera].get(cid, [])
        if not votes:
            return []
        if self._motion_evidence is None:
            return [conf for conf, _ in votes]
        ev = self._motion_evidence
        out = []
        class_ok: bool | None = None  # lazy — tid 없는 표가 있을 때만 평가
        for conf, tid in votes:
            if tid is not None:
                if not ev.track_qualifies(tid):
                    continue
                if self._held_mode == "active" and ev.track_held(tid):
                    # T2 active: carried-in 트랙 표 몰수 — 같은 클래스의 취출
                    # 트랙(별개 tid, head 0)은 유지된다. share 분모(_top_votes)
                    # 도 이 경로를 지나므로 자동 정화 (0713 §10 ses-8 문제).
                    continue
                out.append(conf)
                continue
            if class_ok is None:
                class_ok = ev.class_motion(camera, cid)
            if class_ok:
                out.append(conf)
        return out

    def held_summary(self) -> dict | None:
        """T2 관측 (shadow/active 공통): 카메라×클래스별 [held 트랙 표, 전체
        표]. held 표가 없는 클래스는 생략 — 빈 dict = "측정했고 held 없음".
        None = off/증거 미부착. active에서도 원 득표(_votes)가 남아 있어
        몰수 영향의 사후 재구성이 가능하다."""
        if self._motion_evidence is None or self._held_mode == "off":
            return None
        ev = self._motion_evidence
        out: dict[str, dict[int, list[int]]] = {}
        for camera, by_class in self._votes.items():
            for cid, votes in by_class.items():
                held = sum(
                    1 for _, tid in votes if tid is not None and ev.track_held(tid)
                )
                if held:
                    out.setdefault(camera, {})[cid] = [held, len(votes)]
        return out

    def _weighted_confidence(self, top: list[float], side: list[float]) -> float:
        """원본 voting_ensemble.py combine() 427-458행과 동형 산식.

        입력은 카메라별 최대 conf (원본 top/side_max_confidence 동형 — P1-4).
        양쪽 카메라 검출 시 가중 결합 + 동적 보너스, 단일 카메라 검출 시
        전용 top_only/side_only 가중치를 곱한다(원본 top_weight/side_weight를
        그대로 재사용해 한쪽 conf가 반토막나는 것을 막는다).
        """
        t = max(top) if top else 0.0
        s = max(side) if side else 0.0
        if top and side:
            dynamic_bonus = min(t, s) * self._common_class_bonus
            weighted = t * self._top_weight + s * self._side_weight + dynamic_bonus
        elif top:
            weighted = t * self._top_only_weight
        else:
            weighted = s * self._side_only_weight
        return min(weighted, 1.0)

    def _top_votes(self) -> int:
        """상대 하한(min_vote_share)의 기준값 — 전 class 중 최다 득표.
        1위 자신은 share=1.0이라 절대 잘리지 않는다. 변위 몰수 반영 후 기준
        (몰수된 배경 1위가 share 기준을 오염시키면 안 된다)."""
        classes = set(self._votes["top"]) | set(self._votes["side"])
        return max(
            (
                len(self._effective_votes("top", cid)) + len(self._effective_votes("side", cid))
                for cid in classes
            ),
            default=0,
        )

    def combine(self) -> tuple[VisionCandidate, ...]:
        classes = set(self._votes["top"]) | set(self._votes["side"])
        denominator = max(1, self.gate_passed_frames)
        vote_floor = self._top_votes() * self._min_share
        out: list[VisionCandidate] = []
        for cid in classes:
            top = self._effective_votes("top", cid)
            side = self._effective_votes("side", cid)
            weighted = self._weighted_confidence(top, side)
            votes = len(top) + len(side)
            if votes == 0:
                continue  # 변위 몰수로 전멸 (debug_summary가 no_motion으로 보고)
            ratio = votes / denominator
            if not (ratio >= self._min_ratio or votes >= self._min_count):
                continue
            if votes < vote_floor:
                continue  # 이슈 #10: 1위 대비 미미한 득표 = 노이즈/오염 잔재
            if weighted < self._conf_floor:
                continue
            head, span, first = self._position_signals(cid)
            out.append(
                VisionCandidate(
                    cid, weighted, votes, ratio,
                    head_votes=head, span_ratio=span, first_pos_ratio=first,
                )
            )
        out.sort(key=lambda c: (-c.vote_count, -c.confidence))
        return tuple(out)

    def _position_signals(self, cid: int) -> tuple[int, float, float]:
        """held-object A-1 계측 (0713 §3) — 카메라 결합: head는 합, span은
        최대(한쪽 카메라라도 전 구간 등장이면 carried-in 후보), first는 최소."""
        head = 0
        span = 0.0
        first = 1.0
        seen_any = False
        for camera in ("top", "side"):
            st = self._pos_stats[camera].get(cid)
            frames = self._frames_seen[camera]
            if st is None or st[0] < 0 or frames <= 0:
                continue
            seen_any = True
            head += st[2]
            span = max(span, (st[1] - st[0] + 1) / frames)
            first = min(first, st[0] / frames)
        return (head, round(span, 4), round(first, 4)) if seen_any else (0, 0.0, 0.0)

    def debug_summary(self) -> dict:
        """issue #6 진단: combine()이 왜 특정 class_id를 버렸는지(vote_ratio
        게이트 vs conf_floor) class_id별로 보고한다. combine()과 동일한 게이트
        로직을 읽기 전용으로 중복 계산할 뿐, combine()의 동작·성능에는 영향
        없다(공유 mutable state 없음, 호출하지 않으면 오버헤드 0)."""
        classes = set(self._votes["top"]) | set(self._votes["side"])
        denominator = max(1, self.gate_passed_frames)
        vote_floor = self._top_votes() * self._min_share
        summary: dict[int, dict] = {}
        for cid in classes:
            raw_votes = len(self._votes["top"].get(cid, [])) + len(
                self._votes["side"].get(cid, [])
            )
            top = self._effective_votes("top", cid)
            side = self._effective_votes("side", cid)
            weighted = self._weighted_confidence(top, side)
            votes = len(top) + len(side)
            ratio = votes / denominator
            passes_ratio_gate = ratio >= self._min_ratio or votes >= self._min_count
            if votes == 0 and raw_votes > 0:
                rejected_by = "no_motion"  # 변위 증거 없음 — 진열/배경 검출
                votes = raw_votes  # 진단에는 원 득표를 남긴다 (얼마나 몰수됐나)
            elif not passes_ratio_gate:
                rejected_by = "ratio"
            elif votes < vote_floor:
                rejected_by = "share"
            elif weighted < self._conf_floor:
                rejected_by = "conf_floor"
            else:
                rejected_by = None
            summary[cid] = {
                "votes": votes,
                "ratio": ratio,
                "weighted_conf": weighted,
                "rejected_by": rejected_by,
            }
        return summary
