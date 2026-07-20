"""로드셀 분석 — D4: 구간화는 판단 엔진이 아니라 ingest 소속.

순서 고정 (QA Q3 ①): 반품(+delta)은 stabilization(마지막 안정 구간이
stabilization_wait 이상 지속) 완료 후에만 구간화한다. 미완이면 segments를
비워 반환하고 stabilized=False로 재수집을 요구한다.

드리프트 대응 (QA Q3 ②): delta·segment는 절대값이 아니라 안정 plateau 간
평균 차이로만 계산 → 냉동 사이클의 느린 영점 드리프트는 plateau 평균에
흡수된다(재영점 효과). 구간 스텝 임계는 SensorProfile 소속.

계약: 물리 로드셀 채널은 평균이 아니라 합산해 존 총량으로 쓴다 (다이어그램 11).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import WeightSegment


@dataclass(frozen=True)
class LoadcellSample:
    ts: float
    values: tuple[float, ...]  # 물리 채널 (존당 2채널) — 합산

    @property
    def total(self) -> float:
        return sum(self.values)


@dataclass(frozen=True)
class _Plateau:
    start: int
    end: int
    mean: float


@dataclass(frozen=True)
class LoadcellAnalysis:
    delta_weight: float
    segments: tuple[WeightSegment, ...]
    stabilized: bool
    baseline: float
    reason: str = ""


class LoadcellAnalyzer:
    def __init__(
        self,
        profile: SensorProfile,
        *,
        stable_window: int = 3,
        stability_threshold_grams: float = 2.5,
        stabilization_wait_s: float = 1.0,
    ):
        # 기본값의 시간 의미 (IO-BOARD 2.0.3 이후 샘플링 0.8s 기준):
        # - stable_window=3 → plateau 성립에 연속 2.4s 안정 필요.
        #   (구 기본 5는 0.1s 샘플링 시절 값 — 0.8s에서는 4s가 되어 과도)
        # - stability_threshold_grams=2.5 → 5g 양자화 와이어에서 bin 경계
        #   토글 1회가 섞인 창(std≈2.36)까지 안정으로 허용. 2.0이면 경계에
        #   걸린 참값이 영영 plateau를 못 만든다.
        self._profile = profile
        self._window = stable_window
        self._std_threshold = stability_threshold_grams
        self._stab_wait = stabilization_wait_s

    def analyze(self, samples: Sequence[LoadcellSample]) -> LoadcellAnalysis:
        if len(samples) < self._window * 2:
            return LoadcellAnalysis(0.0, (), False, 0.0, "insufficient_samples")

        plateaus = self._stable_plateaus([s.total for s in samples])
        if len(plateaus) < 2:
            return LoadcellAnalysis(0.0, (), False, 0.0, "insufficient_stable_regions")

        baseline = plateaus[0].mean
        delta = plateaus[-1].mean - baseline

        # QA Q3 ①: 반품은 stabilization 완료 후에만 구간화 (순서 고정)
        if delta > 0:
            last = plateaus[-1]
            duration = samples[last.end].ts - samples[last.start].ts
            if duration < self._stab_wait:
                return LoadcellAnalysis(delta, (), False, baseline, "needs_return_stabilization")

        segments: list[WeightSegment] = []
        for prev, cur in zip(plateaus, plateaus[1:], strict=False):
            step = cur.mean - prev.mean
            if abs(step) >= self._profile.segment_step_grams:
                segments.append(
                    WeightSegment(samples[prev.end].ts, samples[cur.start].ts, step)
                )
        return LoadcellAnalysis(delta, tuple(segments), True, baseline)

    def _stable_plateaus(self, totals: list[float]) -> list[_Plateau]:
        n = len(totals)
        w = self._window
        stable = [False] * n
        for i in range(w - 1, n):
            window = totals[i - w + 1 : i + 1]
            mean = sum(window) / w
            std = (sum((x - mean) ** 2 for x in window) / w) ** 0.5
            if std <= self._std_threshold:
                # 윈도우 끝 인덱스만 마킹 — 전체 마킹은 계단 경계에서
                # 인접 plateau를 하나로 병합시킴 (구간 소실)
                stable[i] = True
        plateaus: list[_Plateau] = []
        start = None
        for i in range(n + 1):
            if i < n and stable[i]:
                if start is None:
                    start = i
            elif start is not None:
                seg = totals[start:i]
                plateaus.append(_Plateau(start, i - 1, sum(seg) / len(seg)))
                start = None
        return plateaus
