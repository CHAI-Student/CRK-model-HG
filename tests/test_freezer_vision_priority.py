"""상품 채널 보존, 방향별 정체성, CLOSE 공동 배정과 완료 증거의 경계."""
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
    WeightSegment,
)
from crk_model.ingest.loadcell import ChannelTerminalLevel, ChannelWeightEvent, LoadcellAnalysis
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import (
    FreezerMovementEvidence,
    FreezerReturnEvidence,
    FreezerTakeEvidence,
    TriggerEvent,
)
from crk_model.ledger.freezer_returns import record_event_takes
from crk_model.ledger.freezer_session import resolve_session_returns
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

DATA = json.loads((Path(__file__).parent / 'fixtures' /
                   'freezer_cross_floor_20261116_evening.json').read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in DATA['products'])
BY_CLASS = {p.class_id: p for p in PRODUCTS}
PROFILES = {z: FREEZER for z in range(1, 6)}


def candidate(cid, votes=10):
    return VisionCandidate(cid, .9, votes, .5)


def take(product, *, zone=1, ts=1, weight=None, status=JudgmentStatus.COMPLETE, count=1):
    weight = product.unit_weight * count if weight is None else weight
    return TriggerEvent('s', zone, ts, -weight, (), JudgmentResult(
        status, (ProductCount(product, count),), .9,
    ))


def returned(candidates, *, zone=5, ts=4, weight=523, complete=False):
    return TriggerEvent('s', zone, ts, weight, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
                        freezer_return=FreezerReturnEvidence(
                            tuple(candidates), True, 'return_direction',
                        ),
                        freezer_movement=FreezerMovementEvidence(
                            (), tuple(candidates), True, vision_priority=True,
                            return_completed=complete,
                        ))


def counts(result):
    return {z.zone: {p.product.class_id: p.count for p in z.products}
            for z in result.zones if z.products}


@pytest.mark.parametrize('index', [0, 1])
def test_saved_evening_sessions_rejudge_channels_without_inventing_completion(monkeypatch, index):
    case = DATA['sessions'][index]
    store = ActiveProductStore()
    store.update(PRODUCTS)
    events = []
    for source in case['triggers']:
        movement = source.get('freezer_movement')
        segments = tuple(WeightSegment(**s) for s in source['segments'])
        channels = movement['channel_deltas'] if movement else []
        levels = tuple(ChannelTerminalLevel(**t)
                       for t in source['trace'].get('loadcell_terminal_levels', []))
        major = max(range(len(channels)), key=lambda i: abs(channels[i])) if channels else -1
        analysis = LoadcellAnalysis(source['delta_weight'], segments, True, 0,
            events=tuple(ChannelWeightEvent(i, delta, segments if i == major else ())
                         for i, delta in enumerate(channels)), terminal_levels=levels)

        def vision(pipeline, request, profile, snapshot, delta, trace, _source=source):
            trace.directional_candidates = {
                k: tuple(VisionCandidate(**c) for c in cs)
                for k, cs in _source['trace'].get('directional_candidates', {}).items()
            }
            trace.uncertain_directions = _source['trace'].get('uncertain_directions', [])
            return tuple(VisionCandidate(**c) for c in _source['vision_candidates'])

        monkeypatch.setattr(TriggerPipeline, '_run_vision', vision)
        pipe = TriggerPipeline(object(), PROFILES, store, analyzer_factory=lambda p: None)
        pipe._analyzer_factory = lambda p, _analysis=analysis: type(
            'RecordedAnalyzer', (), {'analyze': lambda self, samples: _analysis},
        )()
        outcome = pipe.process('s', TriggerRequest(
            source['zone'], {}, (), source['ts'],
            change_timestamps=tuple(source['change_timestamps']),
        ))
        assert outcome.event.status == 'ok'
        assert outcome.event.delta_weight == source['delta_weight']
        events.append(outcome.event)
    for reverse in (False, True):
        result = CloseSettler(default_profile=FREEZER,
            active_products_provider=lambda: PRODUCTS,
            cross_zone=CrossZonePenaltyConfig(enabled=True)).settle(
                's', list(reversed(events)) if reverse else events, PROFILES,
            )
        if index == 1:
            assert counts(result) == {3: {94: 1}}
            assert any('freezer_mixed_resolved' in n for n in result.notes)
        else:
            # 약370g 취출/520g 반납 차이를 없애거나 미관측 완료 증거를 만들지 않는다.
            assert counts(result) == {2: {103: 1, 102: 1}, 4: {92: 1}}
            assert any('freezer_return_unresolved:weight_mismatch' in n for n in result.notes)
            assert all(p.product.class_id != 96 for z in result.zones for p in z.products)
        for zone in result.zones:
            assert zone.weight_delta == sum(e.delta_weight for e in events if e.zone == zone.zone)


def test_verified_channel_in_partial_is_available_for_return_without_extra_billing():
    duck = BY_CLASS[92]
    pc = ProductCount(duck, 1)
    event = replace(take(duck, weight=517, status=JudgmentStatus.PARTIAL),
        freezer_movement=FreezerMovementEvidence((), (), True, (6, -523),
            verified_takes=(FreezerTakeEvidence(1, -523, (pc,), -523),)))
    lots = []
    record_event_takes(lots, event, 15, verified_only=True)
    assert len(lots) == 1 and lots[0].unit_weight == 523
    result = CloseSettler(default_profile=FREEZER).settle(
        's', [event, returned([candidate(92)], weight=522)], PROFILES,
    )
    assert counts(result) == {}


def test_joint_assignment_uses_later_return_to_resolve_earlier_ambiguity():
    first = take(BY_CLASS[92], weight=523)
    second = take(BY_CLASS[102], zone=2, ts=2, weight=530)
    a = returned([candidate(92, 10), candidate(102, 10)], ts=3, weight=525)
    b = returned([candidate(102)], zone=4, ts=4, weight=530)
    for events in ([first, second, a, b], [b, a, second, first]):
        result = CloseSettler(default_profile=FREEZER).settle('s', events, PROFILES)
        assert counts(result) == {}
        assert any('session_return_assignment:class92' in n for n in result.notes)


def test_joint_assignment_keeps_equal_solutions_ambiguous():
    events = [take(BY_CLASS[92], weight=523), take(BY_CLASS[102], zone=2, ts=2),
              returned([candidate(92), candidate(102)], ts=3, weight=525),
              returned([candidate(92), candidate(102)], ts=4, weight=525)]
    notes = []
    assert resolve_session_returns(events, PROFILES, FREEZER, notes) == events
    assert not notes


def test_joint_assignment_does_not_overrule_clear_vision_to_cancel_more_items():
    events = [take(BY_CLASS[92]), take(BY_CLASS[102], zone=2, ts=2),
              returned([candidate(102, 100), candidate(92, 3)], ts=3, weight=525),
              returned([candidate(102)], ts=4, weight=530)]
    notes = []
    assert resolve_session_returns(events, PROFILES, FREEZER, notes) == events
    assert not notes
    result = CloseSettler(default_profile=FREEZER).settle('s', events, PROFILES)
    assert counts(result) == {1: {92: 1}}


def test_joint_assignment_respects_earlier_legacy_or_mixed_return_consumption():
    legacy = replace(returned([candidate(92)], ts=3), freezer_movement=None)
    ambiguous = returned([candidate(92), candidate(102)], ts=4, weight=525)
    events = [take(BY_CLASS[92]), take(BY_CLASS[102], zone=2, ts=2), legacy, ambiguous]
    result = CloseSettler(default_profile=FREEZER).settle('s', events, PROFILES)
    assert counts(result) == {}
    assert any('session_return_assignment:class102' in n for n in result.notes)


def test_joint_assignment_does_not_use_future_takes():
    events = [take(BY_CLASS[92], ts=10), take(BY_CLASS[102], zone=2, ts=11),
              returned([candidate(92), candidate(102)], ts=3, weight=525),
              returned([candidate(102)], ts=4, weight=530)]
    notes = []
    assert resolve_session_returns(events, PROFILES, FREEZER, notes) == events
    assert not notes
    result = CloseSettler(default_profile=FREEZER).settle('s', events, PROFILES)
    assert counts(result) == {1: {92: 1}, 2: {102: 1}}


def test_joint_assignment_cannot_switch_estimators_by_narrowing_candidates():
    first = replace(take(BY_CLASS[92]), freezer_movement=FreezerMovementEvidence(
        (candidate(92),), (), True, (-523,), stable_channel_deltas=(-523,),
    ))
    second = take(BY_CLASS[102], zone=2, ts=2)
    events = [first, second,
              returned([candidate(92), candidate(102)], ts=3, weight=525),
              returned([candidate(102)], ts=4, weight=530)]
    notes = []
    assert resolve_session_returns(events, PROFILES, FREEZER, notes) == events
    assert not notes


def test_completion_evidence_does_not_cancel_the_same_take_twice():
    events = [take(BY_CLASS[92], weight=370),
              returned([candidate(92)], ts=3, complete=True),
              returned([candidate(92)], ts=4, complete=True)]
    result = CloseSettler(default_profile=FREEZER).settle('s', events, PROFILES)
    assert counts(result) == {}
    assert sum('freezer_return_vision_presence' in n for n in result.notes) == 1
    assert any('freezer_return_unresolved:no_prior_take' in n for n in result.notes)


def test_joint_assignment_bounds_work_on_large_sessions():
    events = [take(BY_CLASS[92], count=13)] + [
        returned([candidate(92)], ts=ts) for ts in range(2, 15)
    ]
    notes = []
    assert resolve_session_returns(events, PROFILES, FREEZER, notes) == events
    assert notes == ['freezer_session_assignment_search_limit']


@pytest.mark.parametrize('invalid', ['future', 'wrong_sku', 'multiple_units', 'not_complete',
                                   'no_direction', 'too_heavy', 'unstable', 'movement_unstable'])
def test_visual_return_cannot_bypass_identity_quantity_or_stability(invalid):
    source = take(BY_CLASS[92], weight=370)
    r = returned([candidate(92)], complete=True)
    if invalid == 'future':
        source = replace(source, ts=10)
    elif invalid == 'wrong_sku':
        r = returned([candidate(102)], weight=520, complete=True)
    elif invalid == 'multiple_units':
        source = take(BY_CLASS[92], weight=740, count=2)
    elif invalid == 'not_complete':
        r = returned([candidate(92)])
    elif invalid == 'no_direction':
        r = replace(r, freezer_return=replace(r.freezer_return, source='video'))
    elif invalid == 'too_heavy':
        r = returned([candidate(92)], weight=900, complete=True)
    elif invalid == 'unstable':
        r = replace(r, freezer_return=replace(r.freezer_return, stabilized=False))
    elif invalid == 'movement_unstable':
        r = replace(r, freezer_movement=replace(r.freezer_movement, stabilized=False))
    result = CloseSettler(default_profile=FREEZER).settle('s', [source, r], PROFILES)
    assert counts(result) == {1: {92: source.judgment.products[0].count}}


def test_visual_completion_can_cancel_one_outstanding_item_with_partial_loading():
    source = take(BY_CLASS[92], weight=370)
    r = returned([candidate(92)], complete=True)
    result = CloseSettler(default_profile=FREEZER).settle('s', [source, r], PROFILES)
    assert counts(result) == {}
    assert any('freezer_return_vision_presence' in n for n in result.notes)
    assert sum(z.weight_delta for z in result.zones) == 153


def test_verified_evidence_and_completion_roundtrip_without_changing_old_defaults():
    event = replace(take(BY_CLASS[92]), freezer_movement=FreezerMovementEvidence(
        (candidate(92),), (), True, (-523,), verified_takes=(
            FreezerTakeEvidence(1, -523, (ProductCount(BY_CLASS[92], 1),), -523),
        ), vision_priority=True,
    ))
    document = event_to_dict(event)
    assert event_from_dict(json.loads(json.dumps(document))) == event
    for key in ('verified_takes', 'vision_priority', 'return_completed'):
        document['freezer_movement'].pop(key)
    old = event_from_dict(document).freezer_movement
    assert old.verified_takes == () and not old.vision_priority and not old.return_completed
