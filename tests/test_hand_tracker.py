"""Ultralytics callback 계약과 손 ID를 이용한 행동 증거. GPU 엔진은 Jetson에서 확인한다."""
import sys
from functools import partial
from types import SimpleNamespace

import pytest

from crk_model.adapters.yolo_detector import UltralyticsEngineDetector
from crk_model.perception.detector import Detection
from crk_model.perception.motion_direction import MotionDirectionConfig
from crk_model.perception.motion_evidence import MotionEvidence


class Box:
    def __init__(self, cid, confidence, bbox, track_id=None):
        self.cls = [cid]
        self.conf = [confidence]
        self.xyxy = [bbox]
        self.id = [track_id] if track_id is not None else None


class Result:
    def __init__(self, boxes):
        self.boxes = boxes

    def __getitem__(self, indices):
        return Result([self.boxes[i] for i in indices])


class Tracker:
    def __init__(self):
        self.seen = 0
        self.classes = []

    def reset(self):
        self.seen = 0
        self.classes.clear()


def on_predict_postprocess_end(predictor, persist):
    tracker = predictor.trackers[0]
    for i, result in enumerate(predictor.results):
        tracker.seen += 1
        tracker.classes.extend(int(b.cls[0]) for b in result.boxes)
        predictor.results[i] = Result([
            Box(b.cls[0], b.conf[0], b.xyxy[0], 77) for b in result.boxes
        ])


on_predict_postprocess_end.__module__ = 'ultralytics.trackers.track'


class FakeYOLO:
    def __init__(self, path, task):
        self.callbacks = {'on_predict_postprocess_end': []}
        self.predictor = None
        self.calls = []

    def add_callback(self, event, callback):
        self.callbacks[event].append(callback)

    def predict(self, source, **kwargs):
        self.calls.append(('predict', kwargs))
        return [Result([Box(92, .02, (10, 20, 30, 40)),
                        Box(0, .9, (100, 100, 150, 150))])]

    def track(self, source, **kwargs):
        self.calls.append(('track', kwargs))
        if self.predictor is None:
            self.add_callback('on_predict_postprocess_end',
                              partial(on_predict_postprocess_end, persist=True))
            self.predictor = SimpleNamespace(trackers=[Tracker()], callbacks=self.callbacks,
                                             args=SimpleNamespace(mode='track'))
        count = source if isinstance(source, int) else 1
        self.predictor.results = [Result([Box(92, .02, (10, 20, 30, 40)),
                                          Box(0, .9, (100, 100, 150, 150))])
                                  for _ in range(count)]
        for callback in self.callbacks['on_predict_postprocess_end']:
            callback(self.predictor)
        # BasePredictor는 입력 batch 크기만큼 출력을 순회한다.
        assert len(self.predictor.results) == count
        return self.predictor.results


@pytest.fixture
def detector(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ultralytics', SimpleNamespace(YOLO=FakeYOLO))
    return UltralyticsEngineDetector('test.engine', tracker='bytetrack')


def test_track_keeps_low_confidence_products_and_attaches_hand_ids(detector):
    first = detector.detect('frame')
    second = detector.detect('next-frame')
    assert first == second
    assert first[0].class_id == 92 and first[0].confidence == .02
    assert first[0].track_id is None
    assert first[1].is_hand and first[1].track_id == 77
    tracker = detector._model.predictor.trackers[0]
    assert tracker.classes == [0, 0]
    assert tracker.seen == 2
    assert detector._model.calls[0][1]['tracker'] == 'bytetrack.yaml'
    assert detector._model.calls[0][1]['persist'] is True


def test_padding_is_not_tracked_and_new_fixed_camera_clip_starts_fresh(detector):
    detector.begin_clip('top')
    result = detector._infer(4, 2)
    detections = detector._tracked_detections(result, 2)
    tracker = detector._model.predictor.trackers[0]
    assert len(detections) == 2 and tracker.seen == 2
    detector.begin_clip('side')
    assert tracker.seen == 0
    detector.detect('side-frame')
    assert tracker.seen == 1
    detector.detect('next-side-frame')
    assert tracker.seen == 2
    # 다음 트리거의 side도 별도의 촬영이다. 영상 사이의 공백은 연결하지 않는다.
    detector.begin_clip('side')
    assert tracker.seen == 0
    detector.detect('next-trigger-side-frame')
    assert tracker.seen == 1


def test_tracker_off_preserves_predict_contract(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ultralytics', SimpleNamespace(YOLO=FakeYOLO))
    detector = UltralyticsEngineDetector('test.engine')
    assert all(d.track_id is None for d in detector.detect('frame'))
    assert detector._model.calls[0][0] == 'predict'


def observations(invalid=None, background=False):
    evidence = MotionEvidence(direction_configs={'top': MotionDirectionConfig()})
    for pos in range(10):
        cy = max(260, 380 - pos * 30)
        x = 180 + (20 * (pos - 4) if invalid == 'moving_after' and pos >= 5 else 0)
        product = Detection(92, .9, bbox=(x - 10, cy - 10, x + 10, cy + 10))
        hand_x = x if pos < 5 else 400
        hand_id = None if invalid == 'no_ids' else 7
        hand = Detection(0, .9, True, (hand_x - 20, cy - 20, hand_x + 20, cy + 20), hand_id)
        ds = [product]
        if invalid != 'disappeared' or pos < 5:
            ds.append(hand)
        if invalid == 'other_holds' and pos >= 5:
            ds.append(Detection(0, .9, True, product.bbox, 8))
        if background:
            ds.append(Detection(103, .9, bbox=(10, cy - 10, 30, cy + 10)))
        evidence.observe('top', ds, pos=pos)
    return evidence


def test_same_hand_retreat_and_product_resting_supply_completion_evidence():
    evidence = observations(background=True)
    frames, _, _ = evidence.action_frames({'top': .1})
    assert {d.class_id for ds in frames['return'].values() for d in ds} == {92}
    assert any(d['return_completed'] and d['hand_track_id'] == 7
               for d in evidence.action_details)


@pytest.mark.parametrize('invalid', ['disappeared', 'no_ids', 'moving_after', 'other_holds'])
def test_missing_hand_detection_or_ongoing_hold_does_not_confirm_return(invalid):
    evidence = observations(invalid)
    evidence.action_frames({'top': .1})
    assert not any(d['return_completed'] for d in evidence.action_details)


def test_action_candidates_keep_full_observed_segment_without_weight_time_alignment():
    evidence = observations()
    frames, _, _ = evidence.action_frames({'top': .1})
    assert {pos for _, pos in frames['return']} == set(range(10))
    assert any(d['return_completed'] for d in evidence.action_details)


def test_identical_hand_id_in_side_cannot_complete_top_return():
    evidence = MotionEvidence(direction_configs={
        'top': MotionDirectionConfig(), 'side': MotionDirectionConfig(),
    })
    for pos in range(10):
        cy = max(260, 380 - pos * 30)
        product = Detection(92, .9, bbox=(170, cy - 10, 190, cy + 10))
        detections = [product]
        if pos < 5:
            detections.append(Detection(0, .9, True, product.bbox, 7))
        evidence.observe('top', detections, pos=pos)
        evidence.observe('side', [Detection(0, .9, True, (400, 10, 440, 50), 7)], pos=pos)
    evidence.action_frames({'top': .1, 'side': .1})
    assert evidence.action_details
    assert all(not d['return_completed'] for d in evidence.action_details)
