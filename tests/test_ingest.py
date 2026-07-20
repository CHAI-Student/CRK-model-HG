"""ingest: 멱등성(I7), delta 계산, 구간화 순서(D4·QA Q3), 반품 stabilization."""
from crk_model.core.profiles import REFRIGERATOR
from crk_model.ingest import IdempotencyRegistry, LoadcellAnalyzer, LoadcellSample


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_samples(plateaus, dt=0.1):
    """plateaus: [(value, n_samples), ...] → 계단형 시계열."""
    samples, ts = [], 0.0
    for value, n in plateaus:
        for _ in range(n):
            samples.append(LoadcellSample(ts, (value, 0.0)))  # 트레이 분리: 하중은 단일 채널에
            ts += dt
    return samples


def make_dual(plateaus_ch0, plateaus_ch1, dt=0.1):
    """두 트레이(채널)의 계단형 시계열을 나란히 생성. 총 샘플 수는 동일해야 함."""
    expand = lambda ps: [v for v, n in ps for _ in range(n)]
    ch0, ch1 = expand(plateaus_ch0), expand(plateaus_ch1)
    assert len(ch0) == len(ch1)
    return [
        LoadcellSample(k * dt, (a, b)) for k, (a, b) in enumerate(zip(ch0, ch1))
    ]


def analyzer():
    return LoadcellAnalyzer(
        REFRIGERATOR, stable_window=3, stability_threshold_grams=2.0, stabilization_wait_s=1.0
    )


class TestIdempotency:
    def test_duplicate_within_ttl_returns_original_session(self):
        clock = FakeClock()
        reg = IdempotencyRegistry(ttl_seconds=5.0, clock=clock)
        key = IdempotencyRegistry.key_for(1, {"top": "/a.avi", "side": "/b.avi"})
        first = reg.register(key, "sid-1")
        assert not first.duplicate
        clock.t = 3.0
        second = reg.register(key, "sid-2")
        assert second.duplicate and second.session_id == "sid-1"  # I7

    def test_expires_after_ttl(self):
        clock = FakeClock()
        reg = IdempotencyRegistry(ttl_seconds=5.0, clock=clock)
        key = IdempotencyRegistry.key_for(1, {"top": "/a.avi"})
        reg.register(key, "sid-1")
        clock.t = 6.0
        assert not reg.register(key, "sid-3").duplicate


class TestLoadcellAnalyzer:
    def test_removal_delta_and_segments(self):
        # 500g → -170 → -178: delta=-348, 세그먼트 2개 (시계열 정보 보존)
        samples = make_samples([(500, 10), (330, 10), (152, 10)])
        a = analyzer().analyze(samples)
        assert a.stabilized
        assert abs(a.delta_weight - (-348)) < 1.0
        assert len(a.segments) == 2
        assert abs(a.segments[0].delta_grams - (-170)) < 1.0
        assert abs(a.segments[1].delta_grams - (-178)) < 1.0

    def test_return_needs_stabilization_blocks_segmentation(self):
        # QA Q3 ①: 반품(+delta)의 마지막 안정 구간이 1.0s 미만 → 구간화 보류
        samples = make_samples([(500, 10), (600, 4)])  # 마지막 plateau 0.3s
        a = analyzer().analyze(samples)
        assert not a.stabilized
        assert a.segments == ()
        assert a.reason == "needs_return_stabilization"

    def test_return_stabilized_after_wait(self):
        samples = make_samples([(500, 10), (600, 15)])  # 마지막 plateau 1.4s
        a = analyzer().analyze(samples)
        assert a.stabilized
        assert abs(a.delta_weight - 100) < 1.0
        assert len(a.segments) == 1

    def test_drift_absorbed_by_plateau_means(self):
        # 느린 드리프트(±1g)는 plateau 평균에 흡수 → 가짜 세그먼트 없음 (QA Q3 ②)
        samples = make_samples([(500, 8), (501, 8), (500, 8)])
        a = analyzer().analyze(samples)
        assert a.segments == ()  # 1g 스텝 < segment_step 5g


class TestPerChannelAnalysis:
    """트레이 분리 구조(크로스토크 <5g 실측): 채널별 분석 후 결합."""

    def test_simultaneous_events_on_both_trays_resolve_separately(self):
        # 두 트레이에서 동시 집기: 합산이면 -324 한 덩어리지만,
        # 채널별로는 단품 delta 두 개(-100, -224)로 분리된다
        samples = make_dual(
            [(500, 10), (400, 10)],
            [(300, 10), (76, 10)],
        )
        a = analyzer().analyze(samples)
        assert a.stabilized
        assert abs(a.delta_weight - (-324)) < 1.0
        assert len(a.segments) == 2
        deltas = sorted(s.delta_grams for s in a.segments)
        assert abs(deltas[0] - (-224)) < 1.0
        assert abs(deltas[1] - (-100)) < 1.0

    def test_quiet_tray_noise_excluded_from_zone_delta(self):
        # 이웃 트레이의 sub-threshold 변동(4g < min_weight_change 5g)은
        # 존 delta를 오염시키지 않는다 — 합산 방식에서는 -170-(-4)=-166으로 샜음
        samples = make_dual(
            [(500, 15), (330, 15)],
            [(200, 15), (204, 15)],  # +4g: 채널 게이트 미달
        )
        a = analyzer().analyze(samples)
        assert a.stabilized
        assert abs(a.delta_weight - (-170)) < 1.0
        assert len(a.segments) == 1

    def test_flat_neighbor_does_not_block(self):
        # 무이벤트 트레이(전 구간 평탄 = plateau 1개)는 존 확정을 막지 않음
        samples = make_dual(
            [(500, 10), (330, 10)],
            [(250, 20)],
        )
        a = analyzer().analyze(samples)
        assert a.stabilized
        assert abs(a.delta_weight - (-170)) < 1.0
        assert abs(a.baseline - 750) < 1.0  # 존 baseline = 두 트레이 합

    def test_disturbed_neighbor_blocks_zone(self):
        # 이웃 트레이가 움직였는데 안정되지 못하면(램프) 존 delta 확정 불가
        ramp = [(v, 1) for v in range(200, 400, 10)]
        samples = make_dual([(500, 20)], ramp)
        a = analyzer().analyze(samples)
        assert not a.stabilized
        assert a.reason == "insufficient_stable_regions"
