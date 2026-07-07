"""인과 배리어 (D1, I17) — "시간이 지나서"가 아니라 "인과적으로 완결되어서" 확정.

조작적 정의 (I17):
  ① 존별 enqueued == processed (큐 정합)
  ② 로드셀 안정 판정 (SensorProfile 기준 — 명시적으로 불안정 보고된 존만 차단)
  ③ (카메라 seq 도입 시, D2) close watermark 이전 trigger 전원 도착

고정 debounce는 이 배리어의 상한 타임아웃으로 강등된다 (gateway 소관).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class BarrierStatus:
    satisfied: bool
    pending: tuple[str, ...]  # I8: 미충족 사유를 기계가 읽을 수 있게


class CausalBarrier:
    def __init__(self) -> None:
        self._enqueued: Counter[int] = Counter()
        self._processed: Counter[int] = Counter()
        self._unstable: set[int] = set()
        self._watermark: dict[int, int] = {}  # D2: zone -> close 이전 마지막 seq
        self._last_seq: dict[int, int] = {}

    # -- 큐 정합 (①): 현행 notify_trigger_enqueued/processed 카운터 승격 --
    def notify_enqueued(self, zone: int) -> None:
        self._enqueued[zone] += 1

    def notify_processed(self, zone: int) -> None:
        self._processed[zone] += 1

    # -- 로드셀 안정 (②) --
    def set_loadcell_stable(self, zone: int, stable: bool) -> None:
        if stable:
            self._unstable.discard(zone)
        else:
            self._unstable.add(zone)

    # -- 카메라 seq watermark (③, 선택) --
    def note_seq(self, zone: int, seq: int) -> None:
        self._last_seq[zone] = max(self._last_seq.get(zone, -1), seq)

    def set_close_watermark(self, seq_by_zone: dict[int, int]) -> None:
        self._watermark = dict(seq_by_zone)

    def status(self) -> BarrierStatus:
        pending: list[str] = []
        for zone in sorted(set(self._enqueued) | set(self._processed)):
            gap = self._enqueued[zone] - self._processed[zone]
            if gap > 0:
                pending.append(f"zone{zone}:queue_pending({gap})")
        for zone in sorted(self._unstable):
            pending.append(f"zone{zone}:loadcell_unstable")
        for zone, wm in sorted(self._watermark.items()):
            last = self._last_seq.get(zone, -1)
            if last < wm:
                pending.append(f"zone{zone}:seq_gap(last={last},watermark={wm})")
        return BarrierStatus(satisfied=not pending, pending=tuple(pending))
