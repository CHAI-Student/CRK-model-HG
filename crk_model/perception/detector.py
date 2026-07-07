"""검출기 인터페이스 — YOLO TensorRT 구현은 장치 측 어댑터로 주입 (C1).

I4: 저신뢰 감지(conf 0.01+)도 투표 누적까지 보존 — 검출 단계에서 conf 하한을
걸지 않는다. 최종 conf 필터(0.4)는 투표 결합 후에만 적용.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Detection:
    class_id: int
    confidence: float
    is_hand: bool = False
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


class Detector(Protocol):
    def detect(self, frame) -> Sequence[Detection]: ...
