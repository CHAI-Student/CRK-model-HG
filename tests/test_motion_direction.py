"""취출/반납 Y축 후보 진단: 왕복, 흔들림, 가림, 기존 투표 무영향."""
import pytest

from crk_model.perception.detector import Detection
from crk_model.perception.motion_direction import YDirectionConfig, summarize_y_direction
from crk_model.perception.motion_evidence import MotionEvidence
from crk_model.perception.voting import VotingEnsemble


@pytest.mark.parametrize("ys,status,directions", [
    ([240, 180, 100, 50], "take_candidate", ["take"]),
    ([50, 100, 180, 240], "return_candidate", ["return"]),
    ([240, 180, 100, 50, 100, 180, 240], "mixed", ["take", "return"]),
    ([50, 100, 180, 240, 180, 100, 50], "mixed", ["return", "take"]),
    ([180, 183, 179, 182, 181], "unknown", []),
    ([240, 180], "unknown", []),
    ([240, 180, 182, 100], "take_candidate", ["take"]),
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
    assert summary["top"][1]["track_detail"][0]["y_direction"]["status"] == "take_candidate"
    assert summary["side"][1]["track_detail"][0]["y_direction"]["status"] == "return_candidate"


@pytest.mark.parametrize("kwargs", [
    {"min_observations": 2}, {"max_gap_frames": 0}, {"jitter_px": -1},
    {"reversal_px": 2}, {"min_displacement_px": float("nan")},
])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        YDirectionConfig(**kwargs)
