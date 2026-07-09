"""adapters: HTTP 바인딩 E2E, FrameBundle 뷰 분리, lazy 디코드 계약.

ultralytics/cv2가 필요한 경로는 Jetson 전용(G4)이라 여기서 검증하지 않는다.
fastapi가 없는 환경에서는 HTTP 테스트를 skip한다.
"""
import pytest
from test_service import FakeClock, FakeDetector, frame, moving_frames, samples

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.frames.bundle import FrameBundle
from crk_model.service import ActiveProductStore, ModelService, TriggerPipeline, TriggerRequest


def _node_product(p):
    """ActiveProduct → Node/Edge wire 상품 포맷 (REFERENCE.md 계약)."""
    return {
        "product_idx": p.product_id,
        "product_name": p.name,
        "yolo_class_id": p.class_id,
        "product_weight": str(p.unit_weight),
        "sale_price": p.unit_price,
        "stock_qty": p.stock_qty,
    }


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


class TestFrameBundle:
    def test_gate_uses_view_detector_gets_full(self, cola):
        # 게이트 뷰는 정지(전부 skip), keepalive만 추론 → 검출기 호출 수로 검증
        detector = FakeDetector()
        store = ActiveProductStore()
        store.update([cola])
        pipe = TriggerPipeline(detector, {1: REFRIGERATOR}, store)
        static_view = frame(10)
        bundles = [FrameBundle(full=f"full-{i}", gate_view=static_view) for i in range(9)]
        pipe.process("s1", TriggerRequest(1, {"top": bundles}, samples(500, 400), 1.0))
        # first_frame 1회 + keepalive(8프레임 간격) 1회 = 2회 (모션 없음)
        assert detector.calls == 2


class TestHttpAdapter:
    @pytest.fixture
    def client_and_service(self, cola):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        testclient = pytest.importorskip("fastapi.testclient")
        from crk_model.adapters.http_app import create_app

        svc = ModelService(
            FakeDetector(), profiles={1: REFRIGERATOR}, clock=FakeClock(),
            settings=Settings(close_grace_s=0.0),
        )
        # 디코드 주입: AVI 경로 → 준비된 프레임 (cv2 없이 계약 검증)
        app = create_app(svc, decode=lambda paths: {cam: moving_frames(8) for cam in paths})
        return testclient.TestClient(app), svc

    def test_full_http_flow(self, client_and_service, cola):
        client, svc = client_and_service
        r = client.post(
            "/api/judge/multi-zone",
            json={"session_id": "OPEN", "products": [_node_product(cola)]},
        )
        assert r.json()["status"] == "processing"

        r = client.post(
            "/trigger",
            json={
                "zone": 1,
                "videos": {"top": "/data/t.avi", "side": "/data/s.avi"},
                "loadcells": _wire_loadcells(samples(500, 400)),
            },
        )
        assert r.json()["status"] == "queued"  # 202 의미론 (디코드 전 즉시 응답)

        # 큐 미소진 CLOSE → 배리어 보류 (I17)
        r = client.post("/api/judge/multi-zone", json={"session_id": "CLOSE"})
        assert r.json()["status"] == "processing"

        svc.process_pending()  # 워커 스레드 대행
        r = client.post("/api/judge/multi-zone", json={"session_id": "CLOSE"})
        body = r.json()
        assert body["status"] == "success"
        assert body["totalPrice"] == 1500

    def test_health_reports_barrier(self, client_and_service):
        client, svc = client_and_service
        r = client.get("/api/health")
        body = r.json()
        assert body["status"] == "ok"
        assert body["queue_pending"] == 0
        assert body["barrier_satisfied"] is True

    def test_duplicate_trigger_via_http(self, client_and_service, cola):
        client, svc = client_and_service
        client.post(
            "/api/judge/multi-zone",
            json={"session_id": "OPEN", "products": [_node_product(cola)]},
        )
        payload = {
            "zone": 1,
            "videos": {"top": "/data/t.avi"},
            "loadcells": [
                {
                    "timestamp": 0.0,
                    "raw_value": ["+250.0", "+250.0"],
                    "filtered_value": ["+250.0", "+250.0"],
                    "filter_method": "none",
                }
            ],
        }
        first = client.post("/trigger", json=payload).json()
        second = client.post("/trigger", json=payload).json()
        assert first["status"] == "queued"
        assert second["status"] == "duplicate"  # I7
