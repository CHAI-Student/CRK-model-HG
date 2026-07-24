"""BOCPD 로드셀 분석기 (primary) — 베이지안 온라인 변화점 검출.

Adams & MacKay 2007 (arXiv:0710.3742)의 run-length 사후분포 재귀를 이 도메인에
맞게 축소 구현한 것 (claudedocs/research_judgment_performance_20260722.md §2).

왜: 현행 `_stable_plateaus`는 "3연속 샘플 std ≤ 2.5g"라는 경성 창을 요구한다.
0.8s 캐던스에서 이 조건은 — post-roll 4s = 5샘플이면 마진 1샘플(#14
insufficient_stable_regions → 무음 0원), 1.6초 간격 연속 취출이면 플래토가
2샘플뿐이라 성립 불가(issue #16 로그 3: ch0 delta 뭉개짐 → 오과금 연쇄의
출발점) — 이다. BOCPD는 연속성 요건 없이 "현재 run이 새 레벨일 확률"과
레벨 추정을 동시에 얻는다.

모델: 관측 노이즈 고정 가우시안(σ, 기본 2.5g — 5g 양자화 경계 토글 허용값),
run별 평균에 켤레 정규 사전(모호 사전 κ₀=0.01 — 새 레벨이 어디로 튀어도
changepoint 가설이 유효). hazard 상수 H (기본 0.1 ≈ 평균 run 10샘플 = 8s).

**primary 승격 (2026-07-23)**: shadow 병행 실측(63관측/2 mismatch, 이후
17건 mismatch 0)을 거쳐 기본 분석기로 확정 — shadow 관측 장치는 2026-07-24
삭제, plateau는 `MODEL__LOADCELL__ANALYZER=plateau` 롤백 스위치로 유지.
계산은 순수 파이썬 O(n·max_run), 트리거당 샘플 ~20-70개라 비용 무시 가능.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import WeightSegment
from crk_model.ingest.loadcell import (
    ChannelWeightEvent,
    LoadcellAnalysis,
    LoadcellSample,
)


@dataclass(frozen=True)
class BocpdSegment:
    start: int  # 샘플 인덱스 (포함)
    end: int  # 샘플 인덱스 (포함)
    level: float  # 구간 레벨 추정 (샘플 평균)


@dataclass(frozen=True)
class BocpdChannel:
    channel: int
    segments: tuple[BocpdSegment, ...]
    delta: float  # 마지막 레벨 − 첫 레벨
    delta_std: float  # σ·√(1/n_first + 1/n_last)


@dataclass(frozen=True)
class BocpdAnalysis:
    delta_weight: float  # 존 총 delta = Σ 채널 delta
    delta_std: float
    channels: tuple[BocpdChannel, ...]
    reason: str = ""  # "" | "insufficient_samples"


class BocpdAnalyzer:
    def __init__(
        self,
        sigma: float = 2.5,
        hazard: float = 0.1,
        prior_kappa: float = 0.01,
        max_run: int = 128,
    ):
        self._sigma2 = sigma * sigma
        self._sigma = sigma
        self._log_h = math.log(hazard)
        self._log_1mh = math.log(1.0 - hazard)
        self._prior_kappa = prior_kappa
        self._max_run = max_run

    def analyze(self, samples: Sequence[LoadcellSample]) -> BocpdAnalysis:
        if len(samples) < 2:
            return BocpdAnalysis(0.0, 0.0, (), reason="insufficient_samples")
        n_ch = len(samples[0].values)
        channels: list[BocpdChannel] = []
        for ch in range(n_ch):
            xs = [s.values[ch] for s in samples]
            channels.append(self._analyze_channel(ch, xs))
        delta = sum(c.delta for c in channels)
        var = sum(c.delta_std * c.delta_std for c in channels)
        return BocpdAnalysis(delta, math.sqrt(var), tuple(channels))

    def _analyze_channel(self, ch: int, xs: list[float]) -> BocpdChannel:
        maps = self._map_run_lengths(xs)
        segs = self._segments(xs, maps)
        first, last = segs[0], segs[-1]
        n_f = first.end - first.start + 1
        n_l = last.end - last.start + 1
        delta = last.level - first.level
        std = self._sigma * math.sqrt(1.0 / n_f + 1.0 / n_l)
        return BocpdChannel(ch, tuple(segs), delta, std)

    def _map_run_lengths(self, xs: list[float]) -> list[int]:
        """샘플별 MAP run length. 메시지 = (log가중, 사후 μ, 사후 κ)."""
        mu0 = xs[0]  # 사전 평균 — κ₀가 모호해 값 자체는 거의 무의미
        msgs: dict[int, tuple[float, float, float]] = {
            0: (0.0, mu0, self._prior_kappa)
        }
        maps = [0]
        for x in xs[1:]:
            growth: dict[int, tuple[float, float, float]] = {}
            cp_terms: list[float] = []
            for r, (lw, mu, kappa) in msgs.items():
                # 예측분포: N(x; μ, σ²(1 + 1/κ))
                var = self._sigma2 * (1.0 + 1.0 / kappa)
                lpred = -0.5 * math.log(2.0 * math.pi * var) - (x - mu) ** 2 / (2.0 * var)
                # 켤레 갱신 (run이 x를 흡수)
                mu_p = (kappa * mu + x) / (kappa + 1.0)
                if r + 1 <= self._max_run:
                    growth[r + 1] = (lw + lpred + self._log_1mh, mu_p, kappa + 1.0)
                cp_terms.append(lw + lpred + self._log_h)
            new_msgs = dict(growth)
            new_msgs[0] = (_logsumexp(cp_terms), mu0, self._prior_kappa)
            # 정규화 (수치 안정) + MAP
            z = _logsumexp([lw for lw, _, _ in new_msgs.values()])
            msgs = {r: (lw - z, mu, k) for r, (lw, mu, k) in new_msgs.items()}
            maps.append(max(msgs, key=lambda r: msgs[r][0]))
        return maps

    def _segments(self, xs: list[float], maps: list[int]) -> list[BocpdSegment]:
        """MAP run length에서 구간을 역방향 재구성 — 레벨은 구간 샘플 평균.

        경계 부기: changepoint 메시지(r=0)는 점프 샘플이 도착하기 **전** 단계에서
        생성되고 점프 샘플부터 흡수하므로, 시각 t·run 길이 r의 흡수 구간은
        (t−r+1 .. t)다. 재구성 후 레벨 차가 2σ 이내인 인접 구간은 병합한다
        (경계 1샘플 파편이 첫/끝 플래토의 n을 깎아 std를 부풀리는 것 방지)."""
        segs: list[BocpdSegment] = []
        e = len(xs) - 1
        while e >= 0:
            s = min(e, max(0, e - maps[e] + 1))
            level = sum(xs[s : e + 1]) / (e - s + 1)
            segs.append(BocpdSegment(s, e, level))
            e = s - 1
        segs.reverse()
        merged: list[BocpdSegment] = []
        for seg in segs:
            if merged and abs(seg.level - merged[-1].level) <= 2.0 * self._sigma:
                prev = merged[-1]
                n_prev = prev.end - prev.start + 1
                n_cur = seg.end - seg.start + 1
                level = (prev.level * n_prev + seg.level * n_cur) / (n_prev + n_cur)
                merged[-1] = BocpdSegment(prev.start, seg.end, level)
            else:
                merged.append(seg)
        return merged


def _logsumexp(vals: list[float]) -> float:
    m = max(vals)
    if m == -math.inf:
        return m
    return m + math.log(sum(math.exp(v - m) for v in vals))


class BocpdLoadcellAnalyzer:
    """BOCPD를 primary 분석기로 승격할 때의 어댑터 — LoadcellAnalyzer 계약 동형.

    `MODEL__LOADCELL__ANALYZER=bocpd`로 선택된다 (기본 plateau — 승격은
    아카이브 실측에서 shadow가 우세할 때만, 레포 관행). 반환 계약은
    LoadcellAnalysis 그대로: reason 문자열(insufficient_* /
    needs_return_stabilization), min_weight_change 채널 게이트, 세그먼트
    스텝 임계, 반품 안정화 대기(QA Q3 ①), 채널 이벤트(멀티트레이 2단계)
    전부 plateau 경로와 동일 의미론을 유지한다 — 바뀌는 것은 "안정 구간"의
    정의(3연속 std 창 → run-length 사후분포)뿐이다.
    """

    name = "bocpd"

    def __init__(
        self,
        profile: SensorProfile,
        *,
        sigma: float = 2.5,
        hazard: float = 0.1,
        stabilization_wait_s: float = 1.0,
    ):
        self._profile = profile
        self._inner = BocpdAnalyzer(sigma=sigma, hazard=hazard)
        self._stab_wait = stabilization_wait_s

    def analyze(self, samples: Sequence[LoadcellSample]) -> LoadcellAnalysis:
        if len(samples) < 2:
            return LoadcellAnalysis(0.0, (), False, 0.0, "insufficient_samples")
        res = self._inner.analyze(samples)
        min_change = self._profile.min_weight_change_grams
        step = self._profile.segment_step_grams
        baseline = sum(c.segments[0].level for c in res.channels)
        events: list[ChannelWeightEvent] = []
        pending_delta = 0.0
        pending = False
        moved = False
        for c in res.channels:
            if len(c.segments) >= 2:
                moved = True
            if abs(c.delta) < min_change:
                continue  # 평탄/노이즈 트레이 — baseline에만 기여
            if c.delta > 0:
                # 반품 안정화 대기 (QA Q3 ①): 마지막 레벨이 충분히 지속돼야
                # 구간화 — plateau 경로와 동일 계약.
                last = c.segments[-1]
                duration = samples[last.end].ts - samples[last.start].ts
                if duration < self._stab_wait:
                    pending = True
                    pending_delta += c.delta
                    continue
            segs = tuple(
                WeightSegment(
                    samples[prev.end].ts, samples[cur.start].ts, cur.level - prev.level
                )
                for prev, cur in zip(c.segments, c.segments[1:], strict=False)
                if abs(cur.level - prev.level) >= step
            )
            events.append(ChannelWeightEvent(c.channel, c.delta, segs))
        if pending:
            delta = pending_delta + sum(e.delta_grams for e in events)
            return LoadcellAnalysis(
                delta, (), False, baseline, "needs_return_stabilization"
            )
        if events:
            segments = sorted(
                (s for e in events for s in e.segments), key=lambda s: s.start_ts
            )
            delta = sum(e.delta_grams for e in events)
            return LoadcellAnalysis(
                delta, tuple(segments), True, baseline, events=tuple(events)
            )
        if moved:
            # 변화는 있었지만 전 채널 게이트 미달 — 합산 delta를 실어 보내
            # pipeline의 below_min_weight_change 스킵이 판단 (plateau 동형)
            return LoadcellAnalysis(res.delta_weight, (), True, baseline)
        # 전 채널 평탄 — plateau 경로의 "전 채널 평탄" 계약과 동일하게
        # vision_only 강제 사유를 유지한다 (로드셀이 트리거를 냈는데 변화가
        # 안 보이면 로드셀 판독을 신뢰하지 않는 보수적 방향).
        return LoadcellAnalysis(0.0, (), False, baseline, "insufficient_stable_regions")
