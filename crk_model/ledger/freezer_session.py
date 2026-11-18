"""CLOSE에서 반납들을 함께 배정한다. 미관측 SKU나 미래 취출은 만들지 않는다."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile
from crk_model.ledger.events import TriggerEvent
from crk_model.ledger.freezer_returns import (
    TakeLot,
    evidence_action_time,
    match_movement_return,
    record_event_takes,
)


def resolve_session_returns(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default_profile: SensorProfile, notes: list[str],
) -> list[TriggerEvent]:
    returns = [e for e in events if e.delta_weight > 0 and e.freezer_return
               and e.freezer_return.stabilized
               and not profiles.get(e.zone, default_profile).weight_is_discriminative]
    enabled = {id(e) for e in returns
               if e.freezer_movement and e.freezer_movement.vision_priority}
    if len(returns) < 2 or not enabled:
        return list(events)
    if len(returns) > 12:
        notes.append("freezer_session_assignment_search_limit")
        return list(events)
    if any(e.freezer_actions is not None or (
        e.delta_weight >= (profiles.get(e.zone, default_profile).min_trigger_delta_grams or 5)
        and e.freezer_return is None
    ) for e in events):
        # 구 반납의 무게 기반 소비를 재구성할 수 없는 기록은 기존 정산을 보존한다.
        return list(events)
    lots: list[TakeLot] = []
    for event in events:
        profile = profiles.get(event.zone, default_profile)
        if profile.weight_is_discriminative:
            continue
        first = len(lots)
        record_event_takes(lots, event, profile.tolerance_grams, verified_only=True)
        for lot in lots[first:]:
            lot.ts = evidence_action_time(event)
    if not lots:
        return list(events)
    observed_endpoints = [lot.stable_unit_weight is not None for lot in lots]
    if any(observed_endpoints) and not all(observed_endpoints):
        # 후보 축소로 BOCPD/안정 끝점 중 유리한 계산식으로 바뀌지 않도록 한다.
        return list(events)
    returns.sort(key=evidence_action_time)
    best = -1
    solutions = []
    visits = 0

    def search(index: int, chosen: list[int | None], count: int) -> None:
        nonlocal best, visits
        visits += 1
        if visits > 4096:
            raise OverflowError
        if index == len(returns):
            if count > best:
                best = count
                solutions.clear()
            if count == best:
                solutions.append(tuple(chosen))
            return
        event = returns[index]
        profile = profiles.get(event.zone, default_profile)
        eligible = [lot for lot in lots if lot.ts <= evidence_action_time(event)]
        if id(event) not in enabled:
            matched = match_movement_return(
                event.freezer_return, event.delta_weight, event.zone, eligible,
                profile.tolerance_grams, event.freezer_movement, profile.min_trigger_delta_grams,
            )
            for lot, n in matched.allocation:
                lot.remaining -= n
            search(index + 1, [*chosen, None], count)
            for lot, n in matched.allocation:
                lot.remaining += n
            return
        search(index + 1, [*chosen, None], count)
        original = match_movement_return(
            event.freezer_return, event.delta_weight, event.zone, eligible,
            profile.tolerance_grams, event.freezer_movement, profile.min_trigger_delta_grams,
        )
        if original.allocation:
            # 이미 명확한 비전/실측 연결은 더 많은 반납을 설명하려고 바꾸지 않는다.
            choices = {lot.product.class_id for lot, _ in original.allocation}
        elif original.reason == "ambiguous_identity":
            top_votes = max((c.vote_count for c in event.freezer_return.candidates), default=0)
            choices = {c.class_id for c in event.freezer_return.candidates
                       if c.vote_count >= .5 * top_votes}
        else:
            return
        for candidate in event.freezer_return.candidates:
            if candidate.vote_count < 3 or candidate.class_id not in choices:
                continue
            matched = match_movement_return(
                replace(event.freezer_return, candidates=(candidate,)),
                event.delta_weight, event.zone, eligible, profile.tolerance_grams,
                event.freezer_movement, profile.min_trigger_delta_grams,
            )
            if not matched.allocation:
                continue
            for lot, n in matched.allocation:
                lot.remaining -= n
            search(index + 1, [*chosen, candidate.class_id], count + 1)
            for lot, n in matched.allocation:
                lot.remaining += n

    try:
        search(0, [], 0)
    except OverflowError:
        notes.append("freezer_session_assignment_search_limit")
        return list(events)
    assignments = {}
    for i, event in enumerate(returns):
        if id(event) not in enabled:
            continue
        choices = {solution[i] for solution in solutions}
        # 동점 해가 다른 상품/미배정을 주장하면 입력 순서로 억지 선택하지 않는다.
        if len(choices) != 1 or None in choices:
            continue
        cid = next(iter(choices))
        candidates = tuple(c for c in event.freezer_return.candidates if c.class_id == cid)
        assignments[id(event)] = replace(event, freezer_return=replace(
            event.freezer_return, candidates=candidates,
        ))
        if len(event.freezer_return.candidates) > 1:
            notes.append(f"zone{event.zone}:freezer_session_return_assignment:class{cid}")
    return [assignments.get(id(event), event) for event in events]
