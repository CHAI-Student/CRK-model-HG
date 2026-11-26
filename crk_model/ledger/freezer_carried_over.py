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
    floor: float | None = None,
) -> list[TriggerEvent]:
    """앞서 과금된 상품을 손에 든 채 다른 층을 열었을 때 그 상품이 다시
    과금되는 것을 막는다 (2026-11-19 ses-1-1795061395 zone2: zone1 97을 든
    채 zone2를 열어 −205g에 97(370g)이 identity partial 과금).

    세 조건이 모두 맞을 때만 해당 클래스 C를 비-COMPLETE 이벤트에서 뺀다.
    - 이 이벤트보다 앞선 이벤트가 C를 이미 과금했다 (손에 든 상품의 출처)
    - C가 다른 층에서 온 상품이라는 지문이 있다. 둘 중 하나다.
      - 이 이벤트의 전체 비전 후보 C가 carried-in 지문(head_votes ≥ 5)을 가진다
      - (2026-11-26) 앞서 C를 과금한 **다른 층** 이벤트의 무게 하강이 이 이벤트가 열린
        동안(ts ~ 마지막 변화 시각) 끝났다. 다른 층에서 C를 꺼낸 순간이 공유 카메라로
        이 층 클립에 꺼내는 방향으로 찍힌 것이라 head_votes가 0이다 (ses-3-1795673059
        2층: 1층 2(750g) 하강 66.3~67.2초, 2층 트리거 65.6초 시작 → 2층 −210g에 2 PARTIAL).
        층끼리 같은 로드셀 시계라 영상-무게 시각 맞춤이 필요 없다. 이 층 하강이 상품
        크기(floor) 미만이거나, 상품 크기 이상 올라간 구간이 있거나(이 층 취출+반납 혼합),
        출처 층 청구가 그 층 무게와 맞지 않으면 쓰지 않는다(`_lifted_during`). floor가
        없으면 이 지문은 쓰지 않는다.
    - 이벤트 무게가 C의 어떤 개수와도 근접 밴드(2·gate_n) 밖이다
    무게가 C와 맞거나 지문이 없으면 아무것도 바꾸지 않는다.
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
        earlier = [
            other for other in out
            if other is not event and other.status == "ok" and other.ts < event.ts
            and other.judgment.status in (JudgmentStatus.COMPLETE, JudgmentStatus.PARTIAL)
        ]
        lifted = _lifted_during(event, earlier, floor, profile.count_gate, count_unit_slack)
        if not held and not lifted:
            continue
        billed_before = {pc.product.class_id for other in earlier for pc in other.judgment.products}
        gate = profile.count_gate
        dropped = []
        for pc in event.judgment.products:
            cid = pc.product.class_id
            if (cid not in held and cid not in lifted) or cid not in billed_before:
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
            # head_votes 지문 없이 다른 층 하강 시각으로만 판단한 경우를 구분한다.
            + ("" if all(pc.product.class_id in held for pc in dropped)
               else ":lifted_during_trigger")
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


def _lifted_during(
    event: TriggerEvent, earlier: Sequence[TriggerEvent], floor: float | None,
    gate: float, slack: float,
) -> set[int]:
    """다른 층에서 이 이벤트가 열린 동안 무게가 빠진(꺼낸) 앞선 과금 이벤트의 상품 클래스.

    빈 집합인 경우:
    - 이 이벤트 하강이 상품 크기(floor) 미만 — 상품 취출이 아닌 접촉 잔여는 다른 CLOSE
      규칙(지문 중복 PARTIAL 제거·미청구 review)이 다룬다.
    - 이 이벤트에 상품 크기 이상 올라간 구간이 있다 — 이 층에서 C를 꺼내며 다른 상품을
      넣은 혼합이면 순변화가 C와 맞지 않아도 C 취출이 실제일 수 있다.
    클래스 C마다 두 조건을 더 본다.
    - 이 층 하강이 C 1개보다 상품 크기 이상 가볍다 — 이 층에서는 C 1개가 빠질 수 없었다.
      두 층에서 C를 하나씩 꺼낸 진짜 취출은 무게가 C에 가깝거나(일부 걸침으로 덜 읽혀도)
      C + 다른 상품으로 더 무거우므로 여기에 들지 않는다.
    - 출처 층 청구가 그 층 무게로 설명된다(근접 밴드 안) — 출처 자체가 무게와 어긋나면
      어느 쪽이 실제 취출인지 이 근거로 가를 수 없다."""
    if (floor is None or -event.delta_weight < floor
            or any(s.delta_grams >= floor for s in event.segments)):
        return set()
    end = max((event.ts, *event.change_timestamps))
    lifted: set[int] = set()
    for other in earlier:
        if other.zone == event.zone or not any(
                s.delta_grams < 0 and event.ts <= s.end_ts <= end for s in other.segments):
            continue
        for pc in other.judgment.products:
            if -event.delta_weight > pc.product.unit_weight - floor:
                continue
            rest = sum(o.product.unit_weight * o.count for o in other.judgment.products
                       if o is not pc)
            if not _weight_decisively_inconsistent(
                    -other.delta_weight - rest, pc.product, gate, slack):
                lifted.add(pc.product.class_id)
    return lifted
