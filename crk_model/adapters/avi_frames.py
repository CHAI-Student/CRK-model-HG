"""AVI → FrameBundle 디코드 어댑터 (원본 frame_extractor 대응).

- 디코드는 워커 스레드에서 lazy로 일어난다 (LazyAviFrames): /trigger 응답은
  202 의미론대로 즉시 반환되고, 무거운 작업은 단일 워커(I7)가 순차 수행.
- 스트리밍: 480×480×3 bytes 프레임 ~400장을 리스트로 상주시키면 카메라당
  ~276MB, 두 카메라 동시 처리 시 4GB Jetson에서 OOM 위험 → decode_avi는
  제너레이터로 프레임을 한 번에 하나씩만 메모리에 둔다.
- 디코더 선택 (env `MODEL__VIDEO__DECODER` = "auto"(기본)|"ffmpeg"|"opencv"):
  auto는 ffmpeg NVDEC(hwaccel cuda) 가용 + numpy 존재 시 ffmpeg 스트리밍
  파이프, 아니면 cv2(CPU 디코드)로 폴백. ffmpeg/cv2/numpy는 모두 lazy
  import (이 레포는 런타임 의존성 0 원칙 — 모듈 최상단 import 금지).
- 게이트 뷰: 그레이 120×120 다운스케일 (L1 비용 절감).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping

from crk_model.frames.bundle import FrameBundle

_hwaccel_cache: bool | None = None


def _ffmpeg_hwaccel_available() -> bool:
    """ffmpeg -hwaccels 출력에 "cuda" 포함 여부를 1회 확인해 캐시."""
    global _hwaccel_cache
    if _hwaccel_cache is not None:
        return _hwaccel_cache
    try:
        result = subprocess.run(
            ["ffmpeg", "-hwaccels"], capture_output=True, text=True, timeout=5
        )
        _hwaccel_cache = "cuda" in result.stdout.lower()
    except Exception:
        _hwaccel_cache = False
    return _hwaccel_cache


def _select_decoder() -> str:
    """env로 지정된 디코더를 고른다. auto는 ffmpeg 가용성+numpy 존재로 판단."""
    choice = os.environ.get("MODEL__VIDEO__DECODER", "auto").strip().lower()
    if choice in ("ffmpeg", "opencv"):
        return choice
    # auto
    if shutil.which("ffmpeg") is None:
        return "opencv"
    try:
        import numpy  # noqa: F401
    except ImportError:
        return "opencv"
    return "ffmpeg" if _ffmpeg_hwaccel_available() else "opencv"


def decode_avi(
    path: str,
    *,
    size: int = 480,
    gate_size: int = 120,
) -> Iterator[FrameBundle]:
    """AVI를 프레임 단위로 디코드해 FrameBundle을 yield하는 스트리밍 이터레이터.

    I1: 열기 실패·0프레임 디코드는 조용한 무검출이 아니라 IOError로 전파
    (파이프라인이 error 이벤트화). "0프레임" 판정은 첫 next() 시점에 이뤄진다.
    """
    decoder = _select_decoder()
    if decoder == "ffmpeg":
        gen = _decode_avi_ffmpeg(path, size=size, gate_size=gate_size)
    else:
        gen = _decode_avi_opencv(path, size=size, gate_size=gate_size)

    # 첫 프레임을 미리 당겨서 "0프레임" 여부를 즉시 판정 (I1) — 이후 프레임은
    # 정상적으로 지연 방출.
    try:
        first = next(gen)
    except StopIteration as exc:
        raise OSError(f"no frames decoded: {path}") from exc

    def _stream() -> Iterator[FrameBundle]:
        try:
            yield first
            yield from gen
        finally:
            gen.close()  # 조기 종료 시 cv2/subprocess 리소스 즉시 해제

    return _stream()


def _decode_avi_opencv(
    path: str, *, size: int, gate_size: int
) -> Iterator[FrameBundle]:
    import cv2  # lazy

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        # I1: 열기 실패는 조용한 무검출이 아니라 예외 → 파이프라인이 error 이벤트화
        raise OSError(f"cannot open video: {path}")
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            full = cv2.resize(img, (size, size))
            gray = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
            yield FrameBundle(full=full, gate_view=cv2.resize(gray, (gate_size, gate_size)))
    finally:
        cap.release()


def _decode_avi_ffmpeg(
    path: str, *, size: int, gate_size: int
) -> Iterator[FrameBundle]:
    import numpy as np  # lazy

    frame_bytes = size * size * 3
    cmd = ["ffmpeg"]
    if _ffmpeg_hwaccel_available():  # cuda 미지원 호스트에서 강제 지정 시 open 실패 방지
        cmd.extend(["-hwaccel", "cuda"])
    cmd.extend(
        [
            "-i",
            path,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{size}x{size}",
            "-v",
            "error",
            "-",
        ]
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    decoded = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = _read_exact(proc.stdout, frame_bytes)
            if buf is None:
                break
            full = np.frombuffer(buf, dtype=np.uint8).reshape((size, size, 3)).copy()
            gray = full.mean(axis=2).astype(np.uint8)
            gate_view = _downsample_gray(gray, gate_size)
            decoded += 1
            yield FrameBundle(full=full, gate_view=gate_view)
        proc.stdout.close()
        returncode = proc.wait(timeout=10)
        if returncode != 0:
            stderr_tail = _stderr_tail(proc)
            raise OSError(
                f"ffmpeg decode failed (rc={returncode}) for {path}: {stderr_tail}"
            )
        if decoded == 0:
            stderr_tail = _stderr_tail(proc)
            raise OSError(f"no frames decoded: {path} ({stderr_tail})")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()  # zombie 방지


def _read_exact(stream, n: int) -> bytes | None:
    """정확히 n바이트를 읽는다. EOF로 0바이트면 None, 도중 끊기면 부분 프레임
    폐기(다음 프레임 없음과 동일 취급)."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if chunks:
                return None  # 잘린 마지막 프레임 — 폐기
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _downsample_gray(gray, gate_size: int):
    """numpy 그레이 배열을 gate_size×gate_size로 최근접 다운샘플 (cv2 없이)."""
    h, w = gray.shape
    row_idx = (_arange(gate_size) * h // gate_size)
    col_idx = (_arange(gate_size) * w // gate_size)
    return gray[row_idx][:, col_idx]


def _arange(n: int):
    import numpy as np  # lazy

    return np.arange(n)


def _stderr_tail(proc, limit: int = 240) -> str:
    try:
        data = proc.stderr.read() if proc.stderr else b""
    except Exception:
        data = b""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


class LazyAviFrames(Mapping):
    """카메라→AVI 경로를 받고, 첫 접근 시점(=워커 스레드)에 디코드한다.

    스트리밍화: 각 __getitem__ 호출은 새 디코드 스트림(제너레이터)을 연다.
    소비처(pipeline._run_vision)는 카메라당 정확히 1회만 순회하므로 캐시 없이도
    재호출 시 재디코드 비용만 감수하면 되고, 대신 프레임 전체 상주를 피한다.
    """

    def __init__(self, video_paths: Mapping[str, str], **decode_kwargs):
        self._paths = dict(video_paths)
        self._kwargs = decode_kwargs

    def __getitem__(self, camera: str) -> Iterator[FrameBundle]:
        if camera not in self._paths:
            raise KeyError(camera)
        return decode_avi(self._paths[camera], **self._kwargs)

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)
