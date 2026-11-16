"""냉동 반납: 비전으로 SKU를 고르고, 앞선 취출의 측정 delta로 수량을 검증한다."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.types import ActiveProduct, ProductCount, VisionCandidate
from crk_model.ledger.events import FreezerReturnEvidence


@dataclass
class TakeLot:
    zone: int
    product: ActiveProduct
    remaining: int
    unit_weight: float  # 한 SKU 취출은 abs(delta)/수량. 여러 SKU는 개별 측정이 없어 등록값.


def record_take(
    lots: list[TakeLot], zone: int, delta: float, products: Sequence[ProductCount],
) -> None:
    for pc in products:
        if pc.count <= 0:
            continue
        unit = abs(delta) / pc.count if len(products) == 1 else pc.product.unit_weight
        if math.isfinite(unit) and unit > 0:
            lots.append(TakeLot(zone, pc.product, pc.count, unit))


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
