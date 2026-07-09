"""Ultralytics TensorRT .engine 어댑터 — perception.Detector 구현 (제약 C1).

현행 파라미터 보존: conf=0.01(I4: 저신뢰 투표 보존), max_det=20, imgsz=480,
FP16 엔진, is_hand = class 0. ultralytics는 Jetson system-site 것을 lazy import
한다 (개발 PC에서 이 모듈 import만으로는 아무것도 로드되지 않음).
"""
from __future__ import annotations

from collections.abc import Sequence

from crk_model.perception.detector import Detection


class UltralyticsEngineDetector:
    def __init__(
        self,
        model_path: str,
        *,
        imgsz: int = 480,
        conf: float = 0.01,
        max_det: int = 20,
        hand_class_id: int = 0,
        device: int | str = 0,
    ):
        from ultralytics import YOLO  # lazy: Jetson system-site 전용

        self._model = YOLO(model_path, task="detect")
        self._imgsz = imgsz
        self._conf = conf
        self._max_det = max_det
        self._hand_class = hand_class_id
        self._device = device

    @property
    def class_names(self) -> dict:
        """엔진이 로드한 YOLO class_id → 이름 맵 (원본 engine_class_names 대응).

        adapters/serve.py가 이걸로 product→class_id 이름 매핑(issue #6)을 만든다.
        """
        return self._model.names

    def detect(self, frame) -> Sequence[Detection]:
        full = getattr(frame, "full", frame)  # FrameBundle 언랩
        results = self._model.predict(
            full,
            imgsz=self._imgsz,
            conf=self._conf,
            max_det=self._max_det,
            device=self._device,
            verbose=False,
        )
        detections: list[Detection] = []
        boxes = results[0].boxes
        if boxes is None:
            return detections
        for box in boxes:
            cls = int(box.cls[0])
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
            detections.append(
                Detection(
                    class_id=cls,
                    confidence=float(box.conf[0]),
                    is_hand=(cls == self._hand_class),
                    bbox=(x1, y1, x2, y2),
                )
            )
        return detections
