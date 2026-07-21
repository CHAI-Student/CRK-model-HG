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
import glob
import logging
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

LOGGER = logging.getLogger("live_engine_preview")


def _build_csi_pipeline(sensor_id: int, width: int, height: int, fps: float) -> str:
    width = width if width > 0 else 1280
    height = height if height > 0 else 720
    fps = fps if fps > 0 else 30.0
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! "
        f"video/x-raw(memory:NVMM),width={width},height={height},framerate={fps:g}/1 ! "
        "nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! "
        "appsink drop=1"
    )


def _parse_source(
    value: str, width: int = 0, height: int = 0, fps: float = 0.0
) -> tuple[int | str, str | None]:
    """Parse the --source value.

    Returns a (source, forced_backend) tuple. forced_backend is None unless the
    source syntax implies a specific capture backend (e.g. csi:/gst: always
    need CAP_GSTREAMER regardless of --backend).
    """
    text = value.strip()

    if text.startswith("csi:"):
        sensor_id_text = text[len("csi:") :].strip()
        sensor_id = int(sensor_id_text) if sensor_id_text else 0
        return _build_csi_pipeline(sensor_id, width, height, fps), "gstreamer"

    if text.startswith("gst:"):
        return text[len("gst:") :], "gstreamer"

    if text.startswith("/dev/video"):
        return text, None

    if text.isdigit():
        return int(text), None

    return text, None


def _parse_classes(value: str | None) -> list[int] | None:
    if value is None or value.strip() == "":
        return None
    classes: list[int] = []
    for part in value.split(","):
        stripped = part.strip()
        if stripped:
            classes.append(int(stripped))
    return classes


def _list_video_devices() -> list[str]:
    return sorted(glob.glob("/dev/video*"))


def _run_v4l2_ctl_list_devices() -> str | None:
    if not shutil.which("v4l2-ctl"):
        return None
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(v4l2-ctl --list-devices failed to run: {exc})"
    output = (result.stdout or "") + (result.stderr or "")
    return output.strip() or "(v4l2-ctl --list-devices returned no output)"


def _find_device_holders(device: str) -> str | None:
    """Return a human-readable description of processes holding `device` open.

    Tries `fuser` first, then falls back to `lsof`. Returns None if neither
    tool is available (caller should treat that as "skipped", not "free").
    """
    if shutil.which("fuser"):
        try:
            result = subprocess.run(
                ["fuser", device],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        pids = (result.stdout or result.stderr or "").strip()
        return pids or ""

    if shutil.which("lsof"):
        try:
            result = subprocess.run(
                ["lsof", "-t", device],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        pids = (result.stdout or "").strip().replace("\n", " ")
        return pids or ""

    return None


def _run_diagnostics(log: logging.Logger | None = None) -> list[str]:
    """Inspect /dev/video* devices and report what is holding them open.

    Runs without any heavy (cv2/ultralytics) imports so it is usable both as
    `--list-devices` before model load and as an automatic post-mortem after
    a failed capture open. Returns the discovered device paths.
    """
    log = log or LOGGER
    devices = _list_video_devices()

    if not devices:
        log.error(
            "no /dev/video* devices found — if this is a CSI camera "
            "(e.g. Jetson onboard camera), it cannot be opened by V4L2 index/path. "
            "Use the nvarguscamerasrc GStreamer pipeline instead, e.g. "
            "--source csi:0 (see README troubleshooting section)."
        )
        return devices

    log.info("found /dev/video* devices: %s", ", ".join(devices))
    log.info(
        "hint: USB cameras usually expose 2 device nodes per camera "
        "(one for video capture, one for metadata) — the odd-numbered node "
        "(e.g. /dev/video1) is often the metadata node and cannot be opened "
        "as a capture source."
    )

    v4l2_ctl_output = _run_v4l2_ctl_list_devices()
    if v4l2_ctl_output is None:
        log.info("v4l2-ctl not found in PATH; skipping `v4l2-ctl --list-devices`.")
    else:
        log.info("v4l2-ctl --list-devices:\n%s", v4l2_ctl_output)

    any_holder_tool = shutil.which("fuser") or shutil.which("lsof")
    if not any_holder_tool:
        log.info("neither fuser nor lsof found in PATH; skipping device-occupancy check.")
    else:
        for device in devices:
            holders = _find_device_holders(device)
            if holders is None:
                continue
            if holders:
                log.warning(
                    "%s is held open by pid(s): %s — "
                    "다른 프로세스(카메라 캡처 서비스로 추정)가 장치를 점유 중 — "
                    "프리뷰 전에 해당 서비스를 중지하거나 사용하지 않는 장치를 지정하세요.",
                    device,
                    holders,
                )
            else:
                log.info("%s is not currently held open by any process.", device)

    return devices


def _print_open_failure_help(args: argparse.Namespace) -> None:
    LOGGER.error(
        "camera/video source could not be opened source=%s backend=%s",
        args.source,
        args.backend,
    )
    devices = _run_diagnostics()

    model_hint = args.model
    example_lines = []
    if devices:
        first = devices[0]
        example_lines.append(
            f"  python scripts/live_engine_preview.py --model {model_hint} "
            f"--source {first} --backend v4l2"
        )
    else:
        example_lines.append(
            f"  python scripts/live_engine_preview.py --model {model_hint} "
            "--source /dev/video1 --backend v4l2"
        )
    example_lines.append(
        f"  python scripts/live_engine_preview.py --model {model_hint} --source csi:0"
    )
    example_lines.append(
        f"  python scripts/live_engine_preview.py --model {model_hint} "
        "--source 'gst:<custom gstreamer pipeline>'"
    )
    LOGGER.error("try one of the following commands:\n%s", "\n".join(example_lines))


def _open_capture(args: argparse.Namespace) -> cv2.VideoCapture:
    source, forced_backend = _parse_source(args.source, args.width, args.height, args.fps)
    backend = forced_backend or args.backend

    if backend == "gstreamer":
        capture = cv2.VideoCapture(source, cv2.CAP_GSTREAMER)
    elif backend == "v4l2":
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    elif backend == "ffmpeg":
        capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    elif isinstance(source, int):
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    elif isinstance(source, str) and source.startswith("/dev/video"):
        capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
    else:
        capture = cv2.VideoCapture(source)

    if isinstance(source, int) or (isinstance(source, str) and source.startswith("/dev/video")):
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
        "--source",
        default="0",
        help=(
            "Camera index (e.g. 0), /dev/videoN path, video file path, RTSP URL, "
            "csi:N for a Jetson CSI sensor (nvarguscamerasrc), or gst:<pipeline> "
            "for a custom GStreamer pipeline."
        ),
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
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help=(
            "Run camera preflight diagnostics (list /dev/video* devices, "
            "v4l2-ctl info, and which processes hold them open), then exit "
            "without loading the model. Does not require cv2/ultralytics."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

    if args.list_devices:
        # Preflight diagnostics only: no cv2/ultralytics import, no model load.
        devices = _run_diagnostics()
        return 0 if devices else 1

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
        _print_open_failure_help(args)
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
