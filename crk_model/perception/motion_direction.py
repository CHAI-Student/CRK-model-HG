"""Y축 방향 진단. 감소=취출 후보, 증가=반납 후보이며 결제에는 미사용.

좌표는 디코더 크롭의 bbox 중심이다. 0/250px 도달 조건은 영상 보정 전에는
적용하지 않는다. 가림 공백과 방향 전환을 분리해 왕복 동작도 보존한다.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class YDirectionConfig:
    min_observations: int = 3
    min_displacement_px: float = 20.0
    jitter_px: float = 3.0
    reversal_px: float = 10.0
    max_gap_frames: int = 12  # 게이트 스킵 포함 디코드 위치 기준

    def __post_init__(self) -> None:
        if self.min_observations < 3 or self.max_gap_frames < 1:
            raise ValueError("Y direction needs >=3 observations and a positive gap limit")
        values = (self.jitter_px, self.reversal_px, self.min_displacement_px)
        if not all(math.isfinite(v) for v in values) or not (
            0 <= self.jitter_px < self.reversal_px <= self.min_displacement_px
        ):
            raise ValueError("Y direction requires 0 <= jitter < reversal <= displacement")


def summarize_y_direction(
    observations: Sequence[tuple[int, float]], config: YDirectionConfig
) -> dict:
    """(디코드 위치, 중심 y) 관측에서 방향 구간을 추출한다.

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
        first, start_y = points[0]
        last, end_y = points[-1]
        delta = end_y - start_y
        if abs(delta) < config.min_displacement_px:
            return
        segments.append({
            "direction": "take" if delta < 0 else "return",
            "first": first, "last": last, "obs": len(points),
            "start_y": round(start_y, 1), "end_y": round(end_y, 1),
            "delta_y": round(delta, 1),
        })

    for pos, cy in observations:
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
        "min_observations": config.min_observations,
        "min_displacement_px": config.min_displacement_px,
        "jitter_px": config.jitter_px,
        "reversal_px": config.reversal_px,
        "max_gap_frames": config.max_gap_frames,
        "segment_count": len(segments),
        "segments": segments[:8],
    }
