"""ingest — 입력 정규화 계층: loadcell 구간화(D4)·trigger 멱등성(I7).

구간화 분석기는 두 구현이 계약 동형(`LoadcellAnalysis` 반환)이다:
`BocpdLoadcellAnalyzer`가 **primary**(2026-07-23 승격, `MODEL__LOADCELL__ANALYZER`
기본값)이고 `LoadcellAnalyzer`(plateau)는 롤백 스위치다 — 선택은
`service/model_service.py`의 analyzer_factory가 한다.
"""
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.idempotency import IdempotencyRegistry, RegisterResult
from crk_model.ingest.loadcell import (
    ChannelWeightEvent,
    LoadcellAnalysis,
    LoadcellAnalyzer,
    LoadcellSample,
)

__all__ = [
    "BocpdLoadcellAnalyzer",
    "ChannelWeightEvent",
    "IdempotencyRegistry",
    "LoadcellAnalysis",
    "LoadcellAnalyzer",
    "LoadcellSample",
    "RegisterResult",
]
