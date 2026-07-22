"""무게 우도 score shadow (docs/0722_weight_likelihood_design.md Phase 1).

계약: 판정 무변경 — scorer는 순위·diff만 산출한다. clamp(±log k)가 I-V의
연속판(무게는 거부권)임을 경계 케이스로 고정하고, 파이프라인 배선은
trace.likelihood_shadow 기록 여부로 검증한다.
"""
import math

from conftest import cand

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
)
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.likelihood import WeightLikelihoodScorer
from crk_model.ledger.archive import _trace_to_dict
from crk_model.perception.detector import Detection
from crk_model.service import ActiveProductStore, TriggerPipeline, TriggerRequest
from crk_model.service.pipeline import TriggerTrace


def ctx(delta, products, candidates, profile=FREEZER, vision_only=False):
    return JudgmentContext(
        zone=1, profile=profile, delta_weight=delta,
        segments=(), vision_candidates=tuple(candidates),
        active_products=tuple(products), vision_only=vision_only,
    )


def judged(product, count, conf=0.8):
    return JudgmentResult(
        JudgmentStatus.COMPLETE, (ProductCount(product, count),), conf,
        reason="freezer_vision_first_single",
    )


BAGEL = ActiveProduct(
    "P27", "베이글", class_id=27, unit_weight=155.0, unit_price=2800, stock_qty=8
)
DUMPLING = ActiveProduct(
    "P13", "만두", class_id=13, unit_weight=185.0, unit_price=2100, stock_qty=8
)


class TestApplicability:
    def test_refrigerated_profile_not_applicable(self, cola):
        scorer = WeightLikelihoodScorer()
        c = ctx(-100.0, [cola], [cand(1)], profile=REFRIGERATOR)
        assert scorer.shadow(c, judged(cola, 1)) is None

    def test_return_delta_not_applicable(self, cola):
        scorer = WeightLikelihoodScorer()
        assert scorer.shadow(ctx(+100.0, [cola], [cand(1)]), judged(cola, 1)) is None

    def test_no_candidates_not_applicable(self, cola):
        scorer = WeightLikelihoodScorer()
        assert scorer.shadow(ctx(-100.0, [cola], []), judged(cola, 1)) is None


class TestScoring:
    def test_agreement_no_mismatch(self, cola):
        # 단독 후보·잔차 0 — 현행 판정과 score 1위 일치
        scorer = WeightLikelihoodScorer()
        entry = scorer.shadow(
            ctx(-100.0, [cola], [cand(1, conf=0.9, votes=20)]), judged(cola, 1)
        )
        assert entry is not None
        assert entry["mismatch"] is False
        assert entry["top"]["items"] == [[1, 1]]
        assert entry["current"]["score"] == entry["top"]["score"]

    def test_case_c_recorded_as_diff(self):
        # 실사고 C (issue #16): 베이글×5 (잔차 32, 34표, conf 1.0) vs
        # 만두×4 (잔차 3, 25표, conf 0.8). 현행 판정(3b 중재)은 베이글×5.
        # k=20 기본에서는 무게 우도가 만두를 1위로 올릴 수 있다 — Phase 1은
        # 그 diff를 기록하는 것 자체가 목적 (승격은 실측 후).
        scorer = WeightLikelihoodScorer()
        c = ctx(
            -775.0 + 32.0,  # 5×155=775, 잔차 32 → delta=-743
            [BAGEL, DUMPLING],
            [cand(27, conf=1.0, votes=34), cand(13, conf=0.8, votes=25)],
        )
        entry = scorer.shadow(c, judged(BAGEL, 5, conf=1.0))
        assert entry is not None
        items = {tuple(map(tuple, e["items"])) for e in entry["ranking"]}
        assert ((27, 5),) in items and ((13, 4),) in items
        assert entry["current"]["items"] == [[27, 5]]
        assert isinstance(entry["mismatch"], bool)

    def test_k1_disables_weight_ranking_follows_vision(self):
        # k=1 → clamp 폭 0 = 무게 무력. 잔차가 아무리 유리해도 득표·conf
        # 우위(베이글)가 1위 — "사고 시 k=1로 즉시 무력화" 롤백 스토리.
        scorer = WeightLikelihoodScorer(k=1.0)
        c = ctx(
            -743.0,
            [BAGEL, DUMPLING],
            [cand(27, conf=1.0, votes=34), cand(13, conf=0.8, votes=25)],
        )
        entry = scorer.shadow(c, judged(BAGEL, 5, conf=1.0))
        assert entry["top"]["items"] == [[27, 5]]
        assert entry["mismatch"] is False

    def test_weight_term_clamped_at_log_k(self):
        # 잔차가 큰 배정의 log_l_weight는 −log k 아래로 score에 기여하지
        # 못한다 (clamped=True로 관측 가능).
        k = 20.0
        scorer = WeightLikelihoodScorer(k=k)
        c = ctx(
            -600.0,  # 베이글 n=4(620, 잔차 20) / 만두 n=3(555, 잔차 45)
            [BAGEL, DUMPLING],
            [cand(27, conf=0.9, votes=30), cand(13, conf=0.9, votes=30)],
        )
        entry = scorer.shadow(c, judged(BAGEL, 4))
        for e in entry["ranking"]:
            # 필드가 소수 3자리로 반올림돼 기록되므로 그만큼의 여유를 둔다
            assert e["score"] >= e["log_p_vision"] - math.log(k) - 2e-3
            if e["clamped"]:
                assert e["log_l_weight"] < -math.log(k)

    def test_sigma_eff_scales_with_count(self):
        # 같은 잔차라도 개수가 많으면 σ_eff가 커져 벌점이 줄어든다 —
        # gate_n(개당 slack)의 연속판.
        scorer = WeightLikelihoodScorer(sigma_db=5.0, sigma_d_default=3.5)
        one = scorer._score(((27, 1),), 155.0 + 20.0, 3.5,
                            {27: BAGEL}, {27: cand(27, votes=10)}, 10)
        five = scorer._score(((27, 5),), 775.0 + 20.0, 3.5,
                             {27: BAGEL}, {27: cand(27, votes=10)}, 10)
        assert five["log_l_weight"] > one["log_l_weight"]

    def test_bocpd_sigma_d_used_over_default(self, cola):
        # sigma_d 주입(BOCPD delta_std)이 기본 3.5 대신 쓰인다.
        scorer = WeightLikelihoodScorer()
        entry = scorer.shadow(
            ctx(-100.0, [cola], [cand(1, votes=10)]), judged(cola, 1),
            sigma_d=12.0,
        )
        assert entry["sigma_d"] == 12.0

    def test_no_detection_judgment_mismatch_when_score_bills(self, cola):
        # 현행이 무과금(NO_DETECTION)인데 score 1위가 존재 — diff로 기록돼야
        # "score였다면 과금했을 세션"을 아카이브에서 셀 수 있다.
        scorer = WeightLikelihoodScorer()
        nd = JudgmentResult(JudgmentStatus.NO_DETECTION, reason="forced_final_no_match")
        entry = scorer.shadow(ctx(-100.0, [cola], [cand(1, votes=10)]), nd)
        assert entry["mismatch"] is True
        assert entry["current"]["items"] == []
        assert entry["current"]["score"] is None


def _frame(value):
    return [[value] * 4 for _ in range(4)]


def _moving_frames(n):
    return [_frame(10 if i % 2 == 0 else 200) for i in range(n)]


def _samples(start, end, n=10, dt=0.1):
    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value, 0.0)))
        ts += dt
    return out


class _MovingDetector:
    """class_id=1 검출 — 모션 변위 증거 통과용 드리프트 (FakeDetector 동형)."""

    def __init__(self):
        self.calls = 0

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        off = 12.0 * (self.calls % 8)
        return [Detection(1, 0.8, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0))]


class TestPipelineWiring:
    """Phase 1 배선 계약: 판정 무변경 + trace.likelihood_shadow 기록."""

    def _pipe(self, cola, **kwargs):
        store = ActiveProductStore()
        store.update([cola])
        return TriggerPipeline(_MovingDetector(), {2: FREEZER}, store, **kwargs)

    def _request(self):
        return TriggerRequest(2, {"top": _moving_frames(8)}, _samples(500, 400), 1.0)

    def test_enabled_records_entry_and_judgment_unchanged(self, cola):
        on = self._pipe(cola, likelihood_shadow_enabled=True)
        off = self._pipe(cola)
        out_on = on.process("s1", self._request())
        out_off = off.process("s1", self._request())
        assert out_on.trace.likelihood_shadow is not None
        entry = out_on.trace.likelihood_shadow[0]
        assert entry["scorer"] == "weight_likelihood"
        # 판정 무변경: shadow 유무가 판정 결과에 영향을 주지 않는다
        assert out_on.event.judgment.status is out_off.event.judgment.status
        assert out_on.event.judgment.products == out_off.event.judgment.products

    def test_disabled_by_default(self, cola):
        outcome = self._pipe(cola).process("s1", self._request())
        assert outcome.trace.likelihood_shadow is None

    def test_multi_tray_entries_carry_channel(self, cola, bar170):
        store = ActiveProductStore()
        store.update([cola, bar170])

        class TwoClassDetector(_MovingDetector):
            def detect(self, frame, allowed_class_ids=None):
                self.calls += 1
                off = 12.0 * (self.calls % 8)
                return [
                    Detection(1, 0.85, bbox=(50.0 + off, 50.0, 100.0 + off, 100.0)),
                    Detection(3, 0.80, bbox=(150.0 + off, 50.0, 200.0 + off, 100.0)),
                ]

        pipe = TriggerPipeline(
            TwoClassDetector(), {2: FREEZER}, store, likelihood_shadow_enabled=True
        )
        out, ts = [], 0.0
        for k in range(20):
            v0 = 500 if k < 10 else 400   # ch0 −100 (콜라)
            v1 = 400 if k < 10 else 230   # ch1 −170 (아이스바)
            out.append(LoadcellSample(ts, (v0, v1)))
            ts += 0.1
        outcome = pipe.process(
            "s1", TriggerRequest(2, {"top": _moving_frames(20)}, out, 1.0)
        )
        shadow = outcome.trace.likelihood_shadow
        assert shadow is not None and len(shadow) == 2
        assert {e.get("channel") for e in shadow} == {0, 1}

    def test_archive_trace_serialization_includes_shadow(self):
        trace = TriggerTrace(likelihood_shadow=[{"scorer": "weight_likelihood"}])
        d = _trace_to_dict(trace)
        assert d["likelihood_shadow"] == [{"scorer": "weight_likelihood"}]
