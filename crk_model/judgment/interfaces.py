"""판정 계층 인터페이스 (설계 v2 — 셀 단위 모델).

v1의 Stage/Strategy 15종 체인은 존 합산 delta의 부분집합 합 문제를 풀기 위한
것이었다. v2는 셀(로드셀 채널)당 한 상품 종류라는 전제로 문제 자체가 붕괴 —
판정은 셀별 독립이고 경로는 4개뿐이다 (README "추론 설계 v2").
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    CellOutcome,
    JudgmentResult,
    VisionCandidate,
)


@dataclass(frozen=True)
class JudgmentContext:
    zone: int
    profile: SensorProfile
    # ingest 산출 셀 관측 (resolved=False 상태) — 채널별 delta/segments/안정성
    cells: tuple[CellOutcome, ...]
    vision_candidates: tuple[VisionCandidate, ...]
    active_products: tuple[ActiveProduct, ...]
    # 셀 정체성 신념 (channel -> product_id) — CellBeliefStore가 확신한 셀만
    identities: Mapping[int, str] = field(default_factory=dict)
    vision_only: bool = False


@dataclass(frozen=True)
class JudgmentDecision:
    """판정 출력 — 존 집계(result)와 셀별 판정(cells)을 함께 반환한다.

    cells의 product_id는 신념 갱신 입력이다 (pipeline이 CellBeliefStore.observe).
    contradictions는 알려진 셀에서 비전+무게가 함께 다른 상품을 지목한 증거 —
    반복되면 신념 강등(재배치 자기 교정)으로 이어진다.
    """

    result: JudgmentResult
    cells: tuple[CellOutcome, ...]
    contradictions: tuple[tuple[int, str], ...] = ()  # (channel, rival_product_id)
