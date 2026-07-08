"""FastAPI HTTP 어댑터 — 무로직 바인딩 (계약·불변식은 전부 ModelService에 있음).

원본 라우트 대응:
- POST /trigger                 → ModelService.handle_trigger (202 의미론)
- POST /api/judge/multi-zone    → ModelService.handle_multi_zone
- GET  /api/health              → 상태·큐 잔량·배리어 상태

워커: start_worker_thread()가 단일 데몬 스레드에서 drain 루프를 돈다 (I7·C2).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Mapping

from crk_model.ingest.loadcell import LoadcellSample
from crk_model.service.model_service import ModelService


def _default_decode(video_paths: Mapping[str, str]):
    from crk_model.adapters.avi_frames import LazyAviFrames

    return LazyAviFrames(video_paths)


def create_app(service: ModelService, *, decode: Callable | None = None):
    from fastapi import Body, FastAPI  # lazy

    decode = decode or _default_decode
    app = FastAPI(title="CRK-model-HG")

    @app.post("/trigger")
    def trigger(payload: dict = Body(...)) -> dict:
        video_paths = payload.get("videos", {})
        loadcells = [
            LoadcellSample(s["timestamp"], tuple(s["values"]))
            for s in payload.get("loadcells", [])
        ]
        ts = payload.get("ts") or (loadcells[0].ts if loadcells else time.time())
        return service.handle_trigger(
            {
                "zone": payload["zone"],
                "video_paths": video_paths,   # I7 멱등성 키
                "frames": decode(video_paths),  # lazy — 디코드는 워커에서
                "loadcells": loadcells,
                "ts": ts,
                "seq": payload.get("seq"),
            }
        )

    @app.post("/api/judge/multi-zone")
    def multi_zone(payload: dict = Body(...)) -> dict:
        return service.handle_multi_zone(payload)

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
            if service.process_pending() == 0:
                time.sleep(interval_s)

    thread = threading.Thread(target=_loop, name="trigger-worker", daemon=True)
    thread.start()
    return thread
