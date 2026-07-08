"""이벤트 저널 — 불변 트리거 이벤트의 append-only JSONL 영속화 (D5).

원본의 YAML 세션 영속(data/sessions/)에 대응하되, 이벤트 소싱 구조에 맞게
"집계 상태"가 아니라 "이벤트"를 기록한다. replay()가 G2.5(세션 정산 등가성
게이트)의 훅: 회수한 저널을 재생해 구/신 정산기 diff를 검증한다.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    WeightSegment,
)
from crk_model.ledger.events import TriggerEvent


def event_to_dict(e: TriggerEvent) -> dict:
    return {
        "session_id": e.session_id,
        "zone": e.zone,
        "ts": e.ts,
        "delta_weight": e.delta_weight,
        "segments": [asdict(s) for s in e.segments],
        "judgment": {
            "status": e.judgment.status.value,
            "confidence": e.judgment.confidence,
            "reason": e.judgment.reason,
            "strategy": e.judgment.strategy,
            "products": [
                {"count": pc.count, "product": asdict(pc.product)}
                for pc in e.judgment.products
            ],
        },
        "seq": e.seq,
        "status": e.status,
    }


def event_from_dict(d: dict) -> TriggerEvent:
    j = d["judgment"]
    judgment = JudgmentResult(
        status=JudgmentStatus(j["status"]),
        products=tuple(
            ProductCount(ActiveProduct(**pc["product"]), pc["count"])
            for pc in j["products"]
        ),
        confidence=j["confidence"],
        reason=j["reason"],
        strategy=j["strategy"],
    )
    return TriggerEvent(
        session_id=d["session_id"],
        zone=d["zone"],
        ts=d["ts"],
        delta_weight=d["delta_weight"],
        segments=tuple(WeightSegment(**s) for s in d["segments"]),
        judgment=judgment,
        seq=d["seq"],
        status=d["status"],
    )


class EventJournal:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: TriggerEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")

    def replay(self, session_id: str | None = None) -> list[TriggerEvent]:
        """G2.5 훅: 저널 재생 → 정산기 등가성 검증 입력."""
        if not self._path.exists():
            return []
        events = []
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = event_from_dict(json.loads(line))
                if session_id is None or e.session_id == session_id:
                    events.append(e)
        return events
