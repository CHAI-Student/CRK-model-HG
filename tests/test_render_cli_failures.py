"""render-session 실패 격리 — ffmpeg/numpy 없이 도는 단위 검증.

카메라 1개의 디코드/인코드 실패(마무리 인코드 sink.close 포함)는 그 카메라만
건너뛰고, 남은 카메라·트리거 렌더는 계속된다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from crk_model.adapters import render_cli


class _FakeSink:
    instances: list[_FakeSink] = []
    fail_close_for: str = ""

    def __init__(self, dest, fmt, fps):
        self.dest = dest
        self.closed = False
        self.aborted = False
        _FakeSink.instances.append(self)

    def write(self, frame) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self.dest.name == _FakeSink.fail_close_for:
            raise OSError("ffmpeg encode failed (rc=1): simulated")

    def abort(self) -> None:
        self.aborted = True


@pytest.fixture
def fake_render(monkeypatch):
    _FakeSink.instances = []
    monkeypatch.setattr(render_cli, "FfmpegSink", _FakeSink)
    monkeypatch.setattr(render_cli, "_canvas_template", lambda header, origin: (None, 0))
    monkeypatch.setattr(render_cli, "_compose_frame", lambda *args: None)
    monkeypatch.setattr(
        render_cli, "decode_avi",
        lambda path, crop: iter([SimpleNamespace(full=None)] * 3),
    )
    return _FakeSink


def _trigger(video):
    return {
        "zone": 2,
        "delta_weight": -135.5,
        "judgment": {"status": "complete", "products": [{"class_id": 27, "count": 1}]},
        "video_paths": {"side": str(video), "top": str(video)},
        "trace": {"frame_detections": []},
    }


def test_close_failure_skips_only_that_camera(tmp_path, capsys, fake_render):
    video = tmp_path / "v.avi"
    video.write_bytes(b"")
    fake_render.fail_close_for = "trig0_side.mp4"  # 정렬상 side가 먼저 렌더된다

    outputs = render_cli.render_trigger(
        _trigger(video), 0, "ses-f1", tmp_path / "out", "mp4", 6.0, [], None,
    )

    assert [p.name for p in outputs] == ["trig0_top.mp4"]
    side, top = fake_render.instances
    assert side.aborted and not top.aborted and top.closed
    err = capsys.readouterr().err
    assert "[trig 0/side] 실패" in err and "simulated" in err


def test_unexpected_error_aborts_sink_and_propagates(tmp_path, fake_render, monkeypatch):
    video = tmp_path / "v.avi"
    video.write_bytes(b"")
    fake_render.fail_close_for = ""

    def boom(*args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(render_cli, "_compose_frame", boom)
    with pytest.raises(RuntimeError):
        render_cli.render_trigger(
            _trigger(video), 0, "ses-f2", tmp_path / "out", "mp4", 6.0, [], None,
        )
    assert fake_render.instances[0].aborted
