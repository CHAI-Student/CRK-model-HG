"""StrictWeightMatcher — 무게 우선 백트래킹 조합 탐색 (다이어그램 6).

로드셀이 tolerance 내로 정확하다는 가정 → 무게로 가능한 조합을 먼저 뽑고,
그 중 YOLO가 본 것만 남겨 vision confidence로 최종 선택.

불변식: I5(stock=0 제외) · I12(count ≤ stock)는 탐색 공간에서 강제.
tolerance는 SensorProfile 단일 소스 — 조기 종료(D7)도 같은 함수를 쓴다.

개수 오컴 (count_occam, 이슈 #23 0806 3-1): 단일 종 n=1 적합이 있으면 그
최소 잔차보다 **엄격히 더 잘 맞지 않는** 단일 종 n≥2 적합을 후보에서
제외한다 — freezer ①의 `_occam_filter`(0730 시나리오)와 같은 규칙의 냉장
strict판. 실사고: Δ-275(단백질바55 + 오로나민275 동시 취출의 ch1)에서
오로나민×1(잔차 0)과 단백질바×5(55×5=275, 잔차 0)가 **동률**이 되자
match_score의 vision 항(conf 1.0 vs 0.93)만으로 ×5가 이겨 54x6 오과금.
무게가 역산한 ×N 가설은 n=1 관측 가설을 더 잘 설명할 때만 자격이 있다.
다품종 조합에는 미적용 — freezer ③과 같은 이유로 flat 유지 (조합의
우연 적합은 simplicity·vision 항이 이미 감점하고, 여기까지 좁히면
정당한 동시 다종 취출이 n=1 우연에 밀린다).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from crk_model.core.types import ActiveProduct, ProductCount, VisionCandidate


@dataclass(frozen=True)
class Combination:
    products: tuple[ProductCount, ...]
    weight_error: float
    match_score: float


class StrictWeightMatcher:
    def __init__(self, max_items: int = 6, max_kinds: int = 3, count_occam: bool = True):
        self.max_items = max_items
        self.max_kinds = max_kinds
        # 단일 종 ×N 개수 오컴 (모듈 docstring, MODEL__JUDGMENT__STRICT_COUNT_OCCAM).
        # False = 구 동작 (롤백 센티널).
        self.count_occam = count_occam

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
        if self.count_occam:
            combos = self._occam_filter(combos)
        # combination_sort_key: -match_score → 종류 수 → 오차
        combos.sort(key=lambda c: (-c.match_score, len(c.products), c.weight_error))
        return combos

    @staticmethod
    def _occam_filter(combos: list[Combination]) -> list[Combination]:
        """단일 종 ×N 개수 오컴 (모듈 docstring): n=1 적합의 최소 잔차보다
        엄격히 더 잘 맞지 않는 단일 종 n≥2 적합을 실격. n=1 적합이 없으면
        (진짜 다량 취출) 무발동 — freezer `_occam_filter`와 동일 규칙."""
        best_single = min(
            (
                c.weight_error
                for c in combos
                if len(c.products) == 1 and c.products[0].count == 1
            ),
            default=None,
        )
        if best_single is None:
            return combos
        return [
            c
            for c in combos
            if len(c.products) > 1
            or c.products[0].count == 1
            or c.weight_error < best_single
        ]

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
