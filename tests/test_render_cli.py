"""render-session — 아카이브 bbox 기록의 오버레이 렌더 검증.

전제 조건이 없는 환경(ffmpeg/numpy 미설치)에서는 해당 시나리오만 skip
(test_frames_streaming과 동일 정책). 검증 항목:
1. 아카이브 문서 + AVI → 오버레이 mp4 생성 (end-to-end, --map 경로 이식 포함)
2. frame_detections 없는 세션은 명확한 안내와 함께 실패(rc=1)
3. 그리기 프리미티브(_rect/_text)가 실제로 픽셀을 찍는다
4. --map 접두사 치환 규칙
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from crk_model.adapters.render_cli import _remap, main

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
try:
    import numpy as np

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

pytestmark = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_NUMPY), reason="render-session은 ffmpeg+numpy 필요"
)


@pytest.fixture(autouse=True)
def _pin_decoder(monkeypatch):
    """main()의 os.environ.setdefault("MODEL__VIDEO__DECODER", ...)가 같은
    프로세스의 다른 테스트로 새지 않게 monkeypatch로 감싼다 (자동 복원)."""
    monkeypatch.setenv("MODEL__VIDEO__DECODER", "ffmpeg")


def _make_test_avi(path) -> str:
    """ffmpeg testsrc로 480x480 6프레임짜리 소형 avi를 만든다."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=size=480x480:rate=6:duration=1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


def _archive_doc(session_id, video_path, frame_detections):
    trace = {"yolo_calls": 2, "reason_codes": []}
    if frame_detections is not None:
        trace["frame_detections"] = frame_detections
    return {
        "session_id": session_id,
        "status": "finalized",
        "triggers": [
            {
                "ts": 1.0,
                "zone": 2,
                "delta_weight": -135.5,
                "status": "processed",
                "judgment": {
                    "status": "complete",
                    "products": [{"class_id": 27, "count": 1}],
                },
                "video_paths": {"top": video_path},
                "trace": trace,
            }
        ],
    }


def _write_archive(tmp_path, doc):
    date_dir = tmp_path / "sessions" / "2026-02-04"
    date_dir.mkdir(parents=True)
    p = date_dir / f"{doc['session_id']}.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return tmp_path / "sessions"


RECORDS = [
    {
        "camera": "top",
        "pos": 0,
        "detections": [
            {"class_id": 27, "conf": 0.86, "bbox": [50, 60, 120, 140], "hand": False,
             "kept": True},
            {"class_id": 13, "conf": 0.41, "bbox": [420, 60, 470, 140], "hand": False,
             "kept": False},
            {"class_id": 0, "conf": 0.9, "bbox": [200, 200, 300, 300], "hand": True,
             "kept": True},
        ],
    },
    {"camera": "top", "pos": 2, "detections": []},  # 추론했으나 무검출
]


class TestRenderEndToEnd:
    def test_renders_overlay_mp4(self, tmp_path, capsys):
        avi = _make_test_avi(tmp_path / "top.avi")
        sessions = _write_archive(
            tmp_path, _archive_doc("ses-r1", avi, RECORDS)
        )
        out = tmp_path / "render"
        rc = main(["ses-r1", "--dir", str(sessions), "--out", str(out)])
        assert rc == 0
        dest = out / "ses-r1" / "trig0_top.mp4"
        assert dest.exists() and dest.stat().st_size > 0

    def test_map_remaps_device_paths(self, tmp_path):
        _make_test_avi(tmp_path / "top.avi")
        # 아카이브에는 기기 경로가 박혀 있다 — --map으로 로컬 경로 이식
        sessions = _write_archive(
            tmp_path, _archive_doc("ses-r2", "/home/crk/videos/top.avi", RECORDS)
        )
        out = tmp_path / "render"
        rc = main([
            "--latest", "--dir", str(sessions), "--out", str(out),
            "--map", f"/home/crk/videos={tmp_path}",
        ])
        assert rc == 0
        assert (out / "ses-r2" / "trig0_top.mp4").exists()

    def test_jpg_format_writes_frames(self, tmp_path):
        avi = _make_test_avi(tmp_path / "top.avi")
        sessions = _write_archive(tmp_path, _archive_doc("ses-r3", avi, RECORDS))
        out = tmp_path / "render"
        rc = main(["ses-r3", "--dir", str(sessions), "--out", str(out),
                   "--format", "jpg"])
        assert rc == 0
        jpgs = list((out / "ses-r3" / "trig0_top").glob("*.jpg"))
        assert len(jpgs) >= 6

    def test_session_without_records_fails_with_guidance(self, tmp_path, capsys):
        avi = _make_test_avi(tmp_path / "top.avi")
        sessions = _write_archive(
            tmp_path, _archive_doc("ses-r4", avi, frame_detections=None)
        )
        rc = main(["ses-r4", "--dir", str(sessions), "--out", str(tmp_path / "o")])
        assert rc == 1
        assert "SAVE_DETECTIONS" in capsys.readouterr().err

    def test_missing_video_fails_cleanly(self, tmp_path, capsys):
        sessions = _write_archive(
            tmp_path, _archive_doc("ses-r5", "/nonexistent/top.avi", RECORDS)
        )
        rc = main(["ses-r5", "--dir", str(sessions), "--out", str(tmp_path / "o")])
        assert rc == 1
        assert "영상 없음" in capsys.readouterr().err


class TestDrawingPrimitives:
    def test_rect_and_text_stamp_pixels(self):
        from crk_model.adapters.render_cli import _rect, _text

        img = np.zeros((480, 480, 3), dtype=np.uint8)
        _rect(img, (50.0, 60.0, 120.0, 140.0), (0, 255, 0), thickness=2)
        assert img[60, 85].tolist() == [0, 255, 0]  # 상변
        assert img[100, 50].tolist() == [0, 255, 0]  # 좌변
        assert img[100, 85].tolist() == [0, 0, 0]  # 내부는 비어 있다 (외곽선만)

        before = int(img.sum())
        _text(img, 200, 200, "27 0.86", (255, 255, 255), scale=2)
        assert int(img.sum()) > before  # 글리프 픽셀이 실제로 찍혔다

    def test_rect_clamps_out_of_frame(self):
        from crk_model.adapters.render_cli import _rect

        img = np.zeros((480, 480, 3), dtype=np.uint8)
        _rect(img, (-20.0, -20.0, 700.0, 700.0), (255, 0, 0))  # 예외 없이 클램프
        assert img.sum() > 0


class TestRemap:
    def test_prefix_substitution(self):
        maps = [("/home/crk/videos", "/tmp/dl")]
        assert _remap("/home/crk/videos/z2/top.avi", maps) == "/tmp/dl/z2/top.avi"
        assert _remap("/other/top.avi", maps) == "/other/top.avi"
        assert _remap("/other/top.avi", []) == "/other/top.avi"
