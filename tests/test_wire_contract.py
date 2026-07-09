"""wire 계약 검증 (REFERENCE.md) — G0 정적 게이트 트랙.

REFERENCE.md 75-112행(트리거 성공 응답), 187-196행(VIDEO_FILE_NOT_FOUND),
7-21행(/api/health)에 정의된 wire 필드가 어댑터 응답에 실제로 존재하는지 검증한다.

fastapi가 없는 환경에서는 test_adapters.py와 동일하게 skip한다.
"""
import pytest
from test_service import FakeClock, FakeDetector, moving_frames, samples

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.service import ModelService


def _wire_loadcells(samples_):
    """LoadcellSample 목록 → 계약 loadcell wire(raw/filtered 문자열)."""
    return [
        {
            "timestamp": s.ts,
            "raw_value": [f"{v:+.1f}" for v in s.values],
            "filtered_value": [f"{v:+.1f}" for v in s.values],
            "filter_method": "none",
        }
        for s in samples_
    ]


@pytest.fixture
def client_and_service():
    pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from crk_model.adapters.http_app import create_app

    svc = ModelService(
        FakeDetector(), profiles={1: REFRIGERATOR}, clock=FakeClock(),
        settings=Settings(close_grace_s=0.0),
    )
    # 디코드 주입(cv2 없이 계약 검증) — 기본으로 비디오 경로 검증이 꺼진다.
    app = create_app(svc, decode=lambda paths: {cam: moving_frames(8) for cam in paths})
    return testclient.TestClient(app), svc


class TestTriggerWireContract:
    """REFERENCE.md 75-112행: /trigger 성공 응답 필드."""

    def test_success_response_has_reference_fields(self, client_and_service):
        client, _svc = client_and_service
        r = client.post(
            "/trigger",
            json={
                "zone": 1,
                "videos": {"top": "/data/t.avi", "side": "/data/s.avi"},
                "loadcells": _wire_loadcells(samples(500, 400)),
            },
        )
        assert r.status_code == 200
        body = r.json()
        # 원본 계약 필드 (REFERENCE.md)
        assert body["success"] is True
        assert body["session_id"] == body["trigger_id"]  # 원본 Camera는 로깅에만 사용
        assert body["door_session_id"] is None  # 아직 문 세션 없음
        assert body["message"] == "Trigger accepted"
        assert body["status"] == "queued"
        assert body["waiting_for"] is None
        # 하위호환 필드 유지
        assert "trigger_id" in body

    def test_duplicate_response_success_true(self, client_and_service):
        client, _svc = client_and_service
        payload = {
            "zone": 1,
            "videos": {"top": "/data/t.avi"},
            "loadcells": _wire_loadcells(samples(500, 400)),
        }
        first = client.post("/trigger", json=payload).json()
        second = client.post("/trigger", json=payload).json()
        assert first["status"] == "queued"
        assert second["status"] == "duplicate"
        assert second["success"] is True  # duplicate도 접수 자체는 성공으로 본다
        assert second["session_id"] == second["trigger_id"]


class TestVideoFileNotFound:
    """REFERENCE.md 187-196행: VIDEO_FILE_NOT_FOUND 사전 검증."""

    def test_missing_video_path_returns_400_when_validation_on(self):
        pytest.importorskip("fastapi")
        testclient = pytest.importorskip("fastapi.testclient")
        from crk_model.adapters.http_app import create_app

        svc = ModelService(
        FakeDetector(), profiles={1: REFRIGERATOR}, clock=FakeClock(),
        settings=Settings(close_grace_s=0.0),
    )
        # decode 주입 없이 validate_video_paths=True를 명시 — 실 파일 검사 경로를 검증.
        app = create_app(svc, validate_video_paths=True)
        client = testclient.TestClient(app)

        r = client.post(
            "/trigger",
            json={
                "zone": 1,
                "videos": {"top": "/no/such/path/top.avi", "side": "/no/such/path/side.avi"},
                "loadcells": _wire_loadcells(samples(500, 400)),
            },
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["error_code"] == "VIDEO_FILE_NOT_FOUND"
        assert "/no/such/path/top.avi" in detail["missing"]
        assert "/no/such/path/side.avi" in detail["missing"]

    def test_validation_off_by_default_when_decode_injected(self, client_and_service):
        # decode가 주입되면(테스트 픽스처처럼) 기본으로 검증이 꺼져 가짜 경로로도 통과한다.
        client, _svc = client_and_service
        r = client.post(
            "/trigger",
            json={
                "zone": 1,
                "videos": {"top": "/no/such/path.avi"},
                "loadcells": _wire_loadcells(samples(500, 400)),
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"


class TestHealthWireContract:
    """REFERENCE.md 7-21행: /api/health 필드."""

    def test_health_has_reference_fields(self, client_and_service):
        client, _svc = client_and_service
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "HEALTHY"
        assert body["status"] == "ok"
        assert body["yolo_loaded"] is True
        assert body["session_store_ready"] is True
        assert isinstance(body["timestamp"], float)
        # 우리 쪽 추가 진단 필드도 유지
        assert "door_state" in body
        assert "queue_pending" in body
        assert "barrier_satisfied" in body
        assert "barrier_pending" in body
