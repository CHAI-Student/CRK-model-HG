"""frames: 모션 게이트(D6), 손 래치(I16), keepalive, 트레이스 카운터(I8)."""
from dataclasses import replace

from crk_model.core.profiles import REFRIGERATOR
from crk_model.frames import FixedBatchCollector, HandLatch, MotionGate


def frame(value):
    return [[value] * 4 for _ in range(4)]


def make_gate(keepalive=8, threshold=0.02):
    profile = replace(
        REFRIGERATOR, motion_gate_keepalive=keepalive, motion_gate_threshold=threshold
    )
    latch = HandLatch(exit_confirm_frames=2)
    return MotionGate(profile, latch), latch


class TestMotionGate:
    def test_first_frame_always_inferred(self):
        gate, _ = make_gate()
        assert gate.evaluate(frame(10)).reason == "first_frame"

    def test_static_frame_skipped_and_counted(self):
        gate, _ = make_gate()
        gate.evaluate(frame(10))
        d = gate.evaluate(frame(10))
        assert not d.infer and d.reason == "skip"
        assert gate.gate_skipped_frames == 1  # I8: 신설 카운터

    def test_motion_frame_inferred(self):
        gate, _ = make_gate()
        gate.evaluate(frame(10))
        assert gate.evaluate(frame(200)).reason == "motion"

    def test_hand_latch_blocks_skip(self):
        # I16: 손 래치 활성 동안 동일 프레임도 스킵 금지
        gate, latch = make_gate()
        gate.evaluate(frame(10))
        latch.update_after_inference(hand_in_roi=True)
        assert gate.evaluate(frame(10)).reason == "hand_latch"

    def test_latch_releases_after_exit_confirmed(self):
        gate, latch = make_gate()
        gate.evaluate(frame(10))
        latch.update_after_inference(hand_in_roi=True)
        latch.update_after_inference(hand_in_roi=False)  # 퇴장 미확인 1
        assert latch.active
        latch.update_after_inference(hand_in_roi=False)  # 퇴장 확인
        assert not latch.active
        assert gate.evaluate(frame(10)).reason == "skip"

    def test_keepalive_forces_inference(self):
        gate, _ = make_gate(keepalive=3)
        gate.evaluate(frame(10))
        assert gate.evaluate(frame(10)).reason == "skip"
        assert gate.evaluate(frame(10)).reason == "skip"
        assert gate.evaluate(frame(10)).reason == "keepalive"

    def test_comparison_baseline_is_last_passed_frame(self):
        # 스킵된 프레임이 아니라 직전 "통과" 프레임과 비교
        gate, _ = make_gate(threshold=0.5)
        gate.evaluate(frame(10))
        # diff 50/255 < 50% → skip? pixel_delta=15 → changed 100% ≥ 0.5 → motion
        gate.evaluate(frame(60))
        # threshold 0.5, |60-10|=50 > 15 → 전 픽셀 변화 → motion
        assert gate.processed_frames == 2


class TestBatchCollector:
    def test_default_off_is_passthrough(self):
        c = FixedBatchCollector(batch_size=1)  # D8: 기본 OFF
        assert c.add("top", "f1") == ["f1"]

    def test_cameras_never_interleaved(self):
        c = FixedBatchCollector(batch_size=2)
        assert c.add("top", "t1") is None
        assert c.add("side", "s1") is None
        assert c.add("top", "t2") == ["t1", "t2"]  # side 프레임 미혼입

    def test_flush_reports_padding(self):
        c = FixedBatchCollector(batch_size=4)
        c.add("top", "t1")
        frames, pad = c.flush("top")
        assert frames == ["t1"] and pad == 3
