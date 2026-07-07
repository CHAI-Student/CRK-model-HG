"""구/신 정산기 shadow 병행 (L6 승인 조건 ②) — diff 로깅 후 전환."""
from __future__ import annotations

from typing import Mapping, Sequence

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import FinalizedSettlement
from crk_model.ledger.events import EventLog, TriggerEvent
from crk_model.ledger.settler import CloseSettler


class ShadowSettlerRunner:
    """primary 결과를 반환하되, shadow 결과와의 diff를 기록한다."""

    def __init__(self, primary: CloseSettler, shadow: CloseSettler):
        self._primary = primary
        self._shadow = shadow
        self.diffs: list[str] = []

    def settle(
        self,
        session_id: str,
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        event_log: EventLog | None = None,
    ) -> FinalizedSettlement:
        result = self._primary.settle(session_id, events, profiles, event_log)
        shadow = self._shadow.settle(session_id, events, profiles, None)
        self._diff(session_id, result, shadow)
        return result

    def _diff(self, sid: str, a: FinalizedSettlement, b: FinalizedSettlement) -> None:
        if a.total_price != b.total_price:
            self.diffs.append(
                f"{sid}:total_price primary={a.total_price} shadow={b.total_price}"
            )
        counts_a = {(z.zone, pc.product.product_id): pc.count for z in a.zones for pc in z.products}
        counts_b = {(z.zone, pc.product.product_id): pc.count for z in b.zones for pc in z.products}
        for key in sorted(set(counts_a) | set(counts_b)):
            if counts_a.get(key, 0) != counts_b.get(key, 0):
                self.diffs.append(
                    f"{sid}:count{key} primary={counts_a.get(key, 0)} shadow={counts_b.get(key, 0)}"
                )
