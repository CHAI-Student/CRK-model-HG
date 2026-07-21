"""검출 필터 체인 — 파이프라인 stage ④ (다이어그램 3).

원본 대응: Motion(BboxTracker) / Hand Path / Side ROI / conf 필터 중
- Side ROI: side 카메라는 center_x < 240만 유효 (현행 상수 보존)
- Hand Path: 최근 손 bbox 궤적과 교차하지 않는 제품 검출 제거 (근사 구현)
- Static Track: 같은 자리에 정지 상태로 계속 잡히는 검출 제거 (이슈 #10 —
  전시 영역 밖으로 돌출된 진열 상품이 프리롤부터 전 프레임에 잡혀 최상위
  vision candidate로 오르는 문제. 진열 상품은 움직이지 않고, 진짜 취출/
  손에 든 상품은 움직인다는 물리로 구분 — 위치 단위 억제라서 손님이 바로
  그 돌출 상품을 집으면 bbox가 움직여 억제가 즉시 풀린다)
- Baseline: 손 등장 전(프리롤)에 이미 그 자리에 있던 class를 장면 배경으로
  등록하고, 이후 같은 자리 재검출을 억제 (이슈 #14 후속 — hand_path가
  트리거 단위 리셋으로 프리롤에서 fail-open이 되자, static_track의 연속
  IoU 0.85 조건을 못 채우는 "깜빡이는 고정 물체"가 투표에 들어오는 문제.
  static_track이 "연속·고정밀 정지"라면 baseline은 "손 등장 전 존재"라는
  시간 경계로 배경을 정의한다. 손에 들린 상품은 baseline 위치를 벗어나므로
  통과 — 돌출 상품을 바로 집는 경우도 동일). shadow 모드에서는 드랍 없이
  drop_stats["baseline"]만 계수해 실측 검증 후 active로 승격한다.
- conf 필터: 여기서 하지 않는다 — I4 (저신뢰 투표 보존, 최종 0.4는 voting.combine)

공간 정보가 없는 검출(bbox=0)은 공간 필터를 통과시킨다 (fail-open — 필터의
실패 방향이 "증거 보존"이 되도록. 과잉 제거는 매출 누락 방향이므로 금지).

상태 수명: 손 궤적·정지 트랙은 **영상(트리거) 단위** 상태다 — pipeline이
트리거 시작마다 reset_trigger_state()를 불러야 한다. (기존에는 손 이력이
트리거 간에 남아 이전 영상의 좌표가 다음 영상의 필터 기준이 되는 결함이
있었다.)
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.perception.detector import Detection

_ZERO_BBOX = (0.0, 0.0, 0.0, 0.0)


def _center_x(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[0] + bbox[2]) / 2


def _expand(bbox, margin: float):
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _intersects(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0.0:
        return 0.0
    union = (
        (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    )
    return inter / union if union > 0 else 0.0


@dataclass
class _StaticAnchor:
    """정지 트랙 후보 — 같은 자리(IoU 기준)에서 관측된 연속 횟수를 센다."""

    bbox: tuple[float, float, float, float]
    count: int = 1
    last_seen: int = 0


class DetectionFilterChain:
    def __init__(
        self,
        *,
        side_roi_max_center_x: float = 240.0,
        hand_history_frames: int = 30,
        hand_margin_px: float = 40.0,
        static_track_min_frames: int = 24,
        static_track_iou: float = 0.85,
        baseline_suppress_mode: str = "shadow",
        baseline_suppress_iou: float = 0.5,
    ):
        if baseline_suppress_mode not in ("off", "shadow", "active"):
            raise ValueError(
                f"Invalid baseline_suppress_mode: {baseline_suppress_mode}"
            )
        self._side_max_cx = side_roi_max_center_x
        self._hand_margin = hand_margin_px
        self._hand_history_frames = hand_history_frames
        self._hand_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=hand_history_frames)
        )
        # 정지 트랙 억제 (이슈 #10 돌출 진열 상품): 같은 class가 IoU ≥
        # static_track_iou로 min_frames 이상(추론된 프레임 기준, ≈1초) 같은
        # 자리에 머물면 그 위치의 검출을 투표에서 제거한다. 파라미터는
        # 기기별 튜닝값이 아니라 "진열 상품은 정지해 있다"는 물리 상수 —
        # 손에 든 상품은 손 떨림으로 IoU가 유지되지 않는다. min_frames <= 0
        # 이면 비활성 (MODEL__VISION__STATIC_TRACK_* env).
        self._static_min_frames = static_track_min_frames
        self._static_iou = static_track_iou
        self._static_tracks: dict[str, dict[int, list[_StaticAnchor]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._frame_idx: dict[str, int] = defaultdict(int)
        # Baseline 억제 (모듈 docstring 참조): 카메라별 "손 등장 전" 관측
        # class → bbox anchor 목록. 손이 한 번이라도 잡히면 등록을 멈춘다
        # (손이 내려놓은 물건이 배경으로 오등록되는 것을 방지). 손이 끝까지
        # 안 잡히는 트리거는 등록이 계속되지만, 그 경우에도 억제 대상은
        # "같은 자리 재검출"뿐이라 fail 방향은 증거 보존에 가깝다.
        self._baseline_mode = baseline_suppress_mode
        self._baseline_iou = baseline_suppress_iou
        self._baseline_anchors: dict[str, dict[int, list]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._hand_seen: dict[str, bool] = defaultdict(bool)
        # 진단 (issue #6 2차: side 검출이 195프레임 중 194개 제거 — 어느 단계가
        # 지웠는지 구분 불가했음): 카메라×단계별 제거 카운터. 트리거 시작 시
        # pipeline이 reset_trigger_state()로 초기화하고 종료 시 vote_summary에 싣는다.
        self.drop_stats: dict[str, dict[str, int]] = {}
        self.reset_drop_stats()

    def reset_drop_stats(self) -> None:
        self.drop_stats = {
            "side_roi": {"top": 0, "side": 0},
            "baseline": {"top": 0, "side": 0},  # shadow 모드에서는 "드랍했을" 수
            "static_track": {"top": 0, "side": 0},
            "hand_path": {"top": 0, "side": 0},
        }

    def reset_trigger_state(self) -> None:
        """트리거(영상) 단위 상태 초기화 — pipeline이 트리거 시작마다 호출.

        손 궤적과 정지 트랙은 특정 영상의 좌표계에 묶인 상태라 다음 트리거
        (다른 영상)로 새어 나가면 안 된다."""
        self.reset_drop_stats()
        self._hand_history.clear()
        self._static_tracks.clear()
        self._frame_idx.clear()
        self._baseline_anchors.clear()
        self._hand_seen.clear()

    def _is_static(self, camera: str, d: Detection) -> bool:
        """정지 트랙 갱신 + 억제 여부 판정. 검출이 억제되더라도 카운트는
        계속 누적한다 (hand_path 등 다른 필터와 독립적으로 추적)."""
        if self._static_min_frames <= 0:
            return False
        idx = self._frame_idx[camera]
        anchors = self._static_tracks[camera][d.class_id]
        best, best_iou = None, 0.0
        for a in anchors:
            iou = _iou(d.bbox, a.bbox)
            if iou > best_iou:
                best, best_iou = a, iou
        if best is not None and best_iou >= self._static_iou:
            best.count += 1
            best.bbox = d.bbox
            best.last_seen = idx
            return best.count >= self._static_min_frames
        anchors.append(_StaticAnchor(d.bbox, count=1, last_seen=idx))
        # 한동안 재관측되지 않은 anchor 제거 (움직였거나 사라짐 — 잠깐의
        # 가림(occlusion)은 창 안이라 카운트가 유지된다)
        stale = 2 * self._static_min_frames
        anchors[:] = [a for a in anchors if idx - a.last_seen <= stale]
        return False

    def _is_baseline(self, camera: str, d: Detection) -> bool:
        """배경(baseline) 여부 판정 + 손 등장 전이면 anchor 등록.

        첫 관측은 등록만 하고 통과시킨다(증거 보존) — 억제는 같은 자리
        재검출부터. 매칭 시 anchor bbox를 최신 관측으로 갱신해 느린
        드리프트를 추종한다 (빠르게 움직이는 손에 든 상품은 IoU가
        급락해 추종되지 않음 — static_track과 같은 물리 구분)."""
        if self._baseline_mode == "off":
            return False
        anchors = self._baseline_anchors[camera][d.class_id]
        for i, bbox in enumerate(anchors):
            if _iou(d.bbox, bbox) >= self._baseline_iou:
                anchors[i] = d.bbox
                return True
        if not self._hand_seen[camera]:
            anchors.append(d.bbox)
        return False

    def apply(self, camera: str, detections: Sequence[Detection]) -> list[Detection]:
        self._frame_idx[camera] += 1
        hands = [d for d in detections if d.is_hand]
        if hands:
            self._hand_seen[camera] = True  # 이후 프레임부터 baseline 등록 중지
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
                    self.drop_stats["side_roi"][camera] += 1
                    continue
                # Baseline: 손 등장 전부터 있던 배경 억제 — shadow 모드는
                # 계수만 하고 통과시켜 실측 검증 후 active 승격 (이슈 #14)
                if self._is_baseline(camera, d):
                    self.drop_stats["baseline"][camera] += 1
                    if self._baseline_mode == "active":
                        continue
                # Static Track: 정지 진열 상품 억제 — hand_path보다 먼저
                # 평가해 손 근접 여부와 무관하게 정지 카운트를 누적한다
                # (돌출 상품은 손이 지나가는 길목이라 hand_path를 통과함)
                if self._is_static(camera, d):
                    self.drop_stats["static_track"][camera] += 1
                    continue
                # Hand Path: 손 궤적 근방과 교차하지 않으면 제거
                if history and not any(
                    _intersects(d.bbox, _expand(h, self._hand_margin)) for h in history
                ):
                    self.drop_stats["hand_path"][camera] += 1
                    continue
            out.append(d)
        return out
