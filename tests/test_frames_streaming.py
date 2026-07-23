"""frames streaming: decode_avi 제너레이터화(OOM 방지), ffmpeg NVDEC 경로,
MotionGate._diff_ratio numpy 가속 — 성능/메모리 최적화 트랙 검증.

cv2/ffmpeg/numpy가 없는 환경에서는 해당 시나리오만 skip한다
(이 레포는 런타임 의존성 0 원칙이라 CI 기본 환경에 이들이 없을 수 있다).
"""
from __future__ import annotations

import inspect
import shutil
import subprocess

import pytest
from test_service import FakeDetector, samples

from crk_model.core.profiles import REFRIGERATOR
from crk_model.frames.motion_gate import HandLatch, MotionGate
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
try:
    import numpy as np

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


def _make_test_avi(path) -> str:
    """ffmpeg testsrc로 480x480 5프레임짜리 소형 avi를 만든다."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=480x480:rate=5:duration=1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


class TestHwaccelProbeAndFallback:
    """CI 34연속 실패의 회귀 고정: `-hwaccels` 목록은 컴파일 여부일 뿐이라
    드라이버 없는 호스트에서 `-hwaccel cuda`가 EPERM으로 죽었다. 프로브는
    실초기화 rc로 판정하고, 런타임 실패(0프레임)는 CPU로 1회 폴백한다."""

    def _reset_cache(self, monkeypatch):
        from crk_model.adapters import avi_frames

        monkeypatch.setattr(avi_frames, "_hwaccel_cache", None)
        return avi_frames

    def test_probe_false_when_device_init_fails(self, monkeypatch):
        avi_frames = self._reset_cache(monkeypatch)

        class FakeResult:
            returncode = 1

        monkeypatch.setattr(
            avi_frames.subprocess, "run", lambda *a, **k: FakeResult()
        )
        assert avi_frames._ffmpeg_hwaccel_available() is False

    def test_probe_true_when_device_init_succeeds(self, monkeypatch):
        avi_frames = self._reset_cache(monkeypatch)

        class FakeResult:
            returncode = 0

        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            return FakeResult()

        monkeypatch.setattr(avi_frames.subprocess, "run", fake_run)
        assert avi_frames._ffmpeg_hwaccel_available() is True
        assert "-init_hw_device" in calls["cmd"]  # 컴파일 목록 조회가 아닌 실초기화

    def test_zero_frame_hwaccel_failure_falls_back_to_cpu(self, monkeypatch):
        from crk_model.adapters import avi_frames

        monkeypatch.setattr(avi_frames, "_ffmpeg_hwaccel_available", lambda: True)

        def fake_cmd(path, *, size, gate_size, hwaccel):
            if hwaccel:
                raise OSError("ffmpeg decode failed (rc=255)")
            yield "frame-cpu"

        monkeypatch.setattr(avi_frames, "_decode_avi_ffmpeg_cmd", fake_cmd)
        out = list(avi_frames._decode_avi_ffmpeg("x.avi", size=480, gate_size=120))
        assert out == ["frame-cpu"]

    def test_failure_after_first_frame_propagates_without_fallback(self, monkeypatch):
        # 프레임을 이미 방출한 뒤의 실패는 폴백하면 중복 방출 — I1대로 전파.
        from crk_model.adapters import avi_frames

        monkeypatch.setattr(avi_frames, "_ffmpeg_hwaccel_available", lambda: True)

        def fake_cmd(path, *, size, gate_size, hwaccel):
            yield "frame-1"
            raise OSError("mid-stream failure")

        monkeypatch.setattr(avi_frames, "_decode_avi_ffmpeg_cmd", fake_cmd)
        gen = avi_frames._decode_avi_ffmpeg("x.avi", size=480, gate_size=120)
        assert next(gen) == "frame-1"
        with pytest.raises(OSError):
            next(gen)


class TestDecodeAviStreaming:
    """decode_avi가 제너레이터를 반환하고, 프레임을 한 번에 하나씩만 낸다."""

    @pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg 미설치 환경")
    def test_decode_avi_returns_lazy_iterator(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODEL__VIDEO__DECODER", "ffmpeg" if HAVE_NUMPY else "opencv")
        if not HAVE_NUMPY:
            pytest.skip("numpy 미설치 — ffmpeg 경로는 numpy 필요")
        from crk_model.adapters.avi_frames import decode_avi

        avi = _make_test_avi(tmp_path / "t.avi")
        stream = decode_avi(avi)
        # 전체 리스트가 아니라 이터레이터(제너레이터) — 전체 상주 없음을 타입으로 증명
        assert inspect.isgenerator(stream) or hasattr(stream, "__next__")
        assert not isinstance(stream, (list, tuple))

        first = next(stream)
        assert first.full.shape == (480, 480, 3)
        assert first.gate_view.shape == (120, 120)

    @pytest.mark.skipif(not (HAVE_FFMPEG and HAVE_NUMPY), reason="ffmpeg+numpy 필요")
    def test_partial_consumption_then_close_releases_process(self, tmp_path, monkeypatch):
        # 조기 종료(early termination) 시 서브프로세스가 즉시 해제되는지 확인.
        monkeypatch.setenv("MODEL__VIDEO__DECODER", "ffmpeg")
        from crk_model.adapters.avi_frames import decode_avi

        avi = _make_test_avi(tmp_path / "t2.avi")
        stream = decode_avi(avi)
        next(stream)  # 프레임 1장만 소비 (전체 5장 중)
        stream.close()  # generator.close() — try/finally로 proc.kill() 보장
        # close 이후 재호출해도 StopIteration만 나야 한다(자원 정리됨, 크래시 없음)
        with pytest.raises(StopIteration):
            next(stream)

    @pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg 미설치 환경")
    def test_open_nonexistent_file_raises_ioerror(self, monkeypatch):
        # I1: 열기 실패는 무검출이 아니라 에러로 전파
        monkeypatch.setenv("MODEL__VIDEO__DECODER", "ffmpeg" if HAVE_NUMPY else "opencv")
        if not HAVE_NUMPY:
            pytest.skip("numpy 미설치 — ffmpeg 경로는 numpy 필요")
        from crk_model.adapters.avi_frames import decode_avi

        with pytest.raises(IOError):
            decode_avi("/no/such/video.avi")

    @pytest.mark.skipif(not (HAVE_FFMPEG and HAVE_NUMPY), reason="ffmpeg+numpy 필요")
    def test_lazy_avi_frames_opens_fresh_stream_each_access(self, tmp_path, monkeypatch):
        # LazyAviFrames.__getitem__은 캐시 없이 호출마다 새 스트림을 연다.
        monkeypatch.setenv("MODEL__VIDEO__DECODER", "ffmpeg")
        from crk_model.adapters.avi_frames import LazyAviFrames

        avi = _make_test_avi(tmp_path / "t3.avi")
        laf = LazyAviFrames({"top": avi})
        first_count = sum(1 for _ in laf["top"])
        second_count = sum(1 for _ in laf["top"])
        assert first_count == second_count == 5


class TestDiffRatioEquivalence:
    """numpy fast path와 순수 파이썬 경로의 판정이 같아야 한다."""

    def _gate(self):
        latch = HandLatch()
        return MotionGate(REFRIGERATOR, latch, pixel_delta=15.0)

    def test_list_input_pure_python_path(self):
        gate = self._gate()
        a = [[10] * 4 for _ in range(4)]
        b = [[10] * 4 for _ in range(4)]
        assert gate._diff_ratio(a, b) == 0.0

        c = [[200] * 4 for _ in range(4)]
        assert gate._diff_ratio(a, c) == 1.0

    @pytest.mark.skipif(not HAVE_NUMPY, reason="numpy 미설치")
    def test_numpy_and_list_agree_on_static_frames(self):
        gate = self._gate()
        list_a = [[10] * 4 for _ in range(4)]
        list_b = [[10] * 4 for _ in range(4)]
        np_a = np.array(list_a, dtype=np.uint8)
        np_b = np.array(list_b, dtype=np.uint8)
        assert gate._diff_ratio(list_a, list_b) == gate._diff_ratio(np_a, np_b) == 0.0

    @pytest.mark.skipif(not HAVE_NUMPY, reason="numpy 미설치")
    def test_numpy_and_list_agree_on_motion_frames(self):
        gate = self._gate()
        list_a = [[10] * 4 for _ in range(4)]
        list_b = [[200] * 4 for _ in range(4)]
        np_a = np.array(list_a, dtype=np.uint8)
        np_b = np.array(list_b, dtype=np.uint8)
        assert gate._diff_ratio(list_a, list_b) == gate._diff_ratio(np_a, np_b) == 1.0

    @pytest.mark.skipif(not HAVE_NUMPY, reason="numpy 미설치")
    def test_numpy_uint8_overflow_safe(self):
        # uint8 뺄셈은 wrap-around 하므로 int16 승격 없이는 250 vs 5 차이가
        # (5-250)%256=11 처럼 왜곡될 수 있다 — 오버플로 미방지 시 오탐 회귀.
        gate = self._gate()
        np_a = np.array([[250] * 4 for _ in range(4)], dtype=np.uint8)
        np_b = np.array([[5] * 4 for _ in range(4)], dtype=np.uint8)
        # 실제 차이는 245 > pixel_delta(15) → motion 판정(비율 1.0)이어야 한다
        assert gate._diff_ratio(np_a, np_b) == 1.0

    @pytest.mark.skipif(not HAVE_NUMPY, reason="numpy 미설치")
    def test_gate_evaluate_equivalent_with_numpy_frames(self):
        # MotionGate.evaluate 레벨에서도 등가성 확인 (게이트 결정 자체가 같아야 함)
        gate_list = self._gate()
        gate_np = self._gate()
        list_frames = [
            [[10] * 4 for _ in range(4)],
            [[10] * 4 for _ in range(4)],
            [[200] * 4 for _ in range(4)],
        ]
        np_frames = [np.array(f, dtype=np.uint8) for f in list_frames]

        reasons_list = [gate_list.evaluate(f).reason for f in list_frames]
        reasons_np = [gate_np.evaluate(f).reason for f in np_frames]
        assert reasons_list == reasons_np


class TestPipelineWithGeneratorFrames:
    """pipeline이 dict-of-list뿐 아니라 dict-of-generator frames로도 정상 동작."""

    def _frame_gen(self, n):
        def gen():
            for i in range(n):
                yield [[10 if i % 2 == 0 else 200] * 4 for _ in range(4)]
        return gen()

    def test_process_accepts_generator_frames(self, cola):
        from crk_model.service.snapshot import ActiveProductStore

        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        req = TriggerRequest(
            1,
            {"top": self._frame_gen(8), "side": self._frame_gen(8)},
            samples(500, 400),
            1.0,
        )
        outcome = pipe.process("s1", req)
        assert outcome.event.status != "error"
        assert detector.calls > 0

    def test_empty_list_frames_skip_without_error(self, cola):
        # 빈 리스트(카메라 프레임 없음)는 None이 아니므로 0회 순회하고 계속 진행.
        from crk_model.service.snapshot import ActiveProductStore

        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        req = TriggerRequest(1, {"top": [], "side": []}, samples(500, 400), 1.0)
        outcome = pipe.process("s1", req)
        assert outcome.event.status != "error"
        assert detector.calls == 0

    def test_generator_consumed_exactly_once(self, cola):
        # 카메라당 정확히 1회만 순회 계약 — 소비 후 재순회 불가한 제너레이터를
        # 넘겨도 문제 없어야 한다(list처럼 재사용 가정 X).
        from crk_model.service.snapshot import ActiveProductStore

        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        gen_top = self._frame_gen(6)
        req = TriggerRequest(1, {"top": gen_top}, samples(500, 400), 1.0)
        outcome = pipe.process("s1", req)
        assert outcome.event.status != "error"
        # 제너레이터는 소진됨
        assert list(gen_top) == []
