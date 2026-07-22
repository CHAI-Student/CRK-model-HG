"""ops 로깅: 문 세션 CLOSE(finalize) 시 존별 확정 요약 [OPS][CLOSE]/[OPS][CLOSE_ERROR].

이슈 배경: Jetson 실기 검증 피드백 — CLOSE 시 존별 상세(weight_delta/products/
triggers)가 로그에 없어 현장 디버깅이 어려웠다. crk_model.gateway.state_machine
의 poll()에서 FINALIZED/ERROR 최초 전이 지점에 세션당 1회씩 남긴다.

회귀 금지: I11(FINALIZED 재폴링 멱등), issue #5(반복 폴링 로그 중복 억제)는
로직을 건드리지 않고 로그만 추가했으므로 여기서도 반복 안 됨을 검증한다.
"""
from dataclasses import asdict
from dataclasses import replace as dc_replace

import pytest

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import JudgmentResult, JudgmentStatus, ProductCount
from crk_model.ledger.events import TriggerEvent
from crk_model.ledger.settler import CloseSettler
from crk_model.perception.detector import Detection
from crk_model.service import ModelService


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeDetector:
    """항상 class_id=1 (콜라) 검출. 호출 횟수 추적. error 지정 시 예외 발생."""

    def __init__(self, detections=None, error=None):
        self.calls = 0
        self.error = error
        self._detections = detections if detections is not None else [
            Detection(1, 0.8, bbox=(50.0, 50.0, 100.0, 100.0))
        ]

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        self.last_allowed = allowed_class_ids
        if self.error:
            raise self.error
        # 모션 변위 증거(perception/motion_evidence.py) 통과용 드리프트 —
        # 실물 취출 상품은 움직인다. 12px/프레임, %8 순환으로 side ROI(400)
        # 경계 안에 머문다 (점프 84px ≤ max_jump 150 → 같은 트랙으로 누적).
        off = 12.0 * (self.calls % 8)
        out = []
        for d in self._detections:
            x1, y1, x2, y2 = d.bbox
            if (x1, y1, x2, y2) == (0.0, 0.0, 0.0, 0.0):
                out.append(d)
            else:
                out.append(dc_replace(d, bbox=(x1 + off, y1, x2 + off, y2)))
        return out


def frame(value):
    return [[value] * 4 for _ in range(4)]


def moving_frames(n):
    return [frame(10 if i % 2 == 0 else 200) for i in range(n)]


def samples(start, end, n=10, dt=0.1):
    from crk_model.ingest.loadcell import LoadcellSample

    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value, 0.0)))  # 트레이 분리: 하중은 단일 채널에
        ts += dt
    return out


def make_service(detector=None, clock=None):
    return ModelService(
        detector or FakeDetector(),
        profiles={1: REFRIGERATOR},
        clock=clock or FakeClock(),
        settings=Settings(close_grace_s=0.0),  # 유예는 test_gateway에서 검증
    )


def open_payload(cola):
    return {"session_id": "s1", "state": "OPEN", "active_products": [asdict(cola)]}


def trigger_payload(zone=1, n_frames=8):
    return {
        "zone": zone,
        "frames": {"top": moving_frames(n_frames), "side": moving_frames(n_frames)},
        "loadcells": samples(500, 400),  # delta -100
        "ts": 1.0,
        "video_paths": {"top": "/t.avi", "side": "/s.avi"},
    }


def _close_summary_lines(records):
    return [
        r
        for r in records
        if r.name == "crk_model.ops"
        and "[OPS][CLOSE]" in r.message
        and "session_id=" in r.message
    ]


def _close_zone_lines(records):
    return [
        r
        for r in records
        if r.name == "crk_model.ops" and "[OPS][CLOSE]" in r.message and "zone=" in r.message
    ]


def _close_error_lines(records):
    return [
        r for r in records if r.name == "crk_model.ops" and "[OPS][CLOSE_ERROR]" in r.message
    ]


class TestOpsCloseLogging:
    def test_finalize_emits_single_summary_and_zone_lines(self, cola, caplog):
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        with caplog.at_level("INFO", logger="crk_model.ops"):
            resp = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert resp["status"] == "success"

        summary_lines = _close_summary_lines(caplog.records)
        zone_lines = _close_zone_lines(caplog.records)
        assert len(summary_lines) == 1
        assert len(zone_lines) >= 1
        assert "total_price=1500" in summary_lines[0].message
        assert "zone=1" in zone_lines[0].message
        assert "products=" in zone_lines[0].message
        assert "triggers=" in zone_lines[0].message

    def test_repoll_does_not_repeat_ops_close_log(self, cola, caplog):
        # issue #5 대칭 요구: 재폴링 3회 이상에도 [OPS][CLOSE] 요약 줄은 1회 유지.
        svc = make_service()
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        with caplog.at_level("INFO", logger="crk_model.ops"):
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})

        summary_lines = _close_summary_lines(caplog.records)
        assert len(summary_lines) == 1

    def test_error_session_emits_single_close_error_log(self, cola, caplog):
        svc = make_service(FakeDetector(error=RuntimeError("gpu")))
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        with caplog.at_level("ERROR", logger="crk_model.ops"):
            resp = svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            # 재폴링에도 반복 안 되는지 함께 확인
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
            svc.handle_multi_zone({"session_id": "s1", "state": "CLOSE"})
        assert resp["status"] == "error"

        error_lines = _close_error_lines(caplog.records)
        assert len(error_lines) == 1
        assert "session_id=" in error_lines[0].message
        assert "reason=" in error_lines[0].message

    def test_zone_basket_carries_weight_delta_trigger_count_notes(self, cola, water):
        # settler 직접 호출 단위 테스트: zone별 weight_delta/trigger_count/notes.
        removal = TriggerEvent(
            "s1", 1, 1.0, -cola.unit_weight,
            (), JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(cola, 1),), 0.9, "strict"),
        )
        # zone=1: 반품 100g 이지만 basket이 이미 소진되어 다른 zone(2)의 water와
        # 매칭되지 않으면 unmatched_return으로 남아 zone=1은 products=none이 된다.
        unmatched_return = TriggerEvent(
            "s1", 1, 2.0, 55.0, (), JudgmentResult(JudgmentStatus.NO_DETECTION)
        )

        settlement = CloseSettler().settle(
            "s1", [removal, unmatched_return], {1: REFRIGERATOR, 2: REFRIGERATOR}
        )
        zone1 = next(z for z in settlement.zones if z.zone == 1)
        assert zone1.trigger_count == 2
        assert zone1.weight_delta == pytest.approx(-cola.unit_weight + 55.0)
        assert any("zone1" in n for n in zone1.notes)
