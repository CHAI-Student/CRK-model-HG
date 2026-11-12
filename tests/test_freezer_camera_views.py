"""상단 top + 경사 side: env→ROI/방향 배선과 두 시점의 실제 파이프라인 기여."""
from dataclasses import replace

import pytest

from crk_model.core.config import Settings
from crk_model.core.types import ProductCount
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.perception.detector import Detection
from crk_model.perception.filters import DetectionFilterChain
from crk_model.service import ModelService, TriggerRequest


class CameraScene:
    def __init__(self):
        self.positions = {"top": 0, "side": 0}
        self.allowed = {}

    def detect(self, frame, allowed_class_ids=None):
        camera = "top" if frame[0][0] in (10, 200) else "side"
        self.allowed[camera] = tuple(allowed_class_ids)
        pos = self.positions[camera]
        self.positions[camera] += 1
        cx, cy = (100, [260, 240, 220][pos]) if camera == "top" else ([240, 200, 160][pos], 200)
        out = [Detection(1, .95, bbox=(cx-10, cy-10, cx+10, cy+10))]
        if 0 in allowed_class_ids:
            out.append(Detection(0, .9, is_hand=True, bbox=(cx-20, cy-20, cx+20, cy+20)))
        return out


def request():
    frames = {
        camera: [[[value] * 4 for _ in range(4)] for value in values]
        for camera, values in [("top", [10, 200, 10]), ("side", [30, 230, 30])]
    }
    samples = [LoadcellSample(i * .1, (weight, 0))
               for i, weight in enumerate([500] * 10 + [400] * 10)]
    return TriggerRequest(2, frames, samples, 1.0)


def test_env_reaches_both_camera_direction_and_roi_without_changing_judgment(monkeypatch, cola):
    monkeypatch.setenv("MODEL__MACHINE__CABINET_TYPE", "freezer")
    for key, value in {
        "CAMERA_LAYOUT": "dual_top_proxy", "FREEZER_ROI_Y_SPLIT": "260",
        "FREEZER_SIDE_ROI_Y_SPLIT": "220",
        "FREEZER_TOP_RETURN_BOUNDARY_PX": "240", "FREEZER_SIDE_RETURN_BOUNDARY_PX": "200",
        "FREEZER_SIDE_DIRECTION_AXIS": "x", "FREEZER_SIDE_RETURN_DIRECTION": "decreasing",
        "FREEZER_SIDE_DIRECTION_MIN_DISPLACEMENT_PX": "30", "SIDE_HAND_ENABLED": "1",
    }.items():
        monkeypatch.setenv(f"MODEL__VISION__{key}", value)
    monkeypatch.setenv("MODEL__SESSION__SAVE_DETECTIONS", "1")
    settings = Settings.from_env()
    detector = CameraScene()
    svc = ModelService(detector, settings=settings)
    svc.snapshots.update([cola])
    outcome = svc.pipeline.process("s1", request())
    assert outcome.event.judgment.products == (ProductCount(cola, 1),)
    assert detector.positions == {"top": 3, "side": 3}
    assert all(0 in classes for classes in detector.allowed.values())
    evidence = outcome.trace.vote_summary["motion_evidence"]
    top = evidence["top"][1]["track_detail"][0]["y_direction"]
    side = evidence["side"][1]["track_detail"][0]["motion_direction"]
    assert top["status"] == side["status"] == "return_candidate"
    assert top["segments"][0]["boundary_crossed"] is True
    assert side["segments"][0]["boundary_crossed"] is True
    assert side["axis"] == "x" and side["return_direction"] == "decreasing"
    assert any(r["camera"] == "side" and any(d["hand"] for d in r["detections"])
               for r in outcome.trace.frame_detections)

    baseline = ModelService(CameraScene(), settings=replace(
        settings, freezer_side_direction_axis="y", freezer_side_return_direction="increasing",
        freezer_side_return_boundary_px=None,
    ))
    baseline.snapshots.update([cola])
    assert baseline.pipeline.process("s1", request()).event.judgment == outcome.event.judgment


def test_roi_overrides_are_independent_and_inherit_when_missing():
    f = DetectionFilterChain(
        vertical_roi_region="upper", vertical_roi_y_split=350,
        vertical_roi_by_camera={"side": ("upper", 220)},
    )
    d = Detection(1, .9, bbox=(100, 240, 120, 260))
    assert f.apply("top", [d]) == [d]
    assert f.apply("side", [d]) == []
    assert f.drop_stats["vertical_roi"] == {"top": 0, "side": 1}


def test_camera_roi_can_keep_opposite_region():
    f = DetectionFilterChain(
        vertical_roi_region="upper", vertical_roi_y_split=350,
        vertical_roi_by_camera={"side": ("lower", 220)},
    )
    d = Detection(1, .9, bbox=(100, 140, 120, 160))
    assert f.apply("top", [d]) == [d]
    assert f.apply("side", [d]) == []


def test_side_vertical_off_restores_configured_x_roi():
    f = DetectionFilterChain(
        vertical_roi_region="upper", vertical_roi_y_split=350,
        vertical_roi_by_camera={"side": ("off", 350)}, side_roi_max_center_x=240,
    )
    d = Detection(1, .9, bbox=(300, 140, 320, 160))
    assert f.apply("top", [d]) == [d]
    assert f.apply("side", [d]) == []
    assert f.drop_stats["side_roi"]["side"] == 1


def test_freezer_roi_overrides_do_not_change_refrigerated_filters():
    svc = ModelService(CameraScene(), settings=Settings(
        cabinet_type="refrigerated", camera_layout="dual_top_proxy",
        freezer_roi_y_split=100, freezer_side_roi_y_split=100,
    ))
    d = Detection(1, .9, bbox=(440, 140, 460, 160))
    assert svc.pipeline._filters.apply("top", [d]) == [d]
    assert svc.pipeline._filters.apply("side", [d]) == []  # 냉장 side X ROI 유지


@pytest.mark.parametrize("key,value", [
    ("SIDE_DIRECTION_AXIS", "diagonal"), ("SIDE_RETURN_DIRECTION", "up"),
])
def test_invalid_camera_env_rejected(monkeypatch, key, value):
    monkeypatch.setenv(f"MODEL__VISION__FREEZER_{key}", value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("kwargs", [
    {"freezer_side_return_boundary_px": float("nan")},
    {"freezer_top_return_boundary_px": 481},
    {"freezer_side_direction_min_displacement_px": -1},
    {"freezer_side_roi_y_split": 600},
])
def test_invalid_numeric_geometry_fails_at_startup(kwargs):
    with pytest.raises(ValueError):
        ModelService(CameraScene(), settings=Settings(
            cabinet_type="freezer", camera_layout="dual_top_proxy", **kwargs,
        ))


def test_blank_side_boundary_and_roi_inherit(monkeypatch):
    monkeypatch.setenv("MODEL__VISION__FREEZER_SIDE_RETURN_BOUNDARY_PX", "")
    monkeypatch.setenv("MODEL__VISION__FREEZER_SIDE_ROI_Y_SPLIT", "")
    settings = Settings.from_env()
    assert settings.freezer_side_return_boundary_px is None
    assert settings.freezer_side_roi_y_split is None


@pytest.mark.parametrize("source", ["defaults", "env"])
def test_default_freezer_orientation_is_upward_return(monkeypatch, source):
    monkeypatch.setenv("MODEL__MACHINE__CABINET_TYPE", "freezer")
    monkeypatch.delenv("MODEL__VISION__FREEZER_SIDE_DIRECTION_AXIS", raising=False)
    monkeypatch.delenv("MODEL__VISION__FREEZER_SIDE_RETURN_DIRECTION", raising=False)
    settings = Settings(cabinet_type="freezer") if source == "defaults" else Settings.from_env()
    assert settings.freezer_side_return_direction == "decreasing"
    pipeline = ModelService(CameraScene(), settings=settings).pipeline
    assert {camera: (config.axis, config.return_direction)
            for camera, config in pipeline._freezer_direction_configs.items()} == {
        "top": ("y", "decreasing"), "side": ("y", "decreasing"),
    }
