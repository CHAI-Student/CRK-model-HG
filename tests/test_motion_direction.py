"""취출/반납 Y축 후보 진단: 왕복, 흔들림, 가림, 기존 투표 무영향."""
import pytest

from crk_model.perception.detector import Detection
from crk_model.perception.motion_direction import (
    MotionDirectionConfig,
    YDirectionConfig,
    summarize_direction,
    summarize_y_direction,
)
from crk_model.perception.motion_evidence import MotionEvidence
from crk_model.perception.voting import VotingEnsemble


@pytest.mark.parametrize("ys,status,directions", [
    ([240, 180, 100, 50], "return_candidate", ["return"]),
    ([50, 100, 180, 240], "take_candidate", ["take"]),
    ([240, 180, 100, 50, 100, 180, 240], "mixed", ["return", "take"]),
    ([50, 100, 180, 240, 180, 100, 50], "mixed", ["take", "return"]),
    ([180, 183, 179, 182, 181], "unknown", []),
    ([240, 180], "unknown", []),
    ([240, 180, 182, 100], "return_candidate", ["return"]),
    ([240, 230, 225], "unknown", []),
    ([], "unknown", []),
])
def test_direction_candidates(ys, status, directions):
    result = summarize_y_direction(list(enumerate(ys)), YDirectionConfig())
    assert result["status"] == status
    assert [s["direction"] for s in result["segments"]] == directions
    if status == "mixed":
        # 처음/끝이 같은 좌표여도 왕복 2구간을 모두 보존한다.
        assert result["segments"][0]["last"] == result["segments"][1]["first"]


def test_decode_gap_does_not_manufacture_direction():
    result = summarize_y_direction(
        [(0, 240), (1, 200), (30, 160), (31, 120)], YDirectionConfig()
    )
    assert result["status"] == "unknown"  # 각 구간은 2관측뿐


def test_segments_before_and_after_occlusion_are_preserved():
    result = summarize_y_direction(
        [(0, 240), (1, 200), (2, 160), (30, 50), (31, 100), (32, 150)],
        YDirectionConfig(),
    )
    assert result["status"] == "mixed"
    assert [(s["first"], s["last"]) for s in result["segments"]] == [(0, 2), (30, 32)]


def test_direction_telemetry_does_not_change_votes_or_track_qualification():
    baseline = MotionEvidence()
    diagnostic = MotionEvidence(direction_config=YDirectionConfig())
    results = []
    for evidence in (baseline, diagnostic):
        voting = VotingEnsemble(min_vote_count=1)
        voting.attach_motion_evidence(evidence)
        for pos, cy in enumerate([240, 180, 100, 50, 100, 180, 240]):
            dets = [Detection(1, 0.9, bbox=(50, cy - 20, 90, cy + 20))]
            tids = evidence.observe("top", dets, pos=pos)
            voting.add_frame("top", dets, track_ids=tids, pos=pos)
        results.append(voting.combine())
    assert results[0] == results[1]
    assert baseline.track_qualifies(0) == diagnostic.track_qualifies(0)
    plain = baseline.summary()["top"][1]["track_detail"][0]
    detail = diagnostic.summary()["top"][1]["track_detail"][0]
    assert "y_direction" not in plain
    assert detail["y_direction"]["status"] == "mixed"


def test_hand_alone_has_no_product_direction():
    ev = MotionEvidence(direction_config=YDirectionConfig())
    for pos, cy in enumerate([50, 150, 250]):
        ev.observe(
            "top", [Detection(0, 0.9, is_hand=True, bbox=(50, cy - 20, 90, cy + 20))], pos=pos
        )
    assert ev.summary() == {}


def test_camera_directions_are_independent():
    ev = MotionEvidence(direction_config=YDirectionConfig())
    for pos, (top_y, side_y) in enumerate(zip([240, 180, 100], [50, 100, 180], strict=True)):
        for camera, cy in [("top", top_y), ("side", side_y)]:
            ev.observe(camera, [Detection(1, 0.9, bbox=(50, cy - 20, 90, cy + 20))], pos=pos)
    summary = ev.summary()
    assert summary["top"][1]["track_detail"][0]["y_direction"]["status"] == "return_candidate"
    assert summary["side"][1]["track_detail"][0]["y_direction"]["status"] == "take_candidate"


@pytest.mark.parametrize("kwargs", [
    {"min_observations": 2}, {"max_gap_frames": 0}, {"jitter_px": -1},
    {"reversal_px": 2}, {"min_displacement_px": float("nan")},
])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        YDirectionConfig(**kwargs)


@pytest.mark.parametrize("coords,sign,action,crossed", [
    ([200, 230, 260], "increasing", "return", True),
    ([270, 250, 220], "increasing", "take", True),
    ([270, 250, 220], "decreasing", "return", True),
    ([200, 230, 260], "decreasing", "take", True),
    ([200, 220, 235], "increasing", "return", False),
    ([245, 260, 280], "increasing", "return", False),
])
def test_boundary_crossing_respects_axis_direction_and_margin(coords, sign, action, crossed):
    result = summarize_direction(list(enumerate(coords)), MotionDirectionConfig(
        axis="x", return_direction=sign, boundary_px=240, boundary_margin_px=10,
    ))
    segment, = result["segments"]
    assert segment["direction"] == action
    assert segment["boundary_crossed"] is crossed
    assert segment["start_x"] == coords[0] and segment["end_x"] == coords[-1]
    assert "start_y" not in segment


def test_oblique_side_has_independent_axis_sign_and_threshold():
    ev = MotionEvidence(direction_configs={
        "top": MotionDirectionConfig(boundary_px=240),
        "side": MotionDirectionConfig(
            axis="x", return_direction="decreasing", boundary_px=200, min_displacement_px=30,
        ),
    })
    for pos, (top_y, side_x) in enumerate(zip([260, 240, 220], [240, 200, 160], strict=True)):
        for camera, cx, cy in [("top", 100, top_y), ("side", side_x, 200)]:
            ev.observe(camera, [Detection(1, .9, bbox=(cx-10, cy-10, cx+10, cy+10))], pos=pos)
    summary = ev.summary()
    top = summary["top"][1]["track_detail"][0]["y_direction"]
    side = summary["side"][1]["track_detail"][0]["motion_direction"]
    assert top["status"] == side["status"] == "return_candidate"
    assert top["segments"][0]["boundary_crossed"] is True
    assert side["segments"][0]["boundary_crossed"] is True
    assert side["segments"][0]["delta_x"] == -80
    assert side["axis"] == "x" and side["min_displacement_px"] == 30


def test_per_camera_threshold_is_not_shared():
    ev = MotionEvidence(direction_configs={
        "top": MotionDirectionConfig(min_displacement_px=20),
        "side": MotionDirectionConfig(min_displacement_px=30),
    })
    for pos, cy in enumerate([100, 112, 125]):
        for camera in ("top", "side"):
            ev.observe(camera, [Detection(1, .9, bbox=(50, cy-10, 70, cy+10))], pos=pos)
    summary = ev.summary()
    assert summary["top"][1]["track_detail"][0]["y_direction"]["status"] == "take_candidate"
    assert summary["side"][1]["track_detail"][0]["y_direction"]["status"] == "unknown"


@pytest.mark.parametrize("kwargs", [
    {"axis": "z"}, {"return_direction": "up"}, {"boundary_px": 481},
    {"boundary_px": float("nan")}, {"boundary_margin_px": -1},
])
def test_invalid_camera_geometry_rejected(kwargs):
    with pytest.raises(ValueError):
        MotionDirectionConfig(**kwargs)
