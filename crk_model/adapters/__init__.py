"""장치 어댑터 계층 — 무거운 의존성(ultralytics/cv2/fastapi)은 전부 lazy import.

이 패키지 밖(core~service)은 런타임 의존성 0을 유지한다.
"""
from crk_model.adapters.avi_frames import LazyAviFrames, decode_avi
from crk_model.adapters.http_app import create_app, start_worker_thread
from crk_model.adapters.yolo_detector import UltralyticsEngineDetector

__all__ = [
    "LazyAviFrames",
    "UltralyticsEngineDetector",
    "create_app",
    "decode_avi",
    "start_worker_thread",
]
