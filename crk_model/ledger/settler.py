"""close-time 단일 정산기 v2 — 셀 net 우선 2단계 (README "추론 설계 v2").

v1의 4계층(동존 즉시 → net-delta 교정 → 교차존 → freezer 재solve)을 2단계로
대체한다. 전제 3(한 로드셀에 한 상품 종류)에 의해 셀 net이 곧 진실이다:

① 셀 net 우선 — 세션 전체 셀 순변화 net_c를 정체성 p의 n×w로 재해석.
   게이트 통과 → net으로 개수 확정 (트리거 증분 덮어씀 — "꺼냈다 되돌림",
   트리거 오판, pending 이월분이 전부 여기서 자기 교정). 게이트 실패 →
   증분(트리거별 판정) 유지 (fail-closed, v1 keep_incremental 승계).
② 오배치 반품 교정 — 셀 net의 설명 안 되는 +잔차를 타 셀 장바구니 상품
   무게와 최근접 매칭해 감산 (v1 cross_zone_return의 셀 단위 정밀화).
   완전 동률은 감산 보류 + note, 미매칭은 기록만 (감산 없음 — 과소청구 방향).

보존 계약 (실기 검증):
- finalize 멱등 — 같은 session_id는 항상 같은 결과 객체.
- 반품 정산이 count를 음수로 만들 수 없음 (환수 > 청구 금지).
- 에러 trigger 존재 시 무성 확정 금지 — ErrorSessionPolicy로만 처리.
- 모든 보정은 notes에 사유 코드로 기록.
- 개수 모호(이웃 정수도 게이트 이내) 시 작은 n 채택 (보수 청구).
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence

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
from crk_model.ledger.cells import CellBeliefStore
from crk_model.ledger.events import EventLog, TriggerEvent

_LEGACY_CHANNEL = -1  # cells 없는 이벤트(구 저널/에러 경로)의 존 직접 귀속


def _ok_events(events: Iterable[TriggerEvent]) -> list[TriggerEvent]:
    return [
        e
        for e in events
        if e.status == "ok" and e.judgment.status is not JudgmentStatus.ERROR
    ]


def _profile(
    profiles: Mapping[int, SensorProfile],
    zone: int,
    default: SensorProfile = REFRIGERATOR,
) -> SensorProfile:
    # zone 미지정 시 폴백 — 판정(pipeline)과 정산의 tolerance/count gate 단일
    # 소스 원칙: 호출측(ModelService)이 기기 단위 기본 프로파일을 주입한다.
    return profiles.get(zone, default)


def _notes_for_zone(notes: Sequence[str], zone: int) -> tuple[str, ...]:
    """OPS 로그용 매칭: note가 `zone{N}:` 또는 `zone{N}->` 패턴을 포함하면
    해당 zone에 귀속. `zone{N}` 뒤 경계 구분자까지 확인해 zone=1이 zone=11에
    오매칭되지 않게 한다."""
    pattern = re.compile(rf"zone{zone}(?::|->)")
    return tuple(n for n in notes if pattern.search(n))


class _CellLedger:
    """셀 하나의 세션 집계 — net, 증분(트리거별 확정), 정체성 증거."""

    def __init__(self, zone: int, channel: int):
        self.zone = zone
        self.channel = channel
        self.net = 0.0
        self.incremental: dict[str, int] = {}  # pid -> count (제거 −반품, 확정분만)
        self.identity_votes: dict[str, int] = {}  # 이벤트에 기록된 정체성 증거
        self.unstable = False

    def add(self, cell) -> None:
        self.net += cell.delta_weight
        if not cell.stabilized:
            self.unstable = True
        if cell.product_id:
            self.identity_votes[cell.product_id] = (
                self.identity_votes.get(cell.product_id, 0) + 1
            )
        if cell.resolved and cell.product_id:
            sign = 1 if cell.delta_weight < 0 else -1
            self.incremental[cell.product_id] = (
                self.incremental.get(cell.product_id, 0) + sign * cell.count
            )

    def majority_identity(self) -> str | None:
        if not self.identity_votes:
            return None
        return max(self.identity_votes.items(), key=lambda kv: kv[1])[0]

    def incremental_counts(self) -> dict[str, int]:
        return {pid: c for pid, c in self.incremental.items() if c > 0}


def _count_candidates(target: float, w: float, gate: float) -> list[int]:
    """target ≈ n×w를 게이트 이내로 설명하는 n 후보 (이웃 정수 포함)."""
    if w <= 0 or target <= 0:
        return []
    n = round(target / w)
    return [k for k in (n - 1, n, n + 1) if k >= 1 and abs(target - k * w) <= gate]


class CloseSettler:
    def __init__(
        self,
        error_policy: ErrorSessionPolicy = ErrorSessionPolicy.BLOCK_PAYMENT,
        default_profile: SensorProfile = REFRIGERATOR,
        beliefs: CellBeliefStore | None = None,
        catalog: Callable[[], Sequence[ActiveProduct]] | None = None,
        # catalog: close 시점 상품 정보 소스 (ModelService가 스냅샷 getter 주입).
        # 미주입 시 이벤트에 실린 judgment.products에서만 상품 정보를 얻는다.
    ):
        self.error_policy = error_policy
        self.default_profile = default_profile
        self.beliefs = beliefs or CellBeliefStore()
        self._catalog = catalog
        self._finalized: dict[str, FinalizedSettlement] = {}

    def settle(
        self,
        session_id: str,
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        event_log: EventLog | None = None,
    ) -> FinalizedSettlement:
        if session_id in self._finalized:
            return self._finalized[session_id]  # 멱등

        notes: list[str] = []
        ok = _ok_events(events)
        error_zones = sorted(
            {
                e.zone
                for e in events
                if e.status != "ok" or e.judgment.status is JudgmentStatus.ERROR
            }
        )
        products = self._product_catalog(ok)

        ledgers, legacy = self._collect(ok)
        baskets: dict[int, dict[str, int]] = defaultdict(dict)  # zone -> pid -> count
        unmatched_returns: list[tuple[int, int, float]] = []  # (zone, ch, +g)

        # ---- ① 셀 net 우선 ----
        for ledger in ledgers.values():
            self._resolve_cell(ledger, profiles, products, baskets, unmatched_returns, notes)
        # cells 없는 구형 이벤트: 존 직접 증분 (net 정보 없음 — 교정 불가, 기록만)
        for zone, counts in legacy.items():
            for pid, c in counts.items():
                if c > 0:
                    baskets[zone][pid] = baskets[zone].get(pid, 0) + c

        # ---- ② 오배치 반품 교정 ----
        self._cross_cell_returns(unmatched_returns, baskets, profiles, products, notes)

        zones = self._zone_baskets(events, baskets, products, notes)

        blocked = False
        block_reason = ""
        if error_zones:
            if self.error_policy is ErrorSessionPolicy.BLOCK_PAYMENT:
                blocked = True  # 무성 확정 금지 (fail-closed 기본)
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

    def prune(self, keep_session_ids: set[str]) -> None:
        """무한 성장 방지: 멱등 캐시를 최근 세션만 남기고 정리 (v1 승계)."""
        for sid in [s for s in self._finalized if s not in keep_session_ids]:
            del self._finalized[sid]

    # ---- 수집 ----
    def _product_catalog(self, events: Sequence[TriggerEvent]) -> dict[str, ActiveProduct]:
        products: dict[str, ActiveProduct] = {}
        if self._catalog is not None:
            for p in self._catalog():
                products[p.product_id] = p
        for e in events:  # 이벤트에 실린 상품 정보로 보강 (스냅샷 부재 대비)
            for pc in e.judgment.products:
                products.setdefault(pc.product.product_id, pc.product)
        return products

    @staticmethod
    def _collect(
        events: Sequence[TriggerEvent],
    ) -> tuple[dict[tuple[int, int], _CellLedger], dict[int, dict[str, int]]]:
        ledgers: dict[tuple[int, int], _CellLedger] = {}
        legacy: dict[int, dict[str, int]] = defaultdict(dict)
        for e in sorted(events, key=lambda e: e.ts):
            if e.cells:
                for c in e.cells:
                    key = (e.zone, c.channel)
                    if key not in ledgers:
                        ledgers[key] = _CellLedger(e.zone, c.channel)
                    ledgers[key].add(c)
            elif e.delta_weight < 0:
                for pc in e.judgment.products:
                    legacy[e.zone][pc.product.product_id] = (
                        legacy[e.zone].get(pc.product.product_id, 0) + pc.count
                    )
        return ledgers, legacy

    # ---- ① 셀 net ----
    def _resolve_cell(
        self,
        ledger: _CellLedger,
        profiles: Mapping[int, SensorProfile],
        products: Mapping[str, ActiveProduct],
        baskets: dict[int, dict[str, int]],
        unmatched_returns: list[tuple[int, int, float]],
        notes: list[str],
    ) -> None:
        zone, ch = ledger.zone, ledger.channel
        prof = _profile(profiles, zone, self.default_profile)
        gate = prof.count_gate
        net = ledger.net
        incremental = ledger.incremental_counts()
        tag = f"zone{zone}:ch{ch}"

        def keep_incremental(reason: str) -> None:
            for pid, c in incremental.items():
                baskets[zone][pid] = baskets[zone].get(pid, 0) + c
            notes.append(f"{reason}:{tag}:keep_incremental")

        if abs(net) <= gate:
            # 순변화 없음 (전량 반품 포함) → 과금 없음
            if incremental:
                notes.append(f"cell_net_clear:{tag}")
            return

        identity = self.beliefs.identity(zone, ch) or ledger.majority_identity()
        p = products.get(identity) if identity else None

        if net < 0:
            if p is None:
                # 미지 셀 제거: 무게 단독 유일 매칭 (냉장만 — 냉동 억제 원리)
                if prof.weight_is_discriminative:
                    fits = [
                        (q, ns)
                        for q in products.values()
                        if (ns := _count_candidates(-net, q.unit_weight, gate))
                        and min(ns) <= q.stock_qty
                    ]
                    if len(fits) == 1:
                        q, ns = fits[0]
                        n = min(ns)
                        baskets[zone][q.product_id] = (
                            baskets[zone].get(q.product_id, 0) + n
                        )
                        notes.append(
                            f"cell_net_weight_unique:{tag}:{q.product_id}={n}"
                        )
                        return
                # fail-closed: 추측 과금 금지 — 매출 누락 방향으로 기록만
                if incremental:
                    keep_incremental("cell_identity_unknown")
                else:
                    notes.append(f"cell_identity_unknown:{tag}:no_charge:{net:+.1f}g")
                return
            ns = _count_candidates(-net, p.unit_weight, gate)
            if not ns or min(ns) > p.stock_qty:
                keep_incremental("cell_net_gate_failed")
                return
            n = min(ns)  # 모호 시 작은 n (보수 청구)
            baskets[zone][p.product_id] = baskets[zone].get(p.product_id, 0) + n
            if len(ns) > 1:
                notes.append(f"count_ambiguous_floor:{tag}:{p.product_id}={n}")
            notes.append(f"cell_net_resolve:{tag}:{p.product_id}={n}")
            return

        # net > gate: 셀이 무거워짐 — 자기 상품 반납이면 과금 없음(count ≥ 0),
        # 설명 안 되면 오배치 반품 후보로 ② 단계에 넘긴다.
        if p is not None and _count_candidates(net, p.unit_weight, gate):
            notes.append(f"surplus_return:{tag}")
            return
        unmatched_returns.append((zone, ch, net))

    # ---- ② 오배치 반품 ----
    def _cross_cell_returns(
        self,
        unmatched: list[tuple[int, int, float]],
        baskets: dict[int, dict[str, int]],
        profiles: Mapping[int, SensorProfile],
        products: Mapping[str, ActiveProduct],
        notes: list[str],
    ) -> None:
        for zone, ch, ret in unmatched:
            remaining = ret
            deducted = True
            while deducted:
                deducted = False
                candidates: list[tuple[float, int, ActiveProduct]] = []
                for bz, counts in baskets.items():
                    tol = _profile(profiles, bz, self.default_profile).count_gate
                    for pid, c in counts.items():
                        if c <= 0 or pid not in products:
                            continue
                        q = products[pid]
                        dist = abs(remaining - q.unit_weight)
                        if dist <= tol:
                            candidates.append((dist, bz, q))
                if not candidates:
                    break
                candidates.sort(key=lambda t: t[0])
                best = candidates[0]
                ties = [
                    c
                    for c in candidates[1:]
                    if c[0] == best[0] and c[2].product_id != best[2].product_id
                ]
                if ties:
                    # 완전 동률 — 감산 보류 (과금 오류 방지 우선, README 규칙)
                    notes.append(
                        f"cross_cell_return_ambiguous:zone{zone}:ch{ch}:{remaining:+.1f}g"
                    )
                    return
                _dist, bz, q = best
                baskets[bz][q.product_id] -= 1  # count ≥ 0 (감산 전 c > 0 확인)
                notes.append(
                    f"cross_cell_return:zone{zone}:ch{ch}->zone{bz}:{q.product_id}-1"
                )
                remaining -= q.unit_weight
                deducted = remaining > _profile(
                    profiles, zone, self.default_profile
                ).count_gate
            if remaining > _profile(profiles, zone, self.default_profile).count_gate:
                notes.append(f"unmatched_return:zone{zone}:ch{ch}:{remaining:+.1f}g")

    # ---- 존 집계 ----
    def _zone_baskets(
        self,
        events: Sequence[TriggerEvent],
        baskets: Mapping[int, Mapping[str, int]],
        products: Mapping[str, ActiveProduct],
        notes: Sequence[str],
    ) -> list[ZoneBasket]:
        all_zones = sorted(set(baskets) | {e.zone for e in events})
        zones = []
        for zone in all_zones:
            zone_events = [e for e in events if e.zone == zone]
            weight_delta = sum(e.delta_weight for e in zone_events)
            pcs = tuple(
                ProductCount(products[pid], c)
                for pid, c in sorted(baskets.get(zone, {}).items())
                if c > 0 and pid in products
            )
            for pc in pcs:
                assert pc.count >= 0  # 구조상 보장, 방어적 확인
            zones.append(
                ZoneBasket(
                    zone,
                    pcs,
                    weight_delta,
                    len(zone_events),
                    _notes_for_zone(notes, zone),
                )
            )
        return zones


def interim_summary(
    session_id: str,
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    default_profile: SensorProfile = REFRIGERATOR,
) -> InterimSummary:
    """잠정 집계 — 셀별 확정 증분만 반영 (결제 전달 금지 타입).

    pending 셀·net 재해석은 반영하지 않는다 — 확정은 close 정산기 한 곳에서.
    """
    del profiles, default_profile  # v2: 증분 집계는 프로파일 불필요 (시그니처 유지)
    zone_counts: dict[int, dict[str, int]] = defaultdict(dict)
    catalog: dict[str, ActiveProduct] = {}
    for e in sorted(_ok_events(events), key=lambda e: e.ts):
        for pc in e.judgment.products:
            catalog.setdefault(pc.product.product_id, pc.product)
        if e.cells:
            for c in e.cells:
                if c.resolved and c.product_id:
                    sign = 1 if c.delta_weight < 0 else -1
                    zone_counts[e.zone][c.product_id] = (
                        zone_counts[e.zone].get(c.product_id, 0) + sign * c.count
                    )
        elif e.delta_weight < 0:
            for pc in e.judgment.products:
                zone_counts[e.zone][pc.product.product_id] = (
                    zone_counts[e.zone].get(pc.product.product_id, 0) + pc.count
                )
    zones = []
    for zone in sorted(zone_counts):
        pcs = tuple(
            ProductCount(catalog[pid], c)
            for pid, c in sorted(zone_counts[zone].items())
            if c > 0 and pid in catalog
        )
        zones.append(ZoneBasket(zone, pcs))
    return InterimSummary(session_id, tuple(zones))
