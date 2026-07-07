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
            samples.append(LoadcellSample(ts, (value / 2, value / 2)))
            ts += dt
    return samples


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
        assert a.segments == ()  # 1g 스텝 < segment_step 4g
