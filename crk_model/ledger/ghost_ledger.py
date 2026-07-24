"""세션 고스트 원장 — CLOSE 2차 패스 (0723 이슈 #17 P1: 옷 프린트 유령 표).

문제: 사람 옷에 프린트된 상품 유사 그래픽(실측 c13·c24)이 세션 내내 사람을
따라다니며 존마다 자격 표를 얻는다 — 사람이 움직이므로 변위 몰수를 통과하고,
표 수·conf도 진짜를 압도할 수 있다(10차 ses-3: c13 24표 conf 0.74 vs 진짜
c23 5표). **트리거 안에서는 진짜 취출과 구분 불가** — 구분 정보는 트리거
사이에 있다: 유령은 여러 존에서 반복 등장하면서 세션 전체에서 단 한 번도
무게의 뒷받침을 받지 못한다.

정의 (배치 사전정보 아님 — 이 세션에서 관측된 증거만, tray_memory와 동일 태도):

  ghost(c) ⇔ c가 서로 다른 존 ≥ min_zones의 removal 이벤트에서 자격 표
  (vote_count ≥ vote_floor)를 얻었고, 세션 내 어떤 무게 뒷받침 판정에도
  c가 없다.

  무게 뒷받침 = COMPLETE이고 reason에 refit/near_gate가 없는 판정의 과금
  (tray_memory 등록 게이트와 동일 원리 — PARTIAL·near_gate·refit은 무게가
  delta 전량 설명을 보증하지 않는 예외 경로라 뒷받침으로 안 친다. 실측
  ses-4-1784807732: 유령 c24가 z1에서 identity_partial로 과금됐지만 무게
  잔차 93g — 이런 과금은 뒷받침이 아니다).

held 실물(존A 취출 후 들고 존B 진입)은 존A에서 무게 뒷받침 과금을 받으므로
ghost가 아니다 — 그쪽은 cross-zone penalty 소관. 이 원장은 "어디서도 무게가
설명해 준 적 없는 정체성"만 잡는다.

CLOSE 시점에 두는 이유: 워터마크(F8)로 전 트리거 도착이 보장돼 세션 스코프
집계가 완결적이고, cross_zone 2차 패스와 같은 자리라 재판정 인프라(zero-GPU
재계산, F9)를 그대로 쓴다. 별도 세션 상태 저장소가 필요 없다 — 순수 함수.

알려진 위험 (shadow 승격 게이트에서 확인할 것): 진짜 상품이 다른 클래스에
과금을 빼앗기면 "무게 뒷받침 없음"이 되어 오플래그될 수 있다 (9차 ses-8의
c40 위상 — 2존 자격 + 뒷받침 0; 11차 ses-9의 3·27 — 오과금이 진짜의
뒷받침을 가로챔). 또한 side 카메라가 한 채널의 여러 존 트레이를 동시에
비추는 광학 구조상, **다른 에피소드라도 존 breadth가 독립 증거가 아닐 수
있다** (11차 ses-9: z5 반품 영상에 z3 진열 27이 잡힘). 에피소드 중복
제거(detect_ghosts)가 공유 영상 케이스는 걸러내지만 광학 공유는 남는
한계 — 승격 게이트의 정답 오플래그율이 이를 감시한다.
min_zones·vote_floor가 1차 방어이고,
active의 실제 개입은 soft 페널티(α) + COMPLETE 게이트 + 승자 유지 원칙
(cross_zone ⑤·⑥ 준용)으로 한정된다. 승격 절차는 held/tube shadow와 동일:
analyze-sessions 라벨 대조에서 정답 클래스 오플래그율 확인 후 active.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import ActiveProduct, JudgmentStatus
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.cross_zone import _penalize_candidates, _same_products
from crk_model.ledger.events import TriggerEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GhostLedgerConfig:
    # off | shadow | active — shadow는 검출·재판정 시뮬레이션을 notes로만
    # 남긴다 (동작 무변경). 승격은 라벨 실측 후 (모듈 docstring).
    mode: str = "shadow"
    # 유령 판정에 필요한 최소 존 수 — 1이면 단일 존 등장만으로 유령이 되어
    # 진짜 소수 표 후보까지 쓸리므로 금지 방향 (≥2 고정 권장)
    min_zones: int = 2
    # 존 등장으로 인정할 최소 자격 표 수 (저득표 스파이크 차단)
    vote_floor: int = 3
    # soft 페널티 계수 — cross_zone ALPHA와 같은 의미 (하드 제외 금지)
    alpha: float = 0.5


def _weight_backed_classes(events: Sequence[TriggerEvent]) -> set[int]:
    """세션 내 무게 뒷받침 과금을 받은 클래스 집합 (tray_memory 등록 게이트와
    동일 원리 — COMPLETE + refit/near_gate 아님)."""
    backed: set[int] = set()
    for e in events:
        if e.status != "ok" or e.judgment.status is not JudgmentStatus.COMPLETE:
            continue
        reason = e.judgment.reason or ""
        if "refit" in reason or "near_gate" in reason:
            continue
        for pc in e.judgment.products:
            backed.add(pc.product.class_id)
    return backed


def detect_ghosts(
    events: Sequence[TriggerEvent], cfg: GhostLedgerConfig
) -> dict[int, tuple[int, ...]]:
    """{ghost class_id: 자격 표를 얻은 존 튜플} — 정의는 모듈 docstring.

    에피소드 중복 제거 (11차 실측 정정): 동시·연쇄 취출의 존 트리거들은
    **연장 병합된 같은 에피소드 영상**을 공유해 후보 집합이 동일하다 (11차
    ses-2: z5/z3 트리거의 candidates가 완전히 같음 — 모든 클래스가 공짜로
    "2존 등장"이 되어 정답 27·30이 오플래그됐다). 같은 영상은 존 breadth
    증거가 될 수 없으므로, 서로 다른 **에피소드(video_paths)** ≥ 2에서
    등장한 클래스만 유령 후보로 남긴다 (analyze-sessions 트랙릿 집계의
    detail 동일성 중복 제거와 같은 원리). video_paths 미기록(구 스키마)
    이벤트는 각각을 별개 에피소드로 취급 — 기존(보수적) 동작 유지."""
    zones_seen: dict[int, set[int]] = defaultdict(set)
    episodes_seen: dict[int, set] = defaultdict(set)
    for e in events:
        if e.status != "ok" or e.delta_weight >= 0:
            continue
        episode = e.video_paths if e.video_paths else ("__ts__", e.zone, e.ts)
        for c in e.vision_candidates:
            if c.class_id > 0 and c.vote_count >= cfg.vote_floor:
                zones_seen[c.class_id].add(e.zone)
                episodes_seen[c.class_id].add(episode)
    backed = _weight_backed_classes(events)
    return {
        cid: tuple(sorted(zs))
        for cid, zs in zones_seen.items()
        if len(zs) >= cfg.min_zones
        and len(episodes_seen[cid]) >= 2
        and cid not in backed
    }


def apply_ghost_demotion(
    events: Sequence[TriggerEvent],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    cfg: GhostLedgerConfig,
    notes: list[str],
    default_profile: SensorProfile = REFRIGERATOR,
    router: JudgmentRouter | None = None,
) -> list[TriggerEvent]:
    """CLOSE 2차 패스 — cross_zone 페널티보다 **먼저** 실행한다: active에서
    유령 후보를 전 이벤트에서 강등해 두면 cross_zone 재판정의 채택 후보에서도
    밀려난다 (10차 ses-11: 진짜 27을 강등한 뒤 유령 13을 채택한 사고의 차단).

    반환: 판정/후보가 교체된 이벤트를 포함한 새 리스트 (원본 불변, I8 notes)."""
    if cfg.mode == "off" or not active_products:
        return list(events)
    ghosts = detect_ghosts(events, cfg)
    if not ghosts:
        return list(events)
    notes.append(
        "ghost_classes:"
        + ",".join(
            f"class{cid}@z{'/'.join(str(z) for z in zones)}"
            for cid, zones in sorted(ghosts.items())
        )
    )
    router = router or JudgmentRouter()
    out: list[TriggerEvent] = []
    for e in events:
        replaced = _repass_event(
            e, ghosts, profiles, active_products, cfg, notes, default_profile, router
        )
        out.append(replaced if replaced is not None else e)
    return out


def _repass_event(
    e: TriggerEvent,
    ghosts: Mapping[int, tuple[int, ...]],
    profiles: Mapping[int, SensorProfile],
    active_products: Sequence[ActiveProduct],
    cfg: GhostLedgerConfig,
    notes: list[str],
    default_profile: SensorProfile,
    router: JudgmentRouter,
) -> TriggerEvent | None:
    if (
        e.status != "ok"
        or e.delta_weight >= 0
        or not e.vision_candidates
        or e.judgment.status is JudgmentStatus.ERROR
    ):
        return None
    penalized = {c.class_id for c in e.vision_candidates if c.class_id in ghosts}
    if not penalized:
        return None
    new_cands = _penalize_candidates(e, penalized, cfg.alpha)
    billed_ghosts = sorted(
        {
            pc.product.class_id
            for pc in e.judgment.products
            if pc.product.class_id in penalized
        }
    )
    if not billed_ghosts:
        # 과금엔 유령이 없는 이벤트 — active면 후속 패스(cross_zone 채택)가
        # 강등된 후보를 보도록 후보만 교체해 둔다. shadow는 무개입.
        return replace(e, vision_candidates=new_cands) if cfg.mode == "active" else None

    ctx = JudgmentContext(
        zone=e.zone,
        profile=profiles.get(e.zone, default_profile),
        delta_weight=e.delta_weight,
        segments=e.segments,
        vision_candidates=new_cands,
        active_products=tuple(active_products),
        vision_only=False,
    )
    rejudged = router.judge(ctx)
    ghost_part = ",".join(f"class{cid}" for cid in billed_ghosts)
    adoptable = (
        rejudged.status is JudgmentStatus.COMPLETE
        and rejudged.products
        and not _same_products(rejudged, e.judgment)
    )
    # 표기는 class 기반 — analyze-sessions가 GT 라벨(class_id)과 직접 대조한다
    would = (
        ",".join(f"class{pc.product.class_id}x{pc.count}" for pc in rejudged.products)
        if adoptable
        else "keep_original"
    )
    if cfg.mode == "shadow":
        notes.append(f"zone{e.zone}:ghost_shadow:billed={ghost_part}:would={would}")
        return None
    if not adoptable:
        # cross_zone ⑤·⑥ 준용: 게이트 실패는 관측 note, 페널티 후에도 유령이
        # 이기면 그대로 인정(무기록) — 어느 쪽이든 후보 강등은 남긴다.
        if rejudged.status is not JudgmentStatus.COMPLETE or not rejudged.products:
            notes.append(
                f"zone{e.zone}:ghost_demotion_gate_failed:keep_original:billed={ghost_part}"
            )
        return replace(e, vision_candidates=new_cands)
    notes.append(f"zone{e.zone}:ghost_demotion:billed={ghost_part}:adopted={would}")
    logger.info(
        "[GHOST] zone=%d rejudged: %s -> %s (ghosts=%s)",
        e.zone,
        [(pc.product.product_id, pc.count) for pc in e.judgment.products],
        [(pc.product.product_id, pc.count) for pc in rejudged.products],
        ghost_part,
    )
    return replace(
        e,
        judgment=replace(rejudged, reason=rejudged.reason + "+ghost_demotion"),
        vision_candidates=new_cands,
    )
