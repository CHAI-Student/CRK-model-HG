"""검출기 인터페이스 — YOLO TensorRT 구현은 장치 측 어댑터로 주입 (C1).

I4: 저신뢰 감지(conf 0.01+)도 투표 누적까지 보존 — 검출 단계에서 conf 하한을
걸지 않는다. 최종 conf 필터(0.4)는 투표 결합 후에만 적용.

allowed_class_ids (perf-gap 보고서 P0-2, 원본 predict classes= 대응):
판매중 상품의 YOLO class만 추론을 허용해 max_det(20) 슬롯을 노이즈 클래스가
잠식하지 못하게 한다. None = 무제한(startup probe 등), 빈 시퀀스 = fail-closed
(검출 0 — 원본 "empty allowlist ⇒ []" 동형). 카메라별 목록은 파이프라인이
구성한다 (top = 상품 + hand, side = 상품만 — 원본 _inference_allowed_class_ids
동형).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

# 시스템 전역 계약: YOLO class 0 = hand (원본 config.vision.hand_class_id 기본값,
# 상품 매핑이 -1 센티널을 쓰는 이유이기도 하다 — 0과의 충돌 방지).
HAND_CLASS_ID = 0


@dataclass(frozen=True)
class Detection:
    class_id: int
    confidence: float
    is_hand: bool = False
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


class Detector(Protocol):
    def detect(
        self, frame, allowed_class_ids: Sequence[int] | None = None
    ) -> Sequence[Detection]: ...


class BatchDetector(Protocol):
    """선택 확장 (T2-2, D8): 게이트 통과 프레임 묶음을 1회 predict로 처리.

    파이프라인은 batch_size > 1이고 검출기가 detect_batch를 제공할 때만
    이 경로를 쓴다 (duck-typing — 기존 Detector 구현/페이크는 무변경).
    반환은 입력 프레임 순서와 정렬된 프레임별 검출 목록이어야 한다."""

    def detect_batch(
        self, frames: Sequence, allowed_class_ids: Sequence[int] | None = None
    ) -> Sequence[Sequence[Detection]]: ...
