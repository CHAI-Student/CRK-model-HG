"""냉동 반납: 비전으로 SKU를 고르고, 앞선 취출의 측정 delta로 수량을 검증한다."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from crk_model.core.types import ActiveProduct, ProductCount, VisionCandidate, WeightSegment
from crk_model.ledger.events import FreezerMovementEvidence, FreezerReturnEvidence


@dataclass
class TakeLot:
    zone: int
    product: ActiveProduct
    remaining: int
    unit_weight: float  # 한 SKU 취출은 abs(delta)/수량. 여러 SKU는 개별 측정이 없어 등록값.
    stable_unit_weight: float | None = None


def record_take(
    lots: list[TakeLot], zone: int, delta: float, products: Sequence[ProductCount],
    *, stable_delta: float | None = None,
) -> None:
    for pc in products:
        if pc.count <= 0:
            continue
        unit = abs(delta) / pc.count if len(products) == 1 else pc.product.unit_weight
        if math.isfinite(unit) and unit > 0:
            stable_unit = None
            if (len(products) == 1 and stable_delta is not None
                    and math.isfinite(stable_delta) and stable_delta * delta > 0):
                stable_unit = abs(stable_delta) / pc.count
            lots.append(TakeLot(zone, pc.product, pc.count, unit, stable_unit))


@dataclass(frozen=True)
class ReturnMatch:
    allocation: list[tuple[TakeLot, int]]
    reason: str
    return_weight: float
    expected_weight: float
    measurement: str  # bocpd_total/channel | stable_total/channel

    def unit_weight(self, lot: TakeLot) -> float:
        if self.measurement.startswith("stable_"):
            assert lot.stable_unit_weight is not None
            return lot.stable_unit_weight
        return lot.unit_weight


def stable_movement_delta(movement: FreezerMovementEvidence | None) -> float | None:
    """양 끝이 안정된 동일 채널 집합의 순변화. 구 기록/불안정/부분 기록은 불가."""
    if (movement is None or not movement.stabilized or not movement.channel_deltas
            or len(movement.stable_channel_deltas) != len(movement.channel_deltas)):
        return None
    if not all(math.isfinite(value) for value in movement.stable_channel_deltas):
        return None
    return sum(movement.stable_channel_deltas)


def match_movement_return(
    evidence: FreezerReturnEvidence, delta: float, zone: int,
    lots: Sequence[TakeLot], tolerance: float,
    movement: FreezerMovementEvidence | None, minimum: float | None,
) -> ReturnMatch:
    """양쪽에 같은 시작·종료 관측이 있으면 그 기준으로 일관되게 비교한다.

    적합한 쪽을 나중에 고르지 않는다. 구 기록 등 한쪽만 관측이 있으면
    양쪽 모두 기존 BOCPD delta를 사용한다. 원본 이벤트의 delta는 유지한다.
    """
    channels = movement.channel_deltas if movement and movement.stabilized else ()
    stable_delta = stable_movement_delta(movement)
    available = [lot for lot in lots if lot.remaining > 0]
    visible = {c.class_id for c in evidence.candidates if c.class_id > 0 and c.confidence > 0}
    relevant = [lot for lot in available if lot.product.class_id in visible]
    use_stable = (evidence.stabilized and stable_delta is not None and stable_delta > 0
                  and bool(relevant)
                  and all(lot.stable_unit_weight is not None for lot in relevant))
    measured = stable_delta if use_stable else delta
    if use_stable:
        channels = movement.stable_channel_deltas
        matching_lots = [replace(lot, unit_weight=lot.stable_unit_weight) for lot in relevant]
    else:
        matching_lots = available
    allocation, reason = match_return(
        evidence, measured, zone, matching_lots, tolerance,
        channel_deltas=channels, min_channel_weight=minimum,
    )
    selected = measured
    channel_match = reason.startswith("matched_channel")
    if channel_match:
        selected = primary_return_delta(measured, channels, minimum)
        assert selected is not None
    expected = sum(lot.unit_weight * count for lot, count in allocation)
    originals = {id(copy): lot for copy, lot in zip(
        matching_lots, relevant if use_stable else available, strict=True,
    )}
    return ReturnMatch(
        [(originals[id(lot)], count) for lot, count in allocation], reason, selected, expected,
        ("stable_" if use_stable else "bocpd_") + ("channel" if channel_match else "total"),
    )


def consume_take(lots: Sequence[TakeLot], zone: int, product_id: str, count: int) -> None:
    """구 형식 반납도 이력을 소진해 이후 신규 반납의 이중 차감을 막는다."""
    for lot in lots:
        if lot.zone == zone and lot.product.product_id == product_id:
            removed = min(count, lot.remaining)
            lot.remaining -= removed
            count -= removed
            if count == 0:
                break


def match_return(
    evidence: FreezerReturnEvidence, delta: float, zone: int,
    lots: Sequence[TakeLot], tolerance: float,
    *, channel_deltas: Sequence[float] = (), min_channel_weight: float | None = None,
) -> tuple[list[tuple[TakeLot, int]], str]:
    if not evidence.stabilized:
        return [], "not_stabilized"
    if not math.isfinite(delta) or delta <= 0:
        return [], "weight_mismatch"
    ranked = sorted(
        (c for c in evidence.candidates if c.class_id > 0 and c.confidence > 0
         and math.isfinite(c.confidence)),
        key=lambda c: (-c.vote_count, -c.confidence),
    )
    if not ranked or ranked[0].vote_count < 3:
        return [], "missing_identity"
    allocation, reason = _match_candidates(ranked, delta, zone, lots, tolerance)
    if allocation or reason != "weight_mismatch":
        return allocation, reason
    primary = primary_return_delta(delta, channel_deltas, min_channel_weight)
    if primary is not None:
        allocation, channel_reason = _match_candidates(ranked, primary, zone, lots, tolerance)
        if allocation:
            return allocation, "matched_channel" + (
                "_unique_refit" if channel_reason == "matched_unique_refit" else ""
            )
    return [], reason


def primary_return_delta(
    total: float, channels: Sequence[float], minimum: float | None,
) -> float | None:
    """단일 상품 채널 + 기존 추론 최소량 미만의 부채널 변화만 분리한다.

    총 delta와 채널 합이 일치해야 한다. 나머지는 삭제하지 않고 정산에서
    잔차/검토 사유로 보존한다. 채널 정보가 없거나 상품 크기 채널이 둘이면 불가.
    """
    if minimum is None or minimum <= 0 or len(channels) < 2:
        return None
    if not all(math.isfinite(value) for value in channels):
        return None
    if not math.isclose(sum(channels), total, abs_tol=1e-6):
        return None
    major = [value for value in channels if abs(value) >= minimum]
    residual = sum(abs(value) for value in channels if abs(value) < minimum)
    if len(major) == 1 and major[0] > 0 and residual < minimum:
        return major[0]
    return None


def _match_candidates(
    ranked: Sequence[VisionCandidate], delta: float, zone: int,
    lots: Sequence[TakeLot], tolerance: float,
) -> tuple[list[tuple[TakeLot, int]], str]:
    if len(ranked) > 1 and ranked[0].vote_count < 1.5 * ranked[1].vote_count:
        return [], "ambiguous_identity"
    cid = ranked[0].class_id
    # 다른 잔여 SKU도 선두의 절반 이상 관측되면 복수 종류 반납일 수 있다.
    # 전체 delta를 선두 SKU 여러 개로 바꾸지 않고 검토 대상으로 남긴다.
    prior_classes = {lot.product.class_id for lot in lots if lot.remaining > 0}
    if any(c.class_id in prior_classes and c.vote_count >= .5 * ranked[0].vote_count
           for c in ranked[1:]):
        return [], "ambiguous_identity"
    allocation, reason = _allocate(cid, delta, zone, lots, tolerance)
    if allocation or reason != "weight_mismatch":
        return allocation, reason
    # 선두의 실측 무게가 반증됐을 때만, 영상에서 실제 관측된 다른 SKU를 검증한다.
    # 미관측 SKU/등록 무게만으로 대체하지 않으며, 적합한 종류가 복수면 확정하지 않는다.
    alternatives = []
    uncertain = False
    for candidate in ranked[1:]:
        if candidate.vote_count < 3 or candidate.class_id not in prior_classes:
            continue
        fit, alternative_reason = _allocate(candidate.class_id, delta, zone, lots, tolerance)
        uncertain |= alternative_reason in ("ambiguous_count", "ambiguous_mapping")
        if fit:
            alternatives.append(fit)
    if len(alternatives) == 1 and not uncertain:
        return alternatives[0], "matched_unique_refit"
    if alternatives or uncertain:
        return [], "ambiguous_identity"
    return [], reason


def _allocate(
    cid: int, delta: float, zone: int, lots: Sequence[TakeLot], tolerance: float,
) -> tuple[list[tuple[TakeLot, int]], str]:
    available = sorted(
        (lot for lot in lots if lot.product.class_id == cid and lot.remaining > 0),
        key=lambda lot: lot.zone != zone,  # 같은 SKU 안에서만 동일 층 우선, 나머지는 시간순
    )
    if not available:
        return [], "no_prior_take"
    if len({lot.product.product_id for lot in available}) != 1:
        return [], "ambiguous_mapping"

    # count와 0.1g 버킷으로 상태를 제한한다. 실제 적합 검증은 반올림 전 무게로 한다.
    # 서로 다른 출발 층/취출 시점의 같은 SKU도 최대 12개까지 함께 반납할 수 있다.
    states: dict[tuple[int, float], tuple[float, tuple[tuple[int, int], ...]]] = {
        (0, 0.0): (0.0, ()),
    }
    for index, lot in enumerate(available):
        updated = {}
        for (count, _bucket), (weight, allocation) in states.items():
            for n in range(min(lot.remaining, 12 - count), -1, -1):
                total = weight + n * lot.unit_weight
                if total > delta + tolerance:
                    continue
                key = (count + n, round(total, 1))
                candidate = (total, allocation + (((index, n),) if n else ()))
                if key not in updated or abs(delta - total) < abs(delta - updated[key][0]):
                    updated[key] = candidate
        states = updated
    fits = [(count, weight, allocation) for (count, _), (weight, allocation) in states.items()
            if count > 0 and abs(delta - weight) <= tolerance]
    if not fits:
        return [], "weight_mismatch"
    if len({count for count, _, _ in fits}) != 1:
        return [], "ambiguous_count"
    _, _, allocation = min(fits, key=lambda fit: abs(delta - fit[1]))
    return [(available[index], n) for index, n in allocation], "matched"


# ---- 같은 층 반납 보완 (2026-11-25, 로드셀 무게 안정화 판단 이후) ----
# 비전이 반납 SKU를 정하지 못한 경우(정체성 없음·모호)에만 같은 층 취출 이력 +
# 실측 무게로 보완한다. 비전이 다른 SKU를 뚜렷하게 본 no_prior_take(같은 무게라도
# 무게로 정체성을 가를 수 없다), 본 SKU의 취출 무게가 반증된 weight_mismatch,
# 반납 무게가 아직 안정되지 않은 not_stabilized는 대상이 아니다 (기존처럼 미해결·검토).
_SAME_FLOOR_FALLBACK_REASONS = frozenset({"missing_identity", "ambiguous_identity"})


def match_same_floor_return(
    evidence: FreezerReturnEvidence, delta: float, zone: int,
    lots: Sequence[TakeLot], tolerance: float, reason: str,
) -> list[tuple[TakeLot, int]]:
    """같은 층의 남은 취출 중 실측 무게가 맞는 SKU가 정확히 하나면 그 SKU로 차감한다.

    - 같은 층 취출만 본다. 다른 층 반납은 기존 비전 연결만 쓴다.
    - 무게가 맞는 SKU가 둘 이상(같은 무게 SKU 등)이거나 개수가 갈리면 확정하지 않는다.
    - 영상에 후보가 관측됐는데 맞는 SKU가 그중에 없으면 확정하지 않는다.
    """
    if reason not in _SAME_FLOOR_FALLBACK_REASONS or not evidence.stabilized:
        return []
    if not math.isfinite(delta) or delta <= 0:
        return []
    same_floor = [lot for lot in lots if lot.zone == zone and lot.remaining > 0]
    fits = []
    for cid in sorted({lot.product.class_id for lot in same_floor}):
        allocation, fit_reason = _allocate(cid, delta, zone, same_floor, tolerance)
        if fit_reason in ("ambiguous_count", "ambiguous_mapping"):
            return []
        if allocation:
            fits.append((cid, allocation))
    if len(fits) != 1:
        return []
    cid, allocation = fits[0]
    visible = {c.class_id for c in evidence.candidates if c.class_id > 0 and c.confidence > 0}
    if visible and cid not in visible:
        return []
    return allocation


@dataclass
class UnbilledTake:
    """무게는 빠졌지만 청구 상품이 없는 취출. 같은 층 반납과 상쇄한다."""

    zone: int
    weight: float  # abs(delta)
    cancelled: bool = False


def cancel_unbilled_take(
    unbilled: Sequence[UnbilledTake], zone: int, delta: float, gate: float,
) -> UnbilledTake | None:
    """같은 층 미청구 취출 중 반납 무게와 gate 안으로 맞는 것(가장 가까운 것)을 상쇄한다.

    청구된 상품이 없으므로 장바구니는 바뀌지 않는다. 남은 미청구 무게는 정산의
    zone status(unbilled weight drop → review)가 순변화로 다시 확인한다."""
    fits = [u for u in unbilled
            if u.zone == zone and not u.cancelled and abs(delta - u.weight) <= gate]
    if not fits:
        return None
    best = min(fits, key=lambda u: abs(delta - u.weight))
    best.cancelled = True
    return best


# ---- 2026-11-26 실기 6세션 보완 (yoona.md #freezer-20261126) ----

def spanning_drop(segments: Sequence[WeightSegment], floor: float) -> float | None:
    """두 트레이 이상이 같은 시간 창에서 함께 빠진 하강의 합(g, 음수) 중 가장 큰 것.

    한 상품이 두 트레이에 걸쳐 놓였다 들린 모양이다 (ses-1-1795668469 5층 11:
    −448.3 −70 = −518.3g). 놓여 있던 동안 상품 일부가 로드셀 밖에 걸쳐 있으면 시작→끝
    순변화(−467.5g)는 상품 무게보다 작다. 합이 상품 크기(floor) 미만이면 None.
    채널 하나의 세그먼트는 서로 겹치지 않으므로 겹치는 하강은 다른 채널이다."""
    best = None
    for a in segments:
        if a.delta_grams >= 0:
            continue
        together = [b.delta_grams for b in segments
                    if b.delta_grams < 0 and b.start_ts < a.end_ts and a.start_ts < b.end_ts]
        total = sum(together)
        if len(together) >= 2 and total <= -floor and (best is None or total < best):
            best = total
    return best


def take_measurement(
    delta: float, products: Sequence[ProductCount],
    movement: FreezerMovementEvidence | None, gate: float, slack: float, floor: float,
    segments: Sequence[WeightSegment] = (),
) -> tuple[float, float | None] | None:
    """② 단일 SKU 취출의 단위 무게 (unit, stable_unit). 대상이 아니면 None(기존 record_take).

    측정값 후보는 트리거 총 delta와, 상품 크기(floor 이상)로 빠진 채널이 하나일 때의
    그 채널 delta다. 개수×등록무게와 개수 게이트 안으로 맞는 측정값 중 가장 가까운
    것을 쓴다(ses-2-1795594482 2층: 총 −467.5g 대신 상품 채널 −475g).
    - 여러 개를 한 번에 꺼냈으면 평균이 낱개 무게가 아니므로(비슷한 상품이 섞였을
      수 있음) 등록 무게를 쓴다.
    - 맞는 측정값이 없으면 기존처럼 총 delta를 쓰되, 등록 무게의 절반을 넘게 더
      무거우면(다른 상품 무게가 섞임) 1개 무게로 쓸 수 없으므로 등록 무게를 쓴다.
    """
    if len(products) != 1 or products[0].count <= 0:
        return None
    n, w = products[0].count, products[0].product.unit_weight
    if w <= 0 or not math.isfinite(delta):
        return None
    stable_total = stable_movement_delta(movement)
    options = [(abs(delta), abs(stable_total)
                if stable_total is not None and stable_total * delta > 0 else None)]
    if movement is not None and len(movement.channel_deltas) >= 2:
        takes = [i for i, d in enumerate(movement.channel_deltas) if d <= -floor]
        if len(takes) == 1:
            i = takes[0]
            stable = None
            if (len(movement.stable_channel_deltas) == len(movement.channel_deltas)
                    and movement.stable_channel_deltas[i] < 0):
                stable = -movement.stable_channel_deltas[i]
            options.append((-movement.channel_deltas[i], stable))
    if (drop := spanning_drop(segments, floor)) is not None:
        options.append((-drop, None))  # 두 트레이에 걸친 상품이 들린 순간의 하강 합
    gate_n = gate + slack * (n - 1)
    verified = sorted(((abs(m - n * w), m, s) for m, s in options if abs(m - n * w) <= gate_n),
                      key=lambda option: option[0])
    if verified:
        _, m, s = verified[0]
        if n >= 2:
            return w, None
        return m, s
    m, s = options[0]
    if m - n * w > w / 2:
        return w, None
    return m / n, (s / n if s else None)


def record_take_measured(
    lots: list[TakeLot], zone: int, delta: float, products: Sequence[ProductCount],
    movement: FreezerMovementEvidence | None, gate: float, slack: float, floor: float,
    segments: Sequence[WeightSegment] = (),
) -> None:
    """record_take + ② 상품 채널 실측. 여러 SKU 취출은 기존 record_take 그대로."""
    measured = take_measurement(delta, products, movement, gate, slack, floor, segments)
    if measured is None:
        record_take(lots, zone, delta, products, stable_delta=stable_movement_delta(movement))
        return
    unit, stable_unit = measured
    pc = products[0]
    lots.append(TakeLot(zone, pc.product, pc.count, unit, stable_unit))


# 비전이 반납 SKU를 세션 취출 이력과 연결하지 못한 사유 중 세션 무게로 보완하는 것.
# no_prior_take: 비전 1위 SKU를 이 세션에서 꺼낸 적 없음(비전 혼동, ses-6 용가리).
# ambiguous_identity: 1·2위가 비슷해 정하지 못함(2026-11-26 사용자 결정 E2).
# weight_mismatch(본 SKU의 무게가 반증)와 missing_identity(관측 부족)는 대상이 아니다.
_SESSION_FALLBACK_REASONS = frozenset({"no_prior_take", "ambiguous_identity"})


def match_session_weight_return(
    evidence: FreezerReturnEvidence, delta: float, zone: int,
    lots: Sequence[TakeLot], tolerance: float, reason: str, observed: set[int],
) -> list[tuple[TakeLot, int]]:
    """⑤ 세션 취출 이력(모든 층) 중 반납 무게가 맞는 SKU가 하나뿐이고, 그 SKU가 이번
    영상에 3표 이상 관측됐으면 그 SKU로 차감한다.

    - 같은 무게 SKU·개수 갈림(ambiguous_count/mapping)이면 확정하지 않는다.
    - 여러 SKU를 섞은 조합도 같은 무게를 설명하면 확정하지 않는다.
    - 영상에 한 번도 관측되지 않은 SKU를 무게만으로 고르지 않는다.
    """
    if reason not in _SESSION_FALLBACK_REASONS or not evidence.stabilized:
        return []
    if not math.isfinite(delta) or delta <= 0:
        return []
    available = [lot for lot in lots if lot.remaining > 0]
    fits = []
    for cid in sorted({lot.product.class_id for lot in available}):
        allocation, fit_reason = _allocate(cid, delta, zone, available, tolerance)
        if fit_reason in ("ambiguous_count", "ambiguous_mapping"):
            return []
        if allocation:
            fits.append((cid, allocation))
    if len(fits) != 1 or fits[0][0] not in observed:
        return []
    if _combination_count(delta, available, tolerance) != 1:
        return []
    return fits[0][1]


def _combination_count(
    delta: float, lots: Sequence[TakeLot], tolerance: float, slack: float = 5.0,
) -> int:
    """남은 취출 이력의 SKU 조합(개수 포함) 중 delta를 설명하는 조합 수."""
    by_class: dict[int, tuple[float, int]] = {}
    for lot in lots:
        unit, n = by_class.get(lot.product.class_id, (lot.unit_weight, 0))
        by_class[lot.product.class_id] = (unit, n + lot.remaining)
    items = list(by_class.values())
    found = 0

    def walk(i: int, total: float, count: int) -> None:
        nonlocal found
        if i == len(items):
            if count and abs(total - delta) <= tolerance + slack * (count - 1):
                found += 1
            return
        unit, available = items[i]
        for k in range(available + 1):
            if total + unit * k > delta + tolerance + slack * (count + k):
                break
            walk(i + 1, total + unit * k, count + k)

    walk(0, 0.0, 0)
    return found
