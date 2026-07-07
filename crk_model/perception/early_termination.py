"""조기 종료 (D7, OPTIMIZED_ARCHITECTURE L2).

적용 한정 (I15): removal(-delta) & 비freezer에서만. 반품과 freezer는
후반 프레임 증거가 중요. 추론만 중단하고 디코드·손경로·트레이스는 완주
(호출측 책임 — 이 판정기는 "추론 중단 가능" 신호만 준다).

이중 기준 금지 (L2 승인 조건 ③): delta 전량 설명 판정은 judge()와 동일한
StrictWeightMatcher + SensorProfile.tolerance_grams 단일 소스를 공유한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import ActiveProduct, VisionCandidate
from crk_model.judgment.strict import StrictWeightMatcher


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
        matcher: StrictWeightMatcher | None = None,
    ):
        self._profile = profile
        self._config = config or EarlyTerminationConfig()
        self._enabled = enabled
        self._matcher = matcher or StrictWeightMatcher()

    def should_stop(
        self,
        *,
        delta_weight: float,
        candidates: Sequence[VisionCandidate],
        active_products: Sequence[ActiveProduct],
        frames_since_hand_exit: int,
    ) -> bool:
        if not (self._enabled and self._profile.early_termination_allowed):
            return False  # I15: freezer 금지
        if delta_weight >= 0:
            return False  # I15: 반품(+delta) 금지
        if frames_since_hand_exit < self._config.hand_exit_frames:
            return False
        ranked = sorted(candidates, key=lambda c: -c.vote_count)
        if not ranked or ranked[0].vote_count < self._config.min_lead_votes:
            return False
        second = ranked[1].vote_count if len(ranked) > 1 else 0
        if ranked[0].vote_count - second < self._config.lead_margin:
            return False
        # delta 전량 설명 — judge()와 단일 tolerance 소스 (D7)
        combos = self._matcher.find_valid_combinations(
            candidates, delta_weight, active_products, self._profile.tolerance_grams
        )
        return bool(combos)
