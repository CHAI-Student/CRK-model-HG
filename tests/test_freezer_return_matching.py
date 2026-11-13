"""실기 4세션 재정산과 비전 SKU + 측정 delta 반납 매칭의 회귀 검증."""
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
    WeightSegment,
)
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.loadcell import LoadcellAnalyzer, LoadcellSample
from crk_model.ledger.archive import _event_to_dict
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

HOTDOG = ActiveProduct("hotdog", "Hotdog", 95, 490, 5, 12)
OTHER = ActiveProduct("other", "Other", 98, 490, 5, 12)
PROFILES = {i: FREEZER for i in range(1, 6)}
FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_cross_floor_20261112.json").read_text())


def taken(zone=2, ts=1, product=HOTDOG, count=1, delta=-465):
    return TriggerEvent("s", zone, ts, delta, (), JudgmentResult(
        JudgmentStatus.PARTIAL, (ProductCount(product, count),), .6,
    ))


def returned(zone=5, ts=2, product=HOTDOG, delta=466.7, stabilized=True, candidates=None):
    if candidates is None:
        candidates = (VisionCandidate(product.class_id, .39, 28, .67),)
    return TriggerEvent(
        "s", zone, ts, delta, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
        freezer_return=FreezerReturnEvidence(tuple(candidates), stabilized, "return_direction"),
    )


def settle(events, profiles=PROFILES):
    return CloseSettler(default_profile=FREEZER).settle("s", events, profiles)


def counts(result):
    out = Counter()
    for zone in result.zones:
        for pc in zone.products:
            out[pc.product.product_id] += pc.count
    return dict(out)


@pytest.mark.parametrize("case", FIXTURE["sessions"], ids=lambda case: case["source"])
def test_recorded_four_sessions_settle_as_user_observed(case):
    # YOLO 재실행이 아닌 저장된 잠정 판정/비전 후보 재생이다. 메타데이터 한계는 fixture에 명시.
    products = [ActiveProduct(**p) for p in FIXTURE["products"]]
    events = [event_from_dict(e) for e in case["events"]]
    settler = CloseSettler(
        default_profile=FREEZER, cross_zone=CrossZonePenaltyConfig(enabled=True),
        active_products_provider=lambda: products,
    )
    result = settler.settle(events[0].session_id, events, PROFILES)
    actual = Counter()
    for zone in result.zones:
        for pc in zone.products:
            actual[str(pc.product.class_id)] += pc.count
    assert dict(actual) == case["expected"]
    assert not any("unmatched_return" in note for note in result.notes)
    for zone in result.zones:
        assert zone.weight_delta == sum(e.delta_weight for e in events if e.zone == zone.zone)


@pytest.mark.parametrize("case", FIXTURE["sessions"][1:3], ids=lambda case: case["source"])
def test_recorded_cross_floor_failures_are_reproduced_by_legacy_weight_match(case):
    events = [replace(event_from_dict(e), freezer_return=None) for e in case["events"]]
    result = settle(events)
    assert any(pc.product.class_id == 95 for zone in result.zones for pc in zone.products)
    assert any("unmatched_return" in note for note in result.notes)


@pytest.mark.parametrize("destination", [2, 3, 5])
@pytest.mark.parametrize("take_delta,return_delta", [(-465, 466.7), (-462.5, 465), (-200, 200)])
def test_same_sku_return_matches_measured_take_not_registered_490g(
    destination, take_delta, return_delta,
):
    result = settle([taken(delta=take_delta), returned(zone=destination, delta=return_delta)])
    assert counts(result) == {}
    assert any("freezer_vision_return" in note for note in result.notes)


def test_matching_sku_in_other_floor_wins_over_same_weight_item_at_destination():
    result = settle([
        taken(zone=2), taken(zone=5, ts=2, product=OTHER), returned(ts=3),
    ])
    assert counts(result) == {OTHER.product_id: 1}


@pytest.mark.parametrize("destination", [2, 5])
def test_returning_different_same_weight_product_never_cancels_hotdog(destination):
    result = settle([taken(), returned(zone=destination, product=OTHER)])
    assert counts(result) == {HOTDOG.product_id: 1}
    assert any("no_prior_take" in note for note in result.notes)


@pytest.mark.parametrize("stabilized,candidates,reason", [
    (False, None, "not_stabilized"),
    (True, (), "missing_identity"),
    (True, (VisionCandidate(95, .9, 20, .5), VisionCandidate(98, .9, 19, .5)),
     "ambiguous_identity"),
])
def test_unresolved_return_keeps_take_and_is_not_reapplied_by_net_or_close(
    stabilized, candidates, reason,
):
    result = settle([taken(), returned(zone=2, stabilized=stabilized, candidates=candidates)])
    assert counts(result) == {HOTDOG.product_id: 1}
    assert result.zones[0].status in ("review", "partial")
    assert any(reason in note for note in result.notes)


def test_catalog_weight_alone_cannot_override_disagreeing_measured_delta():
    result = settle([taken(), returned(delta=490)])
    assert counts(result) == {HOTDOG.product_id: 1}
    assert any("weight_mismatch" in note for note in result.notes)


@pytest.mark.parametrize("difference,matched", [(15, True), (15.1, False)])
def test_measured_return_tolerance_boundary(difference, matched):
    result = settle([taken(), returned(delta=465 + difference)])
    assert counts(result) == ({} if matched else {HOTDOG.product_id: 1})


def test_multiple_returns_consume_remaining_count_once():
    result = settle([
        taken(count=2, delta=-930), returned(), returned(zone=3, ts=3, delta=463.3),
        returned(zone=4, ts=4),
    ])
    assert counts(result) == {}
    assert any("no_prior_take" in note for note in result.notes)


def test_three_same_sku_items_from_two_floors_can_be_returned_together():
    result = settle([
        taken(zone=2, count=2, delta=-930), taken(zone=3, ts=2), returned(ts=3, delta=1395),
    ])
    assert counts(result) == {}


def test_match_uses_appropriate_measurement_when_same_sku_lots_differ():
    result = settle([taken(zone=2, delta=-400), taken(zone=3, ts=2), returned(ts=3)])
    assert counts(result) == {HOTDOG.product_id: 1}
    assert next(z for z in result.zones if z.zone == 2).products
    assert not next(z for z in result.zones if z.zone == 3).products


def test_different_count_hypotheses_are_not_guessed():
    small = replace(HOTDOG, unit_weight=10)
    result = settle([taken(product=small, count=3, delta=-30), returned(delta=15)])
    assert counts(result) == {HOTDOG.product_id: 3}
    assert any("ambiguous_count" in note for note in result.notes)


def test_two_visible_prior_skus_are_not_collapsed_to_multiple_hotdogs():
    evidence = (VisionCandidate(95, .9, 30, .6), VisionCandidate(98, .9, 20, .4))
    result = settle([
        taken(count=3, delta=-1395), taken(zone=3, ts=2, product=OTHER),
        returned(ts=3, delta=1395, candidates=evidence),
    ])
    assert counts(result) == {HOTDOG.product_id: 3, OTHER.product_id: 1}
    assert any("ambiguous_identity" in note for note in result.notes)


def test_temporary_pressure_segment_does_not_replace_final_delta_for_return_matching():
    product = replace(HOTDOG, unit_weight=400)
    source = replace(taken(product=product, delta=-370), segments=(
        WeightSegment(0, 1, 100), WeightSegment(1, 2, -470),
    ))
    result = settle([source, returned(product=product, delta=370)])
    assert counts(result) == {}
    assert any("take=370.0g:return=370.0g" in note for note in result.notes)


def test_legacy_return_also_consumes_measurement_history_before_new_return():
    legacy = replace(returned(zone=2, delta=490), freezer_return=None)
    result = settle([taken(count=2, delta=-930), legacy, returned(ts=3)])
    assert counts(result) == {}


def test_return_does_not_consume_future_take():
    result = settle([returned(ts=1), taken(ts=2)])
    assert counts(result) == {HOTDOG.product_id: 1}


def test_returned_item_taken_again_is_billed():
    result = settle([taken(), returned(), taken(zone=5, ts=3)])
    assert counts(result) == {HOTDOG.product_id: 1}


def test_refrigerated_weight_return_stays_on_legacy_path():
    profile = replace(REFRIGERATOR, tolerance_grams=15)
    result = settle([taken(delta=-490), returned(delta=490, candidates=())],
                    {2: profile, 5: profile})
    assert counts(result) == {}


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_pending_return_is_not_consumed_even_if_positive_delta_reaches_pipeline(analyzer):
    class EmptyDetector:
        def detect(self, frame, allowed_class_ids=None):
            return []

    sequence = [LoadcellSample(i * .1, (500, 0)) for i in range(20)]
    sequence += [LoadcellSample(2 + i * .1, (965, 0)) for i in range(3)]
    store = ActiveProductStore()
    store.update([HOTDOG])
    pipeline = TriggerPipeline(EmptyDetector(), PROFILES, store, analyzer_factory=analyzer)
    outcome = pipeline.process("s", TriggerRequest(5, {}, sequence, 2))
    assert outcome.event.freezer_return.stabilized is False
    assert outcome.event.delta_weight == 465
    assert "return_stabilization_pending" in outcome.trace.reason_codes
    assert counts(settle([taken(), outcome.event])) == {HOTDOG.product_id: 1}


def test_new_return_evidence_survives_journal_and_standard_yaml():
    event = returned()
    replay = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert replay == event
    assert settle([taken(), replay]) == settle([taken(), event])
    old = event_to_dict(event)
    del old["freezer_return"]
    assert event_from_dict(old).freezer_return is None
    yaml = pytest.importorskip("yaml")
    archived = _event_to_dict(event, None, 0)
    loaded = yaml.safe_load(yaml.safe_dump(archived))
    assert loaded["freezer_return"]["candidates"][0]["class_id"] == 95
    assert loaded["freezer_return"]["stabilized"] is True
