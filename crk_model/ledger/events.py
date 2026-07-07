"""이벤트 소싱 (D5): 트리거 = 불변 이벤트 축적. 확정 후 이벤트는 거부+기록 (I11 지원)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from crk_model.core.types import JudgmentResult, WeightSegment


@dataclass(frozen=True)
class TriggerEvent:
    session_id: str
    zone: int
    ts: float
    delta_weight: float
    segments: tuple[WeightSegment, ...]
    judgment: JudgmentResult
    seq: int | None = None  # D2: 카메라 시퀀스 (선택 — 없어도 동작)
    status: str = "ok"  # "ok" | "error" (I1: 처리 실패는 에러로 전파)


@dataclass
class EventLog:
    _events: dict[str, list[TriggerEvent]] = field(default_factory=lambda: defaultdict(list))
    _finalized: set[str] = field(default_factory=set)
    rejected: list[TriggerEvent] = field(default_factory=list)

    def append(self, event: TriggerEvent) -> bool:
        if event.session_id in self._finalized:
            # I11: 확정 후 유입 이벤트는 정산에 반영 불가 — 유실 대신 기록
            self.rejected.append(event)
            return False
        self._events[event.session_id].append(event)
        return True

    def events_for(self, session_id: str) -> tuple[TriggerEvent, ...]:
        return tuple(self._events.get(session_id, ()))

    def mark_finalized(self, session_id: str) -> None:
        self._finalized.add(session_id)
