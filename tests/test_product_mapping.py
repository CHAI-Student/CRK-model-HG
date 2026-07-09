"""product→YOLO class 매핑 (issue #6 결함 수정): camelCase 별칭 + 이름 기반
폴백 + 미매핑 시 -1 센티널(hand=0과 충돌 금지)."""
import pytest

from crk_model.adapters.http_app import _active_product_fields, _normalize_multi_zone


class TestNumericAliasResolution:
    """work item 1-1: 숫자 class_id 필드의 camelCase/snake_case 별칭 전부 인식."""

    def test_yolo_class_id_snake_case(self):
        p = _active_product_fields({"yolo_class_id": 7})
        assert p["class_id"] == 7

    def test_yolo_class_id_camel_case(self):
        p = _active_product_fields({"yoloClassId": 7})
        assert p["class_id"] == 7

    def test_trainingidx(self):
        p = _active_product_fields({"trainingidx": 3})
        assert p["class_id"] == 3

    def test_training_idx_snake_case(self):
        p = _active_product_fields({"training_idx": 4})
        assert p["class_id"] == 4

    def test_training_idx_camel_case(self):
        p = _active_product_fields({"trainingIdx": 5})
        assert p["class_id"] == 5


class TestNameBasedFallback:
    """work item 1-2: 숫자 필드가 없거나 0일 때 이름으로 class_id 조회."""

    def test_product_eng_name_resolves(self):
        name_to_class = {"STICK_BINGGRAE_MELONA_75ML": 12}
        p = _active_product_fields(
            {"product_eng_name": "STICK_BINGGRAE_MELONA_75ML"},
            name_to_class=name_to_class,
        )
        assert p["class_id"] == 12

    def test_product_eng_name_case_insensitive(self):
        name_to_class = {"STICK_BINGGRAE_MELONA_75ML": 12}
        p = _active_product_fields(
            {"product_eng_name": "stick_binggrae_melona_75ml"},
            name_to_class=name_to_class,
        )
        assert p["class_id"] == 12

    def test_falls_back_to_product_name_when_eng_name_absent(self):
        name_to_class = {"COLA_500ML": 9}
        p = _active_product_fields(
            {"product_name": "COLA_500ML"}, name_to_class=name_to_class
        )
        assert p["class_id"] == 9

    def test_falls_back_to_name_field(self):
        name_to_class = {"COLA_500ML": 9}
        p = _active_product_fields({"name": "COLA_500ML"}, name_to_class=name_to_class)
        assert p["class_id"] == 9

    def test_numeric_zero_still_triggers_name_fallback(self):
        # yolo_class_id=0은 hand과 충돌하는 값이므로 "없음"과 동일하게 취급하고
        # 이름 매칭을 시도해야 한다.
        name_to_class = {"COLA_500ML": 9}
        p = _active_product_fields(
            {"yolo_class_id": 0, "product_name": "COLA_500ML"},
            name_to_class=name_to_class,
        )
        assert p["class_id"] == 9


class TestUnmappedSentinel:
    """work item 1-4: 어떤 경로로도 못 찾으면 0이 아니라 -1."""

    def test_no_numeric_no_name_map(self):
        p = _active_product_fields({"product_name": "UNKNOWN_PRODUCT"})
        assert p["class_id"] == -1

    def test_name_not_in_map(self):
        p = _active_product_fields(
            {"product_name": "UNKNOWN_PRODUCT"}, name_to_class={"COLA_500ML": 9}
        )
        assert p["class_id"] == -1

    def test_totally_empty_product(self):
        p = _active_product_fields({})
        assert p["class_id"] == -1


class TestNormalizeMultiZoneThreadsMapping:
    def test_name_to_class_passed_through(self):
        name_to_class = {"COLA_500ML": 9}
        body = {"session_id": "OPEN", "products": [{"product_name": "COLA_500ML"}]}
        normalized = _normalize_multi_zone(body, name_to_class)
        assert normalized["active_products"][0]["class_id"] == 9

    def test_none_mapping_yields_unmapped(self):
        body = {"session_id": "OPEN", "products": [{"product_name": "COLA_500ML"}]}
        normalized = _normalize_multi_zone(body)
        assert normalized["active_products"][0]["class_id"] == -1


class TestOpenMappingLog:
    """work item 1-5: OPEN 로그에 mapped=X/Y unmapped=[...] 기록."""

    def test_unmapped_product_logged(self, caplog):
        from test_service import FakeClock, FakeDetector

        from crk_model.core.profiles import REFRIGERATOR
        from crk_model.service.model_service import ModelService

        svc = ModelService(FakeDetector(), profiles={1: REFRIGERATOR}, clock=FakeClock())
        with caplog.at_level("WARNING", logger="crk_model.service.model_service"):
            svc.handle_multi_zone(
                {
                    "state": "OPEN",
                    "active_products": [
                        {
                            "product_id": "P1",
                            "name": "UNKNOWN",
                            "class_id": -1,
                            "unit_weight": 100.0,
                            "unit_price": 1000,
                            "stock_qty": 5,
                        }
                    ],
                    "seq_watermark": None,
                }
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any("mapped=0/1" in m and "UNKNOWN" in m for m in messages)


class TestHttpEndToEndMapping:
    """work item: create_app(service, yolo_name_to_id=...) — 이름만 있는 상품이
    HTTP 계층을 통해 올바른 class_id로 매핑되는지 종단 검증."""

    @pytest.fixture
    def client_and_service(self):
        pytest.importorskip("fastapi")
        testclient = pytest.importorskip("fastapi.testclient")
        from test_service import FakeClock, FakeDetector

        from crk_model.adapters.http_app import create_app
        from crk_model.core.profiles import REFRIGERATOR
        from crk_model.service.model_service import ModelService

        svc = ModelService(FakeDetector(), profiles={1: REFRIGERATOR}, clock=FakeClock())
        app = create_app(
            svc,
            decode=lambda paths: {},
            yolo_name_to_id={"COLA_500ML": 9},
        )
        return testclient.TestClient(app), svc

    def test_name_only_product_resolves_class_id(self, client_and_service):
        client, svc = client_and_service
        r = client.post(
            "/api/judge/multi-zone",
            json={
                "session_id": "OPEN",
                "products": [
                    {
                        "product_idx": "P1",
                        "product_name": "COLA_500ML",
                        "sale_price": 1500,
                        "stock_qty": 5,
                        "product_weight": "500",
                    }
                ],
            },
        )
        assert r.json()["status"] == "processing"
        snapshot = svc.snapshots.snapshot()
        product = next(p for p in snapshot.products if p.product_id == "P1")
        assert product.class_id == 9
