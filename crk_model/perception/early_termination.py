"""조기 종료 (설계 v2 — 셀 단위 설명 완료 시 추론만 중단).

적용 한정 (v1 승계): removal(−delta) & 비freezer에서만. 반품과 freezer는
후반 프레임 증거가 중요. 추론만 중단하고 디코드·손경로·트레이스는 완주
(호출측 책임 — 이 판정기는 "추론 중단 가능" 신호만 준다).

v2 종료 조건: 투표 수렴 + 손 퇴장 + **모든 활성 셀의 delta가 비전 후보 중
한 상품의 n×w로 설명됨** — 판정(router)과 동일한 tolerance 단일 소스.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import ActiveProduct, CellOutcome, VisionCandidate
from crk_model.judgment.router import _weight_pairs


@dataclass(frozen=True)
class EarlyTerminationConfig:
    min_lead_votes: int = 5
    lead_margin: int = 3
    hand_exit_frames: int = 5  # 손 경로 ROI 밖 퇴장 후 M프레임


class EarlyTerminator:
    def __init__(
        self,
        profile: SensorProfile,
        config: EarlyTerminationConfig | None = None,
        *,
        enabled: bool = True,
    ):
        self._profile = profile
        self._config = config or EarlyTerminationConfig()
        self._enabled = enabled

    def should_stop(
        self,
        *,
        cells: Sequence[CellOutcome],
        candidates: Sequence[VisionCandidate],
        active_products: Sequence[ActiveProduct],
        frames_since_hand_exit: int,
    ) -> bool:
        if not (self._enabled and self._profile.early_termination_allowed):
            return False  # freezer 금지
        active = [
            c
            for c in cells
            if c.stabilized
            and abs(c.delta_weight) >= self._profile.min_weight_change_grams
        ]
        if not active or any(c.delta_weight >= 0 for c in active):
            return False  # 반품(+delta) 포함 시 금지
        if frames_since_hand_exit < self._config.hand_exit_frames:
            return False
        ranked = sorted(candidates, key=lambda c: -c.vote_count)
        if not ranked or ranked[0].vote_count < self._config.min_lead_votes:
            return False
        second = ranked[1].vote_count if len(ranked) > 1 else 0
        if ranked[0].vote_count - second < self._config.lead_margin:
            return False
        # 모든 활성 셀 delta 설명 — router와 단일 tolerance 소스
        vision_classes = {c.class_id for c in candidates}
        tol = self._profile.tolerance_grams
        for cell in active:
            pairs = _weight_pairs(
                abs(cell.delta_weight), active_products, tol, removal=True
            )
            if not any(p.class_id in vision_classes for p, _n, _a in pairs):
                return False
        return True
