"""2026-11-16 다른 층 반납: 저장 세션 재생과 잘못된 상쇄 방지."""
import json
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
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig, apply_cross_zone_penalty
from crk_model.ledger.events import (
    FreezerAction,
    FreezerMovementEvidence,
    FreezerReturnEvidence,
    TriggerEvent,
)
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_cross_floor_20261116.json").read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in FIXTURE["products"])
BY_CLASS = {p.class_id: p for p in PRODUCTS}
FEET, ORIGINAL, DINO = (BY_CLASS[cid] for cid in (103, 102, 98))
PROFILES = {i: FREEZER for i in range(1, 6)}


def candidate(cid, votes=8):
    return VisionCandidate(cid, .38, votes, .5)


def taken(product=FEET, *, zone=1, ts=5, delta=-205):
    return TriggerEvent("s", zone, ts, delta, (), JudgmentResult(
        JudgmentStatus.COMPLETE, (ProductCount(product, 1),), .9,
    ))


def returned(*, delta=205, candidates=None, channels=()):
    candidates = tuple(candidates if candidates is not None else (candidate(103),))
    return TriggerEvent(
        "s", 5, 7, delta, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
        freezer_return=FreezerReturnEvidence(candidates, True, "return_direction"),
        freezer_movement=FreezerMovementEvidence((), candidates, True, tuple(channels)),
    )


def mixed():
    return TriggerEvent(
        "s", 3, 0, -320, (
            WeightSegment(1, 2, 85), WeightSegment(2, 3, 750),
            WeightSegment(3, 4, -1360), WeightSegment(6, 8, 150),
            WeightSegment(8, 9, 55),
        ), JudgmentResult(JudgmentStatus.PARTIAL, (ProductCount(FEET, 1),), .5),
        freezer_movement=FreezerMovementEvidence(
            (candidate(102),), (candidate(103, 3),), True,
        ),
    )


def settle(events, products=PRODUCTS, profiles=PROFILES):
    return CloseSettler(
        default_profile=FREEZER, active_products_provider=lambda: products,
    ).settle("s", events, profiles)


def counts(result):
    return {str(z.zone): {str(pc.product.class_id): pc.count for pc in z.products}
            for z in result.zones if z.products}


@pytest.mark.parametrize("case", FIXTURE["sessions"], ids=lambda case: case["source"])
@pytest.mark.parametrize("reverse", [False, True])
def test_recorded_returns_preserve_correct_sku_origin_and_raw_weight(case, reverse):
    events = [event_from_dict(e) for e in case["events"]]
    before = list(events)
    if reverse:
        events.reverse()
    settler = CloseSettler(
        default_profile=FREEZER, active_products_provider=lambda: PRODUCTS,
        cross_zone=CrossZonePenaltyConfig(enabled=True),
    )
    result = settler.settle(events[0].session_id, events, PROFILES)
    assert counts(result) == case["expected_zones"]
    assert not any("freezer_return_unresolved" in note for note in result.notes)
    assert sorted(events, key=lambda e: (e.ts, e.zone)) == sorted(
        before, key=lambda e: (e.ts, e.zone)
    )
    for zone in result.zones:
        assert zone.weight_delta == sum(e.delta_weight for e in events if e.zone == zone.zone)
        assert zone.trigger_count == sum(e.zone == zone.zone for e in events)
    assert settler.settle(events[0].session_id, [], PROFILES) is result


def test_mixed_episode_can_use_take_after_episode_start_but_before_return():
    result = settle([mixed(), taken()])
    assert counts(result) == {"3": {"102": 1}}
    assert "zone3:freezer_mixed_resolved:take=-525.0g:return=205.0g:net=-320.0g" in result.notes
    assert next(z for z in result.zones if z.zone == 3).weight_delta == -320


@pytest.mark.parametrize("invalid", [
    "no_take_vision", "no_return_vision", "unstable", "future_source", "already_returned",
    "out_of_stock", "wrong_net", "pressure_before_take", "complete_take",
])
def test_mixed_episode_requires_all_independent_evidence(invalid):
    episode = mixed()
    source = taken()
    products = PRODUCTS
    events = []
    if invalid == "no_take_vision":
        episode = replace(episode, freezer_movement=replace(
            episode.freezer_movement, take_candidates=(),
        ))
    elif invalid == "no_return_vision":
        episode = replace(episode, freezer_movement=replace(
            episode.freezer_movement, return_candidates=(),
        ))
    elif invalid == "unstable":
        episode = replace(episode, freezer_movement=replace(
            episode.freezer_movement, stabilized=False,
        ))
    elif invalid == "future_source":
        source = replace(source, ts=11)
    elif invalid == "already_returned":
        source = replace(source, ts=1)
        events.append(replace(returned(), ts=2))
    elif invalid == "out_of_stock":
        products = tuple(replace(p, stock_qty=0) if p.class_id == 102 else p for p in PRODUCTS)
    elif invalid == "wrong_net":
        episode = replace(episode, delta_weight=-250)
    elif invalid == "pressure_before_take":
        episode = replace(episode, delta_weight=-370, segments=(
            WeightSegment(1, 2, 100), WeightSegment(3, 4, -470),
        ))
    elif invalid == "complete_take":
        episode = replace(episode, judgment=replace(episode.judgment,
            status=JudgmentStatus.COMPLETE, products=(ProductCount(ORIGINAL, 1),),
        ))
    result = settle([*events, episode, source], products)
    assert not any("freezer_mixed_resolved" in note for note in result.notes)
    if invalid != "complete_take":
        assert all(pc.product.class_id != 102 for z in result.zones for pc in z.products)


def test_complete_net_match_cannot_protect_an_identity_absent_from_take_direction():
    wrong = ActiveProduct("wrong330", "Wrong 330g", 104, 330, 5, 12)
    episode = replace(mixed(), delta_weight=-330, judgment=JudgmentResult(
        JudgmentStatus.COMPLETE, (ProductCount(wrong, 1),), .9,
    ))
    assert counts(settle([taken(), episode], (*PRODUCTS, wrong))) == {"3": {"102": 1}}


def test_mixed_positive_net_also_preserves_both_actions():
    episode = replace(mixed(), delta_weight=325, segments=(
        WeightSegment(3, 4, -205), WeightSegment(8, 9, 530),
    ), judgment=JudgmentResult(JudgmentStatus.NO_DETECTION),
        freezer_return=FreezerReturnEvidence((candidate(102),), True, "return_direction"),
        freezer_movement=FreezerMovementEvidence((candidate(103),), (candidate(102),), True),
    )
    result = settle([taken(ORIGINAL, delta=-530), episode])
    assert counts(result) == {"3": {"103": 1}}
    assert next(z for z in result.zones if z.zone == 3).weight_delta == 325


@pytest.mark.parametrize("kind", ["unresolved", "pending", "legacy_weight", "legacy_action"])
def test_uncertain_or_legacy_return_does_not_leave_reusable_take_for_mixed_episode(kind):
    earlier = replace(returned(), ts=2)
    if kind == "unresolved":
        earlier = replace(earlier, freezer_return=FreezerReturnEvidence(
            (candidate(98),), True, "return_direction",
        ))
    elif kind == "pending":
        earlier = replace(earlier, freezer_return=replace(earlier.freezer_return, stabilized=False))
    elif kind == "legacy_weight":
        earlier = replace(earlier, freezer_return=None, freezer_movement=None)
    elif kind == "legacy_action":
        earlier = replace(earlier, freezer_return=None, freezer_movement=None,
                          freezer_actions=(FreezerAction(205, taken().judgment),))
    result = settle([taken(ts=1), earlier, mixed()])
    assert not any("freezer_mixed_resolved" in note for note in result.notes)


@pytest.mark.parametrize("missing_order", ["absent", "conflicting", "simultaneous"])
def test_equal_weight_duplicate_needs_consistent_observed_take_order(missing_order):
    events = [event_from_dict(e) for e in FIXTURE["sessions"][2]["events"]
              if e["delta_weight"] == -465]
    if missing_order == "absent":
        events = [replace(e, freezer_movement=replace(e.freezer_movement, take_order=()))
                  for e in events]
    elif missing_order == "conflicting":
        events[0] = replace(events[0], freezer_movement=replace(
            events[0].freezer_movement, take_order=(98, 95),
        ))
    else:
        events = [replace(e, change_timestamps=(10,)) for e in events]
    notes = []
    resolved = apply_cross_zone_penalty(
        events, PROFILES, PRODUCTS, CrossZonePenaltyConfig(enabled=True), notes, FREEZER,
    )
    assert all(e.judgment.products[0].product.class_id == 95 for e in resolved)
    assert any("cross_zone_fingerprint_ambiguous" in note for note in notes)
    assert not any("cross_zone_visual_take_order" in note for note in notes)


@pytest.mark.parametrize("support", [2, 12])
def test_return_refit_requires_observed_alternative_with_enough_votes(support):
    result = settle([
        taken(), taken(DINO, zone=2, ts=6, delta=-470),
        returned(delta=198.333, candidates=(candidate(98, 28), candidate(103, support))),
    ])
    assert counts(result) == ({"2": {"98": 1}} if support == 12
                              else {"1": {"103": 1}, "2": {"98": 1}})


def test_two_weight_fitting_alternative_skus_remain_unresolved():
    other = ActiveProduct("other200", "Other 200g", 104, 200, 5, 12)
    result = settle([
        taken(), taken(other, zone=4, ts=5.5), taken(DINO, zone=2, ts=6, delta=-470),
        returned(candidates=(candidate(98, 30), candidate(103, 12), candidate(104, 10))),
    ], (*PRODUCTS, other))
    assert counts(result) == {"1": {"103": 1}, "2": {"98": 1}, "4": {"104": 1}}
    assert any("ambiguous_identity" in note for note in result.notes)


@pytest.mark.parametrize("channels,total,matched", [
    ((465, 24.2), 489.2, True), ((465, -24.2), 440.8, True),
    ((), 489.2, False), ((465, 24.2), 500, False),
    ((465, 50), 515, False), ((465, 200), 665, False),
    ((465, 30, 30), 525, False),
])
def test_channel_return_requires_single_product_channel_and_preserves_residual(
    channels, total, matched,
):
    event = returned(delta=total, candidates=(candidate(98),), channels=channels)
    result = settle([taken(DINO, delta=-465), event])
    assert counts(result) == ({} if matched else {"1": {"98": 1}})
    destination = next(z for z in result.zones if z.zone == 5)
    assert destination.weight_delta == total
    if matched:
        assert destination.status == "review"
        assert any("freezer_return_channel_residual" in note for note in result.notes)


def test_movement_evidence_roundtrips_without_python_yaml_tags():
    event = replace(mixed(), freezer_movement=replace(
        mixed().freezer_movement, channel_deltas=(-320,), take_order=(102,),
    ))
    document = event_to_dict(event)
    assert event_from_dict(json.loads(json.dumps(document))) == event
    yaml = pytest.importorskip("yaml")
    archive = _event_to_dict(event, None, 0)
    dumped = yaml.safe_dump(archive)
    assert "python/tuple" not in dumped
    assert yaml.safe_load(dumped)["freezer_movement"] == document["freezer_movement"]
    del document["freezer_movement"]
    assert event_from_dict(document).freezer_movement is None


@pytest.mark.parametrize("profile", [FREEZER, REFRIGERATOR])
def test_pipeline_preserves_channel_measurements_for_close(monkeypatch, profile):
    class EmptyDetector:
        def detect(self, frame, allowed_class_ids=None):
            return []

    def vision(pipeline, request, sensor_profile, snapshot, delta, trace):
        trace.directional_candidates["return"] = (candidate(98),)
        return (candidate(98),)

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        EmptyDetector(), {5: profile}, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    samples = [LoadcellSample(i * .1, (500, 100) if i < 20 else (965, 125))
               for i in range(40)]
    outcome = pipeline.process("s", TriggerRequest(5, {}, samples, 7))
    assert outcome.event.delta_weight == 490
    if profile is FREEZER:
        assert outcome.event.freezer_movement.channel_deltas == (465, 25)
        result = settle([taken(DINO, delta=-465), outcome.event])
        assert counts(result) == {}
        assert any("+25.0g:total=+490.0g" in note for note in result.notes)
    else:
        assert outcome.event.freezer_movement is None


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
@pytest.mark.parametrize("last_samples,matched", [(3, False), (20, True)])
def test_negative_net_mixed_return_waits_without_changing_original_weight_analysis(
    monkeypatch, analyzer, last_samples, matched,
):
    class EmptyDetector:
        def detect(self, frame, allowed_class_ids=None):
            return []

    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.directional_candidates.update({
            "take": (candidate(102),), "return": (candidate(103),),
        })
        return (candidate(103, 24), candidate(102, 16))

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    values = [500] * 20 + [-25] * 20 + [180] * last_samples
    samples = [LoadcellSample(i * .1, (weight, 0)) for i, weight in enumerate(values)]
    analysis = analyzer(FREEZER).analyze(samples)
    assert analysis.stabilized is True  # 기존 음수 순변화 판정은 유지한다.
    assert analysis.delta_weight == -320
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        EmptyDetector(), PROFILES, store, analyzer_factory=analyzer,
    )
    outcome = pipeline.process("s", TriggerRequest(3, {}, samples, 0))
    assert outcome.event.delta_weight == -320
    assert outcome.event.freezer_movement.stabilized is matched
    result = settle([taken(ts=.5), outcome.event])
    assert any("freezer_mixed_resolved" in note for note in result.notes) is matched
    if matched:
        assert counts(result) == {"3": {"102": 1}}
    else:
        assert "mixed_return_stabilization_pending" in outcome.trace.reason_codes
