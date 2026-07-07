"""SensorProfile — freezer/냉장 코드 포크를 파라미터 포크로 (D3, QA Q1).

제약 C3(센서 물리): 냉장고 로드셀 ±3g / 냉동고 5~15g.
냉동고에서 무게는 "무엇인지"를 가리지 못하고(weight_is_discriminative=False)
"몇 개인지"만 거친 게이트(±15g, I3)로 검증한다.

게이트 임계·구간화 임계·조기 종료 허용 여부까지 전부 프로파일 소속이다
(OPTIMIZED_ARCHITECTURE L1 승인 조건 ③, QA Q3 ②, I15).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SensorProfile:
    name: str
    # judge()와 조기 종료(D7)가 공유하는 단일 tolerance 소스 — 이중 기준 금지
    tolerance_grams: float
    # 무게가 정체성 판별자 자격이 있는가 (냉동고 False → vision-first)
    weight_is_discriminative: bool
    # freezer 개수 검증 게이트 (I3). None이면 tolerance_grams 사용
    count_gate_tolerance_grams: float | None
    # 저무게 스킵 게이트 — 존 타입별 명시 분리 (QA Q8)
    min_weight_change_grams: float
    # D4: ingest 구간화 스텝 임계 (freezer는 노이즈 5~15g 때문에 크게)
    segment_step_grams: float
    # D6: 모션 게이트 변화 픽셀 비율 임계 (freezer는 김서림·AE 스윙 → 보수적으로 낮게)
    motion_gate_threshold: float
    # D6: 연속 스킵 시 강제 추론 keepalive 간격
    motion_gate_keepalive: int
    # I15: freezer·반품에는 조기 종료 금지 (반품은 delta 부호로 별도 차단)
    early_termination_allowed: bool

    @property
    def count_gate(self) -> float:
        return (
            self.count_gate_tolerance_grams
            if self.count_gate_tolerance_grams is not None
            else self.tolerance_grams
        )


REFRIGERATOR = SensorProfile(
    name="refrigerator",
    tolerance_grams=3.0,
    weight_is_discriminative=True,
    count_gate_tolerance_grams=None,
    min_weight_change_grams=5.0,
    segment_step_grams=4.0,
    motion_gate_threshold=0.02,
    motion_gate_keepalive=8,
    early_termination_allowed=True,
)

FREEZER = SensorProfile(
    name="freezer",
    tolerance_grams=15.0,  # MODEL__WEIGHT__FREEZER_WEIGHT_TOLERANCE_GRAMS 계승
    weight_is_discriminative=False,
    count_gate_tolerance_grams=15.0,  # I3
    min_weight_change_grams=5.0,
    segment_step_grams=20.0,  # 컴프레서 사이클·드리프트 가짜 세그먼트 방지 (QA Q3 ②)
    motion_gate_threshold=0.005,  # 김서림/성에 → 스킵 이득이 0에 수렴해도 정확도 무손실 (fail-safe)
    motion_gate_keepalive=4,
    early_termination_allowed=False,  # I15
)
