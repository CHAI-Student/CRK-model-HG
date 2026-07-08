"""AVI → FrameBundle 디코드 어댑터 (원본 frame_extractor 대응).

- 디코드는 워커 스레드에서 lazy로 일어난다 (LazyAviFrames): /trigger 응답은
  202 의미론대로 즉시 반환되고, 무거운 작업은 단일 워커(I7)가 순차 수행.
- OpenCV(cv2)는 lazy import — Jetson에서는 JetPack 빌드가 NVDEC 경로를 태울
  수 있고, 아니어도 CPU 디코드로 동작한다 (성능은 G4에서 실측).
- 게이트 뷰: 그레이 120×120 다운스케일 (L1 비용 절감).
"""
from __future__ import annotations

from typing import Iterator, Mapping

from crk_model.frames.bundle import FrameBundle


def decode_avi(
    path: str,
    *,
    size: int = 480,
    gate_size: int = 120,
) -> list[FrameBundle]:
    import cv2  # lazy

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        # I1: 열기 실패는 조용한 무검출이 아니라 예외 → 파이프라인이 error 이벤트화
        raise IOError(f"cannot open video: {path}")
    bundles: list[FrameBundle] = []
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            full = cv2.resize(img, (size, size))
            gray = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
            bundles.append(
                FrameBundle(full=full, gate_view=cv2.resize(gray, (gate_size, gate_size)))
            )
    finally:
        cap.release()
    if not bundles:
        raise IOError(f"no frames decoded: {path}")  # I1
    return bundles


class LazyAviFrames(Mapping):
    """카메라→AVI 경로를 받고, 첫 접근 시점(=워커 스레드)에 디코드한다."""

    def __init__(self, video_paths: Mapping[str, str], **decode_kwargs):
        self._paths = dict(video_paths)
        self._kwargs = decode_kwargs
        self._cache: dict[str, list[FrameBundle]] = {}

    def __getitem__(self, camera: str) -> list[FrameBundle]:
        if camera not in self._paths:
            raise KeyError(camera)
        if camera not in self._cache:
            self._cache[camera] = decode_avi(self._paths[camera], **self._kwargs)
        return self._cache[camera]

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)
