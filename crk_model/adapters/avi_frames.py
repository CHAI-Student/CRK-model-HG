"""AVI → FrameBundle 디코드 어댑터 (원본 frame_extractor 대응).

- 기하 계약 (2026-07-24 결정: center-crop 전환, 원본 엔진 좌표계 정합 여부는
  불문): 640×480 소스에서 **center-crop 480×480** — 좌우 각 80px(존 바깥
  영역 절반씩)를 버리고 비율을 보존한다. squash resize(비등방 축소)는 여전히
  피한다 — conf 하락과 bbox 좌표계 왜곡(ROI/hand_margin 상수 어긋남)을 낳기
  때문. 크롭 후 크기가 부족한 소형 소스(테스트 픽스처 등)만 리사이즈로
  보정한다 (운영 640×480에서는 무손실 크롭만 발생).
- 크롭 원점 (2026-07-28, 냉장 실기): crop="center"(기본) | "left" —
  냉장 side 카메라는 존이 화면 왼쪽에 있어 left-crop(x=0..480, 원본 엔진과
  동일 좌표계)을 쓴다. 카메라별 선택은 LazyAviFrames(crop_by_camera=...)
  로 주입 (MODEL__VIDEO__SIDE_CROP → ModelService 배선).
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
    """CUDA 디바이스를 실제로 초기화해 보고 1회 캐시.

    구현 주의 (CI 34연속 실패의 원인): `ffmpeg -hwaccels`는 **빌드에 컴파일된**
    hwaccel 목록이라, NVIDIA 드라이버가 없는 호스트(GitHub 러너, 일반 PC)에서도
    "cuda"가 나온다. 그 목록만 보고 `-hwaccel cuda`를 넘기면 디바이스 생성이
    AVERROR(EPERM)으로 죽어 디코드 전체가 "Error opening output files:
    Operation not permitted"로 실패한다 — CPU 폴백 없이. 컴파일 여부가 아니라
    `-init_hw_device cuda`로 실사용 가능 여부를 검사한다 (Jetson에서만 True)."""
    global _hwaccel_cache
    if _hwaccel_cache is not None:
        return _hwaccel_cache
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error",
                "-init_hw_device", "cuda",
                "-f", "lavfi", "-i", "color=black:size=64x64:rate=5",
                "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=10,
        )
        _hwaccel_cache = result.returncode == 0
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
    crop: str = "center",
) -> Iterator[FrameBundle]:
    """AVI를 프레임 단위로 디코드해 FrameBundle을 yield하는 스트리밍 이터레이터.

    crop: "center"(기본) | "left" — 크롭 원점 (모듈 docstring 기하 계약).

    I1: 열기 실패·0프레임 디코드는 조용한 무검출이 아니라 IOError로 전파
    (파이프라인이 error 이벤트화). "0프레임" 판정은 첫 next() 시점에 이뤄진다.
    """
    if crop not in ("center", "left"):
        # cabinet_type과 동일한 fail-closed: 오타가 조용히 center가 되면
        # bbox 좌표계가 80px 어긋난 채 운영되고 있음을 알 수 없다.
        raise ValueError(f"Invalid crop: {crop}")
    decoder = _select_decoder()
    if decoder == "ffmpeg":
        gen = _decode_avi_ffmpeg(path, size=size, gate_size=gate_size, crop=crop)
    else:
        gen = _decode_avi_opencv(path, size=size, gate_size=gate_size, crop=crop)

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
    path: str, *, size: int, gate_size: int, crop: str = "center"
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
            # 크롭 우선 (모듈 docstring 기하 계약): 640×480 → 480×480.
            # x 원점은 crop 모드(center: 좌우 균등 / left: 0)로 결정.
            # 크롭 후에도 목표에 못 미치는 소형 소스만 리사이즈 보정.
            h, w = img.shape[:2]
            if w > size or h > size:
                y0 = max((h - size) // 2, 0)
                x0 = 0 if crop == "left" else max((w - size) // 2, 0)
                img = img[y0 : y0 + size, x0 : x0 + size]
            if img.shape[0] != size or img.shape[1] != size:
                img = cv2.resize(img, (size, size))
            full = img
            gray = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
            yield FrameBundle(full=full, gate_view=cv2.resize(gray, (gate_size, gate_size)))
    finally:
        cap.release()


def _decode_avi_ffmpeg(
    path: str, *, size: int, gate_size: int, crop: str = "center"
) -> Iterator[FrameBundle]:
    """ffmpeg 디코드 진입점 — hwaccel 시도 후 실패(0프레임) 시 CPU 1회 재시도.

    프로브(_ffmpeg_hwaccel_available)가 통과했어도 런타임에 NVDEC 초기화가
    깨질 수 있다(드라이버 상태 등). 프레임을 하나도 못 얻고 죽은 경우에만
    CPU로 폴백한다 — 원본 frame_extractor의 "HWACCEL: CPU" 폴백 동형.
    프레임을 얻은 뒤의 실패는 폴백하지 않는다(중복 방출 방지, I1 에러 전파)."""
    if _ffmpeg_hwaccel_available():
        got_frame = False
        try:
            for bundle in _decode_avi_ffmpeg_cmd(
                path, size=size, gate_size=gate_size, hwaccel=True, crop=crop
            ):
                got_frame = True
                yield bundle
            return
        except OSError:
            if got_frame:
                raise
    yield from _decode_avi_ffmpeg_cmd(
        path, size=size, gate_size=gate_size, hwaccel=False, crop=crop
    )


def _decode_avi_ffmpeg_cmd(
    path: str, *, size: int, gate_size: int, hwaccel: bool, crop: str = "center"
) -> Iterator[FrameBundle]:
    import numpy as np  # lazy

    frame_bytes = size * size * 3
    cmd = ["ffmpeg"]
    if hwaccel:
        cmd.extend(["-hwaccel", "cuda"])
    # 크롭 우선 (모듈 docstring 기하 계약): min(iw,size) 크롭 후 scale은
    # 640×480 운영 소스에서 1:1 통과(no-op), 소형 소스에서만 확대 보정.
    # x 원점은 crop 모드로 결정 — center는 (iw-ow)/2, left는 0. ow/oh는
    # ffmpeg crop 필터가 계산한 출력 크기(=min(iw,size)/min(ih,size))를
    # 가리키는 내장 변수. 필터 표현식 내 콤마는 인자 구분자와 겹치므로
    # \, 로 이스케이프.
    crop_x = "0" if crop == "left" else "(iw-ow)/2"
    vf = (
        f"crop=min(iw\\,{size}):min(ih\\,{size}):{crop_x}:(ih-oh)/2,"
        f"scale={size}:{size}"
    )
    cmd.extend(
        [
            "-i",
            path,
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
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
            gate_view = _gate_view(full, gate_size)
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
            # EOF: 0바이트든 잘린 마지막 프레임이든 폐기 — "다음 프레임 없음"
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _gate_view(full, gate_size: int):
    """풀 프레임(H×W×3 uint8)에서 게이트 뷰(gate_size² 그레이) 생성 —
    **nearest 다운샘플 후 채널 평균** (cv2 없이).

    종전(풀 프레임 전체를 float 평균 → 다운샘플)과 결과가 **비트 동일**하면서
    평균 연산 픽셀이 16배 적다 (480²=230,400 → 120²=14,400): nearest는 픽셀
    '선택'이라 채널 평균과 순서 교환이 가능하고, 같은 픽셀의 float 평균값에
    같은 astype(uint8) 절삭이 적용된다 (docs/0728_freezer_latency_research.md
    T1-2 — 트리거당 0.5~1.5s 절감 실측 대상)."""
    import numpy as np  # lazy

    h, w = full.shape[:2]
    row_idx = np.arange(gate_size) * h // gate_size
    col_idx = np.arange(gate_size) * w // gate_size
    return full[row_idx][:, col_idx].mean(axis=2).astype(np.uint8)


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

    crop_by_camera: 카메라별 크롭 원점 오버라이드 (예: 냉장 side left-crop —
    MODEL__VIDEO__SIDE_CROP). 미지정 카메라는 center (기존 동작).
    """

    def __init__(
        self,
        video_paths: Mapping[str, str],
        *,
        crop_by_camera: Mapping[str, str] | None = None,
        **decode_kwargs,
    ):
        self._paths = dict(video_paths)
        self._crops = dict(crop_by_camera or {})
        self._kwargs = decode_kwargs

    def __getitem__(self, camera: str) -> Iterator[FrameBundle]:
        if camera not in self._paths:
            raise KeyError(camera)
        return decode_avi(
            self._paths[camera],
            crop=self._crops.get(camera, "center"),
            **self._kwargs,
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)
