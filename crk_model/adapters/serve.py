"""진입점 — `model-service-hg` 콘솔 스크립트.

기동 순서:
1. .env 로드 (stdlib 파서, 원본 Settings 자동 로드 관행 대응)
2. TensorRT 엔진 로드 + startup probe → 실패 시 기동 실패 (이관 리뷰 #1)
3. 워커 스레드 시작 → uvicorn 서빙 (:8002)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_dotenv()

    # 도메인 로거([TRIGGER]/[MULTI-ZONE]/[GATEWAY]) 출력 — uvicorn log_level은
    # uvicorn 로거만 다루므로 crk_model 로거에는 별도 핸들러가 필요하다.
    logging.basicConfig(
        level=os.environ.get("MODEL__LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import numpy as np  # Jetson: system-site numpy<2

    from crk_model.adapters.http_app import create_app, start_worker_thread
    from crk_model.adapters.yolo_detector import UltralyticsEngineDetector
    from crk_model.core.config import Settings
    from crk_model.ledger.journal import EventJournal
    from crk_model.service.model_service import ModelService

    settings = Settings.from_env()
    model_path = os.environ.get(
        "MODEL__VISION__YOLO_MODEL_PATH", "models/set9_doorfas_0323_imbal.engine"
    )
    detector = UltralyticsEngineDetector(model_path)

    journal_path = Path(os.environ.get("MODEL__LEDGER__JOURNAL_PATH", "logs/events.jsonl"))
    service = ModelService(
        detector,
        settings=settings,
        journal=EventJournal(journal_path),
        # 리뷰 #1: 엔진 로드 실패·CUDA 불가 시 여기서 즉시 죽는다 (무증상 기동 금지)
        startup_probe_frame=np.zeros((480, 480, 3), dtype=np.uint8),
    )
    start_worker_thread(service)

    import uvicorn

    uvicorn.run(
        create_app(service),
        host=os.environ.get("MODEL__SERVER__HOST", "0.0.0.0"),
        port=int(os.environ.get("MODEL__SERVER__PORT", "8002")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
