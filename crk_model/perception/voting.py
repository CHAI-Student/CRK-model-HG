"""Voting Ensemble — vote_ratio 분모 = 게이트 통과 프레임 수 (단일 정의).

함정 #4: 모션 게이트(D6)·조기 종료(D7) 어느 조합에서도 분모 의미가 바뀌지
않도록, add_frame()은 "게이트를 통과해 추론된 프레임"에서만 호출한다.

weighted_conf = top×0.5 + side×0.5 + min(top,side)×0.2 (현행 산식 보존).
I4: 저신뢰 투표도 결합 전까지 보존, conf 하한(0.4)은 weighted_conf에만.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from crk_model.core.types import VisionCandidate
from crk_model.perception.detector import Detection


class VotingEnsemble:
    def __init__(
        self,
        *,
        conf_floor: float = 0.4,
        min_vote_ratio: float = 0.05,
        min_vote_count: int = 3,
    ):
        self._conf_floor = conf_floor
        self._min_ratio = min_vote_ratio
        self._min_count = min_vote_count
        self._votes: dict[str, dict[int, list[float]]] = {
            "top": defaultdict(list),
            "side": defaultdict(list),
        }
        self.gate_passed_frames = 0  # 분모 (단일 정의)

    def add_frame(self, camera: str, detections: Sequence[Detection]) -> None:
        """게이트 통과(=추론된) 프레임에서만 호출."""
        self.gate_passed_frames += 1
        for d in detections:
            if d.is_hand:
                continue
            self._votes[camera][d.class_id].append(d.confidence)

    def combine(self) -> tuple[VisionCandidate, ...]:
        classes = set(self._votes["top"]) | set(self._votes["side"])
        denominator = max(1, self.gate_passed_frames)
        out: list[VisionCandidate] = []
        for cid in classes:
            top = self._votes["top"].get(cid, [])
            side = self._votes["side"].get(cid, [])
            t = sum(top) / len(top) if top else 0.0
            s = sum(side) / len(side) if side else 0.0
            weighted = t * 0.5 + s * 0.5 + min(t, s) * 0.2
            votes = len(top) + len(side)
            ratio = votes / denominator
            if not (ratio >= self._min_ratio or votes >= self._min_count):
                continue
            if weighted < self._conf_floor:
                continue
            out.append(VisionCandidate(cid, weighted, votes, ratio))
        out.sort(key=lambda c: (-c.vote_count, -c.confidence))
        return tuple(out)
