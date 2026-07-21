"""service — 조립 계층: ModelService 파사드·trigger 파이프라인·직렬 워커(C2)."""
from crk_model.service.model_service import ModelService
from crk_model.service.pipeline import (
    TriggerOutcome,
    TriggerPipeline,
    TriggerRequest,
    TriggerTrace,
)
from crk_model.service.snapshot import ActiveProductStore, ProductSnapshot
from crk_model.service.worker import SerialTriggerWorker

__all__ = [
    "ActiveProductStore",
    "ModelService",
    "ProductSnapshot",
    "SerialTriggerWorker",
    "TriggerOutcome",
    "TriggerPipeline",
    "TriggerRequest",
    "TriggerTrace",
]
