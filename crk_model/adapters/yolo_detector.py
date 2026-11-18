"""Ultralytics TensorRT .engine 어댑터 — perception.Detector 구현 (제약 C1).

현행 파라미터 보존: conf=0.01(I4: 저신뢰 투표 보존), max_det=20, imgsz=480,
is_hand = class 0. allowed_class_ids가 오면 predict classes=로 추론을 허용
클래스에 제한한다 (P0-2, 원본 동형). ultralytics는 Jetson system-site 것을
lazy import 한다 (개발 PC에서 이 모듈 import만으로는 아무것도 로드되지 않음).

엔진 정밀도는 이 어댑터의 계약이 아니라 export 시점에 정해진다 —
`scripts/convert_engine.sh`는 현재 `half=False`(FP32)로 내보낸다
(2026-07-21 결정). 어댑터는 로드한 엔진을 그대로 쓴다.

T2 (docs/devdoc/research/0728_freezer_latency_research.md): detect_batch가
게이트 통과 프레임 묶음을 **전처리 완료 GPU 텐서** 1회 predict로 처리한다 — 프레임당
predict 비용의 ~72%가 CPU(파이썬 letterbox/BGR→RGB/HWC→CHW//255 + NMS
후처리)라는 실측에 근거. batch > 1은 정적 batch 엔진 재수출이 전제
(scripts/convert_engine.sh BATCH=N).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

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
        tracker: str = "off",
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
        if tracker not in ("off", "bytetrack", "botsort"):
            raise ValueError(f"Unsupported tracker: {tracker}")
        self._tracker = tracker
        self._tracking_count = 0
        self._tracking_raw: list[list[Detection]] = []
        self._tracking_update = None
        if tracker != "off":
            # model.track가 등록하는 callback보다 먼저 실행한다. 원본 저신뢰
            # 상품 검출은 보존하고, 패딩 영상은 tracker.update에 들어가지 않는다.
            self._model.add_callback("on_predict_postprocess_end", self._capture_tracking_input)

    def begin_clip(self, camera: str) -> None:
        """고정 top/side의 새 촬영 영상을 시작한다. 영상 안에서는 ID를 유지한다.

        엔진 한 개로 top 전체, side 전체를 순서대로 추론하므로 새 영상의 첫
        프레임 전에만 추적기를 비운다. 물리적인 카메라 전환 신호는 사용하지
        않는다. 같은 손 ID 숫자도 (camera, 이번 영상) 안에서만 의미가 있다.
        """
        predictor = getattr(self._model, "predictor", None)
        for tracker in getattr(predictor, "trackers", ()):
            tracker.reset()
        self._tracking_raw = []

    def _capture_tracking_input(self, predictor) -> None:
        if getattr(predictor.args, "mode", None) != "track":
            return
        # 공식 track callback을 실제 프레임에만 실행한 뒤 결과 개수를 복원한다.
        # predictor의 출력/저장 루프는 엔진 batch 크기만큼 결과를 기대한다.
        callbacks = predictor.callbacks["on_predict_postprocess_end"]
        for callback in list(callbacks):
            function = getattr(callback, "func", callback)
            if (getattr(function, "__module__", "") == "ultralytics.trackers.track"
                    and getattr(function, "__name__", "") == "on_predict_postprocess_end"):
                self._tracking_update = callback
                callbacks.remove(callback)
        if self._tracking_update is None:
            raise RuntimeError("Unsupported Ultralytics tracking callback API")
        padding = predictor.results[self._tracking_count:]
        predictor.results = predictor.results[:self._tracking_count]
        self._tracking_raw = [self._to_detections(r) for r in predictor.results]
        # 손만 tracker에 전달한다. 상품과 손이 겹쳐도 같은 ID를 서로
        # 빼앗지 않으며 상품의 기존 투표/튜브 경로는 원본 검출을 사용한다.
        predictor.results = [r[[i for i, d in enumerate(raw) if d.is_hand]]
                             for r, raw in zip(predictor.results, self._tracking_raw, strict=True)]
        try:
            self._tracking_update(predictor)
        finally:
            predictor.results.extend(padding)

    def _infer(self, source, count: int, **kwargs):
        if self._tracker == "off":
            return self._model.predict(source, **kwargs)
        self._tracking_count = count
        self._tracking_raw = []
        return self._model.track(
            source, persist=True, tracker=f"{self._tracker}.yaml", **kwargs,
        )

    def _tracked_detections(self, results, count: int) -> list[list[Detection]]:
        if self._tracker == "off":
            return [self._to_detections(r) for r in results[:count]]
        if len(self._tracking_raw) != count or len(results) < count:
            raise RuntimeError("Ultralytics tracking callback did not preserve real frames")
        output = []
        for raw, result in zip(self._tracking_raw, results[:count], strict=True):
            hands = [d for d in self._to_detections(result) if d.is_hand and d.track_id is not None]
            linked = []
            for detection in raw:
                if detection.is_hand and hands:
                    best = max(hands, key=lambda d: _iou(d.bbox, detection.bbox))
                    if _iou(best.bbox, detection.bbox) >= .5:
                        detection = replace(detection, track_id=best.track_id)
                        hands.remove(best)
                linked.append(detection)
            output.append(linked)
        return output

    @property
    def class_names(self) -> dict:
        """엔진이 로드한 YOLO class_id → 이름 맵 (원본 engine_class_names 대응).

        adapters/serve.py가 이걸로 product→class_id 이름 매핑(issue #6)을 만든다.
        """
        return self._model.names

    def detect(
        self, frame, allowed_class_ids: Sequence[int] | None = None
    ) -> Sequence[Detection]:
        if self._batch > 1:
            # 정적 batch 엔진은 **모든** predict가 정확히 batch 크기여야 한다
            # (ultralytics TRT backend: "input size ... not equal to max model
            # size" — 기동 프로브의 단일 프레임 detect가 warmup부터 실패한
            # 실기 사고, 2026-07-29 커밋 코멘트). 단일 프레임도 패딩 배치
            # 경로로 우회한다.
            return self.detect_batch([frame], allowed_class_ids=allowed_class_ids)[0]
        # classes 허용목록 (P0-2, 원본 yolo_wrapper 동형): None = 무제한,
        # 빈 목록 = fail-closed(predict 호출 없이 즉시 []) — 노이즈 클래스가
        # max_det 슬롯을 잠식해 저신뢰 실상품을 밀어내는 것을 원천 차단.
        classes: list[int] | None = None
        if allowed_class_ids is not None:
            classes = [int(c) for c in allowed_class_ids]
            if not classes:
                return []
        full = getattr(frame, "full", frame)  # FrameBundle 언랩
        results = self._infer(
            full, 1,
            imgsz=self._imgsz,
            conf=self._conf,
            max_det=self._max_det,
            device=self._device,
            classes=classes,
            verbose=False,
        )
        return self._tracked_detections(results, 1)[0]

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
        expected = (self._imgsz, self._imgsz, 3)
        if any(getattr(f, "shape", None) != expected for f in fulls):
            if self._batch > 1:
                # 정적 batch 엔진에서는 프레임별 폴백도 batch-1 predict라
                # 동일하게 실패하고, detect가 다시 이리로 위임하면 무한
                # 재귀다 — 계약 위반을 즉시 드러낸다 (운영 입력은 항상
                # imgsz 정방형 크롭이라 도달하지 않아야 정상).
                raise ValueError(
                    "detect_batch with a static batch engine requires "
                    f"{expected} frames; got "
                    f"{[getattr(f, 'shape', None) for f in fulls]}"
                )
            # letterbox 필요 케이스는 어댑터에서 재구현하지 않는다 (좌표계
            # 등식 유지) — ultralytics 전처리가 있는 프레임별 경로로 폴백.
            return [
                list(self.detect(f, allowed_class_ids=allowed_class_ids))
                for f in fulls
            ]
        import numpy as np  # lazy: Jetson system-site
        import torch  # lazy
        n = len(fulls)
        pad = max(self._batch - n, 0)
        stack = np.stack(fulls + [np.zeros(expected, dtype=fulls[0].dtype)] * pad)
        device = (
            self._device
            if isinstance(self._device, str)
            else f"cuda:{self._device}"
        )
        t = torch.from_numpy(stack).to(device)  # uint8 업로드
        # BGR→RGB(채널 역순) + BCHW + 0~1 — 엔진 정밀도에 맞춘 dtype
        # 캐스팅은 ultralytics predictor가 처리한다 (float32로 넘긴다).
        t = t.permute(0, 3, 1, 2)[:, [2, 1, 0], :, :].contiguous().float().div_(255.0)
        results = self._infer(
            t, n,
            imgsz=self._imgsz,
            conf=self._conf,
            max_det=self._max_det,
            device=self._device,
            classes=classes,
            verbose=False,
        )
        return self._tracked_detections(results, n)

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
                    track_id=(int(box.id[0]) if getattr(box, "id", None) is not None else None),
                )
            )
        return detections


def _iou(a, b) -> float:
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(
        0., min(a[3], b[3]) - max(a[1], b[1]),
    )
    area = max(0., a[2] - a[0]) * max(0., a[3] - a[1])
    area += max(0., b[2] - b[0]) * max(0., b[3] - b[1]) - intersection
    return intersection / area if area > 0 else 0.
