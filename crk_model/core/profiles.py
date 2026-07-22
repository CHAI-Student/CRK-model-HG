"""SensorProfile — freezer/냉장 코드 포크를 파라미터 포크로 (D3, QA Q1).

제약 C3(센서 물리): 로드셀(LABD-B3/K3)의 보증 분해능은 5g (division 1g)이고,
IO-BOARD 엣지 단이 5g 양자화를 적용한다(CRK-IO-BOARD 2.0.2). 따라서 냉장
임계도 5g 미만은 물리적으로 무의미하다. 냉동고 노이즈는 5~15g.
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
    min_weight_change_grams=5.0,
    segment_step_grams=5.0,  # 5g 양자화 와이어에서 스텝은 5g 배수로만 옴
    motion_gate_threshold=0.02,
    motion_gate_keepalive=8,
    early_termination_allowed=True,
    motion_evidence_floor_px=10.0,  # 원본 MOTION_MIN_DISPLACEMENT_PX
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
    motion_evidence_floor_px=12.0,  # 원본 FREEZER_MOTION_MIN_DISPLACEMENT_PX
)
