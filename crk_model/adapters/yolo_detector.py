"""Ultralytics TensorRT .engine 어댑터 — perception.Detector 구현 (제약 C1).

현행 파라미터 보존: conf=0.01(I4: 저신뢰 투표 보존), max_det=20, imgsz=480,
FP16 엔진, is_hand = class 0. allowed_class_ids가 오면 predict classes=로
추론을 허용 클래스에 제한한다 (P0-2, 원본 동형). ultralytics는 Jetson
system-site 것을 lazy import 한다 (개발 PC에서 이 모듈 import만으로는
아무것도 로드되지 않음).

T2 (docs/0728_freezer_latency_research.md): detect_batch가 게이트 통과
프레임 묶음을 **전처리 완료 GPU 텐서** 1회 predict로 처리한다 — 프레임당
predict 비용의 ~72%가 CPU(파이썬 letterbox/BGR→RGB/HWC→CHW//255 + NMS
후처리)라는 실측에 근거. batch > 1은 정적 batch 엔진 재수출이 전제
(scripts/convert_engine.sh BATCH=N).
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
        batch: int = 1,
        # detect_batch의 패딩 목표 (D8 1안: 고정 배치 + 패딩, dynamic batch의
        # TRT 프로파일 재선택·할당자 파편화 리스크 회피). 1이면 detect_batch도
        # 들어온 개수 그대로 스택한다 (동적 엔진 전용).
    ):
        from ultralytics import YOLO  # lazy: Jetson system-site 전용

        self._model = YOLO(model_path, task="detect")
        self._imgsz = imgsz
        self._conf = conf
        self._max_det = max_det
        self._hand_class = hand_class_id
        self._device = device
        self._batch = max(int(batch), 1)

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
        return self._to_detections(results[0])

    def detect_batch(
        self, frames: Sequence, allowed_class_ids: Sequence[int] | None = None
    ) -> list[list[Detection]]:
        """게이트 통과 프레임 묶음 추론 (T2-1 + T2-2, perception.BatchDetector).

        - **전처리 완료 GPU 텐서(BCHW·RGB·0~1) 직접 투입**: ultralytics는
          텐서 입력이면 letterbox/BGR→RGB/HWC→CHW//255 전처리를 전부 건너뛴다
          (predictor.preprocess의 not_tensor 분기). uint8로 업로드(전송량 최소)
          후 GPU에서 변환하므로 프레임당 CPU 전처리(~수 ms)가 소멸한다.
          입력이 이미 imgsz 정방형(480×480 center-crop 계약)이라 letterbox가
          no-op이고, **반환 bbox 좌표계도 프레임 좌표 그대로**다 — 입력이
          imgsz와 다르면 이 등식이 깨지므로 프레임별 detect()로 폴백한다.
        - **고정 배치 + 패딩** (D8 1안): len < batch면 0 프레임으로 채우고
          패딩 결과는 폐기 — 정적 batch 엔진의 shape 요구 충족.
        - 기동 프로브(ModelService)가 이 경로를 1회 실행해 엔진 batch/dtype
          불일치를 배포 시점에 fail-fast로 드러낸다 (리뷰 #1 동형).
        """
        classes: list[int] | None = None
        if allowed_class_ids is not None:
            classes = [int(c) for c in allowed_class_ids]
            if not classes:
                return [[] for _ in frames]  # fail-closed (detect와 동형)
        fulls = [getattr(f, "full", f) for f in frames]
        if not fulls:
            return []
        import numpy as np  # lazy: Jetson system-site
        import torch  # lazy

        expected = (self._imgsz, self._imgsz, 3)
        if any(getattr(f, "shape", None) != expected for f in fulls):
            # letterbox 필요 케이스는 어댑터에서 재구현하지 않는다 (좌표계
            # 등식 유지) — ultralytics 전처리가 있는 프레임별 경로로 폴백.
            return [
                list(self.detect(f, allowed_class_ids=allowed_class_ids))
                for f in fulls
            ]
        n = len(fulls)
        pad = max(self._batch - n, 0)
        stack = np.stack(fulls + [np.zeros(expected, dtype=fulls[0].dtype)] * pad)
        device = (
            self._device
            if isinstance(self._device, str)
            else f"cuda:{self._device}"
        )
        t = torch.from_numpy(stack).to(device)  # uint8 업로드
        # BGR→RGB(채널 역순) + BCHW + 0~1 — fp16 변환은 predictor가 수행
        t = t.permute(0, 3, 1, 2)[:, [2, 1, 0], :, :].contiguous().float().div_(255.0)
        results = self._model.predict(
            t,
            imgsz=self._imgsz,
            conf=self._conf,
            max_det=self._max_det,
            device=self._device,
            classes=classes,
            verbose=False,
        )
        return [self._to_detections(r) for r in results[:n]]  # 패딩 결과 폐기

    def _to_detections(self, result) -> list[Detection]:
        detections: list[Detection] = []
        boxes = result.boxes
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
