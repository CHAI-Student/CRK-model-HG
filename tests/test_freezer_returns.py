"""기존 무게 판정 복원과 이전 행동별 아카이브의 재생 호환성."""
import json
from dataclasses import replace

import pytest

from crk_model.core.profiles import FREEZER, REFRIGERATOR
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    WeightSegment,
)
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.loadcell import LoadcellAnalysis, LoadcellAnalyzer, LoadcellSample
from crk_model.ledger.archive import _event_to_dict, _trace_to_dict
from crk_model.ledger.events import FreezerAction, TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detection
from crk_model.perception.filters import DetectionFilterChain
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
    """이전 버전이 저장한 행동별 이벤트. 신규 파이프라인은 이 형식을 생성하지 않는다."""
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


def pipeline_event(
    top, levels, side=(), side_config=None, analyzer=LoadcellAnalyzer,
    filters=None, save_detections=False, products=(A, B, C), top_config=None,
):
    store = ActiveProductStore()
    store.update(products)
    detector = Scene(top, side)
    pipeline = TriggerPipeline(
        detector, PROFILES, store, motion_evidence_enabled=True,
        analyzer_factory=analyzer,
        filters=filters, save_detections=save_detections,
        voting_params={"conf_floor": 0, "min_vote_ratio": 0},
        freezer_direction_configs={
            "top": top_config or MotionDirectionConfig(boundary_px=240),
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


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
@pytest.mark.parametrize("increase,net_take", [(80, 370), (235, 470)])
def test_reported_contact_load_keeps_existing_pickup_without_boundary_crossing(
    analyzer, increase, net_take,
):
    # 실측 +80→-450 / +235→-705 패턴. 상품 정보는 회귀 검증용 합성 데이터다.
    product = ActiveProduct("p95", "P95", 95, net_take, 1600, 12)
    scene = [[detection(95, 100, y)] for y in [200, 300, 400, 380, 360]]
    outcome, _ = pipeline_event(
        scene, [500, 500 + increase, 500 - net_take], analyzer=analyzer,
        products=[product], top_config=MotionDirectionConfig(boundary_px=300),
    )
    assert outcome.event.delta_weight == -net_take
    assert [s.delta_grams for s in outcome.event.segments] == [increase, -net_take - increase]
    assert outcome.trace.crossed_directions == []
    assert outcome.event.freezer_actions is None
    assert outcome.event.judgment.products == (ProductCount(product, 1),)
    assert outcome.event.judgment.strategy == "freezer_vision_first"
    assert settle([outcome.event]).total_price == 1600


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
@pytest.mark.parametrize("levels", [[500, 605], [500, 400, 605]])
@pytest.mark.parametrize("ys", [[200, 240, 280], [300, 280, 260], []])
def test_positive_net_uses_weight_return_with_opposite_missing_or_uncrossed_video(
    analyzer, levels, ys,
):
    scene = [[detection(2, 100, y)] for y in ys]
    outcome, _ = pipeline_event(scene, levels, analyzer=analyzer, products=[B])
    assert outcome.event.delta_weight == 105
    assert outcome.event.freezer_actions is None
    result = settle([take(1, 1, (B, 1)), outcome.event])
    assert counts(result) == {}


def test_bottom_origin_roi_and_direction_diagnostics_preserve_original_bbox():
    scene = [[detection(1, 50, 100), detection(2, 350, y)] for y in [300, 260, 220]]
    filters = DetectionFilterChain(
        vertical_roi_region="lower", vertical_roi_y_split=350,
        vertical_roi_coordinate_origin="bottom_left",
    )
    outcome, _ = pipeline_event(scene, [500, 605], filters=filters, save_detections=True)
    assert outcome.event.freezer_actions is None
    assert outcome.trace.decision_coordinate_origin == "bottom_left"
    assert filters.drop_stats["vertical_roi"]["top"] == 3
    first = outcome.trace.frame_detections[0]["detections"][0]
    assert first["bbox"] == [340, 290, 360, 310]
    tracks = outcome.trace.vote_summary["motion_evidence"]["top"][2]["track_detail"]
    segment, = tracks[0]["y_direction"]["segments"]
    assert (segment["start_y"], segment["end_y"], segment["delta_y"]) == (180, 260, 80)
    archived = _trace_to_dict(outcome.trace)
    assert archived["decision_coordinate_origin"] == "bottom_left"
    assert archived["frame_detections"][0]["detections"][0]["bbox"] == [340, 290, 360, 310]


def test_two_cameras_do_not_double_weight_return():
    scene = [[detection(2, 100, y)] for y in [300, 260, 220]]
    outcome, _ = pipeline_event(scene, [500, 605], side=scene, products=[B])
    assert counts(settle([take(1, 1, (B, 2)), outcome.event])) == {"b": 1}


def test_camera_identity_conflict_does_not_veto_existing_weight_return():
    top = [[detection(1, 100, y)] for y in [300, 260, 220]]
    side = [[detection(2, 100, y)] for y in [300, 260, 220]]
    outcome, _ = pipeline_event(top, [500, 605], side=side)
    assert "return" in outcome.trace.uncertain_directions
    assert outcome.event.freezer_actions is None
    assert counts(settle([take(1, 1, (B, 1)), outcome.event])) == {}


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
@pytest.mark.parametrize("same_product", [True, False])
def test_zero_net_roundtrip_keeps_original_low_weight_skip(same_product, analyzer):
    cid = 1 if same_product else 3
    scene = [[detection(1, 50, y)] for y in [200, 240, 280]]
    scene += [[detection(cid, 350, y)] for y in [280, 240, 200]]
    outcome, detector = pipeline_event(scene, [500, 400, 500], analyzer=analyzer)
    assert detector.calls == 0
    assert outcome.event.freezer_actions is None
    assert outcome.event.judgment.reason == "below_min_weight_change"
    assert counts(settle([outcome.event])) == {}


def test_mixed_video_does_not_replace_weight_router_count(monkeypatch):
    from crk_model.judgment.router import JudgmentRouter

    calls = []
    original = JudgmentRouter.judge

    def capture(router, context):
        calls.append(context)
        return original(router, context)

    monkeypatch.setattr(JudgmentRouter, "judge", capture)
    scene = [[detection(1, 50, y), detection(2, 350, y)] for y in [200, 240, 280]]
    scene += [[detection(2, 350, y)] for y in [280, 240, 200]]
    outcome, _ = pipeline_event(scene, [500, 295, 400])
    assert outcome.trace.crossed_directions == ["return", "take"]
    assert calls[0].delta_weight == -100
    assert [s.delta_grams for s in calls[0].segments] == [-205, 105]
    assert outcome.event.judgment.products
    assert outcome.event.freezer_actions is None


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_positive_net_still_requires_original_return_stabilization(analyzer):
    sequence = samples([500, 400])
    sequence += [LoadcellSample(4 + i * .1, (605, 0)) for i in range(3)]
    analysis = analyzer(FREEZER).analyze(sequence)
    assert not analysis.stabilized and analysis.reason == "needs_return_stabilization"
    assert not analysis.segments


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_temporary_increase_does_not_add_return_wait_to_negative_net(analyzer):
    sequence = samples([500, 580])
    sequence += [LoadcellSample(4 + i * .1, (400, 0)) for i in range(3)]
    analysis = analyzer(FREEZER).analyze(sequence)
    assert analysis.stabilized and analysis.delta_weight == -100
    assert analysis.reason == ""


def test_unstable_loadcell_keeps_original_vision_only_route():
    scene = [[detection(1, 50, y)] for y in [200, 240, 280]]
    scene += [[detection(3, 350, y)] for y in [280, 240, 200]]
    outcome, _ = pipeline_event(scene, [500, 500])
    assert outcome.event.judgment.strategy == "vision_only"
    assert outcome.event.judgment.products
    assert outcome.event.freezer_actions is None


@pytest.mark.parametrize("analyzer", [LoadcellAnalyzer, BocpdLoadcellAnalyzer])
def test_freezer_and_refrigerator_share_original_zero_net_channel_gate(analyzer):
    sequence = samples([500, 400, 500])
    for profile in [FREEZER, REFRIGERATOR]:
        analysis = analyzer(profile).analyze(sequence)
        assert analysis.delta_weight == 0 and not analysis.segments


@pytest.mark.parametrize("destination", [1, 2, 3])
def test_weight_only_return_still_matches_same_or_other_floor(destination):
    returned_by_weight = TriggerEvent(
        "s", destination, 2, 100, (), JudgmentResult(JudgmentStatus.NO_DETECTION),
    )
    result = settle([take(1, 1, (A, 1)), returned_by_weight])
    assert counts(result) == {}
    if destination != 1:
        assert any("cross_zone_return" in note for note in result.notes)


def test_action_journal_roundtrip_and_legacy_compatibility():
    event = returned(2, 2, (B, 1))
    replay = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert replay == event
    events = [take(1, 1, (A, 1), (B, 1)), event]
    assert settle(events) == settle([event_from_dict(event_to_dict(e)) for e in events])
    legacy = event_to_dict(replace(event, freezer_actions=None))
    del legacy["freezer_actions"]
    assert event_from_dict(legacy).freezer_actions is None


def test_shared_video_across_zones_does_not_bypass_archived_action_contamination_checks():
    videos = (("top", "/same-top.avi"), ("side", "/same-side.avi"))
    source = replace(take(1, 1, (A, 1), (B, 1)), video_paths=videos)
    destination = replace(returned(2, 2, (B, 1)), video_paths=videos)
    result = settle([source, destination])
    assert counts(result) == {"a": 1, "b": 1}
    assert all(zone.status in ("review", "partial") for zone in result.zones)
    assert any("freezer_shared_video_action_unresolved" in note for note in result.notes)


def test_new_archive_keeps_weight_event_and_direction_diagnostics():
    scene = [[detection(2, 100, y)] for y in [300, 260, 220]]
    outcome, _ = pipeline_event(scene, [500, 605])
    archive = _event_to_dict(outcome.event, outcome.trace, 10)
    assert archive["freezer_actions"] is None
    assert archive["delta_weight"] == 105
    assert archive["trace"]["directional_candidates"]["return"][0]["class_id"] == 2
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(yaml.safe_dump(archive))
    assert document["delta_weight"] == 105 and document["freezer_actions"] is None


def test_calibrated_slanted_side_camera_records_x_axis_without_vetoing_weight_return():
    scene = [[detection(2, x, 100)] for x in [240, 200, 160]]
    outcome, _ = pipeline_event([], [500, 605], side=scene, products=[B],
                               side_config=MotionDirectionConfig(
                                   axis="x", return_direction="decreasing", boundary_px=200,
                               ))
    assert outcome.trace.crossed_directions == ["return"]
    assert outcome.event.freezer_actions is None
    assert counts(settle([take(2, 1, (B, 1)), outcome.event])) == {}


def test_overlapping_segments_keep_existing_zero_weight_skip():
    class OverlapAnalyzer:
        def analyze(self, _samples):
            return LoadcellAnalysis(0, (
                WeightSegment(0, 1, -100), WeightSegment(.5, 1.5, 100),
            ), True, 500)

    scene = [[detection(1, 50, y)] for y in [200, 240, 280]]
    outcome, detector = pipeline_event(scene, [500, 400, 500], analyzer=lambda _: OverlapAnalyzer())
    assert detector.calls == 0
    assert outcome.event.judgment.reason == "below_min_weight_change"
    assert outcome.event.freezer_actions is None
