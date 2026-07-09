"""검출 필터 체인 — 파이프라인 stage ④ (다이어그램 3).

원본 대응: Motion(BboxTracker) / Hand Path / Side ROI / conf 필터 중
- Side ROI: side 카메라는 center_x < 240만 유효 (현행 상수 보존)
- Hand Path: 최근 손 bbox 궤적과 교차하지 않는 제품 검출 제거 (근사 구현)
- conf 필터: 여기서 하지 않는다 — I4 (저신뢰 투표 보존, 최종 0.4는 voting.combine)

공간 정보가 없는 검출(bbox=0)은 공간 필터를 통과시킨다 (fail-open — 필터의
실패 방향이 "증거 보존"이 되도록. 과잉 제거는 매출 누락 방향이므로 금지).
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence

from crk_model.perception.detector import Detection

_ZERO_BBOX = (0.0, 0.0, 0.0, 0.0)


def _center_x(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[0] + bbox[2]) / 2


def _expand(bbox, margin: float):
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _intersects(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


class DetectionFilterChain:
    def __init__(
        self,
        *,
        side_roi_max_center_x: float = 240.0,
        hand_history_frames: int = 30,
        hand_margin_px: float = 40.0,
    ):
        self._side_max_cx = side_roi_max_center_x
        self._hand_margin = hand_margin_px
        self._hand_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=hand_history_frames)
        )

    def apply(self, camera: str, detections: Sequence[Detection]) -> list[Detection]:
        hands = [d for d in detections if d.is_hand]
        for h in hands:
            if h.bbox != _ZERO_BBOX:
                self._hand_history[camera].append(h.bbox)

        out: list[Detection] = list(hands)  # 손은 래치(I16)용으로 항상 보존
        history = self._hand_history[camera]
        for d in detections:
            if d.is_hand:
                continue
            if d.bbox != _ZERO_BBOX:
                # Side ROI: 존 바깥(오른쪽) 검출 제거
                if camera == "side" and _center_x(d.bbox) >= self._side_max_cx:
                    continue
                # Hand Path: 손 궤적 근방과 교차하지 않으면 제거
                if history and not any(
                    _intersects(d.bbox, _expand(h, self._hand_margin)) for h in history
                ):
                    continue
            out.append(d)
        return out
