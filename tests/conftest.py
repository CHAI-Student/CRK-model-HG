import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from crk_model.core.types import ActiveProduct, VisionCandidate


@pytest.fixture
def cola():
    return ActiveProduct(
        "P001", "콜라", class_id=1, unit_weight=100.0, unit_price=1500, stock_qty=5
    )


@pytest.fixture
def water():
    return ActiveProduct("P002", "물", class_id=2, unit_weight=200.0, unit_price=1000, stock_qty=5)


@pytest.fixture
def bar170():
    return ActiveProduct(
        "P170", "아이스바170", class_id=3, unit_weight=170.0, unit_price=2000, stock_qty=5
    )


@pytest.fixture
def bar178():
    return ActiveProduct(
        "P178", "아이스바178", class_id=4, unit_weight=178.0, unit_price=2500, stock_qty=5
    )


def cand(class_id, conf=0.8, votes=10, ratio=0.5):
    return VisionCandidate(class_id, conf, votes, ratio)
