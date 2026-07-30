"""frames — 프레임 공급 계층: 모션 게이트(D6)·FrameBundle·프레임 프리페처.

배치 추론(D8)은 이 계층의 수집기가 아니라 파이프라인의 마이크로배치 루프
(service/pipeline.py + adapters/yolo_detector.detect_batch)로 구현됐다 —
설계 단계의 FixedBatchCollector는 2026-07-30 삭제
(docs/07-rejected-and-retired.md).
"""
from crk_model.frames.bundle import FrameBundle
from crk_model.frames.motion_gate import GateDecision, HandLatch, MotionGate

__all__ = ["FrameBundle", "GateDecision", "HandLatch", "MotionGate"]
