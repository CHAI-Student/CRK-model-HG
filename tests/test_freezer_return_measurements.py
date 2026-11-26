"""반납 비교는 같은 측정 기준을 사용하며, 불충분한 하중을 임의 보정하지 않는다."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crk_model.core.profiles import FREEZER
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.ledger.archive import _event_to_dict
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import FreezerMovementEvidence, FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_cross_floor_20261116_afternoon.json").read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in FIXTURE["products"])
SOUP = next(p for p in PRODUCTS if p.class_id == 100)
PROFILES = {i: FREEZER for i in range(1, 6)}
CANDIDATE = VisionCandidate(100, .39, 35, .8)


def take(delta=-890, stable=-890, count=1):
    return TriggerEvent(
        "s", 3, 1, delta, (), JudgmentResult(
            JudgmentStatus.PARTIAL, (ProductCount(SOUP, count),), .6,
        ), freezer_movement=FreezerMovementEvidence(
            (CANDIDATE,), (), True, (delta,),
            stable_channel_deltas=() if stable is None else (stable,),
        ),
    )


def returned(delta=912.5, channels=(6.6666666667, 905.8333333333), stable=(5, 900)):
    return TriggerEvent(
        "s", 5, 2, delta, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
        freezer_return=FreezerReturnEvidence((CANDIDATE,), True, "return_direction"),
        freezer_movement=FreezerMovementEvidence(
            (), (CANDIDATE,), True, channels, stable_channel_deltas=stable,
        ),
    )


def settle(events):
    return CloseSettler(
        default_profile=FREEZER, cross_zone=CrossZonePenaltyConfig(enabled=True),
        active_products_provider=lambda: PRODUCTS,
    ).settle(events[0].session_id, events, PROFILES)


def counts(result):
    return {str(z.zone): {str(pc.product.class_id): pc.count for pc in z.products}
            for z in result.zones if z.products}


@pytest.mark.parametrize("reverse", [False, True])
def test_afternoon_recorded_soup_return_is_resolved_without_inventing_duck_weight(reverse):
    events = [event_from_dict(e) for e in FIXTURE["events"]]
    result = settle(events[::-1] if reverse else events)
    assert counts(result) == FIXTURE["expected_zones"]
    assert counts(result) != FIXTURE["intended_zones"]  # 92의 실제 하중은 아직 불충분하다.
    assert any("take=890.0g:return=905.0g" in note for note in result.notes)
    assert "zone4:freezer_return_unresolved:weight_mismatch" in result.notes
    assert "zone5:freezer_return_unresolved:weight_mismatch" not in result.notes
    assert any("return=375.0g:takes=class100=890.0gx1,class92=520.0gx1" in note
               for note in result.notes)
    for zone in result.zones:
        assert zone.weight_delta == sum(e.delta_weight for e in events if e.zone == zone.zone)
    assert next(z for z in result.zones if z.zone == 4).status == "review"


def test_afternoon_soup_return_matches_by_return_tolerance_without_paired_endpoints():
    # 11-16에는 끝점 측정이 없으면 삼계탕 반납(912.5g 대 취출 890g, 22.5g 차)이 15g
    # 허용오차에 걸려 재현됐다. 2026-11-26 냉동 반납 허용오차 25g에서는 BOCPD 총
    # delta로도 연결된다. 92의 실제 하중 부족(zone4)은 여전히 미해결로 남는다.
    events = [event_from_dict(e) for e in FIXTURE["events"]]
    events = [replace(e, freezer_movement=replace(e.freezer_movement, stable_channel_deltas=()))
              if e.freezer_movement else e for e in events]
    result = settle(events)
    assert counts(result) == {"1": {"92": 1, "99": 1}}
    assert any("take=890.0g:return=912.5g" in note for note in result.notes)
    assert "zone4:freezer_return_unresolved:weight_mismatch" in result.notes
    assert "zone5:freezer_return_unresolved:weight_mismatch" not in result.notes


@pytest.mark.parametrize("source_stable,return_stable", [
    (None, (5, 900)), (-890, ()), (-890, (900,)),
])
def test_endpoint_comparison_requires_both_sides_and_the_complete_channel_set(
    source_stable, return_stable,
):
    # 총 925g(+35)·상품 채널 920g(+30)은 반납 허용오차 25g 밖, 끝점 905g만 맞는다.
    result = settle([take(stable=source_stable),
                     returned(delta=925, channels=(5, 920), stable=return_stable)])
    assert counts(result) == {"3": {"100": 1}}
    assert any("weight_mismatch" in note for note in result.notes)


@pytest.mark.parametrize("end,matched", [(905, True), (915, True), (915.1, False)])
def test_endpoint_measurement_uses_freezer_return_tolerance(end, matched):
    # 2026-11-26: 끝점 비교도 냉동 반납 허용오차 25g을 쓴다 (11-16의 15g에서 변경).
    result = settle([take(), returned(channels=(912.5,), stable=(end,))])
    assert counts(result) == ({} if matched else {"3": {"100": 1}})


def test_estimator_is_not_selected_by_which_one_happens_to_fit():
    # 원본 900g은 890g과 맞지만, 양쪽에서 확보한 동일 기준은 890 대 930이다.
    result = settle([take(), returned(delta=900, channels=(900,), stable=(930,))])
    assert counts(result) == {"3": {"100": 1}}
    assert any("return=930.0g:takes=class100=890.0gx1" in note for note in result.notes)


def test_both_take_and_return_use_endpoint_measurements():
    result = settle([take(stable=-870), returned(delta=900, channels=(900,), stable=(880,))])
    assert counts(result) == {}
    assert any("take=870.0g:return=880.0g" in note for note in result.notes)
    assert next(z for z in result.zones if z.zone == 3).weight_delta == -890
    assert next(z for z in result.zones if z.zone == 5).weight_delta == 900


def test_endpoint_measurements_do_not_bypass_stabilization_or_identity():
    event = returned()
    for evidence in (
        replace(event.freezer_return, stabilized=False),
        replace(event.freezer_return, candidates=(VisionCandidate(92, .39, 35, .8),)),
    ):
        assert counts(settle([take(), replace(event, freezer_return=evidence)])) == {
            "3": {"100": 1},
        }


def test_endpoint_channel_match_still_rejects_two_product_size_channels():
    result = settle([take(), returned(delta=1212.5, channels=(306.6667, 905.8333),
                                     stable=(300, 900))])
    assert counts(result) == {"3": {"100": 1}}


def test_endpoint_allocations_consume_original_lots_once():
    first = returned()
    result = settle([take(delta=-1780, stable=-1780, count=2), first,
                     replace(first, ts=3), replace(first, ts=4)])
    assert counts(result) == {}
    assert sum("freezer_vision_return" in note for note in result.notes) == 2
    assert any("no_prior_take" in note for note in result.notes)


def test_endpoint_evidence_survives_journal_and_safe_yaml():
    event = returned()
    document = event_to_dict(event)
    assert event_from_dict(json.loads(json.dumps(document))) == event
    yaml = pytest.importorskip("yaml")
    archive = yaml.safe_load(yaml.safe_dump(_event_to_dict(event, None, 0)))
    assert archive["freezer_movement"]["stable_channel_deltas"] == [5, 900]
    del document["freezer_movement"]["stable_channel_deltas"]
    assert event_from_dict(document).freezer_movement.stable_channel_deltas == ()


@pytest.mark.parametrize("initial_span", [0, 20])
def test_pipeline_records_only_stable_endpoints_for_the_contributing_channels(
    monkeypatch, initial_span,
):
    class EmptyDetector:
        def detect(self, frame, allowed_class_ids=None):
            return []

    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.directional_candidates["return"] = (CANDIDATE,)
        return (CANDIDATE,)

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    samples = [LoadcellSample(i * .1, (500, 100) if i < 20 else (965, 125))
               for i in range(40)]
    samples[2] = LoadcellSample(.2, (500 + initial_span, 100))
    event = pipeline.process("s", TriggerRequest(5, {}, samples, 2)).event
    assert event.freezer_movement.stable_channel_deltas == (
        (465, 25) if initial_span == 0 else ()
    )
