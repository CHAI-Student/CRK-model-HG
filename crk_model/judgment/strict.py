"""StrictWeightMatcher — 무게 우선 백트래킹 조합 탐색 (다이어그램 6).

로드셀이 tolerance 내로 정확하다는 가정 → 무게로 가능한 조합을 먼저 뽑고,
그 중 YOLO가 본 것만 남겨 vision confidence로 최종 선택.

불변식: I5(stock=0 제외) · I12(count ≤ stock)는 탐색 공간에서 강제.
tolerance는 SensorProfile 단일 소스 — 조기 종료(D7)도 같은 함수를 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from crk_model.core.types import ActiveProduct, ProductCount, VisionCandidate


@dataclass(frozen=True)
class Combination:
    products: tuple[ProductCount, ...]
    weight_error: float
    match_score: float


class StrictWeightMatcher:
    def __init__(self, max_items: int = 6, max_kinds: int = 3):
        self.max_items = max_items
        self.max_kinds = max_kinds

    def find_valid_combinations(
        self,
        vision_candidates: Sequence[VisionCandidate],
        delta_weight: float,
        active_products: Sequence[ActiveProduct],
        tolerance: float,
    ) -> list[Combination]:
        target = abs(delta_weight)
        if target < tolerance:
            return []  # target_below_tolerance

        conf = {c.class_id: c.confidence for c in vision_candidates}
        # I5: 품절 제외 / vision 미검출 후보 제외
        pool = [p for p in active_products if p.stock_qty > 0 and p.class_id in conf]
        pool.sort(key=lambda p: -p.unit_weight)

        seen: dict[tuple, tuple[tuple[tuple[ActiveProduct, int], ...], float]] = {}

        def record(current: list[tuple[ActiveProduct, int]], weight: float) -> None:
            if current and abs(weight - target) <= tolerance:
                key = tuple(sorted((p.product_id, c) for p, c in current))
                if key not in seen:
                    seen[key] = (tuple(current), weight)

        def rec(i: int, current: list, weight: float, items: int, kinds: int) -> None:
            record(current, weight)
            if i >= len(pool) or weight >= target + tolerance or items >= self.max_items:
                return
            rec(i + 1, current, weight, items, kinds)  # pool[i] 미사용
            if kinds >= self.max_kinds:
                return
            p = pool[i]
            max_c = min(p.stock_qty, self.max_items - items)  # I12
            for c in range(1, max_c + 1):
                w = weight + p.unit_weight * c
                if w > target + tolerance:
                    break
                rec(i + 1, current + [(p, c)], w, items + c, kinds + 1)

        rec(0, [], 0.0, 0, 0)

        combos = []
        for items, weight in seen.values():
            err = abs(weight - target)
            combos.append(
                Combination(
                    products=tuple(ProductCount(p, c) for p, c in items),
                    weight_error=err,
                    match_score=self._score(items, err, tolerance, conf),
                )
            )
        # combination_sort_key: -match_score → 종류 수 → 오차
        combos.sort(key=lambda c: (-c.match_score, len(c.products), c.weight_error))
        return combos

    def best(self, *args, **kwargs) -> Combination | None:
        combos = self.find_valid_combinations(*args, **kwargs)
        return combos[0] if combos else None

    @staticmethod
    def _score(
        items: Sequence[tuple[ActiveProduct, int]],
        weight_error: float,
        tolerance: float,
        conf: dict[int, float],
    ) -> float:
        weight_score = max(0.0, 1 - weight_error / tolerance) if tolerance > 0 else 0.0
        total_count = sum(c for _, c in items)
        vision_score = (
            sum(conf.get(p.class_id, 0.0) * c for p, c in items) / total_count
            if total_count
            else 0.0
        )
        simplicity = max(0.0, 1 - (len(items) - 1) * 0.2)
        return weight_score * 0.6 + vision_score * 0.3 + simplicity * 0.1
