"""냉동 CLOSE 보완 (2026-11-26, yoona.md #freezer-20261126).

2026-11-25 실기 6세션 검토에서 나온 정산 단계 보완이다. 모두 냉동 프로파일
(weight_is_discriminative=False)에만 적용하고, 조건이 맞지 않으면 이벤트를 그대로 둔다.
원본 이벤트(아카이브·보고용 weight_delta)는 바꾸지 않고 정산 입력 복사본만 바꾼다.

- ① split_tray_movements: 한 트리거 안 트레이 하나는 빠지고 하나는 늘었으면 취출·반납으로 나눈다.
- ⑥ suppress_partial_fingerprint_duplicates: 다른 층 COMPLETE 청구와 같은 영상 지문의
  무게 불일치 PARTIAL 재청구를 뺀다.
- ⑦ fold_contact_release: 반납 직후 같은 층의 작은 감소(손 떼기)를 반납 무게에 합친다.
- ⑧ recover_partial_count: 정체성만 보존한 count=1 PARTIAL의 개수를 층 최종 무게로 다시 센다.
- ④ masked_movement_zones: 순변화가 작아도 상품 크기 하강·상승이 모두 있던 층을 review로 올린다.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from crk_model.core.profiles import SensorProfile, return_tolerance
from crk_model.core.types import ActiveProduct, JudgmentResult, JudgmentStatus, WeightSegment
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.cross_zone import (
    CrossZonePenaltyConfig,
    _rejudge,
    _same_fingerprint,
    windows_mutually_overlap,
)
from crk_model.ledger.events import FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.freezer_carried_over import _weight_decisively_inconsistent


def _is_freezer(profiles: Mapping[int, SensorProfile], default: SensorProfile, zone: int) -> bool:
    return not profiles.get(zone, default).weight_is_discriminative


def _small_change(profile: SensorProfile) -> float:
    """추론 대상이 아닌 작은 변화 기준 (냉동 파일럿 50g)."""
    return profile.min_trigger_delta_grams or profile.min_weight_change_grams


def has_masked_movement(segments: Sequence[WeightSegment], floor: float) -> bool:
    """트리거 안에 상품 크기(floor 이상) 하강과 상승이 모두 있다 — 같은 층 취출과
    다른 상품 반납이 순변화에서 상쇄됐을 수 있다 (ses-2-1795594482 1층 −16.8g)."""
    return (any(s.delta_grams <= -floor for s in segments)
            and any(s.delta_grams >= floor for s in segments))


def split_tray_movements(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default: SensorProfile, floor: float, notes: list[str], *, count_unit_slack: float = 5.0,
) -> list[TriggerEvent]:
    """① 한 트리거 안에서 상품 크기 채널 하나는 빠지고 하나는 늘었으면(트레이가
    물리적으로 나뉨) 취출 이벤트와 반납 이벤트로 나눈다 (ses-3-1795592912 4층).

    양수 채널은 청구 대상이 아니므로 판정 상품은 취출 쪽에 둔다. 나머지 작은 채널
    변화는 취출 쪽 delta에 남겨 층 총합을 보존한다. 청구 상품이 빠진 채널 무게로
    개수 게이트 안에서 맞으면 COMPLETE로 올린다."""
    out: list[TriggerEvent] = []
    for e in events:
        m = e.freezer_movement
        if (e.status != "ok" or not _is_freezer(profiles, default, e.zone) or m is None
                or len(m.channel_deltas) < 2):
            out.append(e)
            continue
        neg = [i for i, d in enumerate(m.channel_deltas) if d <= -floor]
        pos = [i for i, d in enumerate(m.channel_deltas) if d >= floor]
        if len(neg) != 1 or len(pos) != 1:
            out.append(e)
            continue
        ni, pi = neg[0], pos[0]
        stable = len(m.stable_channel_deltas) == len(m.channel_deltas)
        take_delta = sum(d for i, d in enumerate(m.channel_deltas) if i != pi)
        ret_delta = m.channel_deltas[pi]
        products = e.judgment.products
        gate = profiles.get(e.zone, default).count_gate
        n = sum(pc.count for pc in products)
        expected = sum(pc.product.unit_weight * pc.count for pc in products)
        verified = (bool(products) and abs(-m.channel_deltas[ni] - expected)
                    <= gate + count_unit_slack * max(0, n - 1))
        status = (JudgmentStatus.COMPLETE if verified
                  else (e.judgment.status if products else JudgmentStatus.NO_DETECTION))
        take = replace(
            e, delta_weight=take_delta, freezer_return=None,
            judgment=replace(e.judgment, status=status,
                             reason="freezer_tray_split_take:" + (e.judgment.reason or "")),
            freezer_movement=replace(
                m, return_candidates=(), channel_deltas=(m.channel_deltas[ni],),
                stable_channel_deltas=(m.stable_channel_deltas[ni],) if stable else (),
            ),
        )
        evidence = e.freezer_return or FreezerReturnEvidence(
            m.return_candidates or e.vision_candidates, m.stabilized,
            "return_direction" if m.return_candidates else "video",
        )
        ret = replace(
            e, ts=e.ts + 1e-3, delta_weight=ret_delta, freezer_return=evidence,
            judgment=JudgmentResult(JudgmentStatus.NO_DETECTION,
                                    reason="freezer_tray_split_return"),
            freezer_movement=replace(
                m, take_candidates=(), channel_deltas=(ret_delta,),
                stable_channel_deltas=(m.stable_channel_deltas[pi],) if stable else (),
            ),
        )
        notes.append(f"zone{e.zone}:freezer_tray_split:"
                     f"take={take_delta:.1f}g:return={ret_delta:+.1f}g")
        out.extend((take, ret))
    return sorted(out, key=lambda ev: ev.ts)


def fold_contact_release(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default: SensorProfile, floor: float, notes: list[str],
) -> tuple[list[TriggerEvent], set[int]]:
    """⑦ 반납 바로 다음의 같은 층 이벤트가 청구 없는 작은 감소(상품 크기 미만)면
    반납 때 얹힌 손 접촉이 빠진 것으로 보고 반납 무게에 합친다
    (ses-3-1795594851 1층 +786.7g → −65g, 합계 +721.7g = 94). 합친 층은 review."""
    out = sorted(events, key=lambda ev: ev.ts)
    review: set[int] = set()
    for i, r in enumerate(out):
        if (r.status != "ok" or r.freezer_return is None or r.delta_weight <= 0
                or not _is_freezer(profiles, default, r.zone)):
            continue
        j = next((k for k in range(i + 1, len(out)) if out[k].zone == r.zone), None)
        if j is None:
            continue
        nxt = out[j]
        if (nxt.status != "ok" or nxt.delta_weight >= 0 or -nxt.delta_weight >= floor
                or nxt.judgment.products):
            continue
        folded = r.delta_weight + nxt.delta_weight
        if folded < floor:  # 합친 뒤에도 상품 크기여야 한다
            continue
        notes.append(f"zone{r.zone}:freezer_return_contact_release:"
                     f"release={nxt.delta_weight:+.1f}g:return={folded:+.1f}g")
        # 끝점 측정(movement)은 손 접촉이 남은 값이라 합친 무게와 함께 쓰지 않는다.
        out[i] = replace(r, delta_weight=folded, freezer_movement=None)
        out[j] = replace(nxt, delta_weight=0.0, judgment=JudgmentResult(
            JudgmentStatus.NO_DETECTION, reason="freezer_contact_release_folded"))
        review.add(r.zone)
    return out, review


def masked_movement_zones(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default: SensorProfile, floor: float, notes: list[str],
) -> set[int]:
    """④ 순변화가 작아 청구가 없는 트리거에 상품 크기 하강·상승이 모두 있으면
    같은 층 안에서 취출·반납이 상쇄됐을 수 있다 — 그 층에 청구가 없으면 review."""
    zones: set[int] = set()
    for e in events:
        if (e.status != "ok" or not _is_freezer(profiles, default, e.zone)
                or e.judgment.products
                or abs(e.delta_weight) >= _small_change(profiles.get(e.zone, default))
                or not has_masked_movement(e.segments, floor)):
            continue
        big = ",".join(f"{s.delta_grams:+.0f}" for s in e.segments if abs(s.delta_grams) >= floor)
        notes.append(f"zone{e.zone}:freezer_masked_movement:"
                     f"net={e.delta_weight:+.1f}g:segments={big}")
        zones.add(e.zone)
    return zones


def suppress_partial_fingerprint_duplicates(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    products: Sequence[ActiveProduct], default: SensorProfile, notes: list[str],
    cfg: CrossZonePenaltyConfig, *, count_unit_slack: float = 5.0,
) -> list[TriggerEvent]:
    """⑥ 다른 층의 무게 검증된 COMPLETE 청구와 같은 클래스·같은 개수·같은 영상 지문이
    겹치는 창에서 PARTIAL로 다시 청구됐고, 그 PARTIAL 무게가 그 상품의 어떤 개수와도
    맞지 않으면 손에 든 상품의 재청구로 보고 뺀다 (ses-3-1795594851 1층 98).

    남는 상품이 없으면 그 클래스를 뺀 후보로 다시 판정해 COMPLETE일 때만 채택한다.
    작은 채널 판정 제외(③, pipeline)와 함께여야 한다 — 그렇지 않으면 50g 미만
    채널의 PARTIAL이 남아 재판정 결과가 달라진다."""
    if not products:
        return list(events)
    router = JudgmentRouter()
    out = list(events)
    for i, p in enumerate(out):
        prof = profiles.get(p.zone, default)
        if (p.status != "ok" or prof.weight_is_discriminative or p.delta_weight >= 0
                or p.judgment.status is not JudgmentStatus.PARTIAL or not p.judgment.products):
            continue
        strip: dict[int, int] = {}
        for pc in p.judgment.products:
            cid = pc.product.class_id
            mine = next((c for c in p.vision_candidates if c.class_id == cid), None)
            if mine is None:
                continue
            others = sum(o.product.unit_weight * o.count
                         for o in p.judgment.products if o is not pc)
            if not _weight_decisively_inconsistent(-p.delta_weight - others, pc.product,
                                                   prof.count_gate, count_unit_slack):
                continue
            for c in out:
                if (c is p or c.zone == p.zone or c.status != "ok"
                        or c.judgment.status is not JudgmentStatus.COMPLETE):
                    continue
                cpc = next((x for x in c.judgment.products if x.product.class_id == cid), None)
                theirs = next((x for x in c.vision_candidates if x.class_id == cid), None)
                if cpc is None or theirs is None or cpc.count != pc.count:
                    continue
                expected = sum(x.product.unit_weight * x.count for x in c.judgment.products)
                n = sum(x.count for x in c.judgment.products)
                cprof = profiles.get(c.zone, default)
                gate_n = cprof.count_gate + count_unit_slack * max(0, n - 1)
                if abs(-c.delta_weight - expected) > gate_n:
                    continue
                if _same_fingerprint(mine, theirs) and windows_mutually_overlap(c, p, cfg):
                    strip[cid] = c.zone
                    break
        if not strip:
            continue
        remaining = tuple(pc for pc in p.judgment.products if pc.product.class_id not in strip)
        tag = ",".join(f"class{cid}:source=zone{z}" for cid, z in sorted(strip.items()))
        if remaining:
            judgment = replace(p.judgment, products=remaining,
                               reason=p.judgment.reason + "+freezer_fingerprint_partial_pruned")
            notes.append(f"zone{p.zone}:freezer_fingerprint_partial_duplicate:{tag}")
        else:
            rejudged = _rejudge(p, [c for c in p.vision_candidates if c.class_id not in strip],
                                prof, products, router)
            if rejudged.status is JudgmentStatus.COMPLETE and rejudged.products:
                adopted = ",".join(f"{x.product.class_id}x{x.count}" for x in rejudged.products)
                notes.append(f"zone{p.zone}:freezer_fingerprint_partial_duplicate:{tag}:"
                             f"adopted={adopted}")
                judgment = replace(rejudged,
                                   reason=rejudged.reason + "+freezer_fingerprint_partial")
            else:
                notes.append(f"zone{p.zone}:freezer_fingerprint_partial_duplicate:{tag}:adopted=-")
                judgment = JudgmentResult(JudgmentStatus.NO_DETECTION,
                                          reason="freezer_fingerprint_partial_suppressed")
        out[i] = replace(p, judgment=judgment)
    return out


def recover_partial_count(
    events: Sequence[TriggerEvent], profiles: Mapping[int, SensorProfile],
    default: SensorProfile, notes: list[str],
) -> list[TriggerEvent]:
    """⑧ 정체성만 보존한 count=1 PARTIAL(무게가 1개로 맞지 않음)이 층의 유일한 청구이고
    층의 나머지 변화가 모두 작은(50g 미만) 무청구 변화면, 층 최종 무게(그 층 트리거
    합)로 개수를 다시 센다 (ses-8-1795594034 1층: 93+94 −1473.3g을 94 1개로 청구 →
    3층 94 반납이 그 1개를 지워 93 취출이 사라지던 문제).

    n = round(층 무게 / 등록 무게)가 2 이상, 재고 이하이고 오차가 n × 반납 허용오차
    (개당 실측 편차 — 같은 상품의 취출·반납 실측 차 20~25g과 같은 근거) 안일 때만
    n개로 바꾼다. 정체성은 비전 그대로 두고(비슷한 상품은 모델 문제), 상태는
    PARTIAL을 유지한다."""
    out = list(events)
    by_zone: dict[int, list[int]] = {}
    for i, e in enumerate(out):
        if e.status == "ok" and _is_freezer(profiles, default, e.zone):
            by_zone.setdefault(e.zone, []).append(i)
    for zone, idx in by_zone.items():
        claims = [i for i in idx if out[i].judgment.products]
        if len(claims) != 1:
            continue
        p = out[claims[0]]
        j = p.judgment
        if (j.status is not JudgmentStatus.PARTIAL or len(j.products) != 1
                or j.products[0].count != 1 or p.delta_weight >= 0):
            continue
        prof = profiles.get(zone, default)
        small = _small_change(prof)
        if any(out[i].freezer_return is not None or abs(out[i].delta_weight) >= small
               for i in idx if i != claims[0]):
            continue
        product = j.products[0].product
        w = product.unit_weight
        m = -sum(out[i].delta_weight for i in idx)
        if w <= 0 or m <= 0:
            continue
        n = round(m / w)
        if n < 2 or n > product.stock_qty or abs(m - n * w) > n * return_tolerance(prof):
            continue
        notes.append(f"zone{zone}:freezer_partial_count_from_weight:class{product.class_id}:"
                     f"count={n}:zone_delta={-m:.1f}g")
        out[claims[0]] = replace(p, judgment=replace(
            j, products=(replace(j.products[0], count=n),),
            reason=j.reason + "+freezer_partial_count_from_weight"))
    return out
