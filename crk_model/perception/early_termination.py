"""조기 종료 (D7, OPTIMIZED_ARCHITECTURE L2).

적용 한정 (I15): removal(-delta) & 비freezer에서만. 반품과 freezer는
후반 프레임 증거가 중요. 추론만 중단하고 디코드·손경로·트레이스는 완주
(호출측 책임 — 이 판정기는 "추론 중단 가능" 신호만 준다).

**기본 off** (이슈 #22 0805 냉장 20종 실기): "현재 후보 창 안의 설명"은
"남은 프레임이 판정을 못 바꾼다"의 근거가 되지 못한다 — 정답이 아직
화면에 등장하지 않았을 수 있고(2-9: zone3이 top 9컷 처리 후 종료 →
정답 박카스 표 0, 프리롤 진열 맛밤 5표가 86×3=258로 Δ-260을 설명해
오과금), 리드 표가 프리롤 진열·반사광 오검출일 수 있다(3-3: 선반
반사광이 컨디션스틱을 conf 0.4로 지속 검출). 무게 겹침이 흔한 20종
구성에서 이 전제 붕괴가 0805 실기 오판정의 지배 원인이었다. 프레임
처리량은 T2 배치 경로(BATCH_SIZE/TENSOR_INPUT)가 대체한다.

재활성화(env MODEL__VISION__EARLY_TERMINATION=1) 시에도 **전 재고
유일해 게이트**가 강제된다: |delta|를 단일 종 n개로 설명하는 (상품, n)
해가 판매중 전 재고에서 정확히 하나이고, 그 상품이 현재 득표 리드일
때만 "더 볼 필요 없음"이 성립한다. 후보 창 안 유일성은 정보가 아니다 —
매처가 "지금까지 보인 것" 안에서만 찾으면 아직 안 보인 정답의 존재를
원리적으로 알 수 없다. 다품종 조합 설명은 종료 근거로 인정하지 않는다
(ses-46: 60×1+54×3+32×2=738이 Δ-735를 설명해 종료 → 정답 10×2는
표 0. 조합 우연 적합 공간은 조기 종료의 전제를 만족할 수 없다).

이중 기준 금지 (L2 승인 조건 ③): delta 설명 판정은 judge()와 동일한
SensorProfile.tolerance_grams 단일 소스, 탐색 상한은 StrictWeightMatcher.
max_items를 공유한다.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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

    def _unique_solution(
        self, delta_weight: float, active_products: Sequence[ActiveProduct]
    ) -> ActiveProduct | None:
        """전 재고 단일 종 (상품, n) 해가 정확히 하나면 그 상품 (모듈 docstring).

        조작적 정의는 cross_zone._weight_ambiguous·no_candidate_fallback과
        같은 단일 종 전수 탐색 — 다품종 혼합 조합까지 세면 조합 폭발이고,
        조합 설명은 어차피 종료 근거 자격이 없다."""
        target = abs(delta_weight)
        tol = self._profile.tolerance_grams
        unique: ActiveProduct | None = None
        for p in active_products:
            if p.stock_qty <= 0 or p.unit_weight <= 0:
                continue
            for n in range(1, min(p.stock_qty, self._matcher.max_items) + 1):
                if abs(target - n * p.unit_weight) <= tol:
                    if unique is not None:
                        return None  # 해 2개 이상 — 모호
                    unique = p
                    break
        return unique

    def should_stop(
        self,
        *,
        delta_weight: float,
        candidates: Sequence[VisionCandidate] | Callable[[], Sequence[VisionCandidate]],
        active_products: Sequence[ActiveProduct],
        frames_since_hand_exit: int,
    ) -> bool:
        """candidates는 시퀀스 또는 **지연 콜러블**(voting.combine 등).

        콜러블은 값싼 가드(I15 냉동/반품 금지, 손 퇴장 대기, 전 재고 유일해)를
        전부 통과한 뒤에만 호출된다 — 종전에는 호출측이 combine()을 인자
        평가로 매 추론 프레임 실행해, 반환값이 무조건 False인 냉동에서
        O(누적 표²) 비용을 100% 폐기하고 있었다 (0723 비용 문서 §ET 핫패스,
        0728 리서치 T1-1). 유일해 게이트도 O(재고×n) 산술이라 결합보다 싸다."""
        if not (self._enabled and self._profile.early_termination_allowed):
            return False  # I15: freezer 금지
        if delta_weight >= 0:
            return False  # I15: 반품(+delta) 금지
        if frames_since_hand_exit < self._config.hand_exit_frames:
            return False
        # 전 재고 유일해 게이트 (이슈 #22 0805) — 모호하면 완주
        unique = self._unique_solution(delta_weight, active_products)
        if unique is None:
            return False
        if callable(candidates):
            candidates = candidates()
        ranked = sorted(candidates, key=lambda c: -c.vote_count)
        if not ranked or ranked[0].vote_count < self._config.min_lead_votes:
            return False
        second = ranked[1].vote_count if len(ranked) > 1 else 0
        if ranked[0].vote_count - second < self._config.lead_margin:
            return False
        # 리드 일치: 유일해 상품이 득표 리드일 때만 — 리드가 다른 클래스면
        # 지금 보이는 증거와 무게 해가 어긋난다는 뜻 (진열·오염 리드 신호)
        return ranked[0].class_id == unique.class_id
