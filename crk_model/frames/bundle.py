"""FrameBundle — 모션 게이트와 검출기가 같은 프레임의 다른 뷰를 쓴다.

게이트는 다운스케일 그레이(~120×120, absdiff 비용 절감 — L1)를,
검출기는 풀 프레임(BGR 480×480)을 소비한다. 파이프라인은 bundle이면
gate_view/full을 각각 꺼내고, 평범한 프레임이면 그대로 양쪽에 쓴다
(테스트·단순 환경 호환).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FrameBundle:
    full: Any       # 검출기 입력 (BGR 480x480)
    gate_view: Any  # 모션 게이트 입력 (다운스케일 그레이스케일)
