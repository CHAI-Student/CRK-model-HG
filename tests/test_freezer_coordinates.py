"""냉동의 아래 원점: 검출 ROI·이동 방향·렌더가 같은 위치를 해석한다."""
import pytest

from crk_model.adapters import render_cli
from crk_model.perception.detector import Detection
from crk_model.perception.filters import DetectionFilterChain
from crk_model.perception.motion_direction import MotionDirectionConfig, summarize_direction


@pytest.mark.parametrize("camera", ["top", "side"])
@pytest.mark.parametrize("image_y,kept", [(20, False), (129, False), (130, True),
                                          (131, True), (470, True)])
def test_bottom_350px_roi_includes_boundary_and_preserves_detection(camera, image_y, kept):
    filters = DetectionFilterChain(
        vertical_roi_region="lower", vertical_roi_y_split=350,
        vertical_roi_coordinate_origin="bottom_left",
    )
    # X=450은 side의 기존 X 경계 밖이지만 냉동 수직 ROI 사용 중에는 유효하다.
    product = Detection(1, .9, bbox=(440, image_y - 5, 460, image_y + 5))
    assert filters.apply(camera, [product]) == ([product] if kept else [])


def test_bottom_origin_upper_roi_and_hand_geometry():
    filters = DetectionFilterChain(
        vertical_roi_region="upper", vertical_roi_y_split=350,
        vertical_roi_coordinate_origin="bottom_left",
    )
    upper = Detection(1, .9, bbox=(10, 90, 30, 110))  # 아래 원점 Y=380
    lower = Detection(2, .9, bbox=(10, 290, 30, 310))  # 아래 원점 Y=180
    hand = Detection(0, .9, is_hand=True, bbox=(0, 0, 480, 480))
    assert filters.apply("top", [upper, lower, hand]) == [hand, upper]


@pytest.mark.parametrize("image_ys,action,logical_ys", [
    ([150, 130, 110], "return", (330, 370)),
    ([110, 130, 150], "take", (370, 330)),
])
def test_bottom_origin_action_crosses_asymmetric_boundary(image_ys, action, logical_ys):
    summary = summarize_direction(
        list(enumerate(image_ys)), MotionDirectionConfig(boundary_px=350),
    )
    segment, = summary["segments"]
    assert summary["coordinate_origin"] == "bottom_left"
    assert segment["direction"] == action
    assert segment["boundary_crossed"] is True
    assert (segment["start_y"], segment["end_y"]) == logical_ys


def test_bottom_origin_leaves_x_direction_and_boundary_unchanged():
    summary = summarize_direction(
        list(enumerate([330, 350, 370])), MotionDirectionConfig(axis="x", boundary_px=350),
    )
    segment, = summary["segments"]
    assert segment["direction"] == "return"
    assert segment["boundary_crossed"] is True
    assert (segment["start_x"], segment["end_x"]) == (330, 370)


@pytest.mark.parametrize("origin,expected_label", [
    ("bottom_left", " XY 20,360"), (None, ""),
])
def test_render_center_label_respects_recorded_origin(origin, expected_label):
    # 스탬프 없는 과거 세션에는 새로운 좌표 해석을 적용하지 않는다.
    assert render_cli._decision_center_label((10, 100, 30, 140), origin) == expected_label


def test_render_bottom_origin_keeps_bbox_on_original_image(monkeypatch):
    np = pytest.importorskip("numpy")
    labels = []
    monkeypatch.setattr(render_cli, "_text", lambda *a, **kw: labels.append(a[3]))
    template, status_y = render_cli._canvas_template([], "bottom_left")
    record = {"detections": [{"class_id": 1, "conf": .9, "bbox": [10, 100, 30, 140]}]}
    frame = np.zeros((480, 480, 3), dtype=np.uint8)
    canvas = render_cli._compose_frame(template, status_y, frame, record, 0, "bottom_left")
    x, y = render_cli.FRAME_X, render_cli.FRAME_Y
    assert canvas[y + 110, x + 10].tolist() == list(render_cli._class_color(1))
    assert canvas[y + 360, x + 10].tolist() == [0, 0, 0]
    assert "(0,0) Y+ UP" in labels
    assert "1 0.90 XY 20,360" in labels
