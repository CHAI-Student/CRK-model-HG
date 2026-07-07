"""close-time 단일 글로벌 정산기 (D5, L6, QA Q6·Q7).

반품 복구 3계층 + freezer close resolver(4층)를 하나의 정산기로 통합.
3계층은 내부 매칭 우선순위로 강등: 동존 즉시 > net-delta > 교차존.

불변식:
- I11: finalize 멱등 — 같은 session_id는 항상 같은 결과 객체.
- I14: 반품 정산이 존별 count를 음수로 만들 수 없음 (환수 > 청구 금지).
- I13: 에러 trigger 존재 시 무성 확정 금지 — ErrorSessionPolicy로만 처리.
- I3: freezer close 재solve도 개수 게이트 통과 필수, 실패 시 증분 결과 유지.
- I8: 모든 보정은 notes에 사유 코드로 기록.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from crk_model.core.policy import ErrorSessionPolicy
from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    FinalizedSettlement,
    InterimSummary,
    JudgmentStatus,
    ProductCount,
    ZoneBasket,
)
from crk_model.ledger.events import EventLog, TriggerEvent


class _Basket:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.products: dict[str, ActiveProduct] = {}

    def add(self, product: ActiveProduct, count: int = 1) -> None:
        self.products[product.product_id] = product
        self.counts[product.product_id] = self.counts.get(product.product_id, 0) + count

    def remove_one(self, product_id: str) -> bool:
        if self.counts.get(product_id, 0) <= 0:
            return False  # I14: 음수 금지
        self.counts[product_id] -= 1
        return True

    def set_count(self, product_id: str, count: int) -> None:
        assert count >= 0  # I14
        self.counts[product_id] = count

    def weight(self) -> float:
        return sum(self.products[pid].unit_weight * c for pid, c in self.counts.items())

    def items(self) -> list[tuple[ActiveProduct, int]]:
        return [(self.products[pid], c) for pid, c in self.counts.items() if c > 0]

    def to_zone(self, zone: int) -> ZoneBasket:
        return ZoneBasket(
            zone,
            tuple(
                ProductCount(p, c)
                for p, c in sorted(self.items(), key=lambda t: t[0].product_id)
            ),
        )


def _ok_events(events: Iterable[TriggerEvent]) -> list[TriggerEvent]:
    return [
        e
        for e in events
        if e.status == "ok" and e.judgment.status is not JudgmentStatus.ERROR
    ]


def _profile(profiles: Mapping[int, SensorProfile], zone: int) -> SensorProfile:
    return profiles.get(zone, REFRIGERATOR)


def pass_same_zone(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile]
) -> tuple[dict[int, _Basket], list[tuple[int, float]]]:
    """1층: 동존 즉시 복구 — removal은 판정 품목 축적, return은 무게 매칭 차감."""
    baskets: dict[int, _Basket] = defaultdict(_Basket)
    unmatched: list[tuple[int, float]] = []
    for e in sorted(events, key=lambda e: e.ts):
        b = baskets[e.zone]
        tol = _profile(profiles, e.zone).tolerance_grams
        if e.delta_weight < 0:
            for pc in e.judgment.products:
                b.add(pc.product, pc.count)
        elif e.delta_weight > 0:
            if not _match_return(b, e.delta_weight, tol):
                unmatched.append((e.zone, e.delta_weight))
    return baskets, unmatched


def _match_return(b: _Basket, ret_weight: float, tol: float) -> bool:
    for p, _c in b.items():
        if abs(ret_weight - p.unit_weight) <= tol:
            return b.remove_one(p.product_id)
    items = b.items()
    for i, (p1, c1) in enumerate(items):
        for j, (p2, _c2) in enumerate(items):
            if j < i or (i == j and c1 < 2):
                continue
            if abs(ret_weight - (p1.unit_weight + p2.unit_weight)) <= tol:
                return b.remove_one(p1.product_id) and b.remove_one(p2.product_id)
    return False


class CloseSettler:
    def __init__(self, error_policy: ErrorSessionPolicy = ErrorSessionPolicy.BLOCK_PAYMENT):
        self.error_policy = error_policy
        self._finalized: dict[str, FinalizedSettlement] = {}

    def settle(
        self,
        session_id: str,
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        event_log: EventLog | None = None,
    ) -> FinalizedSettlement:
        if session_id in self._finalized:
            return self._finalized[session_id]  # I11: 멱등

        notes: list[str] = []
        ok = _ok_events(events)
        error_zones = sorted(
            {
                e.zone
                for e in events
                if e.status != "ok" or e.judgment.status is JudgmentStatus.ERROR
            }
        )

        baskets, unmatched = pass_same_zone(ok, profiles)
        self._pass_net_delta(baskets, ok, profiles, unmatched, notes)
        self._pass_cross_zone(baskets, unmatched, profiles, notes)
        self._freezer_resolve(baskets, ok, profiles, notes)

        zones = []
        for zone in sorted(baskets):
            zb = baskets[zone].to_zone(zone)
            for pc in zb.products:
                assert pc.count >= 0  # I14 (구조상 보장, 방어적 확인)
            zones.append(zb)

        blocked = False
        block_reason = ""
        if error_zones:
            if self.error_policy is ErrorSessionPolicy.BLOCK_PAYMENT:
                blocked = True  # I13: 무성 확정 금지 (fail-closed 기본)
                block_reason = f"error_trigger_present:zones={error_zones}"
            else:  # FINALIZE_ERROR_FREE_ZONES (Node 합의 시에만)
                zones = [z for z in zones if z.zone not in error_zones]
                notes.append(f"error_zones_excluded:{error_zones}")
                if not zones:
                    blocked = True
                    block_reason = "all_zones_errored"

        settlement = FinalizedSettlement(
            session_id, tuple(zones), blocked, block_reason, tuple(notes)
        )
        self._finalized[session_id] = settlement
        if event_log is not None:
            event_log.mark_finalized(session_id)
        return settlement

    @staticmethod
    def _pass_net_delta(
        baskets: dict[int, _Basket],
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        unmatched: list[tuple[int, float]],
        notes: list[str],
    ) -> None:
        """2층: 세션 net delta와 어긋난 과잉 청구 교정."""
        for zone, b in baskets.items():
            tol = _profile(profiles, zone).tolerance_grams
            net = sum(e.delta_weight for e in events if e.zone == zone)
            excess = b.weight() - max(0.0, -net)
            while excess > tol:
                cands = [p for p, _c in b.items() if p.unit_weight <= excess + tol]
                if not cands:
                    break
                p = min(cands, key=lambda p: abs(p.unit_weight - excess))
                b.remove_one(p.product_id)
                notes.append(f"net_delta_correction:zone{zone}:{p.product_id}-1")
                # 이 보정이 설명한 동존 미매칭 반품 소거 (교차존 이중 차감 방지)
                for k, (z, wt) in enumerate(unmatched):
                    if z == zone and abs(wt - p.unit_weight) <= tol:
                        unmatched.pop(k)
                        break
                excess -= p.unit_weight

    @staticmethod
    def _pass_cross_zone(
        baskets: dict[int, _Basket],
        unmatched: list[tuple[int, float]],
        profiles: Mapping[int, SensorProfile],
        notes: list[str],
    ) -> None:
        """3층: 미매칭 반품을 다른 존 장바구니와 매칭 (존 착오 반납)."""
        for zone, wt in unmatched:
            hit = False
            for oz, b in baskets.items():
                if oz == zone:
                    continue
                tol = _profile(profiles, oz).tolerance_grams
                for p, _c in b.items():
                    if abs(wt - p.unit_weight) <= tol:
                        b.remove_one(p.product_id)
                        notes.append(
                            f"cross_zone_return:zone{zone}->zone{oz}:{p.product_id}-1"
                        )
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                notes.append(f"unmatched_return:zone{zone}:{wt:+.1f}g")

    @staticmethod
    def _freezer_resolve(
        baskets: dict[int, _Basket],
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        notes: list[str],
    ) -> None:
        """4층: freezer 부호있는 net basket 재solve (불안정 close 대비, QA Q6)."""
        for zone, b in list(baskets.items()):
            prof = _profile(profiles, zone)
            if prof.weight_is_discriminative:
                continue
            gate = prof.count_gate
            net = sum(e.delta_weight for e in events if e.zone == zone)
            if net >= -gate:
                # 순변화 없음(전량 반품 포함) → 과금 없음
                if b.items():
                    notes.append(f"freezer_close_resolve:zone{zone}:net~0->clear")
                    for pid in list(b.counts):
                        b.set_count(pid, 0)
                continue
            kinds = b.items()
            if len(kinds) == 1:
                p, _c = kinds[0]
                count = round(-net / p.unit_weight) if p.unit_weight > 0 else 0
                if (
                    1 <= count <= p.stock_qty  # I12
                    and abs(-net - count * p.unit_weight) <= gate  # I3
                ):
                    b.set_count(p.product_id, count)
                    notes.append(f"freezer_close_resolve:zone{zone}:{p.product_id}={count}")
                else:
                    # I3: 게이트 실패 시 다품목/재solve 확정 금지 → 증분 결과 유지
                    notes.append(f"freezer_close_gate_failed:zone{zone}:keep_incremental")
            elif len(kinds) > 1:
                notes.append(f"freezer_close_multi_kind:zone{zone}:keep_incremental")


def interim_summary(
    session_id: str,
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
) -> InterimSummary:
    """잠정 집계 (I10) — 1층(동존)만 반영. 결제 전달 금지 타입."""
    baskets, _ = pass_same_zone(_ok_events(events), profiles)
    return InterimSummary(
        session_id, tuple(baskets[z].to_zone(z) for z in sorted(baskets))
    )
