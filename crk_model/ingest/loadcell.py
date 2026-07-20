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
        """존 분석 진입점 — 다채널 샘플은 채널(트레이)별로 분석해 결합한다.

        하드웨어 실측(2026-07): 존의 두 로드셀은 각자 트레이로 하중이
        분리되어 있고 채널 간 크로스토크 < 5g(1 양자화 스텝). 따라서
        상품 이벤트는 항상 단일 채널에 온전히 실리며, 합산 분석은
        조용한 트레이의 노이즈를 delta에 섞고 동시 이벤트를 뭉갠다.
        채널별로 plateau 분석을 돌리고 |delta| >= min_weight_change인
        채널만 존 delta에 기여시킨다. 반환 계약(reason 문자열 포함)은
        기존 합산 분석과 동일하다.
        """
        if not samples:
            return LoadcellAnalysis(0.0, (), False, 0.0, "insufficient_samples")
        n_ch = len(samples[0].values)
        if n_ch <= 1:
            return self._analyze_series(samples)

        # 1패스: 채널별 분석 + 분류
        settled: list[LoadcellAnalysis] = []      # plateau >=2, 안정 완료
        pending: list[LoadcellAnalysis] = []      # 반품 stabilization 대기
        flat_baseline = 0.0                       # 무이벤트(평탄) 트레이 baseline
        for ch in range(n_ch):
            series = [LoadcellSample(s.ts, (s.values[ch],)) for s in samples]
            a = self._analyze_series(series)
            if a.reason == "insufficient_samples":
                return a
            if a.stabilized:
                settled.append(a)
                continue
            if a.reason == "needs_return_stabilization":
                pending.append(a)
                continue
            totals = [s.total for s in series]
            mean = sum(totals) / len(totals)
            std = (sum((x - mean) ** 2 for x in totals) / len(totals)) ** 0.5
            if std <= self._std_threshold:
                # 전 구간 평탄(plateau 1개) = 무이벤트 트레이 — 존 확정을
                # 막지 않고 baseline에만 기여한다.
                flat_baseline += mean
                continue
            # 움직였는데 안정에 실패한 트레이 → 존 delta 확정 불가
            return LoadcellAnalysis(0.0, (), False, 0.0, a.reason)

        baseline = flat_baseline + sum(a.baseline for a in settled + pending)

        # 반품 대기 채널이 있으면 존 전체가 대기: 구 계약대로 delta는 실어
        # 보내되(판정은 delta를 쓴다) 구간화는 보류(segments=()).
        if pending:
            delta = sum(a.delta_weight for a in pending) + sum(
                a.delta_weight
                for a in settled
                if abs(a.delta_weight) >= self._profile.min_weight_change_grams
            )
            return LoadcellAnalysis(
                delta, (), False, baseline, "needs_return_stabilization"
            )

        gated = [
            a for a in settled
            if abs(a.delta_weight) >= self._profile.min_weight_change_grams
        ]
        if gated:
            delta = sum(a.delta_weight for a in gated)
            segments = sorted(
                (seg for a in gated for seg in a.segments),
                key=lambda seg: seg.start_ts,
            )
            return LoadcellAnalysis(delta, tuple(segments), True, baseline)
        if settled:
            # 변화는 관측됐지만 전부 게이트 미달 — 합산 delta를 그대로 내보내
            # pipeline의 below_min_weight_change 스킵이 판단하게 한다 (구
            # 합산 분석과 동일 계약).
            return LoadcellAnalysis(
                sum(a.delta_weight for a in settled), (), True, baseline
            )
        # 전 채널 평탄: 변화 자체가 없음 (구 합산 분석의 flat 동작과 동일)
        return LoadcellAnalysis(
            0.0, (), False, baseline, "insufficient_stable_regions"
        )

    def _analyze_series(self, samples: Sequence[LoadcellSample]) -> LoadcellAnalysis:
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
