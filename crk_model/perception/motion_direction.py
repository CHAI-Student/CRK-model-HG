"""카메라별 취출·반납 방향 구간 진단. 최종 무게 판정에는 영향을 주지 않는다.

입력은 디코더 크롭의 bbox 중심이며, 냉동 Y 판단은 아래 원점으로 변환한다.
카메라 장착 각도로 영상 회전을 추정하지 않는다. X/Y 축·방향·경계를 카메라별로 설정한다.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.perception.coordinates import COORDINATE_ORIGINS, y_from_image


@dataclass(frozen=True)
class MotionDirectionConfig:
    min_observations: int = 3
    min_displacement_px: float = 20.0
    jitter_px: float = 3.0
    reversal_px: float = 10.0
    max_gap_frames: int = 12  # 게이트 스킵 포함 디코드 위치 기준
    axis: str = "y"
    return_direction: str = "increasing"  # 아래 원점: 위쪽(Y 증가)=반납, 아래쪽=취출
    coordinate_origin: str = "bottom_left"
    boundary_px: float | None = None
    boundary_margin_px: float = 10.0

    def __post_init__(self) -> None:
        if self.min_observations < 3 or self.max_gap_frames < 1:
            raise ValueError("Motion direction needs >=3 observations and a positive gap limit")
        values = (self.jitter_px, self.reversal_px, self.min_displacement_px)
        if not all(math.isfinite(v) for v in values) or not (
            0 <= self.jitter_px < self.reversal_px <= self.min_displacement_px
        ):
            raise ValueError("Motion direction requires 0 <= jitter < reversal <= displacement")
        if self.axis not in ("x", "y"):
            raise ValueError(f"Invalid motion direction axis: {self.axis}")
        if self.return_direction not in ("increasing", "decreasing"):
            raise ValueError(f"Invalid return direction: {self.return_direction}")
        if self.coordinate_origin not in COORDINATE_ORIGINS:
            raise ValueError(f"Invalid coordinate origin: {self.coordinate_origin}")
        if not math.isfinite(self.boundary_margin_px) or self.boundary_margin_px < 0:
            raise ValueError("Motion boundary margin must be finite and nonnegative")
        if self.boundary_px is not None and (
            not math.isfinite(self.boundary_px) or not 0 <= self.boundary_px <= 480
        ):
            raise ValueError("Motion boundary must be within the 480px crop")


def summarize_direction(
    observations: Sequence[tuple[int, float]], config: MotionDirectionConfig,
    *, segment_limit: int | None = 8,
) -> dict:
    """(디코드 위치, 영상 축 좌표)를 판단 좌표로 바꾸어 방향 구간을 추출한다.

    bbox 입력은 위쪽 원점 영상 좌표다. 출력의 Y 좌표와 경계는 설정한 원점 기준이다.

    시작점 대비 변위와 관측 수를 함께 요구한다. 작은 역방향 흔들림은 허용하되
    극점에서 reversal_px 이상 되돌아오면 극점을 공유하는 별도 구간을 만든다.
    긴 미검출 공백은 기존 bbox 트랙이 재연결돼도 방향 증거로 연결하지 않는다.
    """
    segments: list[dict] = []
    run: list[tuple[int, float]] = []
    direction = 0
    peak = 0

    def emit(points: list[tuple[int, float]]) -> None:
        if len(points) < config.min_observations:
            return
        first, start = points[0]
        last, end = points[-1]
        delta = end - start
        if abs(delta) < config.min_displacement_px:
            return
        return_sign = 1 if config.return_direction == "increasing" else -1
        action = "return" if delta * return_sign > 0 else "take"
        crossed = None
        if config.boundary_px is not None:
            before = (start - config.boundary_px) * return_sign
            after = (end - config.boundary_px) * return_sign
            margin = config.boundary_margin_px
            crossed = (
                before <= -margin and after >= margin
                if action == "return"
                else before >= margin and after <= -margin
            )
        segments.append({
            "direction": action,
            "first": first, "last": last, "obs": len(points),
            f"start_{config.axis}": round(start, 1), f"end_{config.axis}": round(end, 1),
            f"delta_{config.axis}": round(delta, 1),
            "boundary_crossed": crossed,
        })

    for pos, image_coordinate in observations:
        cy = (
            y_from_image(image_coordinate, config.coordinate_origin)
            if config.axis == "y" else image_coordinate
        )
        if run and (pos <= run[-1][0] or pos - run[-1][0] > config.max_gap_frames):
            emit(run[:peak + 1])
            run, direction, peak = [], 0, 0
        run.append((pos, cy))
        if len(run) == 1:
            continue
        if direction == 0:
            delta = cy - run[0][1]
            if abs(delta) > config.jitter_px:
                direction = 1 if delta > 0 else -1
                peak = len(run) - 1
        elif direction * (cy - run[peak][1]) >= 0:
            peak = len(run) - 1
        elif direction * (run[peak][1] - cy) >= config.reversal_px:
            emit(run[:peak + 1])
            run = run[peak:]
            direction = -direction
            peak = len(run) - 1
    emit(run[:peak + 1])

    directions = {segment["direction"] for segment in segments}
    status = "unknown"
    if len(directions) > 1:
        status = "mixed"
    elif directions:
        status = f"{next(iter(directions))}_candidate"
    return {
        "status": status,
        "axis": config.axis,
        "return_direction": config.return_direction,
        "coordinate_origin": config.coordinate_origin,
        "boundary_px": config.boundary_px,
        "boundary_margin_px": config.boundary_margin_px,
        "min_observations": config.min_observations,
        "min_displacement_px": config.min_displacement_px,
        "jitter_px": config.jitter_px,
        "reversal_px": config.reversal_px,
        "max_gap_frames": config.max_gap_frames,
        "segment_count": len(segments),
        "segments": segments[:segment_limit],
    }


# 초기 Y축 진단 API와 기존 호출부 호환.
YDirectionConfig = MotionDirectionConfig
summarize_y_direction = summarize_direction
