"""2026-11-26 실기 냉동 세션, 들고 온 상품 규칙(yoona.md #freezer-carried-in-20261126)과
두 트레이에 걸친 상품 판정(#freezer-tray-spanning-20261126)의 회귀 검증.

저장 판정·비전 후보 재생이며 영상 재추론이 아니다. 상품 번호는 1~12(구 92~103 −91)이고,
정답과 다른 세션은 fixture의 known_gap에 원인을 남긴다.
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from crk_model.core.profiles import FREEZER
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
    WeightSegment,
)
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import FreezerMovementEvidence, TriggerEvent
from crk_model.ledger.freezer_returns import take_measurement
from crk_model.ledger.journal import event_from_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest, tray_spanning_judgment
from crk_model.service.snapshot import ActiveProductStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_sessions_20261126.json").read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in FIXTURE["products"])
BY_CLASS = {p.class_id: p for p in PRODUCTS}
PROFILES = {i: FREEZER for i in range(1, 6)}


def settle(events):
    return CloseSettler(
        default_profile=FREEZER, cross_zone=CrossZonePenaltyConfig(enabled=True),
        active_products_provider=lambda: PRODUCTS,
    ).settle(events[0].session_id, events, PROFILES)


def class_counts(result):
    out = Counter()
    for zone in result.zones:
        for pc in zone.products:
            out[str(pc.product.class_id)] += pc.count
    return dict(out)


def vc(cid, votes):
    return VisionCandidate(cid, .9, votes, .5)


@pytest.mark.parametrize("case", FIXTURE["sessions"], ids=lambda case: case["source"])
def test_recorded_20261126_sessions(case):
    events = [event_from_dict(e) for e in case["events"]]
    result = settle(events)
    assert class_counts(result) == case["expected"]
    assert sorted(z.zone for z in result.zones if z.status != "complete") == \
        case["expected_non_complete_zones"]
    for note in case["expected_notes"]:
        assert note in result.notes
    for zone in result.zones:  # 보고용 층 무게는 정산 보완과 무관하게 원본 그대로
        assert zone.weight_delta == pytest.approx(
            sum(e.delta_weight for e in events if e.zone == zone.zone))


def _take(zone, ts, cid, delta):
    product = BY_CLASS[cid]
    return TriggerEvent("s", zone, ts, delta, (WeightSegment(ts, ts + 1, delta),),
                        JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(product, 1),), .9),
                        vision_candidates=(vc(cid, 20),))


def _mixed(zone, ts, take, ret, take_g=-505.0, return_g=525.0):
    """한 트리거 안에서 취출(take_g) 뒤 반납(return_g)이 이어진 혼합 이벤트."""
    net = take_g + return_g
    return TriggerEvent(
        "s", zone, ts, net,
        (WeightSegment(ts, ts + 1, take_g), WeightSegment(ts + 2, ts + 3, return_g)),
        JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_return_candidate"),
        vision_candidates=(*take, *ret),
        freezer_movement=FreezerMovementEvidence(tuple(take), tuple(ret), True, (net,)),
    )


def test_carried_in_item_seen_as_take_is_returned_not_billed():
    """ses-1-1795659665 구조: 3층에서 꺼낸 11을 들고 2층으로 와 8을 꺼내고 11을 넣었다.
    공유 카메라라 2층 클립의 취출 방향에도 11만 보인다 — 반납은 연결하고, 2층에서
    꺼낸 505g은 영상 근거가 없으니 청구하지 않고 review로 남긴다."""
    result = settle([_take(3, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5),), (vc(11, 7),))])
    assert class_counts(result) == {}
    assert "zone2:freezer_unbilled_weight_drop:unexplained=505.0g" in result.notes
    assert next(z for z in result.zones if z.zone == 2).status == "review"


def test_take_direction_with_another_sku_still_bills_that_sku():
    """취출 방향에 반납 SKU 말고 다른 상품(8)도 보이면 기존 분해를 그대로 쓴다."""
    result = settle([_take(3, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5), vc(8, 6)), (vc(11, 7),))])
    assert class_counts(result) == {"8": 1}
    assert any("zone2:freezer_mixed_resolved:" in note for note in result.notes)


def test_same_floor_lot_is_not_treated_as_carried_in():
    """반납 SKU를 같은 층에서 꺼냈다면 들고 온 상품이 아니다 — 같은 SKU의 반복·개수
    혼합은 지금처럼 분해하지 않는다."""
    result = settle([_take(2, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5),), (vc(11, 7),))])
    assert not any("freezer_mixed_resolved" in note for note in result.notes)


# ---- 두 트레이에 걸친 상품 (ses-1-1795668469 5층 11) ----

class _EmptyDetector:
    def detect(self, frame, allowed_class_ids=None):
        return []


def _spanning_outcome(monkeypatch, ch1_drop_at):
    """5층 ch0 +37.5 → −448.3, ch1 +50 → −35 → −70 (끝 −410.8 / −55, 합 −465.8g).
    ch1_drop_at: ch1이 −70 빠지는 샘플 위치 (90이면 ch0 −448.3과 같은 순간)."""
    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.directional_candidates["take"] = (vc(8, 7),)  # 공유 카메라에 잡힌 2층 너겟
        return (vc(11, 28), vc(8, 11), vc(4, 3))

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        _EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    ch0 = [1000] * 30 + [1037.5] * 60 + [589.2] * 70
    ch1 = [500] * 30 + [550] * 30 + [515] * (ch1_drop_at - 60) + [445] * (160 - ch1_drop_at)
    levels = zip(ch0, ch1, strict=True)
    samples = [LoadcellSample(i * .1, level) for i, level in enumerate(levels)]
    return pipeline.process("s", TriggerRequest(5, {}, samples, 7))


def test_product_spanning_two_trays_is_judged_by_the_drop_they_share(monkeypatch):
    """두 트레이가 같은 순간 함께 빠진 무게(−518.3g)로 11을 판정한다. 트레이별로는 둘 다
    11 PARTIAL이라 대칭 오염 가드가 모두 뺐고, 시작→끝 합(−465.8g)으로는 4(475g)가 맞는다."""
    outcome = _spanning_outcome(monkeypatch, ch1_drop_at=90)
    judgment = outcome.event.judgment
    assert judgment.status is JudgmentStatus.COMPLETE
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(11, 1)]
    assert judgment.reason.startswith("multi_tray_spanning[")
    assert "freezer_tray_spanning" in outcome.trace.reason_codes
    assert outcome.event.delta_weight == pytest.approx(-465.8)  # 보고용 무게는 그대로


def test_trays_dropping_at_different_moments_are_not_merged(monkeypatch):
    outcome = _spanning_outcome(monkeypatch, ch1_drop_at=120)
    assert outcome.event.judgment.products == ()
    assert "freezer_tray_spanning" not in outcome.trace.reason_codes


def test_spanning_needs_the_same_identity_on_every_tray():
    ctx = JudgmentContext(zone=5, profile=FREEZER, delta_weight=-465.8, segments=(),
                          vision_candidates=(vc(11, 28), vc(8, 11)), active_products=PRODUCTS,
                          vision_only=False)
    segments = (WeightSegment(8.9, 9.0, -448.3), WeightSegment(8.9, 9.0, -70.0))

    def partial(cid):
        return JudgmentResult(JudgmentStatus.PARTIAL, (ProductCount(BY_CLASS[cid], 1),), .9)

    router = JudgmentRouter()
    assert tray_spanning_judgment(router, ctx, [partial(11), partial(11)], segments) is not None
    assert tray_spanning_judgment(router, ctx, [partial(11), partial(8)], segments) is None


def test_take_lot_uses_the_shared_drop_of_a_spanning_product():
    """11 취출 이력은 함께 빠진 −518.3g으로 기록돼 2층 반납 525g과 맞는다(순변화 −467.5g)."""
    segments = (WeightSegment(2.3, 3.2, 37.5), WeightSegment(2.3, 3.2, 50.0),
                WeightSegment(3.9, 4.8, -35.0), WeightSegment(5.5, 6.4, -448.3),
                WeightSegment(5.5, 6.4, -70.0))
    movement = FreezerMovementEvidence((), (), True, (-412.5, -55.0))
    one = (ProductCount(BY_CLASS[11], 1),)
    assert take_measurement(-467.5, one, movement, 15, 5, 100, segments)[0] == pytest.approx(518.3)
    assert take_measurement(-467.5, one, movement, 15, 5, 100) == (467.5, None)
