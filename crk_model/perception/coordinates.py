"""검출기 영상 좌표와 냉동 판단 좌표의 변환. bbox 자체는 영상 좌표를 유지한다."""
from __future__ import annotations

CROP_HEIGHT_PX = 480.0
COORDINATE_ORIGINS = ("top_left", "bottom_left")


def y_from_image(image_y: float, origin: str) -> float:
    """480px 크롭에서 왼쪽 아래 원점 Y = 480 - 검출기 영상 Y."""
    return CROP_HEIGHT_PX - image_y if origin == "bottom_left" else image_y
