"""모션 변위 증거 — "변위 없는 클래스는 투표 몰수" (원본 BboxTracker 사후 필터 대응).

물리 원칙: **집어간 상품은 움직이고, 진열 상품은 안 움직인다.** static_track
(연속 IoU 정지)과 baseline(손 등장 전 존재)은 이 물리의 대리 신호였고 각자
구멍이 있었다 — static_track은 깜빡이는 정지 물체를 놓치고(연속성 요건),
baseline은 손 신호에 의존해 top(프리롤에 이미 손)에서는 무력, side(hand
미추론)에서는 폭주했다 (issue #16 실기 4건). 변위는 대리가 아니라 물리 그
자체를 측정한다: 깜빡여도 변위 ~0이면 진열이고, 손이 안 보여도 움직이면
취출이다.

- 트랙: 카메라×클래스별로 최근접 중심 매칭(점프 상한 max_jump_px)으로 잇는다.
  IoU 앵커(static_track 방식)를 쓰지 않는 이유: 빠르게 움직이는 상품은 프레임
  간 IoU가 무너져 트랙이 끊긴다 — 중심 거리 매칭이어야 한다.
- 통과 조건: 어느 한 트랙이라도 `누적 경로 ≥ thr` **또는** `시점 대비 최대
  변위 ≥ thr`, `thr = max(floor_px, size_scale × 평균 bbox 크기)` (원본
  _motion_threshold_for_detection 동형 — 큰 물체는 더 크게 움직여야 한다).
  같은 클래스가 진열+취출로 동시에 있어도 취출 트랙 하나가 클래스를 살린다
  (정체성 판정에 안전한 방향).
- 좌표 계약: left-crop 480×480 (P0-1) — 원본의 픽셀 임계(10/12px)를 그대로
  쓸 수 있는 전제. squash 좌표계에서는 재보정 없이 이식 불가였다.
- fail-open: bbox 없는 검출(=(0,0,0,0))은 변위를 잴 수 없으므로 그 카메라×
  클래스를 면제한다 (filters.py와 동일한 "실패 방향 = 증거 보존" 원칙).
- 적용 지점: VotingEnsemble.combine()의 클래스 거부권 (perception 계층 —
  판정층은 이미 걸러진 득표 순위를 신뢰한다는 층별 책임 유지).
- T1 계측 (docs/0723_tracklet_cost_benefit.md §8, 판정 영향 0): 트랙별
  디코드 위치 통계(first/last/head_obs)를 summary()의 track_detail로
  아카이브에 싣는다. 용도 두 가지 — ① held-object 강등(0713 A-2)을 클래스
  단위가 아닌 **트랙 단위**로 재구현하기 위한 분포 실측 (S2: 같은 클래스의
  carried-in 트랙과 새 취출 트랙은 first_pos가 다르다), ② 트리거당 트랙
  수로 grab 순간의 트랙 단절(fragmentation) 빈도 실측 (G2 재연관 창 도입
  판단 근거). pos는 voting.add_frame과 동일한 "게이트 스킵 포함 디코드
  위치" — head_obs는 표가 아니라 관측 수 기준이다 (entry_conf 미달 저신뢰
  검출도 트랙은 이으므로, held 트랙의 프리롤 존재를 표보다 빠짐없이 센다).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from crk_model.perception.detector import Detection

_ZERO_BBOX = (0.0, 0.0, 0.0, 0.0)


@dataclass
class _Track:
    last_cx: float
    last_cy: float
    first_cx: float
    first_cy: float
    tid: int = -1  # 트랙 id — 투표의 트랙 귀속(트랙릿 투표)용
    camera: str = ""  # held 판정의 스트림 길이 가드(_max_pos) 참조용
    path: float = 0.0  # 누적 이동 경로
    max_disp: float = 0.0  # 시점 대비 최대 변위
    size_sum: float = 0.0  # max(w,h) 누적 (임계 스케일용)
    n: int = 0
    matched_frame: int = -1  # 프레임당 1회 매칭 (동일 프레임 다중 흡수 방지)
    # T1 위치 계측 (모듈 docstring) — 디코드 스트림 위치(pos) 기준
    first_pos: int = -1
    last_pos: int = -1
    head_obs: int = 0  # pos < head_frames 관측 수

    def observe(self, cx: float, cy: float, size: float, frame_idx: int) -> None:
        step = ((cx - self.last_cx) ** 2 + (cy - self.last_cy) ** 2) ** 0.5
        self.path += step
        disp = ((cx - self.first_cx) ** 2 + (cy - self.first_cy) ** 2) ** 0.5
        self.max_disp = max(self.max_disp, disp)
        self.last_cx, self.last_cy = cx, cy
        self.size_sum += size
        self.n += 1
        self.matched_frame = frame_idx

    def passes(self, floor_px: float, size_scale: float) -> bool:
        thr = max(floor_px, size_scale * (self.size_sum / self.n)) if self.n else floor_px
        return self.path >= thr or self.max_disp >= thr


@dataclass
class MotionEvidence:
    """추론된 프레임의 (필터 통과) 검출을 관찰해 카메라×클래스 변위 증거를 쌓는다."""

    floor_px: float = 10.0
    size_scale: float = 0.10  # 원본 bbox_size×0.10 (스케일 적응 임계)
    max_jump_px: float = 150.0  # 원본 max_distance_px 계열 — 트랙 연결 점프 상한
    head_frames: int = 30  # T1: head 구간 길이 — voting head_frames와 동일 규약
    # G2 재연관 창 (0723 문서 §2): 가림/미검출로 끊긴 트랙을 완화 반경으로
    # 다시 잇는다 — grab 순간 단절로 새 트랙이 태어나 변위가 부족해지는
    # 잠재 결함의 보완. 오연결의 실패 방향은 first 승계 → 변위 과대 → 표
    # 생존(fail-open, 증거 보존)이라 보수적 배율로 충분하다.
    reassoc_factor: float = 1.5  # 공백 트랙의 완화 반경 배율 (1.5×150=225px)
    reassoc_window: int = 12  # 완화를 허용하는 최대 공백 (추론 프레임 수, ≈1s)
    # T2 held 트랙 판정 (0713 A-2의 트랙 단위 재구현): head 구간(head_frames)
    # 관측이 held_min_head 이상인 트랙 = carried-in 의심. 0713 §10 실측:
    # 클래스 단위 head가 held 27~33 vs 진짜 취출 0~2로 분리 — 트랙 단위는
    # S2(동일 상품 들고+취출)에서 취출 트랙을 보존하는 것이 개선점.
    held_min_head: int = 5
    # 프리롤 부족 가드 (0713 §6): 관측 스트림이 공칭 프리롤 120프레임의
    # 절반 미만이면 위치 의미가 왜곡 — 카메라 단위로 held 판정 전체 비활성.
    held_min_stream: int = 60
    _tracks: dict[tuple[str, int], list[_Track]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _exempt: set[tuple[str, int]] = field(default_factory=set)
    _frame_idx: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    _track_by_id: dict[int, _Track] = field(default_factory=dict)
    _next_tid: int = 0
    _max_pos: dict[str, int] = field(
        default_factory=lambda: defaultdict(lambda: -1)
    )

    def observe(
        self, camera: str, detections: Sequence[Detection], pos: int | None = None
    ) -> list[int | None]:
        """검출을 트랙에 귀속시키고 검출별 트랙 id를 반환 (트랙릿 투표).

        반환 리스트는 detections와 정렬이 같다. None = 손 또는 bbox 없음
        (트랙 귀속 불가 — 투표 계층에서 클래스 단위 판정으로 폴백).

        pos: 게이트 스킵 포함 디코드 위치 (voting.add_frame과 동일 값) —
        T1 트랙별 위치 계측 입력. None이면 계측 생략 (하위호환)."""
        idx = self._frame_idx[camera]
        self._frame_idx[camera] = idx + 1
        ids: list[int | None] = []
        for d in detections:
            if d.is_hand:
                ids.append(None)  # 손은 래치/hand_path 소관 — 투표 대상이 아님
                continue
            if d.bbox == _ZERO_BBOX:
                self._exempt.add((camera, d.class_id))  # fail-open (모듈 docstring)
                ids.append(None)
                continue
            x1, y1, x2, y2 = d.bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            size = max(x2 - x1, y2 - y1)
            tracks = self._tracks[(camera, d.class_id)]
            best, best_dist = None, float("inf")
            for t in tracks:
                if t.matched_frame == idx:
                    continue  # 이 프레임에서 이미 소비된 트랙
                # G2 재연관 창: 직전 프레임에 이어진 트랙은 기본 반경, 1프레임
                # 이상 끊겼던 트랙은 reassoc_window 안에서 완화 반경으로 잇는다
                # (같은 클래스 버킷 한정 — 승계 오염 방지는 클래스 조건이 담당).
                gap = idx - t.matched_frame
                radius = self.max_jump_px
                if 1 < gap <= self.reassoc_window:
                    radius *= self.reassoc_factor
                dist = ((cx - t.last_cx) ** 2 + (cy - t.last_cy) ** 2) ** 0.5
                if dist <= radius and dist < best_dist:
                    best, best_dist = t, dist
            if best is None:
                best = _Track(
                    last_cx=cx, last_cy=cy, first_cx=cx, first_cy=cy,
                    tid=self._next_tid, camera=camera,
                )
                self._track_by_id[self._next_tid] = best
                self._next_tid += 1
                tracks.append(best)
            best.observe(cx, cy, size, idx)
            if pos is not None:
                if best.first_pos < 0:
                    best.first_pos = pos
                best.last_pos = pos
                if pos < self.head_frames:
                    best.head_obs += 1
                if pos > self._max_pos[camera]:
                    self._max_pos[camera] = pos
            ids.append(best.tid)
        return ids

    def track_qualifies(self, tid: int) -> bool:
        """트랙릿 투표: 이 트랙의 표가 유효한가 — 트랙 자신의 변위 증거.

        클래스 단위(class_motion)보다 정밀하다: 같은 클래스가 진열+취출로
        동시에 있어도 진열 인스턴스 트랙의 표는 몰수되고 움직인 트랙의
        표만 남는다 — "오래 보이는 것 = 표 많은 것" 편향의 종결."""
        t = self._track_by_id.get(tid)
        return t is not None and t.passes(self.floor_px, self.size_scale)

    def track_held(self, tid: int) -> bool:
        """T2 held 판정: 프리롤 head 구간부터 지속 관측된(carried-in) 트랙인가.

        head_obs ≥ held_min_head (지속 등장 — 1프레임 오검출 방어, 0713 §3)
        + 스트림 길이 가드(관측 최대 pos < held_min_stream이면 프리롤 부족 —
        카메라 단위 전체 비활성, 0713 §6 S3). pos 미계측 호출은 head_obs가
        0이라 항상 False (하위호환 = 무영향)."""
        t = self._track_by_id.get(tid)
        if t is None or t.head_obs < self.held_min_head:
            return False
        return self._max_pos.get(t.camera, -1) + 1 >= self.held_min_stream

    def class_motion(self, camera: str, class_id: int) -> bool:
        """이 카메라의 이 클래스 표가 유효한가 — 변위 증거 or 면제."""
        if (camera, class_id) in self._exempt:
            return True
        tracks = self._tracks.get((camera, class_id))
        if not tracks:
            return True  # 관측 없음 = 표도 없음 (moot) — 몰수할 것이 없다
        return any(t.passes(self.floor_px, self.size_scale) for t in tracks)

    def summary(self) -> dict:
        """vote_summary 진단용 — 카메라×클래스별 통과 여부와 최대 경로/임계.

        track_detail (T1): 트랙별 {first/last/obs/head_obs/passed} — 관측 수
        상위 8개까지 (배경 깜빡임으로 생기는 1~2관측 잔트랙의 아카이브
        범람 방지; 전체 트랙 수는 tracks가 이미 담는다)."""
        out: dict[str, dict[int, dict]] = defaultdict(dict)
        for (camera, cid), tracks in self._tracks.items():
            best = max(tracks, key=lambda t: max(t.path, t.max_disp))
            thr = (
                max(self.floor_px, self.size_scale * (best.size_sum / best.n))
                if best.n
                else self.floor_px
            )
            detail = [
                {
                    "first": t.first_pos,
                    "last": t.last_pos,
                    "obs": t.n,
                    "head_obs": t.head_obs,
                    "passed": t.passes(self.floor_px, self.size_scale),
                    "held": self.track_held(t.tid),  # T2 판정 결과 (shadow 관측)
                }
                for t in sorted(tracks, key=lambda t: -t.n)[:8]
            ]
            out[camera][cid] = {
                "passed": self.class_motion(camera, cid),
                "best_path": round(max(best.path, best.max_disp), 1),
                "threshold": round(thr, 1),
                "tracks": len(tracks),
                "track_detail": detail,
            }
        for camera, cid in self._exempt:
            out[camera].setdefault(cid, {"passed": True, "exempt": True})
        return {cam: dict(classes) for cam, classes in out.items()}
