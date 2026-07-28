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

from crk_model.adapters.render_cli import _influential, _remap, main

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


def _make_test_avi(path, size="480x480") -> str:
    """ffmpeg testsrc로 6프레임짜리 소형 avi를 만든다."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=size={size}:rate=6:duration=1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


def _archive_doc(session_id, video_path, frame_detections, camera_crops=None,
                 camera="top"):
    trace = {"yolo_calls": 2, "reason_codes": []}
    if frame_detections is not None:
        trace["frame_detections"] = frame_detections
    if camera_crops is not None:
        trace["camera_crops"] = camera_crops
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
                "video_paths": {camera: video_path},
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
            # 신 스키마: 판정 기여 검출만, kept 필드 없음
            {"class_id": 27, "conf": 0.86, "bbox": [50, 60, 120, 140], "hand": False},
            # 구 스키마 하위호환: kept=False(필터/진입 컷 탈락분)는 그리지 않는다
            {"class_id": 13, "conf": 0.41, "bbox": [420, 60, 470, 140], "hand": False,
             "kept": False},
            {"class_id": 0, "conf": 0.9, "bbox": [200, 200, 300, 300], "hand": True},
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

    def test_left_crop_stamp_applied(self, tmp_path):
        """냉장 side left-crop 세션: 아카이브 camera_crops 스탬프로 디코드."""
        avi = _make_test_avi(tmp_path / "side.avi", size="640x480")
        records = [{
            "camera": "side", "pos": 0,
            "detections": [
                {"class_id": 7, "conf": 0.9, "bbox": [10, 60, 90, 140], "hand": False},
            ],
        }]
        sessions = _write_archive(
            tmp_path,
            _archive_doc(
                "ses-r6", avi, records,
                camera_crops={"top": "center", "side": "left"}, camera="side",
            ),
        )
        out = tmp_path / "render"
        rc = main(["ses-r6", "--dir", str(sessions), "--out", str(out)])
        assert rc == 0
        assert (out / "ses-r6" / "trig0_side.mp4").exists()


class TestInfluentialFilter:
    def test_legacy_kept_false_is_skipped(self):
        rec = {"detections": [
            {"class_id": 1, "conf": 0.9, "bbox": [0, 0, 1, 1], "hand": False},
            {"class_id": 2, "conf": 0.4, "bbox": [0, 0, 1, 1], "hand": False,
             "kept": False},
            {"class_id": 3, "conf": 0.8, "bbox": [0, 0, 1, 1], "hand": False,
             "kept": True},
        ]}
        assert [d["class_id"] for d in _influential(rec)] == [1, 3]
        assert _influential(None) == []


class TestDecodeCropOrigin:
    """decode_avi crop="left" 기하: 640×480에서 x=0..480 (center는 80..560)."""

    def _striped_avi(self, path):
        # 왼쪽 160px 빨강 + 오른쪽 480px 파랑 — 크롭 원점에 따라 x=100의
        # 색이 달라진다 (left: 빨강 / center: 소스 x=180 → 파랑).
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=red:size=160x480:rate=5:duration=1",
                "-f", "lavfi", "-i", "color=blue:size=480x480:rate=5:duration=1",
                "-filter_complex", "hstack",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        return str(path)

    def test_left_vs_center_geometry(self, tmp_path, monkeypatch):
        from crk_model.adapters.avi_frames import decode_avi

        avi = self._striped_avi(tmp_path / "striped.avi")
        first_left = next(iter(decode_avi(avi, crop="left"))).full
        first_center = next(iter(decode_avi(avi, crop="center"))).full
        # BGR — mjpeg 손실 압축 감안해 채널 우위로 판정
        b, g, r = first_left[240, 100]
        assert r > 150 and b < 100  # left-crop: x=100은 빨강 띠 안
        b, g, r = first_center[240, 100]
        assert b > 150 and r < 100  # center-crop: 소스 x=180 → 파랑

    def test_invalid_crop_fail_closed(self, tmp_path):
        from crk_model.adapters.avi_frames import decode_avi

        with pytest.raises(ValueError):
            decode_avi("whatever.avi", crop="right")


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
