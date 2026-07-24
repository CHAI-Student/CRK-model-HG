"""검출 필터 체인 — 파이프라인 stage ④ (다이어그램 3).

원본 대응: Motion(BboxTracker) / Hand Path / Side ROI / conf 필터 중
- Side ROI: side 카메라는 center_x < 400만 유효 — center-crop 480×480
  좌표계에서의 값 (원본 left-crop 좌표계의 side_roi_x_max=400을 그대로
  이식 — 2026-07-24 center-crop 전환으로 크롭 원점이 이동했으므로 실기
  재측정 필요). 구값 240은 squash resize 좌표계 산물로 side 검출을 과잉
  제거했다.
- Hand Path: 최근 손 bbox 궤적과 교차하지 않는 제품 검출 제거 (근사 구현)
- conf 필터: 여기서 하지 않는다 — I4 (저신뢰 투표 보존, 최종 0.4는 voting.combine)

정지 진열 상품 억제는 여기서 하지 않는다 — 과거의 static_track(연속 IoU
정지)·baseline(손 등장 전 배경 등록)은 "진열 상품은 움직이지 않는다"는
물리의 대리 신호였고, MotionEvidence의 트랙 단위 변위 몰수가 같은 물리를
직접 재면서 은퇴했다 (0723 트랙릿 문서 §5, 이슈 #16 실기 4건).

공간 정보가 없는 검출(bbox=0)은 공간 필터를 통과시킨다 (fail-open — 필터의
실패 방향이 "증거 보존"이 되도록. 과잉 제거는 매출 누락 방향이므로 금지).

상태 수명: 손 궤적은 **영상(트리거) 단위** 상태다 — pipeline이 트리거
시작마다 reset_trigger_state()를 불러야 한다. (기존에는 손 이력이 트리거
간에 남아 이전 영상의 좌표가 다음 영상의 필터 기준이 되는 결함이 있었다.)
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
        side_roi_max_center_x: float = 400.0,
        hand_history_frames: int = 30,
        hand_margin_px: float = 40.0,
        vertical_roi_region: str = "off",
        # 냉동 dual-top 수직 ROI (원본 freezer_roi_vertical_region, P1-5 이식):
        # "upper"|"lower"면 **두 카메라 모두** center_y 기준 해당 절반만 유지
        # (dual_top_proxy — side 스트림도 실제로는 top 뷰). 이때 side x-ROI는
        # 원본과 동일하게 생략된다. "off"(기본) = 기존 동작.
        vertical_roi_y_split: float = 300.0,
        # center-crop 480×480 좌표계의 분할선(세로축, crop 원점 이동 영향 없음)
        # — 원본 freezer_roi_y_split(원본 운영값 240 → 300, 2026-07-24).
        top_roi_enabled: bool = False,
        # 냉장(top+side) 레이아웃의 top 카메라 수직 ROI (원본 top_roi_enabled):
        # 트리거 delta가 0이 아닐 때 center_y >= split(하단 절반)만 유지.
        # 수직 ROI(dual-top)가 켜져 있으면 그쪽이 우선한다.
        top_roi_y_split: float = 240.0,
        hand_conf_floor: float = 0.0,
        # 손 검출 conf 하한 (원본 hand_confidence_threshold, P1-7 이식): 이
        # 값 미만의 hand 검출은 래치·궤적에 쓰지 않는다 — 유령 손이 모션
        # 게이트 래치(I16)와 hand_path 기준을 오염시키는 것을 차단. 0 = off.
    ):
        if vertical_roi_region not in ("off", "upper", "lower"):
            # cabinet_type과 동일한 fail-closed: 오타가 조용히 off가 되면
            # ROI 없이 운영되고 있음을 알 수 없다.
            raise ValueError(f"Invalid vertical_roi_region: {vertical_roi_region}")
        self._side_max_cx = side_roi_max_center_x
        self._vertical_region = vertical_roi_region
        self._vertical_split = vertical_roi_y_split
        self._top_roi_enabled = top_roi_enabled
        self._top_roi_split = top_roi_y_split
        self._hand_conf_floor = hand_conf_floor
        # top ROI의 방향 게이트 (원본 _top_roi_direction): 트리거 delta가
        # 0이면 필터 미적용. pipeline이 set_trigger_delta로 주입한다.
        self._trigger_delta: float | None = None
        self._hand_margin = hand_margin_px
        self._hand_history_frames = hand_history_frames
        self._hand_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=hand_history_frames)
        )
        # 진단 (issue #6 2차: side 검출이 195프레임 중 194개 제거 — 어느 단계가
        # 지웠는지 구분 불가했음): 카메라×단계별 제거 카운터. 트리거 시작 시
        # pipeline이 reset_trigger_state()로 초기화하고 종료 시 vote_summary에 싣는다.
        self.drop_stats: dict[str, dict[str, int]] = {}
        self.reset_drop_stats()

    def reset_drop_stats(self) -> None:
        self.drop_stats = {
            "side_roi": {"top": 0, "side": 0},
            "vertical_roi": {"top": 0, "side": 0},  # dual-top 수직 ROI + top ROI
            "hand_conf": {"top": 0, "side": 0},
            "hand_path": {"top": 0, "side": 0},
        }

    def reset_trigger_state(self) -> None:
        """트리거(영상) 단위 상태 초기화 — pipeline이 트리거 시작마다 호출.

        손 궤적은 특정 영상의 좌표계에 묶인 상태라 다음 트리거(다른 영상)로
        새어 나가면 안 된다."""
        self.reset_drop_stats()
        self._hand_history.clear()
        self._trigger_delta = None

    def set_trigger_delta(self, delta: float | None) -> None:
        """top ROI의 방향 게이트 입력 (원본 _top_roi_direction) — delta가
        None/0이면 top ROI 미적용. pipeline이 reset 직후 호출한다."""
        self._trigger_delta = delta

    def _vertical_roi_rejects(self, camera: str, d: Detection) -> bool:
        """수직 ROI (P1-5): dual-top 수직 ROI가 켜져 있으면 두 카메라 공통,
        아니면 top 카메라 한정 top ROI(delta 있을 때 하단 절반 유지)."""
        cy = (d.bbox[1] + d.bbox[3]) / 2
        if self._vertical_region != "off":
            if self._vertical_region == "lower":
                return cy < self._vertical_split
            return cy > self._vertical_split  # upper: center_y <= split 유지
        if (
            self._top_roi_enabled
            and camera == "top"
            and self._trigger_delta is not None
            and self._trigger_delta != 0.0
        ):
            return cy < self._top_roi_split  # 원본: center_y >= split 유지
        return False

    def apply(self, camera: str, detections: Sequence[Detection]) -> list[Detection]:
        # Hand conf floor (P1-7): 유령 손을 래치·궤적 입력에서 제외 — 통과한
        # 손만 hand_path 기준·래치(I16)에 쓰인다.
        hands = []
        for d in detections:
            if not d.is_hand:
                continue
            if self._hand_conf_floor > 0 and d.confidence < self._hand_conf_floor:
                self.drop_stats["hand_conf"][camera] += 1
                continue
            hands.append(d)
        for h in hands:
            if h.bbox != _ZERO_BBOX:
                self._hand_history[camera].append(h.bbox)

        out: list[Detection] = list(hands)  # 손은 래치(I16)용으로 항상 보존
        history = self._hand_history[camera]
        for d in detections:
            if d.is_hand:
                continue
            if d.bbox != _ZERO_BBOX:
                # 수직 ROI (P1-5): dual-top이면 두 카메라 공통(상/하단 절반),
                # 아니면 top ROI(냉장 레이아웃, delta 있을 때만)
                if self._vertical_roi_rejects(camera, d):
                    self.drop_stats["vertical_roi"][camera] += 1
                    continue
                # Side ROI: 존 바깥(오른쪽) 검출 제거 — dual-top 수직 ROI가
                # 켜져 있으면 side 스트림도 top 뷰이므로 생략 (원본 동형)
                if (
                    self._vertical_region == "off"
                    and camera == "side"
                    and _center_x(d.bbox) >= self._side_max_cx
                ):
                    self.drop_stats["side_roi"][camera] += 1
                    continue
                # Hand Path: 손 궤적 근방과 교차하지 않으면 제거
                if history and not any(
                    _intersects(d.bbox, _expand(h, self._hand_margin)) for h in history
                ):
                    self.drop_stats["hand_path"][camera] += 1
                    continue
            out.append(d)
        return out
