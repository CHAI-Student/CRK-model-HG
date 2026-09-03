"""교차존 비전 오염 페널티 — CLOSE 2차 패스 (docs/devdoc/design/cross_zone_penalty.md).

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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.events import TriggerEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrossZonePenaltyConfig:
    """카메라 계약 상수(replay/trigger)는 CRK-CAMERA 설정과 단일 소스 유지 —
    env(MODEL__CROSS_ZONE__*)로 조정한다. α·ε·θ 초기값은 Phase 1 계측으로
    보정 예정 (docs/devdoc/design/cross_zone_penalty.md §7)."""

    enabled: bool = False
    # 카메라 프리롤 (CRK-CAMERA replay_duration=4.0, 120프레임)
    replay_s: float = 4.0
    # change 후 저장 지속 (CRK-CAMERA trigger duration=4.0, 7c8395f)
    trigger_s: float = 4.0
    # IO-BOARD 감지 지연 마진 (§3.1): 폴링 0.8s + serial/SSE 지연 + 여유
    epsilon_s: float = 1.0
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


def windows_mutually_overlap(
    e1: TriggerEvent, e2: TriggerEvent, cfg: CrossZonePenaltyConfig
) -> bool:
    """두 이벤트의 오염 창이 **양방향**으로 겹치는가 — 같은 물리적 장면을
    공유한다는 판별. _mutual_exemptions(상호 강등 가드)와 ghost_ledger의
    에피소드 중복 제거(이슈 #22 ses-6)가 공유하는 단일 소스."""
    lo1, hi1 = contamination_window(e1, cfg)
    lo2, hi2 = contamination_window(e2, cfg)
    return any(lo1 <= t <= hi1 for t in sub_event_anchors(e2)) and any(
        lo2 <= t <= hi2 for t in sub_event_anchors(e1)
    )


def _penalty_sources(
    e: TriggerEvent,
    events: Sequence[TriggerEvent],
    cfg: CrossZonePenaltyConfig,
) -> tuple[dict[int, tuple[str, int, float]], list[str]]:
    """③ 페널티 소스 P(E): W(E)와 겹치는 타 존 서브이벤트의 귀속 상품 집합.

    반환: ({class_id: (product_id, source_zone, source_anchor)}, 침묵 진단)
    — 소스 이벤트가 무판정이거나 confidence < θ면 제외 (R1). 창이 겹쳤는데
    θ에서 탈락한 소스는 진단 목록으로 보고한다 (9차 ses-8: 전 경로가 조용히
    비면 페널티 미발동 원인을 아카이브만으로 알 수 없었다)."""
    lo, hi = contamination_window(e, cfg)
    sources: dict[int, tuple[str, int, float]] = {}
    low_conf: list[str] = []
    for other in events:
        if other.zone == e.zone or other.status != "ok":
            continue
        j = other.judgment
        overlapping = [t for t in sub_event_anchors(other) if lo <= t <= hi]
        if not overlapping:
            continue
        if not j.products or j.confidence < cfg.source_conf_min:
            low_conf.append(f"zone{other.zone}@{j.confidence:.2f}")
            continue
        for pc in j.products:
            if pc.product.class_id > 0 and pc.product.class_id not in sources:
                sources[pc.product.class_id] = (
                    pc.product.product_id, other.zone, overlapping[0]
                )
    return sources, low_conf


def _judgment_residual(e: TriggerEvent) -> float | None:
    """원 판정의 자기 delta 설명 잔차 |abs(delta) − Σ n·w| — 상호 강등 가드의
    비교 키. unit_weight 미기록(구 스키마)이면 None (비교 불가)."""
    if not e.judgment.products:
        return None
    expected = 0.0
    for pc in e.judgment.products:
        if pc.product.unit_weight <= 0:
            return None
        expected += pc.count * pc.product.unit_weight
    return abs(abs(e.delta_weight) - expected)


# 센서 보증 분해능 (C3): 5g 미만 잔차 차이는 물리적으로 무의미 — self-fit
# 비교에서 이보다 명확히 우세할 때만 "자기 무게가 대안을 선호한다"고 본다.
_SELF_FIT_MARGIN_G = 5.0

# 무겹침 침묵 진단(_near_miss_sources)의 보고 상한 — 이보다 먼 타 존
# 이벤트는 명백한 별개 에피소드라 침묵이 정상이고, 노트는 노이즈다.
_NO_OVERLAP_NOTE_HORIZON_S = 30.0


def _near_miss_sources(
    e: TriggerEvent, events: Sequence[TriggerEvent]
) -> list[str]:
    """침묵 진단 2종째 (이슈 #23 0806 ses-28): 소스 자격 이벤트(타 존·정상·
    판정 있음)가 세션에 있는데 **오염 창이 안 겹쳐** 페널티가 전혀 검토되지
    않은 경우의 관측 노트 재료. 창 폭이 앵커 ±(replay+ε)≈±5s라 순차 취출
    간격이 그보다 크면 상호 강등 가드·소스 겹침이 전부 불성립하는데,
    아카이브에는 아무 흔적이 없어 "cross_zone이 안 돈다"로 보였다.

    반환: ["zone3@dt=7.2s", ...] — 가장 가까운 앵커 간격, 근접순. 간격 >
    _NO_OVERLAP_NOTE_HORIZON_S는 제외 (명백한 별개 에피소드)."""
    anchors_e = sub_event_anchors(e)
    misses: list[tuple[float, int]] = []
    for other in events:
        if other.zone == e.zone or other.status != "ok":
            continue
        if not other.judgment.products:
            continue  # 무판정은 창이 겹쳐도 소스 자격이 없다 — 진단 무의미
        dt = min(abs(a - b) for a in anchors_e for b in sub_event_anchors(other))
        if dt <= _NO_OVERLAP_NOTE_HORIZON_S:
            misses.append((dt, other.zone))
    return [f"zone{z}@dt={dt:.1f}s" for dt, z in sorted(misses)]


def _self_fit_prefers_alternative(
    e: TriggerEvent,
    cid: int,
    active_products: Sequence[ActiveProduct],
    max_count: int = 6,
) -> bool:
    """상호 강등 가드의 면제 자격 검사 (10차 ses-1: z2 Δ-172.5가 자기 후보
    23(잔차 3.5)을 훨씬 잘 설명하는데도 존 간 잔차 비교만으로 공유 클래스
    27의 면제를 받아 27이 양존 중복 과금됐다).

    자기 존의 delta가 X(cid)보다 **다른 vision 후보**를 분해능 마진(C3, 5g)
    이상 명확히 잘 설명하면, 이 존은 X의 진짜 소스 claimant 자격이 없다 —
    잔차 크기와 무관하게 면제에서 밀려난다. 다품목 판정은 잔차 비교가
    단일 종 대안과 이종 비교라 중립(False) — 기존 동작 유지."""
    if len(e.judgment.products) != 1:
        return False
    r_x = _judgment_residual(e)
    if r_x is None:
        return False
    target = abs(e.delta_weight)
    candidate_classes = {c.class_id for c in e.vision_candidates} - {cid}
    best_alt: float | None = None
    for p in active_products:
        if p.class_id not in candidate_classes or p.stock_qty <= 0 or p.unit_weight <= 0:
            continue
        for n in range(1, min(p.stock_qty, max_count) + 1):
            r = abs(target - n * p.unit_weight)
            if best_alt is None or r < best_alt:
                best_alt = r
    return best_alt is not None and best_alt + _SELF_FIT_MARGIN_G <= r_x


def _captive_only_zones(events: Sequence[TriggerEvent]) -> dict[int, set[int]]:
    """class_id → 그 클래스를 **유일한** 비전 후보로 들고 있는 zone 집합.

    session42 실사고: zone1(진짜 소스)·zone2·zone3이 모두 소바바치킨(67) 하나만
    후보로 들고 있었다(FOV 겹침 유출). 이 경우 zone2/zone3처럼 소스 외에도
    같은 클래스만 유일 후보로 든 '피해 zone'이 2곳 이상이면, 이는 인접 zone이
    우연히 같은 상품을 진짜로도 팔았을 개연성보다 순수 유출일 개연성이
    압도적으로 크다 — soft 페널티의 '경쟁 후보 없는 단독 후보는 신뢰'
    원칙(2-zone 가정)이 깨지는 지점이다."""
    out: dict[int, set[int]] = defaultdict(set)
    for e in events:
        if e.status == "ok" and len(e.vision_candidates) == 1:
            out[e.vision_candidates[0].class_id].add(e.zone)
    return out


# 지문 일치 억제(ses-34/35, ses-36/37) 판정 여유. 확신도는 여러 소수 자리까지
# 일치해야 하는 강한 신호라 오차를 거의 안 둔다(ses-36/37 70: 두 zone
# confidence가 0.9902377456426621로 13자리까지 동일 — 우연으론 불가능한
# 수준). 표는 카메라별 프레임 수 차이로 자연 변동이 있어 상대 오차(10%)로
# 넓히되, 확신도 정밀 일치를 필수 조건으로 둬서 남발을 막는다(ses-36/37
# 70: 표 60 vs 58, 3.4% 차이 — 절대 오차 1표로는 못 잡았다).
_FINGERPRINT_VOTE_RATIO_TOLERANCE = 0.1
_FINGERPRINT_CONF_TOLERANCE = 1e-6


def _same_fingerprint(a: VisionCandidate, b: VisionCandidate) -> bool:
    if abs(a.confidence - b.confidence) > _FINGERPRINT_CONF_TOLERANCE:
        return False
    vote_tolerance = max(1, round(_FINGERPRINT_VOTE_RATIO_TOLERANCE * max(a.vote_count, b.vote_count)))
    return abs(a.vote_count - b.vote_count) <= vote_tolerance


def _trial_replacement_valid(
    e: TriggerEvent,
    cid: int,
    active_products: Sequence[ActiveProduct],
    profile: SensorProfile,
    router: JudgmentRouter,
) -> bool:
    """cid를 뺀 후보로 재판정했을 때, 개수 슬랙 없이도(base count_gate) 딱
    맞는 COMPLETE 대체가 있는가 — ses-3 zone1/zone5 66 실사고: 진짜 66을
    가진 zone도 66을 빼면 남은 후보로 억지로 스케일된 조합(70×2, 잔차가
    n-스케일 게이트 경계에 겨우 걸침)을 만들어낼 수 있어, "대체가 되는지
    여부"만으로는 진짜/가짜를 못 가른다. base gate(개수 슬랙 미적용)로
    엄격히 걸러 억지 적합을 배제해야 진짜 대체(자기 상품이 따로 있다는
    독립 증거)만 남는다."""
    trial = router.judge(
        JudgmentContext(
            zone=e.zone,
            profile=profile,
            delta_weight=e.delta_weight,
            segments=e.segments,
            vision_candidates=tuple(
                c for c in e.vision_candidates if c.class_id != cid
            ),
            active_products=tuple(active_products),
            vision_only=False,
        )
    )
    if trial.status is not JudgmentStatus.COMPLETE or not trial.products:
        return False
    residual = abs(
        abs(e.delta_weight)
        - sum(pc.count * pc.product.unit_weight for pc in trial.products)
    )
    return residual <= profile.count_gate


def _fingerprint_duplicate_suppression(
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    notes: list[str],
    default_profile: SensorProfile,
    router: JudgmentRouter,
) -> list[TriggerEvent]:
    """서로 다른 zone에서 같은 클래스가 표·확신도까지 거의 완전히 동일하게
    청구되는 경우(ses-34/35의 71, ses-36/37의 70 — zone3·zone4 양쪽에 토씨
    하나 안 틀리게 잡힘) — 인접 zone이 실제로 같은 상품을 또 팔았다고 보기엔
    후보 지문이 지나치게 정확히 일치해 카메라 화각 유출로 본다.

    _captive_only_zones(session42)와 달리 후보가 여러 개 있는(비-captive)
    zone에도 적용된다. 중복 클래스를 제거한 후보 풀로 재판정해 COMPLETE
    대체가 있으면 채택하고, 없으면 잔차가 더 나쁜 쪽의 중복 청구만
    제거한다. _repass_event의 재판정 결과(out)에 최종 한 번만 적용해 기존
    재판정 로직은 건드리지 않는다."""
    zone_billing: dict[int, dict[int, TriggerEvent]] = defaultdict(dict)
    for e in events:
        if e.status != "ok" or e.judgment.status is not JudgmentStatus.COMPLETE:
            continue
        for pc in e.judgment.products:
            if pc.product.class_id > 0:
                zone_billing[pc.product.class_id].setdefault(e.zone, e)

    to_strip: dict[int, set[int]] = defaultdict(set)  # id(event) -> class_ids
    for cid, per_zone in zone_billing.items():
        if len(per_zone) < 2:
            continue  # 서로 다른 zone 2곳 이상에서 청구된 경우만 대상
        fingerprint: dict[int, VisionCandidate] = {}
        counts: dict[int, int] = {}
        for zone, e in per_zone.items():
            for c in e.vision_candidates:
                if c.class_id == cid:
                    fingerprint[zone] = c
                    break
            for pc in e.judgment.products:
                if pc.product.class_id == cid:
                    counts[zone] = pc.count
                    break
        matched: set[int] = set()
        zones = sorted(fingerprint)
        for i, z1 in enumerate(zones):
            for z2 in zones[i + 1 :]:
                # 개수까지 같아야 진짜 유출 지문 — 개수가 다르면 각 zone이
                # 서로 다른 실제 판매를 봤을 개연성이 있어 손대지 않는다
                # (ses-8 상호 강등 fixture: 같은 후보 목록을 재사용했지만
                # zone1=2개·zone2=1개로 실제로는 다른 판매였다).
                if counts.get(z1) != counts.get(z2):
                    continue
                if _same_fingerprint(fingerprint[z1], fingerprint[z2]):
                    matched.add(z1)
                    matched.add(z2)
        if len(matched) < 2:
            continue
        survivors = [per_zone[z] for z in matched]
        residuals = {
            id(e): (r if (r := _judgment_residual(e)) is not None else float("inf"))
            for e in survivors
        }
        ranked = sorted(survivors, key=lambda e: residuals[id(e)])
        best = ranked[0]
        # 잔차 근소(≤ 5g, 센서 분해능 마진) 시엔 무게만으로 진짜를 못 가른다
        # (ses-3 zone1=5g vs zone5=0g 66 실사고: 잔차가 더 작은 zone5를
        # "진짜"로 보고 실제로 맞았던 zone1을 지워버렸다). 이때만 제거 후
        # 대체 판정을 시도해, 대체가 안 되는(=자기 상품일 개연성이 큰) 쪽을
        # 남긴다. 잔차가 뚜렷이 갈리는 대다수 케이스는 추가 판정 없이 기존
        # 그대로 — 계산량 증가는 이 근소 동률 구간에만 국한된다.
        if (
            len(ranked) >= 2
            and residuals[id(ranked[1])] - residuals[id(best)] <= _SELF_FIT_MARGIN_G
        ):
            non_replaceable = [
                e for e in survivors
                if not _trial_replacement_valid(
                    e, cid, active_products,
                    profiles.get(e.zone, default_profile), router,
                )
            ]
            if len(non_replaceable) == 1:
                best = non_replaceable[0]
        for e in survivors:
            if e is not best:
                to_strip[id(e)].add(cid)

    if not to_strip:
        return list(events)

    out: list[TriggerEvent] = []
    for e in events:
        strip = to_strip.get(id(e))
        if not strip:
            out.append(e)
            continue
        remaining = tuple(
            pc for pc in e.judgment.products if pc.product.class_id not in strip
        )
        removed = ",".join(
            f"class{pc.product.class_id}"
            for pc in e.judgment.products
            if pc.product.class_id in strip
        )
        notes.append(
            f"zone{e.zone}:cross_zone_fingerprint_duplicate_suppressed:{removed}"
        )
        if remaining:
            judgment = replace(
                e.judgment,
                products=remaining,
                reason=e.judgment.reason + "+cross_zone_fingerprint_duplicate",
            )
        else:
            profile = profiles.get(e.zone, default_profile)
            replacement = router.judge(
                JudgmentContext(
                    zone=e.zone,
                    profile=profile,
                    delta_weight=e.delta_weight,
                    segments=e.segments,
                    vision_candidates=tuple(
                        candidate
                        for candidate in e.vision_candidates
                        if candidate.class_id not in strip
                    ),
                    active_products=tuple(active_products),
                    vision_only=False,
                )
            )
            if replacement.status is JudgmentStatus.COMPLETE and replacement.products:
                adopted = ",".join(
                    f"{pc.product.product_id}x{pc.count}"
                    for pc in replacement.products
                )
                notes.append(
                    f"zone{e.zone}:cross_zone_fingerprint_duplicate_replaced:"
                    f"removed={removed}:adopted={adopted}"
                )
                judgment = replace(
                    replacement,
                    reason=replacement.reason + "+cross_zone_fingerprint_duplicate",
                )
            else:
                judgment = JudgmentResult(
                    JudgmentStatus.NO_DETECTION,
                    confidence=0.0,
                    reason="cross_zone_fingerprint_duplicate_suppressed",
                )
        out.append(replace(e, judgment=judgment))
    return out


def _mutual_exemptions(
    events: Sequence[TriggerEvent],
    cfg: CrossZonePenaltyConfig,
    active_products: Sequence[ActiveProduct] = (),
) -> set[tuple[int, int]]:
    """상호 강등 가드 (8차 ses-3): 두 존이 같은 정체성 X를 판정했고 오염
    창이 **양방향**으로 겹치면, 각자가 상대를 소스로 X를 강등해 X가 정산에서
    통째로 소멸한다 — 오염 가설("X는 상대 존 취출이 비쳐서 잡혔다")은 소스
    존이 X를 유지해야 성립하므로 자기모순이다 (실사고: z2 46 잔차 1 ·
    z3 46 잔차 14가 서로를 강등해 둘 다 30 채택 → 맞던 z2까지 오답).

    해소: 무게 잔차가 더 정확한 쪽이 X의 진짜 소스 — 그 존은 X 페널티를
    면제한다(원 판정 유지). 잔차 동률·비교 불가면 양쪽 다 면제 — 무게가
    판별하지 못하면 개입하지 않는다는 ④와 같은 태도.

    반환: {(zone, class_id)} — _repass_event가 penalized에서 제외한다.
    단방향 오염(한쪽만 X 판정)은 집합에 들어가지 않아 기존 동작 그대로다."""
    exempt: set[tuple[int, int]] = set()
    valid = [
        e for e in events
        if e.status == "ok"
        and e.judgment.products
        and e.judgment.confidence >= cfg.source_conf_min
    ]
    for i, e1 in enumerate(valid):
        for e2 in valid[i + 1:]:
            if e1.zone == e2.zone:
                continue
            shared = {
                pc.product.class_id for pc in e1.judgment.products
            } & {pc.product.class_id for pc in e2.judgment.products}
            shared = {cid for cid in shared if cid > 0}
            if not shared:
                continue
            if not windows_mutually_overlap(e1, e2, cfg):
                continue  # 양방향 겹침이 아니면 상호 강등이 성립하지 않는다
            r1, r2 = _judgment_residual(e1), _judgment_residual(e2)
            for cid in shared:
                # self-fit 자격 (10차 ses-1): 자기 delta가 다른 후보를 명확히
                # 선호하는 존은 X의 claimant 자격이 없다 — 자격 있는 존이
                # 잔차 비교 없이 이긴다. 둘 다 무자격이면 기존 잔차 비교로
                # 폴백 (상호 소멸 방지 불변식 유지 — 8차 ses-3).
                unfit1 = _self_fit_prefers_alternative(e1, cid, active_products)
                unfit2 = _self_fit_prefers_alternative(e2, cid, active_products)
                if unfit1 != unfit2:
                    exempt.add(((e2 if unfit1 else e1).zone, cid))
                elif r1 is None or r2 is None or r1 == r2:
                    exempt.add((e1.zone, cid))
                    exempt.add((e2.zone, cid))
                elif r1 < r2:
                    exempt.add((e1.zone, cid))
                else:
                    exempt.add((e2.zone, cid))
    return exempt


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
    exempt = _mutual_exemptions(events, cfg, active_products)
    captive = _captive_only_zones(events)
    out: list[TriggerEvent] = []
    for e in events:
        replaced = _repass_event(
            e, events, profiles, active_products, cfg, notes, default_profile,
            router, exempt, captive,
        )
        out.append(replaced if replaced is not None else e)
    return _fingerprint_duplicate_suppression(
        out, profiles, active_products, notes, default_profile, router
    )


def _repass_event(
    e: TriggerEvent,
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    cfg: CrossZonePenaltyConfig,
    notes: list[str],
    default_profile: SensorProfile,
    router: JudgmentRouter,
    exempt: set[tuple[int, int]] = frozenset(),
    captive: Mapping[int, set[int]] | None = None,
) -> TriggerEvent | None:
    # 대상: 정상 removal 판정 + vision 후보 보유 (반품·에러·무후보는 무관)
    if (
        e.status != "ok"
        or e.delta_weight >= 0
        or not e.vision_candidates
        or e.judgment.status is JudgmentStatus.ERROR
    ):
        return None
    sources, low_conf = _penalty_sources(e, events, cfg)
    if not sources and low_conf:
        # 침묵 진단 (9차 ses-8): 오염 창은 겹쳤지만 소스 판정 conf가 θ 미만
        # — 페널티 미발동 사유를 아카이브에 남긴다 (동작 무변경).
        notes.append(
            f"zone{e.zone}:cross_zone_source_low_conf:" + ",".join(low_conf)
        )
    elif not sources:
        # 침묵 진단 2종째 (이슈 #23 0806 ses-28): 소스 자격 이벤트는 있는데
        # 오염 창(±5s)이 안 겹침 — "켜져 있는데 왜 안 도나"를 아카이브만으로
        # 판별하게 한다 (동작 무변경, 근접 30s 이내만 보고).
        near = _near_miss_sources(e, events)
        if near:
            notes.append(
                f"zone{e.zone}:cross_zone_no_overlap:" + ",".join(near)
            )
    penalized = {c.class_id for c in e.vision_candidates if c.class_id in sources}
    guarded = {cid for cid in penalized if (e.zone, cid) in exempt}
    if guarded:
        # 상호 강등 가드 (_mutual_exemptions) — 이 존이 X의 진짜 소스로
        # 판별됐거나 무게가 판별 불가한 경우. 사유는 notes로 관측 가능.
        penalized -= guarded
        notes.append(
            f"zone{e.zone}:cross_zone_mutual_exempt:"
            + ",".join(f"class{cid}" for cid in sorted(guarded))
        )
    if not penalized:
        return None  # 오염 창 겹침 없음 또는 후보와 무관 — 기존 동작과 동일
    billed_classes = {pc.product.class_id for pc in e.judgment.products}
    if (
        e.judgment.status is JudgmentStatus.COMPLETE
        and billed_classes
        and not (penalized & billed_classes)
    ):
        # 0826 실기(ses-3 zone2): 오염 후보가 실제 과금 상품과 무관하면(예:
        # multi_tray로 이미 여러 상품을 나눠 잡은 zone인데, 오염 클래스가 그중
        # 어느 것도 아님) 재판정 자체가 불필요 — 지금 재판정은 트리거의
        # 다중 채널/세그먼트 분리 결과를 통째로 버리고 zone 전체 무게를
        # 단일 후보로 다시 계산하므로, 손댈 필요가 없는데 재판정하면 이미
        # 맞았던 다중 상품 결과가 단일 상품으로 뭉개질 위험만 있다. PARTIAL은
        # 기존 경로(재판정 후 무변화 확인)가 이미 안전하게 처리하므로 그대로
        # 둔다 — COMPLETE만 최적화한다.
        return None
    profile = profiles.get(e.zone, default_profile)
    # ④의 KEEP 전제("무게가 유일 해 → 기존 무게 매칭이 이미 방어했다")는 원
    # 판정이 COMPLETE(무게 검증 통과)일 때만 참이다 — 이슈 #22 ses-4 z3:
    # 무게 무검증 relaxed_partial이 오염 후보(단위무게 525g)를 Δ-80g에
    # 과금했는데 ④가 침묵 KEEP해 재판정 기회 자체가 없었다. PARTIAL 원
    # 판정은 ④를 건너뛰고 재판정으로 — ⑥ COMPLETE 게이트가 여전히
    # "보정하려다 더 나빠지는" 경로를 막는다 (R2).
    robust_alternative = _robust_alternative_partial(
        e, penalized, active_products, profile
    )
    if (
        e.judgment.status is JudgmentStatus.COMPLETE
        and not _weight_ambiguous(e, active_products, profile)
        and robust_alternative is None
    ):
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
    # 방지 (R2). 단, 원 판정이 PARTIAL이고 실제 과금 상품이 오염 클래스로
    # 확인된 경우에는 이 전제가 성립하지 않는다. 무게 미검증 오염 과금을
    # 보호하는 대신 오염 상품만 제거한다 (COMPLETE 보호 정책은 무변경).
    if rejudged.status is not JudgmentStatus.COMPLETE or not rejudged.products:
        if robust_alternative is not None:
            notes.append(
                f"zone{e.zone}:cross_zone_robust_alternative:"
                f"adopted={robust_alternative.products[0].product.product_id}x1:"
                f"source={src_part}"
            )
            return replace(e, judgment=robust_alternative)
        contaminated = tuple(
            pc for pc in e.judgment.products if pc.product.class_id in penalized
        )
        # 페널티 후에도 같은 상품 PARTIAL이 다시 이기면 인접 존에서 실제로
        # 같은 상품을 취출했을 가능성이 남는다 — soft 페널티의 "이기면 인정"
        # 원칙에 따라 보존. 후보가 사라지거나 다른 PARTIAL로 바뀐 경우만 원
        # 오염 과금을 제거한다.
        same_partial_survived = bool(rejudged.products) and _same_products(
            rejudged, e.judgment
        )
        # 3자 이상 유출 강제 억제 (session42): 이 zone이 오염 클래스를 유일
        # 후보로만 들고 있고, 같은 소스에서 유출된 '피해 zone'이 이 zone
        # 말고도 하나 더 있으면 — soft 페널티가 이겨도(경쟁 후보가 없어
        # 당연히 이김) 인접 zone 실판매 개연성보다 순수 유출 개연성이 훨씬
        # 크다고 본다. 2-zone(소스 1 + 피해 1) 케이스는 기존 동작 유지.
        forced_suppress = False
        if same_partial_survived and captive:
            for pc in contaminated:
                cid = pc.product.class_id
                source_zone = sources[cid][1] if cid in sources else None
                victims = captive.get(cid, set()) - {source_zone}
                if e.zone in victims and len(victims) >= 2:
                    forced_suppress = True
                    break
        if (
            e.judgment.status is JudgmentStatus.PARTIAL
            and contaminated
            and (not same_partial_survived or forced_suppress)
        ):
            remaining = tuple(
                pc
                for pc in e.judgment.products
                if pc.product.class_id not in penalized
            )
            suppressed = ",".join(
                pc.product.product_id for pc in contaminated
            )
            notes.append(
                f"zone{e.zone}:cross_zone_penalty_gate_failed:"
                f"suppress_contaminated_partial={suppressed}:source={src_part}"
            )
            if remaining:
                judgment = replace(
                    e.judgment,
                    products=remaining,
                    reason=e.judgment.reason + "+cross_zone_contamination_pruned",
                )
            else:
                judgment = JudgmentResult(
                    JudgmentStatus.NO_DETECTION,
                    confidence=0.0,
                    reason="cross_zone_contaminated_partial_suppressed",
                )
            return replace(e, judgment=judgment)
        notes.append(
            f"zone{e.zone}:cross_zone_penalty_gate_failed:keep_original:source={src_part}"
        )
        return None
    if _same_products(rejudged, e.judgment) and robust_alternative is None:
        return None  # 페널티 후에도 오염 후보가 이김 — 그대로 인정 (⑤)
    if _same_products(rejudged, e.judgment) and robust_alternative is not None:
        notes.append(
            f"zone{e.zone}:cross_zone_robust_alternative:"
            f"adopted={robust_alternative.products[0].product.product_id}x1:"
            f"source={src_part}"
        )
        return replace(e, judgment=robust_alternative)

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


def _robust_alternative_partial(
    e: TriggerEvent,
    penalized: set[int],
    active_products: Sequence[ActiveProduct],
    profile: SensorProfile,
) -> JudgmentResult | None:
    """오염 후보를 제외한 경쟁 단품을 상품 편차 범위에서 보존한다."""
    if (
        not profile.weight_is_discriminative
        or e.judgment.status not in (JudgmentStatus.COMPLETE, JudgmentStatus.PARTIAL)
        or not any(pc.count > 1 for pc in e.judgment.products)
    ):
        return None
    candidates = {candidate.class_id: candidate for candidate in e.vision_candidates}
    source_votes = max(
        (candidates[cid].vote_count for cid in penalized if cid in candidates),
        default=0,
    )
    if source_votes <= 0:
        return None
    products = {product.class_id: product for product in active_products}
    target = abs(e.delta_weight)
    tolerance = profile.tolerance_grams * 3.0
    alternatives = [
        (candidate, products[candidate.class_id])
        for candidate in e.vision_candidates
        if candidate.class_id not in penalized
        and candidate.class_id in products
        and products[candidate.class_id].stock_qty > 0
        and products[candidate.class_id].unit_weight > 0
        and abs(target - products[candidate.class_id].unit_weight) <= tolerance
    ]
    if not alternatives:
        return None
    candidate, product = max(
        alternatives, key=lambda item: (item[0].vote_count, item[0].confidence)
    )
    return JudgmentResult(
        JudgmentStatus.PARTIAL,
        (ProductCount(product, 1),),
        confidence=candidate.confidence * 0.5,
        reason="cross_zone_robust_alternative_partial",
    )


def _same_products(a, b) -> bool:
    """두 판정의 (product_id, count) 집합이 동일한지 비교 (⑤ 재판정 무변화 판별)."""

    def key(j):
        return sorted((pc.product.product_id, pc.count) for pc in j.products)

    return key(a) == key(b)
