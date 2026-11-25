"""CLOSE carried-over 억제 — 앞서 과금된 상품을 손에 든 채 다른 층을 열었을 때
그 상품이 무게가 맞지 않는데도 다시 과금되는 것을 막는다 (2026-11-19 추가).

무게를 근거로 청구를 빼는 규칙이라 로드셀 무게 신뢰를 전제로 한다
(2026-11-25 무게 안정화 판단 — yoona.md #freezer-20261125).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import ActiveProduct, JudgmentResult, JudgmentStatus
from crk_model.ledger.cross_zone import CARRIED_IN_MIN_HEAD
from crk_model.ledger.events import TriggerEvent


def _weight_decisively_inconsistent(
    target: float, product: ActiveProduct, gate: float, slack: float,
) -> bool:
    """어떤 개수 n(≥1)으로도 |target − n·w| ≤ 2·gate_n(n) 근접 밴드에 들지
    못하면 True. gate_n(n) = gate + slack·(n−1) — I3 n-스케일 게이트와 같다."""
    w = product.unit_weight
    if w <= 0:
        return False
    max_n = max(1, min(product.stock_qty, int(max(target, 0.0) // w) + 2))
    return all(
        abs(target - n * w) > 2.0 * (gate + slack * (n - 1))
        for n in range(1, max_n + 1)
    )


def suppress_carried_over_partials(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default_profile: SensorProfile, notes: list[str], count_unit_slack: float = 5.0,
) -> list[TriggerEvent]:
    """앞서 과금된 상품을 손에 든 채 다른 층을 열었을 때 그 상품이 다시
    과금되는 것을 막는다 (2026-11-19 ses-1-1795061395 zone2: zone1 97을 든
    채 zone2를 열어 −205g에 97(370g)이 identity partial 과금).

    세 조건이 모두 맞을 때만 해당 클래스 C를 비-COMPLETE 이벤트에서 뺀다.
    - 이 이벤트보다 앞선 이벤트가 C를 이미 과금했다 (손에 든 상품의 출처)
    - 이 이벤트의 전체 비전 후보 C가 carried-in 지문(head_votes ≥ 5)을 가진다
    - 이벤트 무게가 C의 어떤 개수와도 근접 밴드(2·gate_n) 밖이다
    무게가 C와 맞거나 계측 이전 기록(head_votes=0)이면 아무것도 바꾸지 않는다.
    빼낸 자리에 다른 상품을 채우지 않는다 (미청구 방향, fail-closed)."""
    out = list(events)
    for i, event in enumerate(out):
        profile = profiles.get(event.zone, default_profile)
        if (profile.weight_is_discriminative or event.status != "ok"
                or event.delta_weight >= 0
                or event.judgment.status is not JudgmentStatus.PARTIAL
                or not event.judgment.products):
            continue
        held = {c.class_id for c in event.vision_candidates
                if c.head_votes >= CARRIED_IN_MIN_HEAD}
        if not held:
            continue
        billed_before = {
            pc.product.class_id
            for other in out
            if other is not event and other.status == "ok" and other.ts < event.ts
            and other.judgment.status in (JudgmentStatus.COMPLETE, JudgmentStatus.PARTIAL)
            for pc in other.judgment.products
        }
        gate = profile.count_gate
        dropped = []
        for pc in event.judgment.products:
            cid = pc.product.class_id
            if cid not in held or cid not in billed_before:
                continue
            others = sum(o.product.unit_weight * o.count
                         for o in event.judgment.products if o is not pc)
            target = -event.delta_weight - others
            if _weight_decisively_inconsistent(target, pc.product, gate, count_unit_slack):
                dropped.append(pc)
        if not dropped:
            continue
        remaining = tuple(pc for pc in event.judgment.products if pc not in dropped)
        notes.append(
            f"zone{event.zone}:freezer_carried_over_suppressed:"
            + ",".join(pc.product.product_id for pc in dropped)
            + f":delta={event.delta_weight:.1f}g:trigger_ts={event.ts:.3f}"
        )
        if remaining:
            judgment = replace(event.judgment, products=remaining,
                               reason=event.judgment.reason + "+freezer_carried_over_pruned")
        else:
            judgment = JudgmentResult(JudgmentStatus.NO_DETECTION, confidence=0.0,
                                      reason="freezer_carried_over_suppressed",
                                      strategy=event.judgment.strategy)
        out[i] = replace(event, judgment=judgment)
    return out
