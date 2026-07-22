"""Ultralytics TensorRT .engine 어댑터 — perception.Detector 구현 (제약 C1).

현행 파라미터 보존: conf=0.01(I4: 저신뢰 투표 보존), max_det=20, imgsz=480,
FP16 엔진, is_hand = class 0. allowed_class_ids가 오면 predict classes=로
추론을 허용 클래스에 제한한다 (P0-2, 원본 동형). ultralytics는 Jetson
system-site 것을 lazy import 한다 (개발 PC에서 이 모듈 import만으로는
아무것도 로드되지 않음).
"""
from __future__ import annotations

from collections.abc import Sequence

from crk_model.perception.detector import HAND_CLASS_ID, Detection


class UltralyticsEngineDetector:
    def __init__(
        self,
        model_path: str,
        *,
        imgsz: int = 480,
        conf: float = 0.01,
        max_det: int = 20,
        hand_class_id: int = HAND_CLASS_ID,
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

    def detect(
        self, frame, allowed_class_ids: Sequence[int] | None = None
    ) -> Sequence[Detection]:
        # classes 허용목록 (P0-2, 원본 yolo_wrapper 동형): None = 무제한,
        # 빈 목록 = fail-closed(predict 호출 없이 즉시 []) — 노이즈 클래스가
        # max_det 슬롯을 잠식해 저신뢰 실상품을 밀어내는 것을 원천 차단.
        classes: list[int] | None = None
        if allowed_class_ids is not None:
            classes = [int(c) for c in allowed_class_ids]
            if not classes:
                return []
        full = getattr(frame, "full", frame)  # FrameBundle 언랩
        results = self._model.predict(
            full,
            imgsz=self._imgsz,
            conf=self._conf,
            max_det=self._max_det,
            device=self._device,
            classes=classes,
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
