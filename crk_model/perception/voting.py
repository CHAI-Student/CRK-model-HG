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
  이식한다. 진입 컷을 쓰면 평균이 진입자 기준으로 계산되어(원본 동형)
  결합 후 conf_floor는 불필요해진다 — 운영 기본(Settings)은 진입 컷
  0.70(원본 코드 기본) + conf_floor 0.0(원본 동형)이며, 전부
  MODEL__VISION__* env로 조정 가능하다 (.env.example 참조).

weighted_conf:
  양쪽 검출 — top*top_weight + side*side_weight + min(top,side)*common_class_bonus
  단일 검출 — conf * (top_only_weight | side_only_weight)
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
    ):
        self._conf_floor = conf_floor
        self._min_ratio = min_vote_ratio
        self._min_count = min_vote_count
        self._top_weight = top_weight
        self._side_weight = side_weight
        self._common_class_bonus = common_class_bonus
        self._top_only_weight = top_only_weight
        self._side_only_weight = side_only_weight
        self._entry_conf = {"top": entry_conf_top, "side": entry_conf_side}
        self._votes: dict[str, dict[int, list[float]]] = {
            "top": defaultdict(list),
            "side": defaultdict(list),
        }
        self.gate_passed_frames = 0  # 분모 (단일 정의)
        self.entry_dropped = {"top": 0, "side": 0}  # 진단: 진입 컷 탈락 수

    def add_frame(self, camera: str, detections: Sequence[Detection]) -> None:
        """게이트 통과(=추론된) 프레임에서만 호출."""
        self.gate_passed_frames += 1
        entry = self._entry_conf.get(camera, 0.0)
        for d in detections:
            if d.is_hand:
                continue
            if d.confidence < entry:
                self.entry_dropped[camera] += 1  # 진단용 — 어디서 죽었는지 추적
                continue
            self._votes[camera][d.class_id].append(d.confidence)

    def _weighted_confidence(self, top: list[float], side: list[float]) -> float:
        """원본 voting_ensemble.py combine() 427-458행과 동형 산식.

        양쪽 카메라 검출 시 가중 평균 + 동적 보너스, 단일 카메라 검출 시
        전용 top_only/side_only 가중치를 곱한다(원본 top_weight/side_weight를
        그대로 재사용해 한쪽 conf가 반토막나는 것을 막는다).
        """
        t = sum(top) / len(top) if top else 0.0
        s = sum(side) / len(side) if side else 0.0
        if top and side:
            dynamic_bonus = min(t, s) * self._common_class_bonus
            weighted = t * self._top_weight + s * self._side_weight + dynamic_bonus
        elif top:
            weighted = t * self._top_only_weight
        else:
            weighted = s * self._side_only_weight
        return min(weighted, 1.0)

    def combine(self) -> tuple[VisionCandidate, ...]:
        classes = set(self._votes["top"]) | set(self._votes["side"])
        denominator = max(1, self.gate_passed_frames)
        out: list[VisionCandidate] = []
        for cid in classes:
            top = self._votes["top"].get(cid, [])
            side = self._votes["side"].get(cid, [])
            weighted = self._weighted_confidence(top, side)
            votes = len(top) + len(side)
            ratio = votes / denominator
            if not (ratio >= self._min_ratio or votes >= self._min_count):
                continue
            if weighted < self._conf_floor:
                continue
            out.append(VisionCandidate(cid, weighted, votes, ratio))
        out.sort(key=lambda c: (-c.vote_count, -c.confidence))
        return tuple(out)

    def debug_summary(self) -> dict:
        """issue #6 진단: combine()이 왜 특정 class_id를 버렸는지(vote_ratio
        게이트 vs conf_floor) class_id별로 보고한다. combine()과 동일한 게이트
        로직을 읽기 전용으로 중복 계산할 뿐, combine()의 동작·성능에는 영향
        없다(공유 mutable state 없음, 호출하지 않으면 오버헤드 0)."""
        classes = set(self._votes["top"]) | set(self._votes["side"])
        denominator = max(1, self.gate_passed_frames)
        summary: dict[int, dict] = {}
        for cid in classes:
            top = self._votes["top"].get(cid, [])
            side = self._votes["side"].get(cid, [])
            weighted = self._weighted_confidence(top, side)
            votes = len(top) + len(side)
            ratio = votes / denominator
            passes_ratio_gate = ratio >= self._min_ratio or votes >= self._min_count
            if not passes_ratio_gate:
                rejected_by = "ratio"
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
