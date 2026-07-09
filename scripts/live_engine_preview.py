#!/usr/bin/env python3
"""Live TensorRT engine preview for Jetson camera validation.

This utility is intentionally separate from the FastAPI service (crk_model).
Run it on the Jetson Orin Nano Ubuntu 22.04 device to visually verify the
camera stream and the `.engine` model output with real-time bounding boxes
and labels.

Jetson CUDA/TensorRT 런타임 경로가 필요하면 이 스크립트를 실행하기 전에
`source scripts/jetson_env.sh`로 CUDA_HOME/LD_LIBRARY_PATH 등을 먼저 준비하라
(activate 훅으로 자동 적용되는 setup_jetson.sh를 이미 썼다면 생략 가능).

의존성 참고: 이 스크립트는 crk_model 패키지에 의존하지 않는 완전 독립
유틸이며, cv2/ultralytics를 직접 import한다. crk_model 코어의 "런타임
의존성 0" 원칙은 여기 적용되지 않는다 (Jetson 실기 육안 검증 전용 도구라는
예외).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

LOGGER = logging.getLogger("live_engine_preview")


def _parse_source(value: str) -> int | str:
    text = value.strip()
    if text.isdigit():
        return int(text)
    return text


def _parse_classes(value: str | None) -> list[int] | None:
    if value is None or value.strip() == "":
        return None
    classes: list[int] = []
    for part in value.split(","):
        stripped = part.strip()
        if stripped:
            classes.append(int(stripped))
    return classes


def _open_capture(args: argparse.Namespace) -> cv2.VideoCapture:
    source = _parse_source(args.source)
    backend = args.backend

    if backend == "gstreamer":
        capture = cv2.VideoCapture(source, cv2.CAP_GSTREAMER)
    elif backend == "v4l2":
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    elif backend == "ffmpeg":
        capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    elif isinstance(source, int):
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    else:
        capture = cv2.VideoCapture(source)

    if isinstance(source, int):
        if args.width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        if args.height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        if args.fps > 0:
            capture.set(cv2.CAP_PROP_FPS, args.fps)

    return capture


def _draw_overlay(frame, lines: Iterable[str]) -> None:
    y = 24
    for line in lines:
        cv2.putText(
            frame,
            line,
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            line,
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        y += 24


def _opencv_gui_available() -> bool:
    try:
        build_info = cv2.getBuildInformation()
    except Exception:
        return False

    for line in build_info.splitlines():
        if line.strip().startswith("GUI:"):
            return "NONE" not in line.upper()
    return False


def _resolve_display_backend(args: argparse.Namespace) -> str:
    if args.no_display:
        return "none"
    if args.display_backend != "auto":
        if args.display_backend == "ffplay" and not shutil.which("ffplay"):
            LOGGER.error("ffplay was requested but was not found in PATH.")
            return "none"
        return args.display_backend
    if _opencv_gui_available():
        return "opencv"
    if shutil.which("ffplay"):
        LOGGER.warning("OpenCV GUI backend is unavailable; using ffplay display backend.")
        return "ffplay"
    LOGGER.warning(
        "OpenCV GUI backend is unavailable and ffplay was not found; disabling live display."
    )
    return "none"


class PreviewDisplay:
    def __init__(self, backend: str, window_name: str, fps: float):
        self.backend = backend
        self.window_name = window_name
        self.fps = max(fps, 1.0)
        self._ffplay: subprocess.Popen[bytes] | None = None
        self._frame_shape: tuple[int, int, int] | None = None

    def show(self, frame) -> bool:
        if self.backend == "none":
            return True
        if self.backend == "opencv":
            return self._show_opencv(frame)
        if self.backend == "ffplay":
            return self._show_ffplay(frame)
        raise RuntimeError(f"Unsupported display backend: {self.backend}")

    def close(self) -> None:
        if self._ffplay is not None:
            if self._ffplay.stdin is not None:
                try:
                    self._ffplay.stdin.close()
                except OSError:
                    pass
            try:
                self._ffplay.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._ffplay.terminate()
            self._ffplay = None

        if self.backend == "opencv":
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass

    def _show_opencv(self, frame) -> bool:
        try:
            cv2.imshow(self.window_name, frame)
            key = cv2.waitKey(1) & 0xFF
            return key not in (27, ord("q"))
        except cv2.error as exc:
            LOGGER.error("OpenCV display failed: %s", exc)
            return False

    def _show_ffplay(self, frame) -> bool:
        if self._ffplay is None:
            self._start_ffplay(frame.shape)

        if self._frame_shape != frame.shape:
            LOGGER.error(
                "Frame size changed from %s to %s; ffplay rawvideo cannot resize mid-stream.",
                self._frame_shape,
                frame.shape,
            )
            return False

        if self._ffplay is None or self._ffplay.stdin is None:
            return False
        if self._ffplay.poll() is not None:
            LOGGER.info("ffplay window closed")
            return False

        try:
            if not frame.flags["C_CONTIGUOUS"]:
                frame = frame.copy()
            self._ffplay.stdin.write(frame.tobytes())
            self._ffplay.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            LOGGER.info("ffplay pipe closed")
            return False

    def _start_ffplay(self, frame_shape) -> None:
        height, width = frame_shape[:2]
        self._frame_shape = frame_shape
        command = [
            "ffplay",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-framedrop",
            "-sync",
            "ext",
            "-window_title",
            self.window_name,
            "-f",
            "rawvideo",
            "-pixel_format",
            "bgr24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            f"{self.fps:g}",
            "-i",
            "-",
        ]
        LOGGER.info("starting ffplay display width=%s height=%s fps=%s", width, height, self.fps)
        try:
            self._ffplay = subprocess.Popen(command, stdin=subprocess.PIPE)
        except FileNotFoundError:
            LOGGER.error("ffplay was not found in PATH.")
            self._ffplay = None


def _create_writer(path: str, fps: float, frame_shape) -> cv2.VideoWriter:
    height, width = frame_shape[:2]
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not open video writer: {output}")
    return writer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview a TensorRT .engine model on a live Jetson camera feed.",
    )
    parser.add_argument(
        "--model",
        default="models/set9_doorfas_0323_imbal.engine",
        help="Path to TensorRT .engine file.",
    )
    parser.add_argument(
        "--source", default="0", help="Camera index, video path, RTSP URL, or GStreamer pipeline."
    )
    parser.add_argument(
        "--backend", choices=["auto", "v4l2", "gstreamer", "ffmpeg"], default="auto"
    )
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--imgsz", type=int, default=480)
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--device", default="0")
    parser.add_argument("--max-det", type=int, default=20)
    parser.add_argument(
        "--classes", default=None, help="Optional comma-separated YOLO class IDs."
    )
    parser.add_argument(
        "--crop-width",
        type=int,
        default=480,
        help="Match service inference crop width; 0 disables crop.",
    )
    parser.add_argument("--window-name", default="CRK TensorRT Preview")
    parser.add_argument(
        "--record", default=None, help="Optional MP4 output path for annotated preview."
    )
    parser.add_argument(
        "--display-backend", choices=["auto", "opencv", "ffplay", "none"], default="auto"
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Alias for --display-backend none; useful with --record.",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

    model_path = Path(args.model)
    if not model_path.is_absolute():
        model_path = ROOT_DIR / model_path
    if model_path.suffix != ".engine":
        LOGGER.error("This preview tool is for TensorRT .engine files, got: %s", model_path)
        return 2
    if not model_path.exists():
        LOGGER.error("TensorRT engine not found: %s", model_path)
        return 2

    # Heavy imports are deferred past argparse/path validation so `--help`
    # (and early argument errors) work without cv2/ultralytics installed
    # (e.g. when sanity-checking the CLI off-device).
    global cv2, YOLO
    import cv2  # noqa: PLC0415
    from ultralytics import YOLO  # noqa: PLC0415

    LOGGER.info(
        "loading TensorRT engine path=%s device=%s imgsz=%s", model_path, args.device, args.imgsz
    )
    model = YOLO(str(model_path))
    LOGGER.info("engine loaded classes=%s", len(getattr(model, "names", {}) or {}))

    capture = _open_capture(args)
    if not capture.isOpened():
        LOGGER.error(
            "camera/video source could not be opened source=%s backend=%s",
            args.source,
            args.backend,
        )
        return 2

    class_filter = _parse_classes(args.classes)
    writer = None
    display_backend = _resolve_display_backend(args)
    display = PreviewDisplay(display_backend, args.window_name, args.fps)
    if display_backend == "opencv":
        quit_hint = "press q or ESC to quit"
    elif display_backend == "ffplay":
        quit_hint = "close ffplay window or Ctrl+C to quit"
    else:
        quit_hint = "display disabled; Ctrl+C to quit"
    fps_ema = 0.0
    frame_count = 0

    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                LOGGER.warning("source returned no frame after %s frames", frame_count)
                break

            if args.crop_width > 0 and frame.shape[1] > args.crop_width:
                infer_frame = frame[:, : args.crop_width]
            else:
                infer_frame = frame

            start = time.perf_counter()
            results = model.predict(
                infer_frame,
                imgsz=args.imgsz,
                conf=args.conf,
                device=args.device,
                half=True,
                max_det=args.max_det,
                classes=class_filter,
                verbose=False,
            )
            elapsed = max(time.perf_counter() - start, 1e-6)
            current_fps = 1.0 / elapsed
            fps_ema = current_fps if fps_ema == 0.0 else (fps_ema * 0.9 + current_fps * 0.1)

            result = results[0]
            annotated = result.plot()
            detections = len(result.boxes) if getattr(result, "boxes", None) is not None else 0
            _draw_overlay(
                annotated,
                [
                    f"model={model_path.name}",
                    f"source={args.source} detections={detections} fps={fps_ema:.1f}",
                    quit_hint,
                ],
            )

            if writer is None and args.record:
                writer = _create_writer(args.record, max(args.fps, 1.0), annotated.shape)
            if writer is not None:
                writer.write(annotated)

            if not display.show(annotated):
                break

            frame_count += 1
    finally:
        capture.release()
        if writer is not None:
            writer.release()
        display.close()

    LOGGER.info("preview stopped frames=%s", frame_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
