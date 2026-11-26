"""2026-11-25 실기 냉동 세션과 2026-11-26 CLOSE 보완(yoona.md #freezer-20261126)의 회귀 검증.

저장 판정·비전 후보 재생이며 영상 재추론이 아니다. 정답과 다른 세션은 fixture의
known_gap에 원인(비슷한 상품 혼동·영상 없음)을 남긴다.
"""
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from crk_model.core.profiles import (
    FREEZER,
    FREEZER_RETURN_TOLERANCE_GRAMS,
    REFRIGERATOR,
    product_size_floor,
    return_tolerance,
)
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
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import (
    FreezerMovementEvidence,
    FreezerReturnEvidence,
    TriggerEvent,
)
from crk_model.ledger.freezer_returns import take_measurement
from crk_model.ledger.journal import event_from_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_sessions_20261125.json").read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in FIXTURE["products"])
BY_CLASS = {p.class_id: p for p in PRODUCTS}
PROFILES = {i: FREEZER for i in range(1, 6)}
CASES = {case["source"]: case for case in FIXTURE["sessions"]}


def settle(events, products=PRODUCTS):
    return CloseSettler(
        default_profile=FREEZER, cross_zone=CrossZonePenaltyConfig(enabled=True),
        active_products_provider=lambda: products,
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
def test_recorded_20261125_sessions(case):
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


@pytest.mark.parametrize("source", [
    "ses-3-1795592912", "ses-4-1795593217", "ses-6-1795593508", "ses-3-1795594851",
    "ses-7-1795593955", "ses-1-1795594411",  # 기기에서도 맞았던 세션 — 그대로 유지
])
def test_reported_sessions_match_ground_truth_up_to_similar_products(source):
    # ses-3-1795594851의 101→102는 비슷한 상품(모델 문제)이라 개수·층만 맞으면 된다.
    case = CASES[source]
    result = class_counts(settle([event_from_dict(e) for e in case["events"]]))
    truth = Counter(case["ground_truth"])
    if source == "ses-3-1795594851":
        truth["102"] += truth.pop("101")
    assert result == dict(truth)


def test_similar_product_take_still_appears_in_the_bill():
    """ses-8: 1층 93+94(−1473.3g)를 영상은 94만 봤다. 층 최종 무게로 개수 2를 복원해
    3층 94 반납이 1개만 지우고, 93 자리의 취출이 94로라도 청구에 남는다."""
    case = CASES["ses-8-1795594034"]
    result = settle([event_from_dict(e) for e in case["events"]])
    zone1 = next(z for z in result.zones if z.zone == 1)
    assert [(pc.product.class_id, pc.count) for pc in zone1.products] == [(94, 1)]
    assert zone1.status == "partial"


def _ses2_with_floor1_vision(take, ret, channels):
    """ses-2-1795594482 1층 트리거에 D2로 영상 추론이 돌았다고 가정 (영상 후보는 가정값).
    D2 트리거 판정은 무청구(freezer_masked_movement)이고 영상·채널 증거만 남는다."""
    events = [event_from_dict(e) for e in CASES["ses-2-1795594482"]["events"]]
    i, floor1 = next((i, e) for i, e in enumerate(events) if e.zone == 1)
    vision = (*take, *ret, vc(95, 10))
    movement = FreezerMovementEvidence(
        tuple(take), tuple(ret), True, channels, tuple(c.class_id for c in take),
    )
    judgment = JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_masked_movement")
    events[i] = replace(floor1, vision_candidates=vision, freezer_movement=movement,
                        judgment=judgment)
    return events


@pytest.mark.parametrize("channels", [(10.0, -20.0), (-16.8,)], ids=["two_trays", "one_tray"])
@pytest.mark.parametrize("returning", [(vc(99, 15),), (vc(95, 15),), ()],
                         ids=["return_seen_as_99", "return_seen_as_95", "no_return_direction"])
def test_same_floor_take_and_other_product_return_restores_take(returning, channels):
    """ses-2-1795594482 1층: 102 취출 + 2층 99 반납이 −16.8g으로 상쇄됐다. 반납을 99로
    보든, 2층처럼 95로 혼동하든, 방향을 못 잡든 102가 살아나고 2층 취출이 지워진다."""
    events = _ses2_with_floor1_vision((vc(102, 20),), returning, channels)
    result = settle(events)
    assert class_counts(result) == {"102": 1, "98": 1}
    assert any("zone1:freezer_mixed_resolved:" in note for note in result.notes)
    assert next(z for z in result.zones if z.zone == 1).status == "review"


def test_same_item_roundtrip_with_masked_movement_bills_nothing():
    """D2가 같은 상품을 꺼냈다 그대로 넣은 왕복(순변화 −10g)에 영상 추론을 열어도,
    앞선 취출 이력이 반납을 설명하지 못하면 청구하지 않고 review로만 남긴다."""
    event = TriggerEvent("s", 1, 1, -10.0, (
        WeightSegment(0, 1, -530), WeightSegment(1, 2, 520),
    ), JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_masked_movement"),
        vision_candidates=(vc(102, 30),),
        freezer_movement=FreezerMovementEvidence((vc(102, 20),), (vc(102, 15),), True, (-10.0,)))
    result = settle([event])
    assert class_counts(result) == {}
    assert result.zones[0].status == "review"


def test_masked_floor_without_vision_evidence_stays_review_not_billed():
    events = [event_from_dict(e) for e in CASES["ses-2-1795594482"]["events"]]
    result = settle(events)
    zone1 = next(z for z in result.zones if z.zone == 1)
    assert not zone1.products and zone1.status == "review"


# ---- 규칙 단위 검증 ----

def test_return_tolerance_is_a_freezer_only_code_constant():
    assert FREEZER_RETURN_TOLERANCE_GRAMS == 25.0
    assert return_tolerance(FREEZER) == 25.0
    assert FREEZER.tolerance_grams == 15.0  # 취출 판정 허용오차는 그대로
    assert return_tolerance(REFRIGERATOR) == REFRIGERATOR.tolerance_grams


def test_product_size_floor_is_half_the_lightest_product():
    assert product_size_floor(p.unit_weight for p in PRODUCTS) == 100.0
    assert product_size_floor([]) == float("inf")


def _take(zone, ts, product, count, delta, status=JudgmentStatus.PARTIAL, movement=None):
    return TriggerEvent("s", zone, ts, delta, (), JudgmentResult(
        status, (ProductCount(product, count),), .5,
        reason="vision_first_identity_partial"), freezer_movement=movement)


def _noise(zone, ts, delta):
    return TriggerEvent("s", zone, ts, delta, (), JudgmentResult(
        JudgmentStatus.NO_DETECTION, reason="below_min_weight_change"))


@pytest.mark.parametrize("events,count", [
    # 층 최종 −1473.3g = 94 등록 720g×2 + 33.3g (≤ 2×25g) → 2개
    ([_take(1, 1, BY_CLASS[94], 1, -1495), _noise(1, 2, 21.7)], 2),
    # 오차가 개당 25g을 넘으면 그대로 1개
    ([_take(1, 1, BY_CLASS[94], 1, -1495)], 1),
    # 같은 층에 다른 청구가 있으면 층 무게를 한 상품에 몰아주지 않는다
    ([_take(1, 1, BY_CLASS[94], 1, -1473), _take(1, 2, BY_CLASS[98], 1, -460)], 1),
    # 같은 층에 반납(상품 크기 변화)이 있으면 다시 세지 않는다
    ([_take(1, 1, BY_CLASS[94], 1, -1473), _noise(1, 2, 300)], 1),
    # 무게 검증된 COMPLETE는 대상이 아니다
    ([_take(1, 1, BY_CLASS[94], 1, -1473, status=JudgmentStatus.COMPLETE)], 1),
])
def test_partial_count_is_recovered_only_from_settled_floor_weight(events, count):
    result = settle(events)
    zone1 = next(z for z in result.zones if z.zone == 1)
    billed = {pc.product.class_id: pc.count for pc in zone1.products}
    assert billed.get(94) == count


def test_take_lot_uses_single_product_channel_measurement():
    # ses-2-1795594482 2층: 총 −467.5g(손 접촉 잔여 +7.5g 포함) 대신 상품 채널 −475g.
    movement = FreezerMovementEvidence((vc(95, 12),), (), True, (-475.0, 7.5))
    products = (ProductCount(BY_CLASS[95], 1),)
    assert take_measurement(-467.5, products, movement, 15, 5, 100) == (475.0, None)
    # 두 개를 꺼냈으면 평균이 낱개 무게가 아니므로 등록 무게
    two = (ProductCount(BY_CLASS[94], 2),)
    assert take_measurement(-1450, two, None, 15, 5, 100) == (720.0, None)
    # 등록 무게 절반 넘게 무거우면(다른 상품이 섞임) 등록 무게
    one = (ProductCount(BY_CLASS[94], 1),)
    assert take_measurement(-1495, one, None, 15, 5, 100) == (720.0, None)
    # 맞는 측정이 없고 섞인 것도 아니면 기존처럼 총 delta
    assert take_measurement(-700, one, None, 15, 5, 100) == (700.0, None)


def test_session_weight_return_requires_the_sku_to_be_seen_on_video():
    hotdog, dino = BY_CLASS[95], BY_CLASS[98]
    take = _take(2, 1, hotdog, 1, -475, status=JudgmentStatus.COMPLETE)
    ret = TriggerEvent("s", 4, 2, 480, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
                       vision_candidates=(vc(dino.class_id, 20),),
                       freezer_return=FreezerReturnEvidence(
                           (vc(dino.class_id, 20),), True, "return_direction"))
    assert class_counts(settle([take, ret])) == {"95": 1}  # 95는 영상에 없다
    seen = replace(ret, vision_candidates=(vc(dino.class_id, 20), vc(hotdog.class_id, 4)))
    result = settle([take, seen])
    assert class_counts(result) == {}
    assert "zone4:freezer_return_session_weight:vision=no_prior_take" in result.notes


def test_session_weight_return_refuses_when_a_mixed_combination_also_fits():
    # 반납 +935g = 95(475)+98(460)도, 등록 무게가 맞는 다른 조합도 있으면 확정하지 않는다.
    lots = [
        _take(1, 1, BY_CLASS[95], 1, -475, status=JudgmentStatus.COMPLETE),
        _take(2, 2, BY_CLASS[98], 1, -460, status=JudgmentStatus.COMPLETE),
        _take(3, 3, BY_CLASS[100], 1, -915, status=JudgmentStatus.COMPLETE),
    ]
    ret = TriggerEvent("s", 4, 4, 930, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
                       vision_candidates=(vc(100, 20), vc(95, 5), vc(98, 5)),
                       freezer_return=FreezerReturnEvidence(
                           (vc(97, 20),), True, "return_direction"))
    result = settle([*lots, ret])
    assert class_counts(result) == {"95": 1, "98": 1, "100": 1}
    assert not any("session_weight" in note for note in result.notes)


def test_contact_release_after_return_is_folded_into_the_return_weight():
    take = _take(3, 1, BY_CLASS[94], 1, -720, status=JudgmentStatus.COMPLETE)
    ret = TriggerEvent("s", 1, 2, 786.7, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
                       vision_candidates=(vc(94, 20),),
                       freezer_return=FreezerReturnEvidence((vc(94, 20),), True,
                                                            "return_direction"))
    release = _noise(1, 3, -65.0)
    result = settle([take, ret, release])
    assert class_counts(result) == {}
    assert "zone1:freezer_return_contact_release:release=-65.0g:return=+721.7g" in result.notes
    assert next(z for z in result.zones if z.zone == 1).status == "review"
    # 떼는 변화가 상품 크기(100g)면 합치지 않는다
    result = settle([take, ret, _noise(1, 3, -120.0)])
    assert class_counts(result) == {"94": 1}


def test_tray_split_bills_take_channel_and_returns_the_other_tray():
    # ses-3-1795592912 4층: 트레이 하나 −470g(95), 다른 트레이 +525g(1층 102 반납).
    take102 = _take(1, 1, BY_CLASS[102], 1, -530, status=JudgmentStatus.COMPLETE)
    movement = FreezerMovementEvidence(
        (vc(95, 12),), (vc(102, 8),), True, (-470.0, 525.0), (95,), (-470.0, 525.0))
    floor4 = TriggerEvent("s", 4, 2, 55.0, (), JudgmentResult(
        JudgmentStatus.PARTIAL, (ProductCount(BY_CLASS[95], 1),), .5),
        vision_candidates=(vc(95, 20), vc(102, 10)), freezer_movement=movement)
    result = settle([take102, floor4])
    assert class_counts(result) == {"95": 1}
    assert "zone4:freezer_tray_split:take=-470.0g:return=+525.0g" in result.notes


@pytest.mark.parametrize("small", [30, -30])
def test_small_tray_channel_is_not_judged_as_a_product(monkeypatch, small):
    """③ (11-18 4390e7a 복원): 50g 미만 채널은 판정하지 않는다 — 측정은 movement에 남는다."""
    class EmptyDetector:
        def detect(self, frame, allowed_class_ids=None):
            return []

    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.directional_candidates["take"] = (vc(95, 12),)
        return (vc(95, 20), vc(102, 15))

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    samples = [LoadcellSample(i * .1, (1000, 500) if i < 20 else (525, 500 + small))
               for i in range(40)]
    outcome = pipeline.process("s", TriggerRequest(2, {}, samples, 7))
    assert outcome.event.freezer_movement.channel_deltas == (-475, small)
    assert any(code.startswith("freezer_small_channel:ch1") for code in outcome.trace.reason_codes)
    assert [(pc.product.class_id, pc.count) for pc in outcome.event.judgment.products] == [(95, 1)]


def test_masked_review_needs_a_product_catalog():
    # 상품 목록이 없으면 상품 크기 기준을 정할 수 없어 기존처럼 둔다.
    event = TriggerEvent("s", 1, 1, -16.8, (
        WeightSegment(0, 1, -905), WeightSegment(1, 2, 385), WeightSegment(2, 3, 105),
    ), JudgmentResult(JudgmentStatus.NO_DETECTION, reason="below_min_weight_change"))
    plain = CloseSettler(default_profile=FREEZER).settle("s", [event], PROFILES)
    assert plain.zones[0].status == "complete"
    assert settle([event]).zones[0].status == "review"
