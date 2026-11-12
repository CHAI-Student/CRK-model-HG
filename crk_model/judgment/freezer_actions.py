"""냉동 행동별 판정: 영상이 지목한 종류를 보존하고 무게로 수량만 검증한다."""
from __future__ import annotations

from collections.abc import Sequence
from itertools import product

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)


def infer_freezer_action(
    delta: float,
    candidates: Sequence[VisionCandidate],
    products: Sequence[ActiveProduct],
    profile: SensorProfile,
    *,
    count_unit_slack: float = 5.0,
) -> JudgmentResult:
    """방향별로 확인된 종류 전부를 설명하는 수량 가설이 유일할 때만 확정.

    같은 무게의 A×2와 A+B 중 선택권은 영상에 있다. 후보 종류를 무게에
    맞추어 빼거나 추가하지 않는다. 서로 다른 수량 배분이 모두 가능하면 보류.
    탐색 상한은 기존 냉동 판정과 같은 4종/12개이며 별도 env를 만들지 않는다.
    """
    def unresolved(reason: str) -> JudgmentResult:
        return JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason=reason, strategy="freezer_action"
        )

    if not candidates:
        return unresolved("freezer_action_missing_identity")
    ids = {c.class_id for c in candidates}
    identities = [p for p in products if p.class_id in ids and p.unit_weight > 0]
    if len(ids) > 4 or any(sum(p.class_id == cid for p in identities) != 1 for cid in ids):
        return unresolved("freezer_action_ambiguous_mapping")
    target = abs(delta)
    limits = [
        min(12, p.stock_qty if delta < 0 else 12, int(target / p.unit_weight) + 1)
        for p in identities
    ]
    fits = []
    for counts in product(*(range(1, limit + 1) for limit in limits)):
        total = sum(counts)
        if total > 12:
            continue
        weight = sum(p.unit_weight * n for p, n in zip(identities, counts, strict=True))
        if abs(target - weight) <= profile.count_gate + count_unit_slack * (total - 1):
            fits.append(counts)
            if len(fits) > 1:
                return unresolved("freezer_action_ambiguous_count")
    if not fits:
        return unresolved("freezer_action_weight_mismatch")
    return JudgmentResult(
        JudgmentStatus.COMPLETE,
        tuple(ProductCount(p, n) for p, n in zip(identities, fits[0], strict=True)),
        confidence=min(c.confidence for c in candidates),
        reason="freezer_action_identity_verified",
        strategy="freezer_action",
    )
