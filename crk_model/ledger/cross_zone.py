"""교차존 비전 오염 페널티 — CLOSE 2차 패스 (docs/0711_idea.md).

문제: zone1 세션 유지 중 zone2 취출이 일어나면 zone2 판별용 AVI의 프리롤
(4s)·라이브 구간에 zone1 취출 장면이 물리적으로 섞인다 (F3). zone2의
loadcell은 존별 슬라이스라 오염되지 않으므로 (F4) 조정 대상은 비전 점수뿐.

온라인 순차 처리가 불가한 이유 (F5): 연장 병합된 zone1 POST가 zone2 POST보다
늦게 도착하는 역전이 구조적으로 존재 → 확정 페널티는 워터마크(F8)로 전
트리거 도착이 보장되는 CLOSE 시점에만 적용한다. 잠정 판정은 손대지 않고
FinalizedSettlement만 보정 (I10 정합).

재판정은 zero-GPU: TriggerEvent.vision_candidates가 채택 안 된 후보까지
보존하므로 (F9) 순수 CPU 재계산이다.

안전장치 3중 방어 (R1):
- 소스 신뢰도 게이트: 무판정/저신뢰(confidence < θ) 소스 제외 (오판 전파 차단)
- 무게 모호성 게이트: 무게만으로 후보를 가릴 수 있으면 페널티 미발동
  (무게 단서 > 비전 페널티)
- soft 페널티: vote_ratio/vote_count/confidence × α — 하드 제외 금지.
  인접 존이 실제로 같은 상품을 팔 수 있으므로, 페널티 후에도 오염 후보가
  이기면 그대로 인정한다.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import ActiveProduct, JudgmentStatus
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.events import TriggerEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrossZonePenaltyConfig:
    """카메라 계약 상수(replay/trigger)는 CRK-CAMERA 설정과 단일 소스 유지 —
    env(MODEL__CROSS_ZONE__*)로 조정한다. α·ε·θ 초기값은 Phase 1 계측으로
    보정 예정 (docs/0711_idea.md §7)."""

    enabled: bool = False
    # 카메라 프리롤 (CRK-CAMERA replay_duration=4.0, 120프레임)
    replay_s: float = 4.0
    # change 후 저장 지속 (CRK-CAMERA trigger duration=4.0, 7c8395f)
    trigger_s: float = 4.0
    # IO-BOARD 감지 지연 마진 (폴링 0.099s + 필터 지연, §3.1)
    epsilon_s: float = 0.3
    # soft 페널티 계수 — 오염 후보의 vote_ratio/vote_count/confidence에 곱한다
    alpha: float = 0.5
    # θ: 페널티 소스로 인정할 최소 판정 신뢰도 (미만이면 오판 전파 차단, §4.2 ③)
    source_conf_min: float = 0.35


def sub_event_anchors(e: TriggerEvent) -> tuple[float, ...]:
    """① 서브이벤트 타임라인 (§4.2) — change_timestamps 우선, 구버전 카메라는
    segments.start_ts 폴백, 최후 폴백은 ts 단일 앵커. 세 경우 모두 IO-BOARD
    클럭 축(F7)이라 존 간 비교 가능. F6: 프레임 인덱스 기반 환산은 금지."""
    if e.change_timestamps:
        return e.change_timestamps
    if e.segments:
        return tuple(s.start_ts for s in e.segments)
    return (e.ts,)


def contamination_window(
    e: TriggerEvent, cfg: CrossZonePenaltyConfig
) -> tuple[float, float]:
    """② 오염 창 W(E) = [min(anchors)−REPLAY−ε, max(anchors)+TRIGGER+ε].
    보수적(넓은) 창이 안전 방향 (R4 — 프레임 드롭으로 실제 커버리지가 좁아도
    무해)."""
    anchors = sub_event_anchors(e)
    return (
        min(anchors) - cfg.replay_s - cfg.epsilon_s,
        max(anchors) + cfg.trigger_s + cfg.epsilon_s,
    )


def _penalty_sources(
    e: TriggerEvent,
    events: Sequence[TriggerEvent],
    cfg: CrossZonePenaltyConfig,
) -> dict[int, tuple[str, int, float]]:
    """③ 페널티 소스 P(E): W(E)와 겹치는 타 존 서브이벤트의 귀속 상품 집합.

    반환: {class_id: (product_id, source_zone, source_anchor)} — notes 기록용
    메타 포함. 소스 이벤트가 무판정이거나 confidence < θ면 제외 (R1)."""
    lo, hi = contamination_window(e, cfg)
    sources: dict[int, tuple[str, int, float]] = {}
    for other in events:
        if other.zone == e.zone or other.status != "ok":
            continue
        j = other.judgment
        if not j.products or j.confidence < cfg.source_conf_min:
            continue
        overlapping = [t for t in sub_event_anchors(other) if lo <= t <= hi]
        if not overlapping:
            continue
        for pc in j.products:
            if pc.product.class_id > 0 and pc.product.class_id not in sources:
                sources[pc.product.class_id] = (
                    pc.product.product_id, other.zone, overlapping[0]
                )
    return sources


def _weight_ambiguous(
    e: TriggerEvent,
    active_products: Sequence[ActiveProduct],
    profile: SensorProfile,
    max_count: int = 6,
) -> bool:
    """④ 무게 모호성 게이트 (핵심 안전장치): E의 |delta|를 게이트 내로 설명하는
    (상품, 개수) 해가 서로 다른 상품 2종 이상에서 성립하는가. 무게가 유일 해를
    지지하면 비전 페널티가 개입할 이유가 없다 — 기존 무게 매칭이 이미 방어.

    조작적 정의: vision 후보로 잡힌 상품별 단일 종 n개 설명만 센다 (다품종
    혼합 조합까지 세면 조합 폭발 — 오염 시나리오의 전형인 "w_A ≈ w_B 동률"은
    단일 종 비교로 충분히 잡힌다)."""
    target = abs(e.delta_weight)
    gate = (
        profile.tolerance_grams
        if profile.weight_is_discriminative
        else profile.count_gate
    )
    candidate_classes = {c.class_id for c in e.vision_candidates}
    explainable: set[str] = set()
    for p in active_products:
        if p.class_id not in candidate_classes or p.stock_qty <= 0 or p.unit_weight <= 0:
            continue
        for n in range(1, min(p.stock_qty, max_count) + 1):
            if abs(target - n * p.unit_weight) <= gate:
                explainable.add(p.product_id)
                break
    return len(explainable) >= 2


def _penalize_candidates(e: TriggerEvent, penalized: set[int], alpha: float):
    """⑤ soft 페널티: 오염 후보의 표·신뢰도를 α배로 강등 (하드 제외 금지).
    판정 전략들의 순위 키가 vote_count·confidence이므로 (vote_ratio만 낮추면
    무효) 세 필드를 함께 강등한다."""
    return tuple(
        replace(
            c,
            confidence=c.confidence * alpha,
            vote_count=int(c.vote_count * alpha),
            vote_ratio=c.vote_ratio * alpha,
        )
        if c.class_id in penalized
        else c
        for c in e.vision_candidates
    )


def apply_cross_zone_penalty(
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    cfg: CrossZonePenaltyConfig,
    notes: list[str],
    default_profile: SensorProfile = REFRIGERATOR,
    router: JudgmentRouter | None = None,
) -> list[TriggerEvent]:
    """CLOSE 2차 패스 (§4.1) — 오염 창이 겹치고 무게가 모호한 이벤트만 soft
    페널티로 재판정한다. 재판정이 게이트를 통과하지 못하면 원 판정 유지
    (⑥, R2 — "보정하려다 더 나빠지는" 경로 차단, I3 태도 준용).

    반환: 판정이 교체된 이벤트를 포함한 새 리스트 (원본 불변). 모든 보정은
    notes에 사유 코드 기록 (I8)."""
    if not cfg.enabled or not active_products:
        return list(events)
    router = router or JudgmentRouter()
    out: list[TriggerEvent] = []
    for e in events:
        replaced = _repass_event(
            e, events, profiles, active_products, cfg, notes, default_profile, router
        )
        out.append(replaced if replaced is not None else e)
    return out


def _repass_event(
    e: TriggerEvent,
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    cfg: CrossZonePenaltyConfig,
    notes: list[str],
    default_profile: SensorProfile,
    router: JudgmentRouter,
) -> TriggerEvent | None:
    # 대상: 정상 removal 판정 + vision 후보 보유 (반품·에러·무후보는 무관)
    if (
        e.status != "ok"
        or e.delta_weight >= 0
        or not e.vision_candidates
        or e.judgment.status is JudgmentStatus.ERROR
    ):
        return None
    sources = _penalty_sources(e, events, cfg)
    penalized = {c.class_id for c in e.vision_candidates if c.class_id in sources}
    if not penalized:
        return None  # 오염 창 겹침 없음 또는 후보와 무관 — 기존 동작과 동일
    profile = profiles.get(e.zone, default_profile)
    if not _weight_ambiguous(e, active_products, profile):
        return None  # ④ 무게가 유일 해 → 원 판정 유지 (KEEP)

    ctx = JudgmentContext(
        zone=e.zone,
        profile=profile,
        delta_weight=e.delta_weight,
        segments=e.segments,
        vision_candidates=_penalize_candidates(e, penalized, cfg.alpha),
        active_products=tuple(active_products),
        vision_only=False,
    )
    rejudged = router.judge(ctx)

    src_part = ",".join(
        f"zone{z}@{t:.3f}" for _, z, t in sorted(set(sources.values()))
    )
    # ⑥ 게이트: 재판정이 COMPLETE(라우터가 I6로 tolerance/count gate 통과를
    # 보장)가 아니면 원 판정 유지 — 페널티로 후보 전멸 → NO_DETECTION 전락
    # 방지 (R2).
    if rejudged.status is not JudgmentStatus.COMPLETE or not rejudged.products:
        notes.append(
            f"zone{e.zone}:cross_zone_penalty_gate_failed:keep_original:source={src_part}"
        )
        return None
    if _same_products(rejudged, e.judgment):
        return None  # 페널티 후에도 오염 후보가 이김 — 그대로 인정 (⑤)

    demoted = sorted(
        pc.product.product_id
        for pc in e.judgment.products
        if pc.product.class_id in penalized
    )
    adopted = ",".join(
        f"{pc.product.product_id}x{pc.count}" for pc in rejudged.products
    )
    notes.append(
        f"zone{e.zone}:cross_zone_vision_penalty:demoted={','.join(demoted) or '-'}"
        f":adopted={adopted}:source={src_part}"
    )
    logger.info(
        "[CROSS-ZONE] zone=%d rejudged: %s -> %s (sources=%s)",
        e.zone,
        [(pc.product.product_id, pc.count) for pc in e.judgment.products],
        [(pc.product.product_id, pc.count) for pc in rejudged.products],
        src_part,
    )
    return replace(
        e,
        judgment=replace(
            rejudged, reason=rejudged.reason + "+cross_zone_vision_penalty"
        ),
    )


def _same_products(a, b) -> bool:
    key = lambda j: sorted((pc.product.product_id, pc.count) for pc in j.products)
    return key(a) == key(b)
