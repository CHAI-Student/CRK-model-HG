"""FastAPI HTTP 어댑터 — 무로직 바인딩 (계약·불변식은 전부 ModelService에 있음).

원본 라우트 대응:
- POST /trigger                 → ModelService.handle_trigger (202 의미론)
                                   + REFERENCE.md wire 필드 보강(success/session_id/
                                   door_session_id/message/waiting_for) + 비디오 사전 검증
- POST /api/judge/multi-zone    → ModelService.handle_multi_zone
- GET  /api/health              → REFERENCE.md 계약 필드 + 상태·큐 잔량·배리어 상태(진단용)

워커: start_worker_thread()가 단일 데몬 스레드에서 drain 루프를 돈다 (I7·C2).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

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


def _wire_trigger_response(resp: dict, service: ModelService) -> dict:
    """ModelService.handle_trigger 결과 → REFERENCE.md wire 계약(75-112행) 보강.

    기존 필드(status/trigger_id)는 하위호환을 위해 그대로 유지하고,
    원본 계약 필드(success/session_id/door_session_id/message/waiting_for)를
    덧붙인다 — 필드 매핑은 어댑터 소관, ModelService에는 로직을 두지 않는다.
    """
    status = resp.get("status")
    trigger_id = resp.get("trigger_id")
    door_session_id = service.gateway.session_id
    return {
        **resp,
        "success": status in ("queued", "duplicate"),
        "session_id": trigger_id,
        "door_session_id": door_session_id if door_session_id is not None else None,
        "message": "Trigger accepted" if status == "queued" else "Trigger duplicate (dropped)",
        "waiting_for": None,
    }


def create_app(
    service: ModelService,
    *,
    decode: Callable | None = None,
    validate_video_paths: bool | None = None,
):
    from fastapi import Body, FastAPI, HTTPException  # lazy

    # decode가 주입되면(테스트 등) 실 파일 경로가 아닐 수 있으므로 기본으로 검증을
    # 끈다. 기본 실디코더(_default_decode)일 때만 기본으로 검증을 켠다.
    # 명시적으로 validate_video_paths를 주면 그 값이 항상 우선한다.
    if validate_video_paths is None:
        validate_video_paths = decode is None
    decode = decode or _default_decode
    app = FastAPI(title="CRK-model-HG")

    @app.post("/trigger")
    def trigger(payload: dict = Body(...)) -> dict:
        video_paths = payload.get("videos", {})
        if validate_video_paths:
            missing = [p for p in video_paths.values() if not os.path.isfile(p)]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail={"error_code": "VIDEO_FILE_NOT_FOUND", "missing": missing},
                )
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
        resp = _wire_trigger_response(resp, service)
        logger.info(
            "[TRIGGER] response: %s (queue_pending=%d)", resp, service.worker.pending
        )
        return resp

    # CLOSE는 문이 닫혀있는 동안 계속 재폴링되는 level-triggered 신호라(I11),
    # 매 호출마다 동일 결과를 로그로 남기면 몇 분씩 같은 줄이 반복되어 "멈춘 것처럼"
    # 보인다(issue #5). 응답이 실제로 바뀔 때만 기록해 로그 소음을 줄인다 — 프로토콜
    # 응답 자체(결제 확정 정보)는 그대로 매 호출 반환한다.
    _last_multi_zone_log: list[Any] = [None]

    @app.post("/api/judge/multi-zone")
    def multi_zone(payload: Any = Body(...)) -> dict:
        # Node는 객체 {session_id, products, ...} 또는 상품 배열을 보낸다.
        normalized = _normalize_multi_zone(payload)
        resp = service.handle_multi_zone(normalized)
        log_key = (normalized["state"], resp.get("status"), resp.get("detail"))
        if log_key != _last_multi_zone_log[0]:
            _last_multi_zone_log[0] = log_key
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
            # REFERENCE.md(7-21행) 계약 필드. yolo_loaded/model은 항상 true/"HEALTHY"
            # 로 고정한다 — ModelService 생성 시 startup_probe_frame으로 detector를
            # 1회 실행해 로드 실패를 기동 실패로 만들기(fail-fast, 이관 리뷰 #1) 때문에,
            # 이 핸들러에 도달했다는 것 자체가 이미 startup probe 통과를 의미한다.
            "model": "HEALTHY",
            "status": "ok",  # 생성 시 startup probe를 통과했어야 함 (fail-fast)
            "yolo_loaded": True,
            "session_store_ready": True,
            "timestamp": time.time(),
            # 아래는 우리 쪽 추가 진단 필드 (원본 계약에는 없음)
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
