"""모션 게이트 (D6, OPTIMIZED_ARCHITECTURE L1) — 눈먼 stride의 상위호환.

직전 "통과" 프레임과의 다운스케일 absdiff 변화 비율이 임계 미만이면 YOLO 스킵.
실패 방향은 "스킵 안 함 = 정확도 무손실, 속도 이득만 소멸"로 안전(fail-safe).

I16 (래치형 조작적 정의): 직전 추론 통과 프레임에서 손이 ROI 내였거나
손의 ROI 퇴장이 아직 미확인이면 스킵 불가. 손 bbox는 YOLO 산출물이므로
미추론 프레임에는 존재하지 않음 → 래치로만 검증 가능.

트레이스 계약 (I8): processed_frames 의미 유지 + gate_skipped_frames 신설.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from crk_model.core.profiles import SensorProfile

Frame = Sequence[Sequence[float]]  # 다운스케일 그레이스케일 2D


def _flat(frame: Frame) -> Iterable[float]:
    for row in frame:
        for v in row:
            yield float(v)


@dataclass(frozen=True)
class GateDecision:
    infer: bool
    reason: str  # "first_frame" | "hand_latch" | "motion" | "keepalive" | "skip"


class HandLatch:
    """I16 손 상태 래치. 추론된 프레임에서만 update_after_inference()를 호출한다."""

    def __init__(self, exit_confirm_frames: int = 3):
        self._exit_confirm = exit_confirm_frames
        self._pending = 0
        self.active = False
        self.frames_since_exit = 0  # 조기 종료(D7)의 "퇴장 후 M프레임" 입력

    def update_after_inference(self, hand_in_roi: bool) -> None:
        if hand_in_roi:
            self.active = True
            self._pending = self._exit_confirm
            self.frames_since_exit = 0
        elif self.active:
            self._pending -= 1
            if self._pending <= 0:
                self.active = False
        else:
            self.frames_since_exit += 1


class MotionGate:
    def __init__(
        self,
        profile: SensorProfile,
        hand_latch: HandLatch,
        *,
        pixel_delta: float = 15.0,
    ):
        self._profile = profile
        self._latch = hand_latch
        self._pixel_delta = pixel_delta
        self._prev: Frame | None = None
        self._consecutive_skips = 0
        self.processed_frames = 0
        self.gate_skipped_frames = 0  # I8: 신설 필드

    def evaluate(self, frame: Frame) -> GateDecision:
        self.processed_frames += 1
        decision = self._decide(frame)
        if decision.infer:
            self._prev = frame  # 비교 기준 = 직전 "통과" 프레임
            self._consecutive_skips = 0
        else:
            self.gate_skipped_frames += 1
            self._consecutive_skips += 1
        return decision

    def _decide(self, frame: Frame) -> GateDecision:
        if self._prev is None:
            return GateDecision(True, "first_frame")
        if self._latch.active:
            return GateDecision(True, "hand_latch")  # I16: 스킵 금지
        if self._diff_ratio(self._prev, frame) >= self._profile.motion_gate_threshold:
            return GateDecision(True, "motion")
        if self._consecutive_skips + 1 >= self._profile.motion_gate_keepalive:
            return GateDecision(True, "keepalive")  # 연속 스킵 상한 → 강제 1장
        return GateDecision(False, "skip")

    def _diff_ratio(self, a: Frame, b: Frame) -> float:
        changed = 0
        total = 0
        for va, vb in zip(_flat(a), _flat(b)):
            total += 1
            if abs(va - vb) > self._pixel_delta:
                changed += 1
        return changed / total if total else 0.0
