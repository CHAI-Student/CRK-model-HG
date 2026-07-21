"""ingest — 입력 정규화 계층: loadcell 구간화(D4)·trigger 멱등성(I7)."""
from crk_model.ingest.idempotency import IdempotencyRegistry, RegisterResult
from crk_model.ingest.loadcell import (
    ChannelWeightEvent,
    LoadcellAnalysis,
    LoadcellAnalyzer,
    LoadcellSample,
)

__all__ = [
    "ChannelWeightEvent",
    "IdempotencyRegistry",
    "LoadcellAnalysis",
    "LoadcellAnalyzer",
    "LoadcellSample",
    "RegisterResult",
]
