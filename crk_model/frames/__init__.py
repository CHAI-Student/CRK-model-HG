"""frames — 프레임 공급 계층: 모션 게이트(D6)·FrameBundle·배치 수집기(D8)."""
from crk_model.frames.batch import FixedBatchCollector
from crk_model.frames.bundle import FrameBundle
from crk_model.frames.motion_gate import GateDecision, HandLatch, MotionGate

__all__ = ["FixedBatchCollector", "FrameBundle", "GateDecision", "HandLatch", "MotionGate"]
