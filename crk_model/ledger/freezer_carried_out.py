"""CLOSE carried-out 지문 — 한 층에서 꺼낸 상품이 다음에 열린 다른 층 영상의
첫 프레임부터 손에 들려 있으면(carried-in 지문, head_votes ≥ CARRIED_IN_MIN_HEAD)
그 상품은 앞 층에서 나온 것이다 (2026-11-26 추가, yoona.md #freezer-carried-out-20261126).

지문 조건 (이벤트 E, 클래스 C):
- E는 정상 냉동 취출 이벤트이고, C는 E의 비전 후보(표 ≥ 3)이면서 E 영상 첫
  프레임에는 손에 없었다(head_votes < CARRIED_IN_MIN_HEAD).
- C는 E의 취출 방향 후보(take_candidates, 표 ≥ 3)다 — 영상이 C를 꺼내는 쪽으로 봤다.
- E 뒤에 C가 처음 보인 다른 층 이벤트에서 C가 첫 프레임부터 손에 있었다.

쓰는 곳 두 가지:
1. 유령 판정 예외 (`ghost_exempt_classes`, ses-8-1795685730): 손에 든 채 다른 층을
   열면 그 층 영상에도 표가 남아 ghost_ledger가 "여러 층에 보였지만 무게가 받쳐 준
   적 없는 유령"으로 본다. 꺼낸 층에서만은 유령이 아니다.
2. 같은 무게대 정체성 교체 (`adopt_carried_out_identity`, ses-9-1795685842 ·
   ses-10-1795685937): 무게로는 가를 수 없는 두 상품(93/94, 95/99) 중 청구된 쪽 D는
   다음 층에 들려 나간 흔적이 없고, 다른 쪽 C는 들려 나갔으며 표도 D 이상이면 C로
   바꾼다. 무게는 거부권만 가진다 — 근처 범위(2 × count gate) 밖이면 바꾸지 않는다.

손 ID·박스 수를 개수로 세지 않는다 — 바꾸는 것은 이미 1개로 청구된 상품의 정체성뿐이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.ledger.cross_zone import CARRIED_IN_MIN_HEAD
from crk_model.ledger.events import TriggerEvent

# 그 클래스를 "봤다"로 칠 최소 표 수 (settler._COMBO_VOTE_FLOOR와 같은 값).
VOTE_FLOOR = 3


def _is_freezer_take(e: TriggerEvent, profiles: Mapping[int, SensorProfile],
                     default: SensorProfile) -> bool:
    prof = profiles.get(e.zone, default)
    return (e.status == "ok" and e.delta_weight < 0
            and not prof.weight_is_discriminative)


def _first_later_sighting(
    class_id: int, event: TriggerEvent, events: Sequence[TriggerEvent],
) -> VisionCandidate | None:
    """event 뒤에 다른 층에서 class_id가 처음 보인(표 ≥ VOTE_FLOOR) 후보."""
    later = sorted(
        (e for e in events
         if e.status == "ok" and e.zone != event.zone and e.ts > event.ts),
        key=lambda e: e.ts,
    )
    for e in later:
        c = next((c for c in e.vision_candidates
                  if c.class_id == class_id and c.vote_count >= VOTE_FLOOR), None)
        if c is not None:
            return c
    return None


def carried_into_next_zone(
    class_id: int, event: TriggerEvent, events: Sequence[TriggerEvent],
) -> bool:
    """event 뒤 class_id가 처음 보인 다른 층에서 첫 프레임부터 손에 있었는가."""
    c = _first_later_sighting(class_id, event, events)
    return c is not None and c.head_votes >= CARRIED_IN_MIN_HEAD


def carried_out_classes(
    event: TriggerEvent, events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile], default: SensorProfile,
) -> frozenset[int]:
    """event 층에서 꺼내져 다음 층으로 들려 나간 클래스 (모듈 docstring의 지문)."""
    if not _is_freezer_take(event, profiles, default) or event.freezer_movement is None:
        return frozenset()
    take = {c.class_id for c in event.freezer_movement.take_candidates
            if c.vote_count >= VOTE_FLOOR}
    return frozenset(
        c.class_id for c in event.vision_candidates
        if c.class_id > 0 and c.class_id in take
        and c.vote_count >= VOTE_FLOOR and c.head_votes < CARRIED_IN_MIN_HEAD
        and carried_into_next_zone(c.class_id, event, events)
    )


def ghost_exempt_classes(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default: SensorProfile,
) -> dict[int, frozenset[int]]:
    """zone → 그 층에서는 유령으로 보지 않을 클래스.

    C가 Z에서 들려 나갔고, Z 밖에서 C가 보인 곳(표 ≥ VOTE_FLOOR)이 전부 그 뒤에
    첫 프레임부터 손에 든 채였다면, 다른 층의 표는 Z에서 꺼낸 C를 들고 간
    흔적이다. 다른 층에서 손에 없던 C가 보였으면 예외를 두지 않는다."""
    out: dict[int, set[int]] = {}
    for e in events:
        for cid in carried_out_classes(e, events, profiles, default):
            elsewhere = [
                (o.ts, c.head_votes)
                for o in events if o.status == "ok" and o.zone != e.zone
                for c in o.vision_candidates
                if c.class_id == cid and c.vote_count >= VOTE_FLOOR
            ]
            if all(ts > e.ts and head >= CARRIED_IN_MIN_HEAD for ts, head in elsewhere):
                out.setdefault(e.zone, set()).add(cid)
    return {z: frozenset(cs) for z, cs in out.items()}


def adopt_carried_out_identity(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    products: Sequence[ActiveProduct], default: SensorProfile, notes: list[str],
) -> tuple[list[TriggerEvent], set[int]]:
    """1개 청구(D×1)된 냉동 취출의 정체성을 들려 나간 상품 C로 바꾼다.

    조건 (전부):
    - 들려 나간 클래스가 D가 아닌 C 하나뿐이고, D는 들려 나간 흔적이 없다.
    - C는 이 세션 어디에서도 청구되지 않았다 (다른 층 청구를 겹쳐 세지 않는다).
    - C의 표 ≥ D의 표 (이 이벤트 비전 후보 기준).
    - 무게 거부권: |꺼낸 무게 − C 등록무게| ≤ 2 × count gate (근처 범위).

    결과는 PARTIAL C×1 (`freezer_carried_out_identity`). 무게만으로 고른 스냅이
    다시 덮지 않도록 바꾼 층을 돌려준다 (호출부가 CLOSE 재solve에서 보존)."""
    by_class = {p.class_id: p for p in products}
    billed = {pc.product.class_id for e in events if e.status == "ok"
              for pc in e.judgment.products if pc.count > 0}
    out = list(events)
    adopted: set[int] = set()
    for i, e in enumerate(out):
        j = e.judgment
        if (j.status not in (JudgmentStatus.COMPLETE, JudgmentStatus.PARTIAL)
                or len(j.products) != 1 or j.products[0].count != 1):
            continue
        d = j.products[0].product.class_id
        carried = carried_out_classes(e, out, profiles, default)
        others = carried - {d}
        if len(others) != 1 or carried_into_next_zone(d, e, out):
            continue
        (c,) = others
        prod = by_class.get(c)
        if prod is None or c in billed or prod.unit_weight <= 0 or prod.stock_qty <= 0:
            continue
        vc = {v.class_id: v for v in e.vision_candidates}
        if vc[c].vote_count < vc.get(d, vc[c]).vote_count:
            continue
        gate = profiles.get(e.zone, default).count_gate
        if abs(-e.delta_weight - prod.unit_weight) > 2.0 * gate:
            continue
        out[i] = replace(e, judgment=JudgmentResult(
            status=JudgmentStatus.PARTIAL,
            products=(ProductCount(prod, 1),),
            confidence=vc[c].confidence,
            reason="freezer_carried_out_identity",
            strategy=j.strategy,
        ))
        billed.add(c)
        adopted.add(e.zone)
        notes.append(
            f"zone{e.zone}:freezer_carried_out_identity:billed=class{d}:"
            f"adopted=class{c}:trigger_ts={e.ts:.3f}"
        )
    return out, adopted
