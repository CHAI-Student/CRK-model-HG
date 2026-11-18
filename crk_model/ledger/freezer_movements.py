"""냉동 혼합 취출·반납을 비전, 앞선 취출 실측, 최종 순변화로 함께 검증한다.

중간 양수 스텝을 각각 반납으로 만들지 않는다. 마지막 음수 변화 뒤의
안정된 증가량과 반납 SKU가 앞선 취출 이력을 설명하고, 남은 무게를 취출
방향 SKU가 설명할 때만 정산 내부에서 분해한다. 원본 이벤트는 수정하지 않는다.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import ActiveProduct, JudgmentResult, JudgmentStatus
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.events import FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.freezer_returns import (
    TakeLot,
    match_movement_return,
    match_return,
    record_event_takes,
    record_take,
)


def _end_ts(event: TriggerEvent) -> float:
    return max((event.ts, *event.change_timestamps,
                *(segment.end_ts for segment in event.segments)))


def resolve_mixed_movements(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    products: Sequence[ActiveProduct], default_profile: SensorProfile, notes: list[str],
) -> list[TriggerEvent]:
    """병합 트리거는 반납이 끝난 시점에 검증해, 그 사이 다른 층 취출도 연결한다."""
    if not any(event.freezer_movement for event in events):
        return list(events)
    lots: list[TakeLot] = []
    out: list[TriggerEvent] = []
    router = JudgmentRouter()
    for event in sorted(events, key=_end_ts):
        profile = profiles.get(event.zone, default_profile)
        if profile.weight_is_discriminative or event.freezer_actions is not None:
            if event.freezer_actions is not None:
                lots.clear()  # 구 행동별 정산과 신규 추론이 같은 취출을 재사용하지 않는다.
            out.append(event)
            continue
        movement = event.freezer_movement
        taking_classes = ({c.class_id for c in movement.take_candidates if c.vote_count >= 3}
                          if movement else set())
        billed_classes = {pc.product.class_id for pc in event.judgment.products}
        explained_take = (event.judgment.status is JudgmentStatus.COMPLETE
                          and billed_classes and billed_classes <= taking_classes)
        if (event.delta_weight != 0 and movement is not None and movement.stabilized
                and not explained_take):
            split = _split_mixed(event, lots, products, profile, router)
            if split is not None:
                taken, returned, allocation = split
                for lot, count in allocation:
                    lot.remaining -= count
                record_take(lots, taken.zone, taken.delta_weight, taken.judgment.products)
                out.extend((taken, returned))
                notes.append(
                    f"zone{event.zone}:freezer_mixed_resolved:"
                    f"take={taken.delta_weight:.1f}g:return={returned.delta_weight:.1f}g:"
                    f"net={event.delta_weight:.1f}g"
                )
                continue
        if event.delta_weight < 0:
            record_event_takes(lots, event, profile.tolerance_grams, verified_only=True)
        elif event.delta_weight > 0 and event.freezer_return is not None:
            matched = match_movement_return(
                event.freezer_return, event.delta_weight, event.zone, lots,
                profile.tolerance_grams, movement, profile.min_trigger_delta_grams,
            )
            for lot, count in matched.allocation:
                lot.remaining -= count
            if not matched.allocation:
                lots.clear()  # 미확정 반납이 소비했을 수 있는 취출을 혼합 해석에 재사용하지 않는다.
        elif event.delta_weight >= (profile.min_trigger_delta_grams
                                    or profile.min_weight_change_grams):
            lots.clear()  # 종류 증거 없는 구 반납 이후의 잔여 SKU는 재구성하지 않는다.
        out.append(event)
    return sorted(out, key=lambda event: event.ts)


def _split_mixed(
    event: TriggerEvent, lots: Sequence[TakeLot], products: Sequence[ActiveProduct],
    profile: SensorProfile, router: JudgmentRouter,
) -> tuple[TriggerEvent, TriggerEvent, list[tuple[TakeLot, int]]] | None:
    movement = event.freezer_movement
    taking = tuple(c for c in movement.take_candidates if c.vote_count >= 3)
    returning = tuple(c for c in movement.return_candidates if c.vote_count >= 3)
    if not taking or not returning:
        return None
    segments = sorted(event.segments, key=lambda segment: segment.end_ts)
    last_take = next((i for i in range(len(segments) - 1, -1, -1)
                      if segments[i].delta_grams < 0), None)
    if last_take is None:
        return None
    suffix = segments[last_take + 1:]
    if not suffix or any(segment.delta_grams <= 0 for segment in suffix):
        return None
    returned_weight = sum(segment.delta_grams for segment in suffix)
    if returned_weight < profile.min_weight_change_grams:
        return None
    evidence = FreezerReturnEvidence(returning, True, "return_direction")
    allocation, _ = match_return(
        evidence, returned_weight, event.zone, lots, profile.tolerance_grams,
    )
    if not allocation:
        return None
    # -취출 + 반납 = 최종 delta. 접촉에 따른 중간 +750/-1360을 개별 행동으로 세지 않는다.
    taken_weight = returned_weight - event.delta_weight
    if taken_weight < profile.min_weight_change_grams:
        return None
    judgment = router.judge(JudgmentContext(
        zone=event.zone, profile=profile, delta_weight=-taken_weight, segments=(),
        vision_candidates=taking, active_products=tuple(products), vision_only=False,
    ))
    if judgment.status is not JudgmentStatus.COMPLETE or not judgment.products:
        return None
    returned_classes = {lot.product.class_id for lot, _ in allocation}
    if any(pc.product.class_id in returned_classes for pc in judgment.products):
        return None  # 같은 SKU의 반복/개수 혼합은 이 증거만으로 분해하지 않는다.
    taken = replace(
        event, ts=segments[last_take].end_ts, delta_weight=-taken_weight, segments=(),
        judgment=replace(judgment, reason="freezer_mixed_take:" + judgment.reason),
        vision_candidates=taking, change_timestamps=(segments[last_take].end_ts,),
        freezer_return=None, freezer_movement=None,
    )
    returned = replace(
        event, ts=suffix[-1].end_ts, delta_weight=returned_weight, segments=(),
        judgment=JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_mixed_return"),
        vision_candidates=returning, change_timestamps=(suffix[-1].end_ts,),
        freezer_return=evidence, freezer_movement=None,
    )
    return taken, returned, allocation
