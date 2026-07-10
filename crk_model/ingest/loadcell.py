"""로드셀 분석 — 구간화는 판단 엔진이 아니라 ingest 소속.

순서 고정: 반품(+delta)은 stabilization(마지막 안정 구간이
stabilization_wait 이상 지속) 완료 후에만 구간화한다. 미완이면 segments를
비워 반환하고 stabilized=False로 재수집을 요구한다.

드리프트 대응: delta·segment는 절대값이 아니라 안정 plateau 간
평균 차이로만 계산 → 냉동 사이클의 느린 영점 드리프트는 plateau 평균에
흡수된다(재영점 효과). 구간 스텝 임계는 SensorProfile 소속.

설계 v2 (셀 단위 모델): 물리 채널을 합산하지 않는다 — 채널 = 셀(존 내
좌/우 로드셀)이고 전제 3(한 로드셀에 한 상품 종류)에 의해 채널별 delta가
정체성·개수 판정의 입력이다. wire의 `filtered_value: [ch0, ch1]` 배열
인덱스가 채널 식별자다. analyze_cells()가 채널별 plateau 분석을 반환한다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import WeightSegment


@dataclass(frozen=True)
class LoadcellSample:
    ts: float
    values: tuple[float, ...]  # 물리 채널 (존당 2채널) — 채널 = 셀, 합산 금지 (v2)

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
        stable_window: int = 5,
        stability_threshold_grams: float = 2.0,
        stabilization_wait_s: float = 1.0,
    ):
        self._profile = profile
        self._window = stable_window
        self._std_threshold = stability_threshold_grams
        self._stab_wait = stabilization_wait_s

    def analyze_cells(self, samples: Sequence[LoadcellSample]) -> tuple[LoadcellAnalysis, ...]:
        """채널(=셀)별 독립 분석 (설계 v2). 반환 순서 = 채널 인덱스.

        샘플마다 채널 수가 다르면(전송 결손) 가장 긴 배열 기준으로 채널을
        정하고, 해당 채널 값이 없는 샘플은 그 채널 시계열에서 제외한다.
        """
        channels = max((len(s.values) for s in samples), default=0)
        out = []
        for ch in range(channels):
            series = [s for s in samples if len(s.values) > ch]
            out.append(self._analyze_series(series, [s.values[ch] for s in series]))
        return tuple(out)

    def analyze(self, samples: Sequence[LoadcellSample]) -> LoadcellAnalysis:
        """존 총량(채널 합산) 분석 — 배리어의 로드셀 안정 판정 등 총량이
        필요한 소비자용. 판정 입력은 analyze_cells()를 쓴다 (v2)."""
        return self._analyze_series(list(samples), [s.total for s in samples])

    def _analyze_series(
        self, samples: Sequence[LoadcellSample], totals: list[float]
    ) -> LoadcellAnalysis:
        if len(samples) < self._window * 2:
            return LoadcellAnalysis(0.0, (), False, 0.0, "insufficient_samples")

        plateaus = self._stable_plateaus(totals)
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
