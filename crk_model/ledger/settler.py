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
from crk_model.ledger.cross_zone import (
    CrossZonePenaltyConfig,
    apply_cross_zone_penalty,
)
from crk_model.ledger.events import EventLog, TriggerEvent
from crk_model.ledger.ghost_ledger import GhostLedgerConfig, apply_ghost_demotion


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

    def to_zone(
        self,
        zone: int,
        weight_delta: float = 0.0,
        trigger_count: int = 0,
        notes: tuple[str, ...] = (),
    ) -> ZoneBasket:
        return ZoneBasket(
            zone,
            tuple(
                ProductCount(p, c)
                for p, c in sorted(self.items(), key=lambda t: t[0].product_id)
            ),
            weight_delta,
            trigger_count,
            notes,
        )


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
    # zone 미지정 시 폴백 — MODEL__MACHINE__CABINET_TYPE 이식: 판정(pipeline)과
    # 정산(settle)의 tolerance/count gate 단일 소스 원칙을 지키기 위해 호출측
    # (ModelService → CloseSettler/MultiZoneGateway)이 기기 단위 기본 프로파일을
    # 주입한다. 기본값 REFRIGERATOR는 기존 동작과의 하위호환.
    return profiles.get(zone, default)


def _notes_for_zone(notes: Sequence[str], zone: int) -> tuple[str, ...]:
    """OPS 로그용 근사 매칭: note 문자열이 `zone{N}:` 또는 `zone{N}->`로 시작하는
    패턴에 걸리는 것만 해당 zone에 귀속시킨다. `zone{N}` 뒤에 경계 구분자
    (`:` 또는 `->`)까지 확인해 zone=1이 zone=11에 오매칭되지 않게 한다.
    cross_zone_return(`zone{origin}->zone{dest}:...`)은 origin(반품 시작 zone)
    표기가 항상 문자열 맨 앞에 오므로, 이 매칭 방식은 origin 쪽에만 귀속시킨다
    (도착 zone 쪽 완전 매칭은 하지 않음 — 근사 처리, 최종 보고에 근거 명시)."""
    pattern = re.compile(rf"zone{zone}(?::|->)")
    return tuple(n for n in notes if pattern.search(n))


def pass_same_zone(
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    default_profile: SensorProfile = REFRIGERATOR,
) -> tuple[dict[int, _Basket], list[tuple[int, float]]]:
    """1층: 동존 즉시 복구 — removal은 판정 품목 축적, return은 무게 매칭 차감."""
    baskets: dict[int, _Basket] = defaultdict(_Basket)
    unmatched: list[tuple[int, float]] = []
    for e in sorted(events, key=lambda e: e.ts):
        b = baskets[e.zone]
        tol = _profile(profiles, e.zone, default_profile).tolerance_grams
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
    def __init__(
        self,
        error_policy: ErrorSessionPolicy = ErrorSessionPolicy.BLOCK_PAYMENT,
        default_profile: SensorProfile = REFRIGERATOR,
        *,
        cross_zone: CrossZonePenaltyConfig | None = None,
        ghost: GhostLedgerConfig | None = None,
        # 세션 고스트 원장 (0723 이슈 #17 P1, ledger/ghost_ledger.py) —
        # 기본 shadow (검출·시뮬레이션 notes만). active 승격은 라벨 실측 후.
        # 교차존 재판정(④~⑥)은 allowlist가 필요하다 — ModelService가 세션
        # 스냅샷 provider(ActiveProductStore)를 주입한다. None이면 페널티
        # 패스 비활성 (기존 테스트/직접 생성 하위호환).
        active_products_provider: Callable[[], Sequence[ActiveProduct]] | None = None,
        count_unit_slack: float = 5.0,
        # 냉동 close 재solve의 개수당 게이트 가산(g) — 판정층 gate_n과 동일
        # 원칙 (설계 3a, docs/0722_issue16_arbitration_design.md). I3 게이트를
        # 쓰는 두 지점(판정·정산)이 같은 산식을 유지해야 한다. 0 = flat 게이트.
        vision_combo: bool = True,
        # 0723 이슈 #17: 단일 종 ×N 스냅(N≥2)·게이트 실패 시, 존의 자격 표를
        # 받은 2종 조합이 게이트 안에서 delta를 설명하면 조합을 우선한다
        # ("무게=거부권, 선택=vision" — 0722 중재 설계와 같은 원칙).
        # MODEL__CLOSE__VISION_COMBO=0으로 비활성.
    ):
        self.error_policy = error_policy
        # zone이 profiles dict에 없을 때의 폴백 프로파일 (cabinet_type 이식) —
        # ModelService가 기기 단위 기본 프로파일을 주입한다. 기본값은 기존
        # 동작(REFRIGERATOR)과 동일한 하위호환.
        self.default_profile = default_profile
        self.cross_zone = cross_zone or CrossZonePenaltyConfig()
        self.ghost = ghost or GhostLedgerConfig()
        self._products_provider = active_products_provider
        self.count_unit_slack = count_unit_slack
        self.vision_combo = vision_combo
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
        # 0723 세션 고스트 원장 — cross_zone보다 먼저: active에서 유령 후보를
        # 강등해 두면 cross_zone 재판정의 채택 후보에서도 밀려난다 (ses-11).
        if self.ghost.mode != "off" and self._products_provider is not None:
            ok = apply_ghost_demotion(
                ok,
                profiles,
                tuple(self._products_provider()),
                self.ghost,
                notes,
                self.default_profile,
            )
        # 0711 교차존 비전 오염 페널티 (CLOSE 2차 패스) — 워터마크(F8) 덕분에
        # 이 시점에는 늦게 도착한 연장 병합 이벤트까지 전부 EventLog에 있다.
        # 잠정 판정은 그대로 두고 확정 입력(ok 이벤트의 judgment)만 보정한다.
        if self.cross_zone.enabled and self._products_provider is not None:
            ok = apply_cross_zone_penalty(
                ok,
                profiles,
                tuple(self._products_provider()),
                self.cross_zone,
                notes,
                self.default_profile,
            )
        error_zones = sorted(
            {
                e.zone
                for e in events
                if e.status != "ok" or e.judgment.status is JudgmentStatus.ERROR
            }
        )

        baskets, unmatched = pass_same_zone(ok, profiles, self.default_profile)
        self._pass_net_delta(baskets, ok, profiles, unmatched, notes, self.default_profile)
        self._pass_cross_zone(baskets, unmatched, profiles, notes, self.default_profile)
        self._freezer_resolve(baskets, ok, profiles, notes, self.default_profile)

        # OPS 로그용: basket이 비어 있어도(예: unmatched_return만 있던 zone)
        # 이벤트가 발생했던 zone은 요약에 나와야 한다 — events는 ok+error 전체
        # 원본 파라미터 기준으로 zone 집합을 넓힌다 (error_zones 필터링은 이후).
        all_zones = sorted(set(baskets) | {e.zone for e in events})

        zones = []
        for zone in all_zones:
            # trigger_count는 ok+error 포함 전체 이벤트 수(그 zone에 실제 발생한
            # 트리거 횟수)로 센다 — error_zones는 이미 별도로 추적되므로, zone별
            # trigger_count는 "그 zone에 도달한 전체 이벤트 수"를 나타내는 것이
            # 원본(다이어그램/원본 로그 예시)의 의미에 더 가깝다고 판단.
            zone_events = [e for e in events if e.zone == zone]
            weight_delta = sum(e.delta_weight for e in zone_events)
            trigger_count = len(zone_events)
            zone_notes = _notes_for_zone(notes, zone)
            basket = baskets.get(zone)
            zb = (
                basket.to_zone(zone, weight_delta, trigger_count, zone_notes)
                if basket is not None
                else _Basket().to_zone(zone, weight_delta, trigger_count, zone_notes)
            )
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

    def prune(self, keep_session_ids: set[str]) -> None:
        """무한 성장 방지 (24h+ soak): _finalized 멱등 캐시(I11)를 최근
        keep_session_ids만 남기고 정리한다. 호출측이 현재+직전 K개 세션을
        넘겨야 하며, 여기서는 교집합만 수행한다 (현재 활성 세션은 아직
        _finalized에 없을 수 있으므로 삭제 대상이 아니다)."""
        for sid in [s for s in self._finalized if s not in keep_session_ids]:
            del self._finalized[sid]

    @staticmethod
    def _pass_net_delta(
        baskets: dict[int, _Basket],
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        unmatched: list[tuple[int, float]],
        notes: list[str],
        default_profile: SensorProfile = REFRIGERATOR,
    ) -> None:
        """2층: 세션 net delta와 어긋난 과잉 청구 교정."""
        for zone, b in baskets.items():
            tol = _profile(profiles, zone, default_profile).tolerance_grams
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
        default_profile: SensorProfile = REFRIGERATOR,
    ) -> None:
        """3층: 미매칭 반품을 다른 존 장바구니와 매칭 (존 착오 반납)."""
        for zone, wt in unmatched:
            hit = False
            for oz, b in baskets.items():
                if oz == zone:
                    continue
                tol = _profile(profiles, oz, default_profile).tolerance_grams
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

    def _freezer_resolve(
        self,
        baskets: dict[int, _Basket],
        events: Sequence[TriggerEvent],
        profiles: Mapping[int, SensorProfile],
        notes: list[str],
        default_profile: SensorProfile = REFRIGERATOR,
    ) -> None:
        """4층: freezer 부호있는 net basket 재solve (불안정 close 대비, QA Q6)."""
        for zone, b in list(baskets.items()):
            prof = _profile(profiles, zone, default_profile)
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
                p, c_inc = kinds[0]
                count = round(-net / p.unit_weight) if p.unit_weight > 0 else 0
                # I3 게이트 — n-스케일 (설계 3a): DB 편차·오염은 개수 비례 누적
                gate_n = gate + self.count_unit_slack * max(0, count - 1)
                snap_ok = (
                    1 <= count <= p.stock_qty  # I12
                    and abs(-net - count * p.unit_weight) <= gate_n  # I3
                )
                # 0723 이슈 #17 (7회 반복 실사고: 3+44 취출 → 44×4 스냅):
                # 무게 잔차만으로는 게이트 안 동률인 두 가설(단일 종 ×N vs
                # 2종 조합)을 못 가른다 — 그때의 선택권은 vision("무게=거부권,
                # 선택=vision"). 스냅이 트리거 증분 개수를 **부풀리는** 경우
                # (판정은 c_inc개만 봤는데 무게가 count개를 요구 — 의심 신호)
                # 와 게이트 실패에 한해, 존의 자격 표를 받은 2종 조합이 게이트
                # 안에서 net을 설명하면 조합을 우선. 판정·무게가 일치하는
                # 정상 스냅(count ≤ 증분)은 조합 탐색 자체를 안 한다.
                combo = (
                    self._vision_combo(zone, -net, gate, events, {p.class_id: c_inc})
                    if self.vision_combo
                    and ((count >= 2 and count > c_inc) or not snap_ok)
                    else None
                )
                if combo is not None:
                    for pid in list(b.counts):
                        b.set_count(pid, 0)
                    for prod, n in combo:
                        b.add(prod, n)
                    notes.append(
                        f"freezer_close_resolve_combo:zone{zone}:"
                        + ",".join(f"{prod.product_id}={n}" for prod, n in combo)
                    )
                elif snap_ok:
                    b.set_count(p.product_id, count)
                    notes.append(f"freezer_close_resolve:zone{zone}:{p.product_id}={count}")
                else:
                    # I3: 게이트 실패 시 다품목/재solve 확정 금지 → 증분 결과 유지
                    notes.append(f"freezer_close_gate_failed:zone{zone}:keep_incremental")
            elif len(kinds) > 1:
                notes.append(f"freezer_close_multi_kind:zone{zone}:keep_incremental")

    # 조합의 각 클래스가 요구하는 최소 자격 표 수 — 변위 몰수를 통과한 표가
    # 이만큼 있어야 "vision이 그 클래스를 봤다"로 친다 (유령 스파이크 차단).
    _COMBO_VOTE_FLOOR = 3

    def _vision_combo(
        self,
        zone: int,
        target: float,
        gate: float,
        events: Sequence[TriggerEvent],
        incremental: Mapping[int, int],
    ) -> tuple[tuple[ActiveProduct, int], ...] | None:
        """단일 종 ×N 스냅의 비전 교차 검증 대안: 이 존 removal 이벤트들에서
        자격 표(≥ _COMBO_VOTE_FLOOR)를 받은 서로 다른 2종의 (n_A, n_B) 조합 중
        |target − (n_A·w_A + n_B·w_B)| ≤ gate_n(n_A+n_B)인 것을 찾는다.

        선택 기준: 커버한 표 합 최대 → 트리거 증분(incremental: class_id→개수)
        과의 편차 최소 → 잔차 최소 → 총 개수 최소. 잔차를 1순위로 두지 않는
        이유가 이 기제의 존재 이유다 — 게이트 안이면 무게는 이미 거부권을
        행사하지 않은 것이고, 3+44(잔차 8.5) vs 44×4(잔차 0)의 선택은 c3의
        실존 표·판정 증거가 해야 한다. 증분 편차 항은 같은 2종 안에서 개수
        배분이 갈릴 때(27×3+30×1 vs 27×1+30×4) 트리거 판정이 실제로 본
        개수를 존중한다. I12(재고 상한)·I3(게이트) 준수."""
        if self._products_provider is None:
            return None
        votes: dict[int, int] = {}
        for e in events:
            if e.zone != zone or e.delta_weight >= 0 or e.status != "ok":
                continue
            for c in e.vision_candidates:
                if c.class_id > 0 and c.vote_count >= self._COMBO_VOTE_FLOOR:
                    votes[c.class_id] = max(votes.get(c.class_id, 0), c.vote_count)
        if len(votes) < 2:
            return None
        products = [
            p
            for p in self._products_provider()
            if p.class_id in votes and p.unit_weight > 0 and p.stock_qty > 0
        ]
        best: tuple[tuple, tuple[tuple[ActiveProduct, int], ...]] | None = None
        for i, pa in enumerate(products):
            for pb in products[i + 1:]:
                if pa.class_id == pb.class_id:
                    continue
                for na in range(1, min(pa.stock_qty, 6) + 1):
                    for nb in range(1, min(pb.stock_qty, 6) + 1):
                        total = na + nb
                        residual = abs(
                            target - (na * pa.unit_weight + nb * pb.unit_weight)
                        )
                        if residual > gate + self.count_unit_slack * (total - 1):
                            continue  # I3
                        combo_counts = {pa.class_id: na, pb.class_id: nb}
                        deviation = sum(
                            abs(combo_counts.get(cid, 0) - incremental.get(cid, 0))
                            for cid in set(combo_counts) | set(incremental)
                        )
                        score = (
                            votes[pa.class_id] + votes[pb.class_id],
                            -deviation,
                            -residual,
                            -total,
                        )
                        if best is None or score > best[0]:
                            best = (score, ((pa, na), (pb, nb)))
        return best[1] if best else None


def interim_summary(
    session_id: str,
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    default_profile: SensorProfile = REFRIGERATOR,
) -> InterimSummary:
    """잠정 집계 (I10) — 1층(동존)만 반영. 결제 전달 금지 타입."""
    baskets, _ = pass_same_zone(_ok_events(events), profiles, default_profile)
    return InterimSummary(
        session_id, tuple(baskets[z].to_zone(z) for z in sorted(baskets))
    )
