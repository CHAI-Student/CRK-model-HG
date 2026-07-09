"""FastAPI HTTP 어댑터 — 무로직 바인딩 (계약·불변식은 전부 ModelService에 있음).

원본 라우트 대응:
- POST /trigger                 → ModelService.handle_trigger (202 의미론)
- POST /api/judge/multi-zone    → ModelService.handle_multi_zone
- GET  /api/health              → 상태·큐 잔량·배리어 상태

워커: start_worker_thread()가 단일 데몬 스레드에서 drain 루프를 돈다 (I7·C2).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Mapping

from crk_model.ingest.loadcell import LoadcellSample
from crk_model.service.model_service import ModelService

logger = logging.getLogger(__name__)


def _default_decode(video_paths: Mapping[str, str]):
    from crk_model.adapters.avi_frames import LazyAviFrames

    return LazyAviFrames(video_paths)


# ---- wire 계약 번역 (REFERENCE.md의 Node/Edge 포맷 → 도메인 계약) ----------


def _to_float(value: Any) -> float:
    """로드셀·무게 문자열("+5000") → float. 파싱 불가 시 0.0."""
    try:
        s = str(value).strip()
        if s.startswith("+"):
            s = s[1:]
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value: Any) -> float:
    """timestamp가 ISO 문자열이든 숫자이든 float(epoch/상대초)로 정규화."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _loadcell_from_wire(sample: Mapping[str, Any]) -> LoadcellSample:
    """계약: {timestamp, raw_value:[str], filtered_value:[str], filter_method}.
    무게 판단은 원본 서비스와 동일하게 filtered_value를 우선 사용한다."""
    values = (
        sample.get("filtered_value")
        or sample.get("raw_value")
        or sample.get("values")  # 하위 호환
        or ()
    )
    return LoadcellSample(
        _parse_ts(sample.get("timestamp")),
        tuple(_to_float(v) for v in values),
    )


def _door_state(signal: Any) -> str | None:
    """계약: 문 상태는 session_id 필드에 OPEN/CLOSE 신호로 실려 온다."""
    if isinstance(signal, str) and signal.upper() in ("OPEN", "CLOSE"):
        return signal.upper()
    return None  # 그 외(실 session_id·null) → 폴링


def _active_product_fields(p: Mapping[str, Any]) -> dict:
    """Node 상품(product_idx/product_name/sale_price/...) → ActiveProduct 필드."""
    stock = p.get("stock_qty")
    return {
        "product_id": str(p.get("product_idx") or p.get("product_id") or ""),
        "name": (
            p.get("product_name")
            or p.get("productName")
            or p.get("product_eng_name")
            or p.get("name")
            or ""
        ),
        "class_id": int(
            p.get("yolo_class_id")
            or p.get("trainingidx")
            or p.get("training_idx")
            or 0
        ),
        "unit_weight": _to_float(p.get("product_weight") or p.get("weight") or 0),
        "unit_price": int(_to_float(p.get("sale_price") or p.get("price") or 0)),
        "stock_qty": int(stock) if stock is not None else 999,
    }


def _normalize_multi_zone(body: Any) -> dict:
    """wire(dict 또는 Node 배열) → 도메인 계약 {state, active_products, seq_watermark}."""
    if isinstance(body, list):
        products, signal, seq_watermark = body, None, None
    elif isinstance(body, Mapping):
        products = body.get("products", [])
        signal = body.get("session_id")
        seq_watermark = body.get("seq_watermark")
    else:
        products, signal, seq_watermark = [], None, None
    return {
        "state": _door_state(signal),
        "active_products": [_active_product_fields(p) for p in products],
        "seq_watermark": seq_watermark,
    }


def create_app(service: ModelService, *, decode: Callable | None = None):
    from fastapi import Body, FastAPI  # lazy

    decode = decode or _default_decode
    app = FastAPI(title="CRK-model-HG")

    @app.post("/trigger")
    def trigger(payload: dict = Body(...)) -> dict:
        video_paths = payload.get("videos", {})
        loadcells = [
            _loadcell_from_wire(s) for s in payload.get("loadcells", [])
        ]
        ts = payload.get("ts") or (loadcells[0].ts if loadcells else time.time())
        logger.info(
            "[TRIGGER] received: zone=%s videos=%s loadcells=%d",
            payload.get("zone"), dict(video_paths), len(loadcells),
        )
        resp = service.handle_trigger(
            {
                "zone": payload["zone"],
                "video_paths": video_paths,   # I7 멱등성 키
                "frames": decode(video_paths),  # lazy — 디코드는 워커에서
                "loadcells": loadcells,
                "ts": ts,
                "seq": payload.get("seq"),
            }
        )
        logger.info(
            "[TRIGGER] response: %s (queue_pending=%d)", resp, service.worker.pending
        )
        return resp

    @app.post("/api/judge/multi-zone")
    def multi_zone(payload: Any = Body(...)) -> dict:
        # Node는 객체 {session_id, products, ...} 또는 상품 배열을 보낸다.
        normalized = _normalize_multi_zone(payload)
        resp = service.handle_multi_zone(normalized)
        logger.info(
            "[MULTI-ZONE] state=%s products=%d -> status=%s%s",
            normalized["state"], len(normalized["active_products"]),
            resp.get("status"),
            f" detail={resp['detail']}" if resp.get("detail") else "",
        )
        return resp

    @app.get("/api/health")
    def health() -> dict:
        barrier = service.gateway.barrier.status()
        return {
            "status": "ok",  # 생성 시 startup probe를 통과했어야 함 (fail-fast)
            "door_state": service.gateway.state.value,
            "queue_pending": service.worker.pending,
            "barrier_satisfied": barrier.satisfied,
            "barrier_pending": list(barrier.pending),
        }

    return app


def start_worker_thread(service: ModelService, *, interval_s: float = 0.05) -> threading.Thread:
    """단일 소비자 워커 스레드 (I7: 직렬 추론, TensorRT 충돌 방지)."""

    def _loop() -> None:
        while True:
            try:
                if service.process_pending() == 0:
                    time.sleep(interval_s)
            except Exception:
                # 워커가 죽으면 큐가 영구 적체(배리어 미충족) — 죽는 대신 기록하고 계속.
                # 파이프라인 예외는 I1이 이벤트로 흡수하므로 여기 도달은 저널/게이트웨이 등
                # 인프라 예외뿐이다.
                logger.exception("[WORKER] drain loop error — continuing")
                time.sleep(interval_s)

    thread = threading.Thread(target=_loop, name="trigger-worker", daemon=True)
    thread.start()
    return thread
