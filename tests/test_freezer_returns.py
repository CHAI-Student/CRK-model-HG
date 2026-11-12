"""냉동 반납: 유사 무게의 종류 보존, 방향별 영상, 왕복/교환과 재생 등가성."""
import json
from dataclasses import replace

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
from crk_model.ingest.loadcell import LoadcellAnalysis, LoadcellAnalyzer, LoadcellSample
from crk_model.judgment.freezer_actions import infer_freezer_action
from crk_model.ledger.archive import _event_to_dict
from crk_model.ledger.events import FreezerAction, TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detection
from crk_model.perception.motion_direction import MotionDirectionConfig
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest
from crk_model.service.snapshot import ActiveProductStore

A = ActiveProduct("a", "A", 1, 100, 1000, 12)
B = ActiveProduct("b", "B", 2, 105, 2500, 12)
C = ActiveProduct("c", "C", 3, 100, 3000, 12)
PROFILES = {1: FREEZER, 2: FREEZER, 3: FREEZER}


def judgment(*items):
    return JudgmentResult(JudgmentStatus.COMPLETE,
                          tuple(ProductCount(p, n) for p, n in items), .95)


def take(zone, ts, *items):
    return TriggerEvent("s", zone, ts, -sum(p.unit_weight * n for p, n in items),
                        (), judgment(*items))


def returned(zone, ts, *items, delta=None):
    weight = delta if delta is not None else sum(p.unit_weight * n for p, n in items)
    j = judgment(*items) if items else JudgmentResult(JudgmentStatus.NO_DETECTION)
    return TriggerEvent("s", zone, ts, weight, (), JudgmentResult(JudgmentStatus.COMPLETE),
                        freezer_actions=(FreezerAction(weight, j),))


def settle(events):
    return CloseSettler(default_profile=FREEZER).settle("s", events, PROFILES)


def counts(result):
    out = {}
    for zone in result.zones:
        for pc in zone.products:
            out[pc.product.product_id] = out.get(pc.product.product_id, 0) + pc.count
    return out


@pytest.mark.parametrize("destination", [1, 2, 3])
def test_similar_weight_return_preserves_correct_sku(destination):
    result = settle([take(1, 1, (A, 1)), take(2, 2, (B, 1)), returned(destination, 3, (B, 1))])
    assert counts(result) == {"a": 1}
    assert result.total_price == 1000


def test_mixed_kind_and_more_than_two_returns_across_origins():
    result = settle([take(1, 1, (A, 2)), take(2, 2, (B, 3)),
                     returned(3, 3, (A, 1), (B, 3))])
    assert counts(result) == {"a": 1}


def test_same_sku_can_be_returned_from_multiple_origins():
    assert counts(settle([take(1, 1, (A, 1)), take(2, 2, (A, 2)),
                          returned(3, 3, (A, 3))])) == {}


def test_unseen_same_weight_sku_does_not_cancel_take_at_zero_net():
    result = settle([take(1, 1, (A, 1)), returned(1, 2, (C, 1))])
    assert counts(result) == {"a": 1}
    assert result.zones[0].status == "review"
    assert any("unmatched_identity_return:c" in n for n in result.notes)


def test_unknown_return_does_not_use_net_weight_to_choose_sku():
    result = settle([take(1, 1, (A, 1), (B, 1)), returned(1, 2, delta=105)])
    assert counts(result) == {"a": 1, "b": 1}
    assert result.zones[0].status == "review"


def test_return_cannot_consume_future_take_even_in_same_zone():
    assert counts(settle([returned(1, 1, (A, 2)), take(1, 2, (A, 1))])) == {"a": 1}


def test_returned_item_taken_again_at_destination():
    assert counts(settle([take(1, 1, (B, 2)), returned(3, 2, (B, 1)),
                          take(3, 3, (B, 1))])) == {"b": 2}


def test_extra_return_never_produces_negative_count():
    assert counts(settle([take(1, 1, (B, 1)), returned(2, 2, (B, 3))])) == {}


@pytest.mark.parametrize("delta,classes,expected", [
    (105, [2], {"b": 1}), (205, [1, 2], {"a": 1, "b": 1}),
    (315, [2], {"b": 3}), (-205, [1, 2], {"a": 1, "b": 1}),
])
def test_action_count_is_validated_without_changing_visual_identity(delta, classes, expected):
    candidates = [VisionCandidate(cid, .9, 10, 1) for cid in classes]
    j = infer_freezer_action(delta, candidates, [A, B], FREEZER)
    assert j.status is JudgmentStatus.COMPLETE
    assert {pc.product.product_id: pc.count for pc in j.products} == expected


def test_ambiguous_distribution_keeps_no_guessed_count():
    j = infer_freezer_action(305, [VisionCandidate(cid, .9, 10, 1) for cid in [1, 2]],
                             [A, B], FREEZER)
    assert not j.products and j.reason == "freezer_action_ambiguous_count"


def test_weight_mismatch_does_not_switch_to_other_sku():
    j = infer_freezer_action(200, [VisionCandidate(2, .9, 10, 1)], [A, B], FREEZER)
    assert j.products == (ProductCount(B, 2),)  # B×2 is within its count gate; A is not selected.
    j = infer_freezer_action(160, [VisionCandidate(2, .9, 10, 1)], [A, B], FREEZER)
    assert not j.products


def samples(levels, channels=2):
    return [LoadcellSample(i * .1, (weight,) + (0,) * (channels - 1))
            for i, weight in enumerate(weight for weight in levels for _ in range(20))]


def detection(cid, cx, cy, confidence=.95):
    return Detection(cid, confidence, bbox=(cx-10, cy-10, cx+10, cy+10))


class Scene:
    def __init__(self, top, side=()):
        self.scenes = {"top": iter(top), "side": iter(side)}
        self.calls = 0

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        return next(self.scenes["top" if frame[0][0] in (10, 200) else "side"])


def pipeline_event(top, levels, side=(), side_config=None, analyzer=LoadcellAnalyzer):
    store = ActiveProductStore()
    store.update([A, B, C])
    detector = Scene(top, side)
    pipeline = TriggerPipeline(
        detector, PROFILES, store, motion_evidence_enabled=True,
        analyzer_factory=analyzer,
        voting_params={"conf_floor": 0, "min_vote_ratio": 0},
        freezer_direction_configs={
            "top": MotionDirectionConfig(boundary_px=240),
            "side": side_config or MotionDirectionConfig(),
        },
    )
    frames = {
        cam: [[[low if i % 2 == 0 else high] * 4 for _ in range(4)] for i in range(n)]
        for cam, low, high, n in [("top", 10, 200, len(top)), ("side", 30, 230, len(side))]
    }
    outcome = pipeline.process("s", TriggerRequest(1, frames, samples(levels), 2))
    assert outcome.event.status == "ok", outcome.event.judgment.reason
    return outcome, detector


def test_return_video_uses_moving_product_and_ignores_stationary_similar_item():
    scene = [[detection(1, 50, 100), detection(2, 350, y)] for y in [180, 220, 260]]
    outcome, _ = pipeline_event(scene, [500, 605])
    actions = outcome.event.freezer_actions
    assert actions[0].judgment.products == (ProductCount(B, 1),)
    assert counts(settle([take(1, 1, (A, 1), (B, 1)), outcome.event])) == {"a": 1}


def test_both_cameras_support_one_return_without_double_counting():
    scene = [[detection(2, 100, y)] for y in [180, 220, 260]]
    outcome, _ = pipeline_event(scene, [500, 605], side=scene)
    assert outcome.event.freezer_actions[0].judgment.products == (ProductCount(B, 1),)


def test_camera_identity_conflict_is_reviewed_instead_of_two_product_return():
    top = [[detection(1, 100, y)] for y in [180, 220, 260]]
    side = [[detection(2, 100, y)] for y in [180, 220, 260]]
    outcome, _ = pipeline_event(top, [500, 605], side=side)
    assert "return" in outcome.trace.uncertain_directions
    result = settle([take(1, 1, (A, 1), (B, 1)), outcome.event])
    assert counts(result) == {"a": 1, "b": 1}
    assert result.zones[0].status in ("review", "partial")


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
@pytest.mark.parametrize("same_product", [True, False])
def test_zero_net_take_return_and_same_weight_exchange(same_product, analyzer):
    cid = 1 if same_product else 3
    scene = [[detection(1, 50, y)] for y in [280, 240, 200]]
    scene += [[detection(cid, 350, y)] for y in [200, 240, 280]]
    outcome, detector = pipeline_event(scene, [500, 400, 500], analyzer=analyzer)
    assert detector.calls == 6  # net 0 still runs vision.
    assert [a.delta_weight for a in outcome.event.freezer_actions] == [-100, 100]
    result = settle([outcome.event])
    assert counts(result) == ({} if same_product else {"a": 1})
    if not same_product:
        assert result.zones[0].status == "review"


def test_same_clip_mixed_kind_take_then_return_b():
    scene = [[detection(1, 50, y), detection(2, 350, y)] for y in [280, 240, 200]]
    scene += [[detection(2, 350, y)] for y in [200, 240, 280]]
    outcome, _ = pipeline_event(scene, [500, 295, 400])
    result = settle([outcome.event])
    assert counts(result) == {"a": 1}


def test_mixed_kind_pickup_does_not_collapse_to_a_double_before_later_return():
    scene = [[detection(1, 50, y), detection(2, 350, y)] for y in [280, 240, 200]]
    outcome, _ = pipeline_event(scene, [500, 295])
    assert counts(settle([outcome.event])) == {"a": 1, "b": 1}
    assert counts(settle([outcome.event, returned(3, 3, (B, 1))])) == {"a": 1}


def test_return_before_take_in_same_episode_uses_prior_history_only():
    scene = [[detection(2, 350, y)] for y in [200, 240, 280]]
    scene += [[detection(1, 50, y)] for y in [280, 240, 200]]
    outcome, _ = pipeline_event(scene, [500, 605, 505])
    assert [a.delta_weight for a in outcome.event.freezer_actions] == [105, -100]
    assert counts(settle([take(2, 1, (B, 1)), outcome.event])) == {"a": 1}


def test_class_flicker_along_one_physical_path_is_not_two_products():
    scene = [[detection(cid, 100, y)]
             for cid, y in zip([1, 1, 1, 2, 2, 2], [180, 200, 220, 240, 260, 280], strict=True)]
    outcome, _ = pipeline_event(scene, [500, 705])
    assert "return" in outcome.trace.uncertain_directions
    assert not outcome.event.freezer_actions[0].judgment.products


def test_configured_boundary_requires_full_crossing():
    scene = [[detection(2, 100, y)] for y in [120, 150, 180]]
    outcome, _ = pipeline_event(scene, [500, 605])
    assert not outcome.event.freezer_actions[0].judgment.products


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_return_stabilization_is_required_even_when_round_trip_net_is_zero(analyzer):
    sequence = samples([500, 400])
    sequence += [LoadcellSample(4 + i * .1, (500, 0)) for i in range(3)]
    analysis = analyzer(FREEZER).analyze(sequence)
    assert not analysis.stabilized and analysis.reason == "needs_return_stabilization"
    assert not analysis.segments


def test_unsegmented_mixed_video_does_not_invent_action_quantities():
    scene = [[detection(1, 50, y)] for y in [280, 240, 200]]
    scene += [[detection(3, 350, y)] for y in [200, 240, 280]]
    outcome, _ = pipeline_event(scene, [500, 500])
    assert outcome.event.judgment.status is JudgmentStatus.PARTIAL
    assert settle([outcome.event]).zones[0].status == "review"


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_freezer_channel_keeps_round_trip_segments_but_refrigerator_stays_unchanged(analyzer):
    sequence = samples([500, 400, 500])
    freezer = analyzer(FREEZER).analyze(sequence)
    refrigerator = analyzer(REFRIGERATOR).analyze(sequence)
    assert freezer.delta_weight == 0 and len(freezer.segments) == 2
    assert refrigerator.delta_weight == 0 and not refrigerator.segments


def test_action_journal_roundtrip_and_legacy_compatibility():
    event = returned(2, 2, (B, 1))
    replay = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert replay == event
    events = [take(1, 1, (A, 1), (B, 1)), event]
    assert settle(events) == settle([event_from_dict(event_to_dict(e)) for e in events])
    legacy = event_to_dict(replace(event, freezer_actions=None))
    del legacy["freezer_actions"]
    assert event_from_dict(legacy).freezer_actions is None


def test_shared_video_across_zones_does_not_bypass_contamination_checks():
    videos = (("top", "/same-top.avi"), ("side", "/same-side.avi"))
    source = replace(take(1, 1, (A, 1), (B, 1)), video_paths=videos)
    destination = replace(returned(2, 2, (B, 1)), video_paths=videos)
    result = settle([source, destination])
    assert counts(result) == {"a": 1, "b": 1}
    assert all(zone.status in ("review", "partial") for zone in result.zones)
    assert any("freezer_shared_video_action_unresolved" in note for note in result.notes)


def test_archive_keeps_return_identity_and_directional_candidates():
    scene = [[detection(2, 100, y)] for y in [180, 220, 260]]
    outcome, _ = pipeline_event(scene, [500, 605])
    archive = _event_to_dict(outcome.event, outcome.trace, 10)
    assert archive["freezer_actions"][0]["judgment"]["products"][0]["product"]["product_id"] == "b"
    assert archive["trace"]["directional_candidates"]["return"][0]["class_id"] == 2
    # JSON뿐 아니라 실제 아카이브가 사용하는 YAML에서도 Enum 직렬화가 깨지지 않는다.
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(yaml.safe_dump(archive))
    assert document["freezer_actions"][0]["judgment"]["status"] == "complete"


def test_calibrated_slanted_side_camera_can_identify_return_by_x_axis():
    scene = [[detection(2, x, 100)] for x in [240, 200, 160]]
    outcome, _ = pipeline_event([], [500, 605], side=scene, side_config=MotionDirectionConfig(
        axis="x", return_direction="decreasing", boundary_px=200,
    ))
    assert outcome.event.freezer_actions[0].judgment.products == (ProductCount(B, 1),)
    assert counts(settle([take(2, 1, (A, 1), (B, 1)), outcome.event])) == {"a": 1}


def test_opposing_overlapping_tray_changes_do_not_invent_causal_order():
    class OverlapAnalyzer:
        def analyze(self, _samples):
            return LoadcellAnalysis(0, (
                WeightSegment(0, 1, -100), WeightSegment(.5, 1.5, 100),
            ), True, 500)

    scene = [[detection(1, 50, y)] for y in [280, 240, 200]]
    scene += [[detection(1, 350, y)] for y in [200, 240, 280]]
    outcome, _ = pipeline_event(scene, [500, 400, 500], analyzer=lambda _: OverlapAnalyzer())
    assert not outcome.event.freezer_actions[0].judgment.products
    assert settle([outcome.event]).zones[0].status == "review"
