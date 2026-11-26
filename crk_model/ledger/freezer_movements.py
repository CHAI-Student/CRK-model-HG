"""냉동 혼합 취출·반납을 비전, 앞선 취출 실측, 최종 순변화로 함께 검증한다.

중간 양수 스텝을 각각 반납으로 만들지 않는다. 마지막 음수 변화 뒤의
안정된 증가량과 반납 SKU가 앞선 취출 이력을 설명하고, 남은 무게를 취출
방향 SKU가 설명할 때만 정산 내부에서 분해한다. 원본 이벤트는 수정하지 않는다.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile, product_size_floor, return_tolerance
from crk_model.core.types import ActiveProduct, JudgmentResult, JudgmentStatus, ProductCount
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.events import FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.freezer_close_checks import has_masked_movement
from crk_model.ledger.freezer_returns import (
    TakeLot,
    match_movement_return,
    match_return,
    match_session_weight_return,
    record_take,
    record_take_measured,
)


def _end_ts(event: TriggerEvent) -> float:
    return max((event.ts, *event.change_timestamps,
                *(segment.end_ts for segment in event.segments)))


def resolve_mixed_movements(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    products: Sequence[ActiveProduct], default_profile: SensorProfile, notes: list[str],
    *, count_unit_slack: float = 5.0,
) -> list[TriggerEvent]:
    """병합 트리거는 반납이 끝난 시점에 검증해, 그 사이 다른 층 취출도 연결한다."""
    if not any(event.freezer_movement for event in events):
        return list(events)
    floor = product_size_floor(p.unit_weight for p in products)
    lots: list[TakeLot] = []
    out: list[TriggerEvent] = []
    router = JudgmentRouter()
    for event in sorted(events, key=_end_ts):
        profile = profiles.get(event.zone, default_profile)
        if profile.weight_is_discriminative:
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
            split = _split_mixed(event, lots, products, profile, router, floor)
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
        if event.delta_weight < 0 and (
                event.judgment.status is JudgmentStatus.COMPLETE
                or _floor_weight_verified(event, profile, count_unit_slack)):
            # ② 혼합 해석의 취출 이력도 상품 채널 실측으로 기록한다 (정산과 같은 기준).
            # 층 무게로 확인된 PARTIAL도 넣는다 — 작은 채널을 빼고 판정해 PARTIAL이 됐어도
            # 층 순변화가 청구 상품과 맞으면 실제 취출이다 (2026-11-26 ses-1-1795681459
            # 3층: ch1 −557.5g만으로 11 PARTIAL, 층 −526.25g은 11과 3.75g — 이력이 없어
            # 2층 11 반납 +510g을 맞추지 못하고 2층 8 취출 분해가 빠졌다).
            record_take_measured(
                lots, event.zone, event.delta_weight, event.judgment.products,
                movement, profile.count_gate, count_unit_slack, floor, event.segments,
            )
        elif event.delta_weight > 0 and event.freezer_return is not None:
            matched = match_movement_return(
                event.freezer_return, event.delta_weight, event.zone, lots,
                return_tolerance(profile), movement, profile.min_trigger_delta_grams,
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


def _floor_weight_verified(
    event: TriggerEvent, profile: SensorProfile, count_unit_slack: float,
) -> bool:
    """PARTIAL 취출의 청구 상품 합이 층 순변화를 개수 게이트(gate_n) 안에서 설명하는가."""
    products = event.judgment.products
    if event.judgment.status is not JudgmentStatus.PARTIAL or not products:
        return False
    count = sum(pc.count for pc in products)
    billed = sum(pc.product.unit_weight * pc.count for pc in products)
    gate_n = profile.count_gate + count_unit_slack * max(0, count - 1)
    return billed > 0 and abs(-event.delta_weight - billed) <= gate_n


def _split_mixed(
    event: TriggerEvent, lots: Sequence[TakeLot], products: Sequence[ActiveProduct],
    profile: SensorProfile, router: JudgmentRouter, floor: float,
) -> tuple[TriggerEvent, TriggerEvent, list[tuple[TakeLot, int]]] | None:
    movement = event.freezer_movement
    taking = tuple(c for c in movement.take_candidates if c.vote_count >= 3)
    returning = tuple(c for c in movement.return_candidates if c.vote_count >= 3)
    segments = sorted(event.segments, key=lambda segment: segment.end_ts)
    # 반납 방향 후보가 없어도 상품 크기 하강·상승이 모두 있으면 반납 SKU를 세션 무게
    # 반납과 같은 조건으로만 찾는다 (2026-11-26: D2 상쇄 움직임, 순변화 50g 이상도 포함 —
    # 7 취출 + 11 반납은 순변화가 +60g 안팎이다).
    masked = not returning and has_masked_movement(segments, floor)
    if not taking or (not returning and not masked):
        return None
    takes = [i for i, segment in enumerate(segments) if segment.delta_grams < 0]
    if not takes:
        return None
    last_take = takes[-1]
    suffix, prefix = segments[last_take + 1:], segments[:takes[0]]
    if suffix and all(segment.delta_grams > 0 for segment in suffix) and sum(
            segment.delta_grams for segment in suffix) >= floor:
        returned_part = suffix  # 취출 → 반납
    elif returning and prefix and all(segment.delta_grams > 0 for segment in prefix):
        # 반납 → 취출: 들고 온 상품을 먼저 놓고 이 층 상품을 꺼냈다 (2026-11-26). 손으로
        # 누른 하중도 첫 하강 앞 상승이라, 반납 방향 SKU의 앞선 취출 이력과 무게가 직접
        # 맞을 때만 인정한다(세션 무게 보완 없음).
        returned_part = prefix
    else:
        return None
    returned_weight = sum(segment.delta_grams for segment in returned_part)
    if returned_weight < max(floor, profile.min_weight_change_grams):
        return None
    tolerance = return_tolerance(profile)
    if returning:
        evidence = FreezerReturnEvidence(returning, True, "return_direction")
        allocation, reason = match_return(evidence, returned_weight, event.zone, lots, tolerance)
    else:
        evidence = FreezerReturnEvidence(
            tuple(c for c in event.vision_candidates if c.vote_count >= 3), True, "video",
        )
        allocation, reason = [], "no_prior_take"
    by_session_weight = not allocation and returned_part is suffix
    if by_session_weight:
        # 반납 SKU를 이 세션에서 꺼낸 적 없거나(비전 혼동) 정하지 못했으면, 세션 취출
        # 이력 중 무게가 맞는 SKU가 하나뿐이고 영상에 관측됐을 때만 연결한다.
        observed = {c.class_id for c in (*event.vision_candidates, *returning)
                    if c.vote_count >= 3}
        allocation = match_session_weight_return(
            evidence, returned_weight, event.zone, lots, tolerance, reason, observed,
        )
        # 정한 SKU만 정산에 넘긴다 — 정산 시점에는 이 분해가 만든 취출 이력도 있어, 같은
        # 무게대의 그 SKU가 세션 무게 후보로 다시 겹친다 (ses-1-1795668469 2층: 반납 525g
        # 대 11 518.3g·같은 트리거 8 500g).
        chosen = {lot.product.class_id for lot, _ in allocation}
        seen = {c.class_id: c for c in (*event.vision_candidates, *returning) if c.vote_count >= 3}
        evidence = FreezerReturnEvidence(
            tuple(seen[cid] for cid in sorted(chosen) if cid in seen), True, evidence.source,
        )
    if not allocation:
        return None
    # -취출 + 반납 = 최종 delta. 접촉에 따른 중간 +750/-1360을 개별 행동으로 세지 않는다.
    taken_weight = returned_weight - event.delta_weight
    if taken_weight < profile.min_weight_change_grams:
        return None
    returned_classes = {lot.product.class_id for lot, _ in allocation}
    # 다른 층에서 꺼내 들고 온 반납 SKU. 카메라를 모든 층이 공유해 이 층 클립에 취출
    # 방향으로도 잡히므로(들고 오는 모습), 이 층 취출 후보에서 뺀다 (2026-11-26:
    # ses-1-1795659665 3층 10 → 2층 반납, ses-1-1795670038 2층 11 → 5층 7 취출 + 11 반납).
    carried = ({lot.product.class_id for lot, _ in allocation if lot.zone != event.zone}
               - {lot.product.class_id for lot, _ in allocation if lot.zone == event.zone})
    take_candidates = tuple(c for c in taking if c.class_id not in carried)
    if not take_candidates:
        # 취출 방향 후보가 들고 온 SKU뿐이면 반납만 연결하고, 이 층에서 꺼낸 상품은 영상
        # 근거가 없으므로 청구하지 않는다 — 남은 무게는 정산에서 review로 드러난다.
        judgment = JudgmentResult(JudgmentStatus.NO_DETECTION, reason="carried_in_unidentified")
    else:
        judgment = router.judge(JudgmentContext(
            zone=event.zone, profile=profile, delta_weight=-taken_weight, segments=(),
            vision_candidates=take_candidates, active_products=tuple(products),
            vision_only=False,
        ))
        if judgment.status is not JudgmentStatus.COMPLETE:
            judgment = (_derived_take(take_candidates, taken_weight, products, profile)
                        or judgment)
        if judgment.status is not JudgmentStatus.COMPLETE or not judgment.products:
            return None
        if any(pc.product.class_id in returned_classes for pc in judgment.products):
            return None  # 같은 SKU의 반복/개수 혼합은 이 증거만으로 분해하지 않는다.
    taken = replace(
        event, ts=segments[last_take].end_ts, delta_weight=-taken_weight, segments=(),
        judgment=replace(judgment, reason="freezer_mixed_take:" + judgment.reason),
        vision_candidates=take_candidates, change_timestamps=(segments[last_take].end_ts,),
        freezer_return=None, freezer_movement=None,
    )
    returned = replace(
        event, ts=returned_part[-1].end_ts, delta_weight=returned_weight, segments=(),
        judgment=JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_mixed_return"),
        # 세션 무게로 찾았으면 정산의 반납 매칭도 같은 관측 근거를 보도록 원래 후보를 남긴다.
        vision_candidates=event.vision_candidates if by_session_weight else returning,
        change_timestamps=(returned_part[-1].end_ts,),
        freezer_return=evidence, freezer_movement=None,
    )
    return taken, returned, allocation


def _derived_take(
    taking: Sequence, taken_weight: float, products: Sequence[ActiveProduct],
    profile: SensorProfile,
) -> JudgmentResult | None:
    """반납 실측 − 순변화로 구한 취출 무게는 측정 오차가 두 번 쌓인다
    (ses-2-1795594482 1층: 102 530g을 506.8g으로 계산). 취출 방향 1위 SKU 하나로
    tolerance×2 안에서 설명되고 재고 안일 때만 COMPLETE로 인정한다."""
    top = max(taking, key=lambda c: (c.vote_count, c.confidence))
    product = next((p for p in products if p.class_id == top.class_id and p.unit_weight > 0), None)
    if product is None:
        return None
    n = max(1, round(taken_weight / product.unit_weight))
    residual = abs(taken_weight - n * product.unit_weight)
    if n > product.stock_qty or residual > 2 * profile.tolerance_grams:
        return None
    return JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(product, n),),
                          top.confidence, reason="freezer_derived_take_weight")
