"""SensorProfile — freezer/냉장 코드 포크를 파라미터 포크로 (D3, QA Q1).

제약 C3(센서 물리): 로드셀(LABD-B3/K3)의 보증 분해능은 5g (division 1g)이고,
IO-BOARD 엣지 단이 5g 양자화를 적용한다(CRK-IO-BOARD 2.0.2). 따라서 냉장
임계도 5g 미만은 물리적으로 무의미하다. 냉동고 노이즈는 5~15g.
냉동고에서 무게는 "무엇인지"를 가리지 못하고(weight_is_discriminative=False)
"몇 개인지"만 거친 게이트(±15g, I3)로 검증한다.

게이트 임계·구간화 임계까지 전부 프로파일 소속이다
(OPTIMIZED_ARCHITECTURE L1 승인 조건 ③, QA Q3 ②).
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SensorProfile:
    name: str
    # judge()와 CLOSE 정산이 공유하는 단일 tolerance 소스 — 이중 기준 금지
    tolerance_grams: float
    # 무게가 정체성 판별자 자격이 있는가 (냉동고 False → vision-first)
    weight_is_discriminative: bool
    # freezer 개수 검증 게이트 (I3). None이면 tolerance_grams 사용
    count_gate_tolerance_grams: float | None
    # trigger 끝 샘플의 안정성 확인. None이면 final-delta median 보정 미사용.
    final_stability_span_grams: float | None
    # 저무게 스킵 게이트 — 존 타입별 명시 분리 (QA Q8)
    min_weight_change_grams: float
    # D4: ingest 구간화 스텝 임계 (freezer는 노이즈 5~15g 때문에 크게)
    segment_step_grams: float
    # D6: 모션 게이트 변화 픽셀 비율 임계 (freezer는 김서림·AE 스윙 → 보수적으로 낮게)
    motion_gate_threshold: float
    # D6: 연속 스킵 시 강제 추론 keepalive 간격
    motion_gate_keepalive: int
    # 파일럿용 최소 순 delta. 설정 시 이 값 미만이면 안정 여부와 무관하게
    # 비전 추론을 건너뛴다.
    min_trigger_delta_grams: float | None = None
    # 모션 변위 증거(원본 motion_min_displacement_px 대응, issue #16 후속):
    # 클래스 트랙의 누적 변위가 max(floor, bbox×0.10)을 넘어야 투표 유효.
    # "집어간 상품은 움직이고 진열 상품은 안 움직인다"의 직접 검사 —
    # static_track/baseline이 대리 신호로 쫓던 물리의 일반해. freezer는
    # 김서림·AE 스윙 노이즈 때문에 원본과 동일하게 +2px 보수적.
    motion_evidence_floor_px: float = 10.0

    @property
    def count_gate(self) -> float:
        return (
            self.count_gate_tolerance_grams
            if self.count_gate_tolerance_grams is not None
            else self.tolerance_grams
        )


REFRIGERATOR = SensorProfile(
    name="refrigerator",
    tolerance_grams=5.0,  # 센서 보증 분해능 5g 미만 임계는 무의미 (C3)
    weight_is_discriminative=True,
    count_gate_tolerance_grams=None,
    final_stability_span_grams=None,
    min_weight_change_grams=5.0,
    segment_step_grams=5.0,  # 5g 양자화 와이어에서 스텝은 5g 배수로만 옴
    motion_gate_threshold=0.02,
    motion_gate_keepalive=8,
    motion_evidence_floor_px=10.0,  # 원본 MOTION_MIN_DISPLACEMENT_PX
)

FREEZER = SensorProfile(
    name="freezer",
    tolerance_grams=15.0,  # MODEL__WEIGHT__FREEZER_WEIGHT_TOLERANCE_GRAMS 계승
    weight_is_discriminative=False,
    count_gate_tolerance_grams=15.0,  # I3
    final_stability_span_grams=10.0,
    min_weight_change_grams=5.0,
    segment_step_grams=20.0,  # 컴프레서 사이클·드리프트 가짜 세그먼트 방지 (QA Q3 ②)
    motion_gate_threshold=0.005,  # 김서림/성에 → 스킵 이득이 0에 수렴해도 정확도 무손실 (fail-safe)
    motion_gate_keepalive=4,
    min_trigger_delta_grams=50.0,  # 파일럿: 200g 이상 상품 구성에서 50g 미만 흔들림 추론 억제
    motion_evidence_floor_px=12.0,  # 원본 FREEZER_MOTION_MIN_DISPLACEMENT_PX
)


# 냉동 반납 매칭 허용오차 (2026-11-26 사용자 결정 — env가 아닌 코드 전역값).
# 같은 상품도 꺼낼 때와 넣을 때 실측이 20~25g 다르다(삼계탕 890→912.5g,
# 용가리 465→489.2g). 취출 판정 허용오차(tolerance_grams 15g)는 그대로 둔다 —
# 25g 이상에서는 무게가 맞는 후보가 늘어 공유 카메라에 오염된 영상 1위가 이긴다.
FREEZER_RETURN_TOLERANCE_GRAMS = 25.0


def return_tolerance(profile: SensorProfile) -> float:
    """반납 매칭 허용오차. 냉동(무게가 판별자가 아닌 프로파일)만 전역값을 쓴다."""
    if profile.weight_is_discriminative:
        return profile.tolerance_grams
    return FREEZER_RETURN_TOLERANCE_GRAMS


def product_size_floor(unit_weights: Iterable[float]) -> float:
    """'상품 1개가 움직였다'고 볼 최소 무게 = 가장 가벼운 등록 상품의 절반.

    2026-11-26 검토: 상품이 아닌 채널 변화(손 접촉 잔여·떼는 변화)는 최대 75g,
    상품 채널은 최소 362g(가장 가벼운 상품은 닭발 200g)이다. 80~185g 어디로 두어도
    24세션 결과가 같고 60g에서 손 떼는 변화(−65g)를 상품으로 본다. 절반은 그
    간격의 가운데이자 "0.5개 이상이면 1개"인 개수 반올림과 같은 기준이다.
    순간 세그먼트에는 손 누름도 750g까지 나오므로, 세그먼트에 쓸 때는 영상 추론을
    여는 조건(D2)과 review 표시(④)에만 쓰고 청구를 직접 만들지 않는다."""
    units = [w for w in unit_weights if w > 0]
    return min(units) / 2 if units else math.inf
