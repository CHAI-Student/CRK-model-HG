"""세션 트레이 메모리 (ledger/tray_memory.py) — 세션-학습 배치 증거.

계약: 정적 planogram(금지) 대체 — 운영 입력 없음, OPEN 리셋(cold-start =
prior 0 = 현행 동작), 키는 (zone, channel)(존마다 좌/우 트레이가 별개 상품
가능), 소비는 likelihood shadow의 log_p_tray 항뿐(Phase 1, 판정 무변경).
등록 게이트: COMPLETE + 무게 뒷받침만 (오판 전파 차단; vision 1위 일치
조건은 5차 ses-10 닭-달걀로 Phase 1에서 제외 — tray_memory.py docstring).
세션 가드: reset(session_id) 후 다른 세션 id의 record/priors_for는
무시/중립 (OPEN 리셋 ↔ 구 세션 잔여 트리거의 워커 경쟁 차단).
"""
from conftest import cand

from crk_model.core.profiles import FREEZER
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.likelihood import WeightLikelihoodScorer
from crk_model.ledger.tray_memory import SessionTrayMemory
from crk_model.perception.detector import Detection
from crk_model.service import ActiveProductStore, TriggerPipeline, TriggerRequest


def _ctx(delta, products, candidates, zone=1, vision_only=False):
    return JudgmentContext(
        zone=zone, profile=FREEZER, delta_weight=delta,
        segments=(), vision_candidates=tuple(candidates),
        active_products=tuple(products), vision_only=vision_only,
    )


def _complete(product, count, reason="freezer_vision_first_single"):
    return JudgmentResult(
        JudgmentStatus.COMPLETE, (ProductCount(product, count),), 0.85, reason
    )


MELONA = ActiveProduct(
    "P44", "메로나", class_id=44, unit_weight=79.0, unit_price=800, stock_qty=40
)
ITEM3 = ActiveProduct(
    "P3", "비비고", class_id=3, unit_weight=224.0, unit_price=3700, stock_qty=35
)


class TestMemoryUnit:
    def test_cold_start_neutral(self):
        m = SessionTrayMemory()
        assert m.log_prior(1, 0, 44) == 0.0
        assert m.priors_for(1, 0, [44, 3]) == {}

    def test_same_tray_boost_other_tray_penalty(self):
        m = SessionTrayMemory(boost=0.7, penalty=2.5)
        m.record(5, 0, 44)
        assert m.log_prior(5, 0, 44) == 0.7      # 같은 트레이 → 가점
        assert m.log_prior(5, 1, 44) == -2.5     # 같은 존 다른 트레이 → 감점
        assert m.log_prior(4, 1, 44) == -2.5     # 다른 존 → 감점 (ses-5 계열)
        assert m.log_prior(4, 1, 3) == 0.0       # 무증거 → 중립

    def test_same_and_other_evidence_is_boost(self):
        # 같은 상품이 두 트레이에 정당하게 존재할 수 있다 — same 증거가
        # 있으면 other 증거와 무관하게 강등하지 않는다.
        m = SessionTrayMemory()
        m.record(5, 0, 44)
        m.record(4, 1, 44)
        assert m.log_prior(4, 1, 44) > 0

    def test_unknown_channel_matches_whole_zone(self):
        # 채널 미상(존 합산 이벤트) — 같은 존 전체를 same으로 완화 매칭
        # (모호할 때 강등하지 않는 fail-open 방향)
        m = SessionTrayMemory()
        m.record(5, 0, 44)
        assert m.log_prior(5, None, 44) > 0
        assert m.log_prior(4, None, 44) < 0

    def test_reset_clears_session_boundary(self):
        m = SessionTrayMemory()
        m.record(5, 0, 44)
        m.reset()
        assert m.log_prior(4, 1, 44) == 0.0
        assert m.snapshot() == {}

    def test_session_guard_blocks_stale_trigger(self):
        # OPEN 리셋(락 안)과 구 세션 잔여 트리거(워커, 락 밖)의 순서 역전:
        # 리셋이 세션 id를 고정하면 불일치 record는 무시, priors_for는 중립.
        m = SessionTrayMemory()
        m.reset("ses-2")
        m.record(1, 0, 44, session_id="ses-1")  # 구 세션 잔여 → 무시
        assert m.snapshot() == {}
        m.record(1, 0, 44, session_id="ses-2")
        assert m.snapshot() == {"1:0": {44: 1}}
        assert m.priors_for(2, 0, [44], session_id="ses-1") == {}  # 중립
        assert m.priors_for(2, 0, [44], session_id="ses-2") == {44: -2.5}
        # id 미제공(라이브러리 직접 사용)은 가드 비활성 — 하위호환
        assert m.priors_for(2, 0, [44]) == {44: -2.5}


class TestScorerTrayPrior:
    def test_ses5_ranking_flips_with_other_tray_penalty(self):
        # 이슈 #17 ses-5 재현 (존4 트리거, delta −230): 존5에서 이미 확정된
        # 44(메로나)가 44×3=237로 score 1위가 되던 것을, 타-트레이 감점
        # (−2.5)이 3×1(비비고 224, 잔차 6g)로 뒤집는다.
        scorer = WeightLikelihoodScorer()
        c = _ctx(
            -230.0, [MELONA, ITEM3],
            [cand(44, 0.85, 58), cand(3, 0.84, 7)], zone=4,
        )
        judgment = _complete(MELONA, 3)
        base = scorer.shadow(c, judgment, sigma_d=2.28)
        assert base["top"]["items"] == [[44, 3]]  # prior 없이는 ses-5 오판 재현

        flipped = scorer.shadow(
            c, judgment, sigma_d=2.28, tray_prior={44: -2.5}
        )
        assert flipped["top"]["items"] == [[3, 1]]
        assert flipped["tray_prior"] == {44: -2.5}
        assert flipped["mismatch"] is True
        # 감점된 항목에는 log_p_tray가 기록된다
        e44 = next(e for e in flipped["ranking"] if e["items"] == [[44, 3]])
        assert e44["log_p_tray"] == -2.5


class TestRecordGate:
    def _pipe(self, memory):
        store = ActiveProductStore()
        store.update([MELONA, ITEM3])
        return TriggerPipeline(None, {1: FREEZER}, store, tray_memory=memory)

    def test_records_only_weight_backed_complete(self):
        m = SessionTrayMemory()
        pipe = self._pipe(m)
        candidates = [cand(44, 0.85, 58), cand(3, 0.84, 7)]

        # PARTIAL(near_gate 등) → 미등록
        partial = JudgmentResult(
            JudgmentStatus.PARTIAL, (ProductCount(MELONA, 1),), 0.5,
            "freezer_vision_first_near_gate",
        )
        pipe._record_tray_evidence(_ctx(-79.0, [MELONA], candidates), partial, 0)
        assert m.snapshot() == {}

        # 무게가 정체성을 고른 예외 경로(refit) → 미등록
        pipe._record_tray_evidence(
            _ctx(-79.0, [MELONA], candidates),
            _complete(MELONA, 1, reason="freezer_vision_first_unique_refit"), 0,
        )
        assert m.snapshot() == {}

        # vision_only(무게 무근거) → 미등록
        pipe._record_tray_evidence(
            _ctx(-79.0, [MELONA], candidates, vision_only=True),
            _complete(MELONA, 1), 0,
        )
        assert m.snapshot() == {}

        # COMPLETE + 무게 뒷받침 → 등록
        pipe._record_tray_evidence(
            _ctx(-79.0, [MELONA], candidates), _complete(MELONA, 1), 0
        )
        assert m.snapshot() == {"1:0": {44: 1}}

    def test_records_despite_vision_top_mismatch(self):
        # ses-10 완화 (tray_memory.py docstring): vision 1위(44, held 오염)가
        # 아닌 3의 COMPLETE 과금도 등록한다 — top 불일치가 흔한 오염 존이
        # 정확히 prior가 필요한 곳이라 일치 게이트는 닭-달걀이었다.
        m = SessionTrayMemory()
        pipe = self._pipe(m)
        candidates = [cand(44, 0.85, 58), cand(3, 0.84, 7)]
        pipe._record_tray_evidence(
            _ctx(-224.0, [MELONA, ITEM3], candidates), _complete(ITEM3, 1), 0
        )
        assert m.snapshot() == {"1:0": {3: 1}}


class _MovingDetector:
    """class_id=44 검출 — 모션 변위 증거 통과용 드리프트."""

    def __init__(self):
        self.calls = 0

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        off = 12.0 * (self.calls % 8)
        return [Detection(44, 0.85, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0))]


def _moving_frames(n):
    return [[[10 if i % 2 == 0 else 200] * 4 for _ in range(4)] for i in range(n)]


def _samples(start, end, n=10, dt=0.1):
    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value, 0.0)))
        ts += dt
    return out


class TestPipelineWiring:
    """1차 트리거 확정 → 2차 트리거(타존)의 shadow에 감점이 실리는 E2E."""

    def test_prior_flows_into_second_trigger_shadow(self):
        memory = SessionTrayMemory()
        store = ActiveProductStore()
        store.update([MELONA])
        pipe = TriggerPipeline(
            _MovingDetector(), {1: FREEZER, 2: FREEZER}, store,
            likelihood_shadow_enabled=True, tray_memory=memory,
        )
        # 존1 ch0에서 메로나 1개 확정 (delta −79) → 등록
        first = pipe.process(
            "s1",
            TriggerRequest(1, {"top": _moving_frames(8)}, _samples(500, 421), 1.0),
        )
        assert first.event.judgment.status is JudgmentStatus.COMPLETE
        assert memory.snapshot() == {"1:0": {44: 1}}

        # 존2 트리거의 shadow 항목에 타-트레이 감점이 실린다
        second = pipe.process(
            "s2",
            TriggerRequest(2, {"top": _moving_frames(8)}, _samples(500, 421), 2.0),
        )
        entry = second.trace.likelihood_shadow[0]
        assert entry["tray_prior"] == {44: -2.5}

    def test_session_guard_wired_through_pipeline(self):
        # ModelService가 reset("other")로 세션을 고정한 뒤 다른 세션 id의
        # 트리거가 처리되면(리셋 경쟁 시나리오) 등록이 무시되는지 — pipeline이
        # session_id를 record까지 실어 나르는 배선 검증.
        memory = SessionTrayMemory()
        memory.reset("other-session")
        store = ActiveProductStore()
        store.update([MELONA])
        pipe = TriggerPipeline(
            _MovingDetector(), {1: FREEZER}, store, tray_memory=memory
        )
        out = pipe.process(
            "s1",
            TriggerRequest(1, {"top": _moving_frames(8)}, _samples(500, 421), 1.0),
        )
        assert out.event.judgment.status is JudgmentStatus.COMPLETE
        assert memory.snapshot() == {}  # 세션 불일치 → 미등록

    def test_without_memory_no_prior_field(self):
        store = ActiveProductStore()
        store.update([MELONA])
        pipe = TriggerPipeline(
            _MovingDetector(), {1: FREEZER}, store, likelihood_shadow_enabled=True
        )
        out = pipe.process(
            "s1",
            TriggerRequest(1, {"top": _moving_frames(8)}, _samples(500, 421), 1.0),
        )
        assert "tray_prior" not in out.trace.likelihood_shadow[0]
