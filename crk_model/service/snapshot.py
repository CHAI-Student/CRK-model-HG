"""ActiveProductStore — 재고 스냅샷 (I2, QA Q10).

I2: 빈 allowlist에서 추론 강행하면 판매 중이 아닌 상품을 청구할 수 있음 →
fail-closed 차단 + snapshot_source=last_valid 폴백. OPEN마다 스냅샷 갱신
(다이어그램 10 Active 노트).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from crk_model.core.types import ActiveProduct


@dataclass(frozen=True)
class ProductSnapshot:
    products: tuple[ActiveProduct, ...]
    source: str  # "current" | "last_valid" | "empty"

    @property
    def inference_allowed(self) -> bool:
        return self.source != "empty"  # I2: fail-closed


class ActiveProductStore:
    def __init__(self) -> None:
        self._current: tuple[ActiveProduct, ...] = ()
        self._last_valid: tuple[ActiveProduct, ...] = ()

    def update(self, products: Sequence[ActiveProduct]) -> None:
        self._current = tuple(products)
        if products:
            self._last_valid = tuple(products)

    def snapshot(self) -> ProductSnapshot:
        if self._current:
            return ProductSnapshot(self._current, "current")
        if self._last_valid:
            return ProductSnapshot(self._last_valid, "last_valid")  # I2 폴백
        return ProductSnapshot((), "empty")  # I2 차단
