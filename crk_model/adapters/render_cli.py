"""render-session — 아카이브 bbox 기록을 AVI 위에 오버레이한 검증 영상 생성.

실기 결제(세션)가 끝난 뒤 "모델이 실제로 어디를 잡았는지"를 눈으로 확인하는
도구다. 전제: 세션이 `MODEL__SESSION__SAVE_DETECTIONS=1`로 기록됐어야 한다
(trace.frame_detections — 추론 프레임별 raw 검출 + 필터 통과 여부). live
preview와 달리 **운영 파이프라인이 그 순간 실제로 본 검출**을 그대로 재생한다
— 오프라인 재추론은 TensorRT FP16 엔진과 결과가 달라 여기서는 하지 않는다.

사용 예 (Jetson 또는 아카이브+AVI를 내려받은 개발 PC):

    render-session --latest                       # 가장 최근 세션 전체
    render-session ses-3-1784788285 --trigger 0   # 특정 트리거만
    render-session --latest --format jpg          # 프레임 JPEG로
    render-session --latest --map /home/crk/videos=./videos   # 경로 이식

오버레이 규칙:
- 아카이브에는 **판정 로직에 실제 기여한 검출만** 담긴다 (필터 체인 통과 ∧
  투표 진입 conf 이상 — 파이프라인 기록 시점에 걸러짐). 렌더는 그것만 그린다
  (구 스키마의 kept=False 항목은 무시).
- 상품: 클래스별 고정 색 실선 + "id conf" 라벨 / hand: 흰색
- 캔버스 여백: 카메라 480×480 프레임을 여백 있는 캔버스 가운데 두고, 실험
  정보 헤더는 프레임 **밖** 상단 밴드에 그린다 — bbox 라벨이 프레임 경계를
  넘어도 여백에서 보이고, 헤더가 bbox 라벨을 가리지 않는다. 프레임 경계는
  회색 테두리로 표시.
- 헤더: 세션/트리거/존/delta/판정/카메라, 프레임별 POS + INFER n 또는 SKIP
  (기록이 없는 프레임 = 모션 게이트 스킵 또는 조기 종료 이후)
- 크롭 기하: 아카이브의 camera_crops 스탬프(냉장 side=left 등)를 읽어 기록
  당시와 동일한 크롭으로 디코드한다 — 좌표계 무변환 계약의 유지 장치.

구현 제약: 이 레포는 런타임 의존성 0 — cv2를 쓰지 않는다. 디코드는
adapters.avi_frames.decode_avi(운영과 동일 기하), 그리기는 numpy 슬라이싱
+ 내장 5×7 비트맵 폰트, 인코드는 ffmpeg 파이프(rawvideo → mp4/jpg).
numpy+ffmpeg 필요.
"""
from __future__ import annotations

import argparse
import colorsys
import os
import shutil
import subprocess
import sys
from pathlib import Path

from crk_model.adapters.avi_frames import decode_avi
from crk_model.ledger.archive import SessionArchive, _load_document

FRAME_SIZE = 480
# 캔버스 레이아웃: 헤더 밴드(프레임 밖) + 프레임 주위 여백 — bbox 라벨이
# 카메라 프레임 경계를 넘어도 보이고, 헤더가 라벨을 가리지 않는다.
# 폭/높이는 짝수 유지 (libx264 yuv420p 제약).
HEADER_H = 80  # 헤더 4줄 × 18px + 패딩
LABEL_GAP = 24  # 헤더와 프레임 사이 — 상단 bbox 라벨(y-16)이 들어가는 띠
MARGIN = 40  # 좌/우/하단 여백
CANVAS_W = MARGIN + FRAME_SIZE + MARGIN  # 560
CANVAS_H = HEADER_H + LABEL_GAP + FRAME_SIZE + MARGIN  # 624
FRAME_X = MARGIN  # 캔버스 내 프레임 원점
FRAME_Y = HEADER_H + LABEL_GAP
_CANVAS_BG = (24, 24, 24)
_FRAME_BORDER = (90, 90, 90)

# ---------------------------------------------------------------------------
# 5×7 비트맵 폰트 (uppercase — _text가 upper()로 정규화한다)
# ---------------------------------------------------------------------------
_GLYPHS_RAW = {
    "0": ".XXX. X...X X..XX X.X.X XX..X X...X .XXX.",
    "1": "..X.. .XX.. ..X.. ..X.. ..X.. ..X.. .XXX.",
    "2": ".XXX. X...X ....X ...X. ..X.. .X... XXXXX",
    "3": ".XXX. X...X ....X ..XX. ....X X...X .XXX.",
    "4": "...X. ..XX. .X.X. X..X. XXXXX ...X. ...X.",
    "5": "XXXXX X.... XXXX. ....X ....X X...X .XXX.",
    "6": ".XXX. X.... X.... XXXX. X...X X...X .XXX.",
    "7": "XXXXX ....X ...X. ..X.. .X... .X... .X...",
    "8": ".XXX. X...X X...X .XXX. X...X X...X .XXX.",
    "9": ".XXX. X...X X...X .XXXX ....X ....X .XXX.",
    "A": ".XXX. X...X X...X XXXXX X...X X...X X...X",
    "B": "XXXX. X...X X...X XXXX. X...X X...X XXXX.",
    "C": ".XXX. X...X X.... X.... X.... X...X .XXX.",
    "D": "XXXX. X...X X...X X...X X...X X...X XXXX.",
    "E": "XXXXX X.... X.... XXXX. X.... X.... XXXXX",
    "F": "XXXXX X.... X.... XXXX. X.... X.... X....",
    "G": ".XXX. X...X X.... X.XXX X...X X...X .XXX.",
    "H": "X...X X...X X...X XXXXX X...X X...X X...X",
    "I": ".XXX. ..X.. ..X.. ..X.. ..X.. ..X.. .XXX.",
    "J": "..XXX ...X. ...X. ...X. ...X. X..X. .XX..",
    "K": "X...X X..X. X.X.. XX... X.X.. X..X. X...X",
    "L": "X.... X.... X.... X.... X.... X.... XXXXX",
    "M": "X...X XX.XX X.X.X X.X.X X...X X...X X...X",
    "N": "X...X XX..X X.X.X X..XX X...X X...X X...X",
    "O": ".XXX. X...X X...X X...X X...X X...X .XXX.",
    "P": "XXXX. X...X X...X XXXX. X.... X.... X....",
    "Q": ".XXX. X...X X...X X...X X.X.X X..X. .XX.X",
    "R": "XXXX. X...X X...X XXXX. X.X.. X..X. X...X",
    "S": ".XXXX X.... X.... .XXX. ....X ....X XXXX.",
    "T": "XXXXX ..X.. ..X.. ..X.. ..X.. ..X.. ..X..",
    "U": "X...X X...X X...X X...X X...X X...X .XXX.",
    "V": "X...X X...X X...X X...X X...X .X.X. ..X..",
    "W": "X...X X...X X...X X.X.X X.X.X XX.XX X...X",
    "X": "X...X X...X .X.X. ..X.. .X.X. X...X X...X",
    "Y": "X...X X...X .X.X. ..X.. ..X.. ..X.. ..X..",
    "Z": "XXXXX ....X ...X. ..X.. .X... X.... XXXXX",
    ".": "..... ..... ..... ..... ..... .XX.. .XX..",
    ",": "..... ..... ..... ..... .XX.. ..X.. .X...",
    ":": "..... .XX.. .XX.. ..... .XX.. .XX.. .....",
    "-": "..... ..... ..... XXXXX ..... ..... .....",
    "+": "..... ..X.. ..X.. XXXXX ..X.. ..X.. .....",
    "=": "..... ..... XXXXX ..... XXXXX ..... .....",
    "/": "....X ....X ...X. ..X.. .X... X.... X....",
    "(": "...X. ..X.. .X... .X... .X... ..X.. ...X.",
    ")": ".X... ..X.. ...X. ...X. ...X. ..X.. .X...",
    "[": ".XXX. .X... .X... .X... .X... .X... .XXX.",
    "]": ".XXX. ...X. ...X. ...X. ...X. ...X. .XXX.",
    "%": "XX..X XX..X ...X. ..X.. .X... X..XX X..XX",
    "#": ".X.X. .X.X. XXXXX .X.X. XXXXX .X.X. .X.X.",
    "?": ".XXX. X...X ....X ...X. ..X.. ..... ..X..",
    "_": "..... ..... ..... ..... ..... ..... XXXXX",
    " ": "..... ..... ..... ..... ..... ..... .....",
}


_GLYPH_CACHE: dict = {}  # (char, scale) -> 스케일 적용된 bool 마스크


def _glyph_mask(ch: str, scale: int):
    """스케일 포함 글리프 캐시 — 프레임×글자마다 np.kron을 재계산하던 것이
    render 프로파일 hot spot이었다 (2026-07-29 실측, _compose_frame 11ms/frame
    의 구성 요소). 문자 집합이 작아(≤52 × scale 종류) 캐시가 즉시 포화된다."""
    import numpy as np  # lazy

    key = (ch, scale)
    mask = _GLYPH_CACHE.get(key)
    if mask is None:
        rows = _GLYPHS_RAW.get(ch) or _GLYPHS_RAW["?"]
        base = np.array(
            [[c == "X" for c in row] for row in rows.split()], dtype=bool
        )
        mask = np.kron(base, np.ones((scale, scale), dtype=bool))
        _GLYPH_CACHE[key] = mask
    return mask


def _text(img, x: int, y: int, s: str, color, scale: int = 2) -> None:
    """(x, y)에서 시작하는 텍스트 스탬프 — 프레임 밖은 잘라낸다."""
    h, w = img.shape[:2]
    cx = x
    for ch in s.upper():
        mask = _glyph_mask(ch, scale)
        gh, gw = mask.shape
        x0, y0 = max(cx, 0), max(y, 0)
        x1, y1 = min(cx + gw, w), min(y + gh, h)
        if x1 > x0 and y1 > y0:
            sub = mask[y0 - y : y1 - y, x0 - cx : x1 - cx]
            img[y0:y1, x0:x1][sub] = color
        cx += gw + scale  # 자간 = scale px
        if cx >= w:
            break


def _rect(img, bbox, color, thickness: int = 2) -> None:
    """bbox=(x1,y1,x2,y2) 외곽선 — 좌표는 프레임 안으로 클램프."""
    h, w = img.shape[:2]
    x1 = int(max(0, min(bbox[0], w - 1)))
    y1 = int(max(0, min(bbox[1], h - 1)))
    x2 = int(max(0, min(bbox[2], w - 1)))
    y2 = int(max(0, min(bbox[3], h - 1)))
    if x2 <= x1 or y2 <= y1:
        return
    t = thickness
    img[y1 : y1 + t, x1 : x2 + 1] = color
    img[max(y2 - t + 1, 0) : y2 + 1, x1 : x2 + 1] = color
    img[y1 : y2 + 1, x1 : x1 + t] = color
    img[y1 : y2 + 1, max(x2 - t + 1, 0) : x2 + 1] = color


def _fill(img, x1: int, y1: int, x2: int, y2: int, color) -> None:
    h, w = img.shape[:2]
    img[max(y1, 0) : min(y2, h), max(x1, 0) : min(x2, w)] = color


def _class_color(class_id: int) -> tuple[int, int, int]:
    """클래스별 고정 색 (BGR) — 황금각 hue 순환으로 인접 id도 확연히 다르게."""
    hue = (class_id * 0.618033988749895) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
    return (int(b * 255), int(g * 255), int(r * 255))


_HAND_COLOR = (255, 255, 255)  # 흰색 (BGR)
_TEXT_BG = (0, 0, 0)


# ---------------------------------------------------------------------------
# ffmpeg 출력 싱크 (rawvideo bgr24 파이프 → mp4 또는 numbered jpg)
# ---------------------------------------------------------------------------
_codec_cache: str | None = None


def _pick_codec() -> str:
    """mp4 인코더 선택 — libx264(표준) 없으면 mpeg4 폴백. 1회 캐시."""
    global _codec_cache
    if _codec_cache is None:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, timeout=10, text=True,
            ).stdout
        except Exception:
            out = ""
        _codec_cache = "libx264" if "libx264" in out else "mpeg4"
    return _codec_cache


class FfmpegSink:
    """프레임(bgr24 ndarray)을 받아 mp4 1개 또는 jpg 시퀀스로 쓴다."""

    def __init__(
        self,
        dest: Path,
        fmt: str,
        fps: float,
        width: int = CANVAS_W,
        height: int = CANVAS_H,
    ):
        self._dest = dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-framerate", f"{fps}",
            "-i", "-",
        ]
        if fmt == "mp4":
            codec = _pick_codec()
            cmd.extend(["-c:v", codec])
            if codec == "libx264":
                cmd.extend(["-pix_fmt", "yuv420p", "-crf", "23"])
            cmd.append(str(dest))
        else:  # jpg — dest는 디렉토리, 내부에 %05d.jpg
            dest.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-q:v", "2", str(dest / "%05d.jpg")])
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def write(self, frame) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(frame.tobytes())

    def close(self) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.close()
        except OSError:
            pass  # ffmpeg가 먼저 죽은 경우 — 아래 rc 검사가 실패를 보고한다
        rc = self._proc.wait(timeout=60)
        if rc != 0:
            err = b""
            if self._proc.stderr is not None:
                err = self._proc.stderr.read()
            raise OSError(
                f"ffmpeg encode failed (rc={rc}): "
                f"{err.decode('utf-8', errors='replace')[-240:]}"
            )

    def abort(self) -> None:
        """디코드 실패 등으로 출력이 불완전할 때 — 조용히 프로세스만 정리."""
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.wait()


def _probe_fps(path: str) -> float | None:
    """소스 AVI의 fps (ffprobe) — 실패 시 None(호출측 기본값)."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, timeout=10, text=True,
        ).stdout.strip()
        num, _, den = out.partition("/")
        fps = float(num) / float(den or 1)
        return fps if fps > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 오버레이 렌더
# ---------------------------------------------------------------------------
def _judgment_line(trigger: dict) -> str:
    j = trigger.get("judgment") or {}
    products = ",".join(
        f"{p.get('class_id')}x{p.get('count')}" for p in j.get("products") or []
    )
    line = f"{j.get('status', '?')}"
    if products:
        line += f" {products}"
    return line


def _influential(record: dict | None) -> list[dict]:
    """레코드의 판정 기여 검출 목록 — 신 스키마는 전부, 구 스키마(kept 병기)
    는 kept=False(필터/진입 컷 탈락분)를 제외한다."""
    return [
        d for d in (record or {}).get("detections", ())
        if d.get("kept") is not False
    ]


def _canvas_template(header: list[str]):
    """트리거×카메라당 1회 생성하는 캔버스 템플릿 — 배경·프레임 테두리·
    **정적 헤더**(세션/판정/카메라 줄)를 미리 그려 둔다. 프레임마다 바뀌는
    것은 카메라 프레임·bbox·상태줄뿐이므로 per-frame 작업이 template.copy()
    + 소량 스탬프로 줄어든다 (실측 11ms/frame의 지배 요인 제거).

    반환: (template, status_y) — status_y는 프레임별 상태줄의 y 오프셋."""
    import numpy as np  # lazy

    canvas = np.empty((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    canvas[:, :] = _CANVAS_BG
    # 카메라 프레임 경계 테두리 — 여백과 실제 시야의 구분선
    _rect(
        canvas,
        (FRAME_X - 2, FRAME_Y - 2, FRAME_X + FRAME_SIZE + 1, FRAME_Y + FRAME_SIZE + 1),
        _FRAME_BORDER,
        thickness=1,
    )
    y = 4
    for line in header:
        _text(canvas, 3, y, line, (0, 255, 255), scale=2)
        y += 18
    return canvas, y


def _compose_frame(template, status_y: int, frame, record: dict | None, pos: int):
    """카메라 프레임 1장을 템플릿 위에 얹고 bbox + 상태줄을 그린다.

    헤더는 프레임 밖 상단 밴드(HEADER_H, 템플릿에 사전 렌더), 프레임은
    (FRAME_X, FRAME_Y) — bbox 라벨(y-16)이 프레임 위 경계를 넘으면
    LABEL_GAP 띠에 그려져 잘리지도, 헤더를 가리지도 않는다.
    record=None이면 미추론 프레임."""
    canvas = template.copy()
    canvas[FRAME_Y : FRAME_Y + FRAME_SIZE, FRAME_X : FRAME_X + FRAME_SIZE] = frame

    for d in _influential(record):
        bbox = d.get("bbox") or (0, 0, 0, 0)
        if tuple(bbox) == (0, 0, 0, 0):
            continue  # 공간 정보 없는 검출 (테스트 더미 등)
        cb = (
            FRAME_X + bbox[0], FRAME_Y + bbox[1],
            FRAME_X + bbox[2], FRAME_Y + bbox[3],
        )
        if d.get("hand"):
            color = _HAND_COLOR
            label = f"HAND {d.get('conf', 0):.2f}"
        else:
            color = _class_color(int(d.get("class_id", 0)))
            label = f"{d.get('class_id')} {d.get('conf', 0):.2f}"
        _rect(canvas, cb, color, thickness=2)
        # 라벨은 박스 위 — 여백 덕에 프레임 밖으로 나가도 보인다. 헤더
        # 밴드 아래로는 클램프해 실험 정보와 절대 겹치지 않는다.
        lx = int(cb[0])
        ly = max(int(cb[1]) - 18, HEADER_H + 2)
        _fill(canvas, lx, ly, lx + 12 * len(label) + 4, ly + 16, _TEXT_BG)
        _text(canvas, lx + 2, ly + 1, label, color, scale=2)

    if record is None:
        status = f"POS {pos:03d} SKIP"
        color = (160, 160, 160)
    else:
        status = f"POS {pos:03d} INFER {len(_influential(record))}"
        color = (0, 255, 0)
    _text(canvas, 3, status_y, status, color, scale=2)
    return canvas


def _remap(path: str, mappings: list[tuple[str, str]]) -> str:
    """--map OLD=NEW 접두사 치환 (기기 경로 → 로컬 경로 이식용)."""
    for old, new in mappings:
        if path.startswith(old):
            return new + path[len(old) :]
    return path


def render_trigger(
    trigger: dict,
    index: int,
    session_id: str,
    out_dir: Path,
    fmt: str,
    fps: float,
    mappings: list[tuple[str, str]],
    cameras: list[str] | None,
) -> list[Path]:
    """트리거 1건의 카메라별 오버레이 영상 생성 — 출력 경로 목록 반환."""
    trace = trigger.get("trace") or {}
    records = trace.get("frame_detections")
    if records is None:
        print(
            f"  [trig {index}] frame_detections 없음 — "
            "MODEL__SESSION__SAVE_DETECTIONS=1로 기록된 세션이 아닙니다",
            file=sys.stderr,
        )
        return []
    # 좌표계 계약: 기록 당시의 크롭 원점(냉장 side=left 등)으로 디코드해야
    # bbox가 맞는다. 스탬프 없는 구 아카이브는 center (구 동작).
    crops = trace.get("camera_crops") or {}
    by_camera: dict[str, dict[int, dict]] = {}
    for r in records:
        by_camera.setdefault(r["camera"], {})[int(r["pos"])] = r

    header_base = [
        f"{session_id} T{index} Z{trigger.get('zone')} "
        f"D{trigger.get('delta_weight', 0):+.1f}G",
        _judgment_line(trigger),
    ]
    outputs: list[Path] = []
    video_paths: dict = trigger.get("video_paths") or {}
    for camera, raw_path in sorted(video_paths.items()):
        if cameras and camera not in cameras:
            continue
        path = _remap(raw_path, mappings)
        if not Path(path).exists():
            print(f"  [trig {index}/{camera}] 영상 없음: {path}", file=sys.stderr)
            continue
        cam_fps = fps or _probe_fps(path) or 20.0
        dest = out_dir / f"trig{index}_{camera}" if fmt == "jpg" else (
            out_dir / f"trig{index}_{camera}.mp4"
        )
        sink = FfmpegSink(dest, fmt, cam_fps)
        crop = crops.get(camera, "center")
        template, status_y = _canvas_template(
            header_base + [f"CAM {camera} ({crop})"]
        )
        ok = False
        try:
            for pos, bundle in enumerate(decode_avi(path, crop=crop)):
                canvas = _compose_frame(
                    template, status_y, bundle.full,
                    by_camera.get(camera, {}).get(pos), pos,
                )
                sink.write(canvas)
            ok = True
        except OSError as exc:
            # 카메라 1개의 디코드/인코드 실패가 나머지 렌더를 막지 않는다
            print(f"  [trig {index}/{camera}] 실패: {exc}", file=sys.stderr)
        finally:
            if ok:
                sink.close()
            else:
                sink.abort()
        if ok:
            outputs.append(dest)
            print(f"  [trig {index}/{camera}] -> {dest}")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render-session",
        description="세션 아카이브의 bbox 기록을 AVI 위에 오버레이한 검증 영상 생성",
    )
    parser.add_argument("session_id", nargs="?", help="세션 id (예: ses-3-1784788285)")
    parser.add_argument(
        "--latest", action="store_true", help="가장 최근 세션 (실험 직후용)"
    )
    parser.add_argument(
        "--dir", default="data/sessions", help="아카이브 루트 (기본: data/sessions)"
    )
    parser.add_argument(
        "--out", default="data/render", help="출력 루트 (기본: data/render/<세션id>/)"
    )
    parser.add_argument(
        "--format", choices=("mp4", "jpg"), default="mp4",
        help="mp4 영상(기본) 또는 프레임별 jpg",
    )
    parser.add_argument(
        "--fps", type=float, default=0.0,
        help="출력 fps (기본 0 = 소스 AVI에서 probe, 실패 시 20)",
    )
    parser.add_argument(
        "--trigger", type=int, action="append", default=[],
        help="렌더할 트리거 인덱스 (반복 지정, 기본: 전체)",
    )
    parser.add_argument(
        "--camera", action="append", default=[],
        help="렌더할 카메라 (top/side, 반복 지정, 기본: 전체)",
    )
    parser.add_argument(
        "--map", action="append", default=[], metavar="OLD=NEW",
        help="video_paths 접두사 치환 (기기 경로를 로컬 경로로, 반복 지정)",
    )
    args = parser.parse_args(argv)

    if bool(args.session_id) == args.latest:
        parser.error("session_id 또는 --latest 중 정확히 하나를 지정해야 합니다")
    if shutil.which("ffmpeg") is None:
        print("ffmpeg가 필요합니다 (인코드/디코드)", file=sys.stderr)
        return 1
    try:
        import numpy  # noqa: F401 — 그리기 경로 전제 조건 (lazy 확인)
    except ImportError:
        print("numpy가 필요합니다 (오버레이 그리기)", file=sys.stderr)
        return 1
    # 디코더 고정: avi_frames의 auto는 CUDA 불가 호스트에서 opencv를 고르는데
    # (Jetson 운영 폴백), 이 CLI는 ffmpeg가 어차피 필수라 cv2 없는 개발 PC
    # 에서도 ffmpeg CPU 디코드로 동작하게 기본을 ffmpeg로 둔다 (env 우선).
    os.environ.setdefault("MODEL__VIDEO__DECODER", "ffmpeg")
    mappings = []
    for m in args.map:
        old, sep, new = m.partition("=")
        if not sep:
            parser.error(f"--map 형식 오류 (OLD=NEW 필요): {m!r}")
        mappings.append((old, new))

    archive = SessionArchive(args.dir)
    if args.latest:
        path = archive.latest()
        if path is None:
            print(f"아카이브가 비어 있습니다: {args.dir}", file=sys.stderr)
            return 1
    else:
        path = archive.find(args.session_id)
        if path is None:
            print(
                f"세션 아카이브를 찾을 수 없습니다: {args.session_id} (dir={args.dir})",
                file=sys.stderr,
            )
            return 1
    doc = _load_document(path)
    session_id = doc.get("session_id") or path.stem
    out_dir = Path(args.out) / session_id
    triggers = doc.get("triggers") or []
    print(f"세션 {session_id}: 트리거 {len(triggers)}건 ({path})")

    outputs: list[Path] = []
    for i, trigger in enumerate(triggers):
        if args.trigger and i not in args.trigger:
            continue
        outputs.extend(
            render_trigger(
                trigger, i, session_id, out_dir, args.format,
                args.fps, mappings, args.camera or None,
            )
        )
    if not outputs:
        print("렌더된 출력이 없습니다", file=sys.stderr)
        return 1
    print(f"완료: {len(outputs)}개 출력 -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
