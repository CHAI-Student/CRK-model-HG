"""2026-11-26 실기 냉동 세션, 들고 온 상품 규칙(yoona.md #freezer-carried-in-20261126),
두 트레이에 걸친 상품 판정(#freezer-tray-spanning-20261126), 손에 든 후보를 뺀 취출
재판정(#freezer-carried-retry-20261126)의 회귀 검증.

저장 판정·비전 후보 재생이며 영상 재추론이 아니다. 상품 번호는 1~12(구 92~103 −91)이고,
정답과 다른 세션은 fixture의 known_gap에 원인을 남긴다.
"""
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from crk_model.core.profiles import FREEZER
from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
    WeightSegment,
)
from crk_model.ingest.bocpd import BocpdLoadcellAnalyzer
from crk_model.ingest.loadcell import LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.cross_zone import CrossZonePenaltyConfig
from crk_model.ledger.events import FreezerMovementEvidence, FreezerReturnEvidence, TriggerEvent
from crk_model.ledger.freezer_returns import take_measurement
from crk_model.ledger.journal import event_from_dict
from crk_model.ledger.settler import CloseSettler
from crk_model.service.pipeline import TriggerPipeline, TriggerRequest, tray_spanning_judgment
from crk_model.service.snapshot import ActiveProductStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "freezer_sessions_20261126.json").read_text())
PRODUCTS = tuple(ActiveProduct(**p) for p in FIXTURE["products"])
BY_CLASS = {p.class_id: p for p in PRODUCTS}
PROFILES = {i: FREEZER for i in range(1, 6)}


def settle(events):
    return CloseSettler(
        default_profile=FREEZER, cross_zone=CrossZonePenaltyConfig(enabled=True),
        active_products_provider=lambda: PRODUCTS,
    ).settle(events[0].session_id, events, PROFILES)


def class_counts(result):
    out = Counter()
    for zone in result.zones:
        for pc in zone.products:
            out[str(pc.product.class_id)] += pc.count
    return dict(out)


def vc(cid, votes):
    return VisionCandidate(cid, .9, votes, .5)


@pytest.mark.parametrize("case", FIXTURE["sessions"], ids=lambda case: case["source"])
def test_recorded_20261126_sessions(case):
    events = [event_from_dict(e) for e in case["events"]]
    result = settle(events)
    assert class_counts(result) == case["expected"]
    assert sorted(z.zone for z in result.zones if z.status != "complete") == \
        case["expected_non_complete_zones"]
    for note in case["expected_notes"]:
        assert note in result.notes
    for zone in result.zones:  # 보고용 층 무게는 정산 보완과 무관하게 원본 그대로
        assert zone.weight_delta == pytest.approx(
            sum(e.delta_weight for e in events if e.zone == zone.zone))


def _take(zone, ts, cid, delta):
    product = BY_CLASS[cid]
    return TriggerEvent("s", zone, ts, delta, (WeightSegment(ts, ts + 1, delta),),
                        JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(product, 1),), .9),
                        vision_candidates=(vc(cid, 20),))


def _mixed(zone, ts, take, ret, take_g=-505.0, return_g=525.0):
    """한 트리거 안에서 취출(take_g) 뒤 반납(return_g)이 이어진 혼합 이벤트."""
    net = take_g + return_g
    return TriggerEvent(
        "s", zone, ts, net,
        (WeightSegment(ts, ts + 1, take_g), WeightSegment(ts + 2, ts + 3, return_g)),
        JudgmentResult(JudgmentStatus.NO_DETECTION, reason="freezer_return_candidate"),
        vision_candidates=(*take, *ret),
        freezer_movement=FreezerMovementEvidence(tuple(take), tuple(ret), True, (net,)),
    )


def _floor_event(zone, steps, take, ret):
    """파이프라인 출력 모양의 냉동 트리거: 순변화 50g 미만이면 D2 무청구, 50g 이상
    양수면 반납 증거(반납 방향 후보, 없으면 영상 후보)를 단다."""
    net = sum(steps)
    segments = tuple(WeightSegment(10 + 2 * i, 11 + 2 * i, d) for i, d in enumerate(steps))
    small = abs(net) < 50
    freezer_return = None if small or net < 0 else FreezerReturnEvidence(
        tuple(ret or take), True, "return_direction" if ret else "video")
    return TriggerEvent(
        "s", zone, 10.0, net, segments,
        JudgmentResult(JudgmentStatus.NO_DETECTION,
                       reason="freezer_masked_movement" if small else "freezer_return_candidate"),
        vision_candidates=(*take, *ret), freezer_return=freezer_return,
        freezer_movement=FreezerMovementEvidence(tuple(take), tuple(ret), True, (net,)),
    )


@pytest.mark.parametrize("steps,take,ret,expected,review", [
    # ses-1-1795670038 5층 모양: 들고 온 11이 취출 방향 1위(11표), 7은 5표
    ([32.5, -515, 210, 320], (vc(11, 11), vc(7, 5)), (vc(11, 10),), {"7": 1}, []),
    ([530, -470], (vc(11, 11), vc(7, 5)), (vc(11, 10),), {"7": 1}, []),  # 반납 → 취출
    ([-470, 500], (vc(11, 11), vc(7, 5)), (), {"7": 1}, []),  # 반납 방향 없음, +30g
    ([-470, 530], (vc(11, 11), vc(7, 5)), (), {"7": 1}, []),  # 반납 방향 없음, +60g
    ([-470, 530], (vc(7, 5),), (vc(11, 10),), {"7": 1}, []),
    ([-470, 530], (vc(11, 11),), (vc(11, 10),), {}, [5]),  # 7 미검출 → 무청구 review
    ([165, -690], (vc(11, 11),), (vc(11, 10),), {"11": 1}, [5]),  # 누름 + 취출은 반납 아님
], ids=["take_then_return", "return_then_take", "no_direction_small_net",
        "no_direction_large_net", "only_real_take_seen", "real_take_unseen", "press_not_return"])
def test_other_floor_item_returned_while_taking_here(steps, take, ret, expected, review):
    """2층 11 취출 → 5층에서 7 취출 + 11 반납. 들고 온 11은 5층 취출 후보에서 빼고
    반납으로 2층 청구를 지운다 (공유 카메라라 들고 오는 모습이 취출 방향으로 잡힌다)."""
    result = settle([_take(2, 0.0, 11, -525.0), _floor_event(5, steps, take, ret)])
    assert class_counts(result) == expected
    assert [z.zone for z in result.zones if z.status != "complete"] == review


def test_carried_in_item_seen_as_take_is_returned_not_billed():
    """ses-1-1795659665 구조: 3층에서 꺼낸 11을 들고 2층으로 와 8을 꺼내고 11을 넣었다.
    공유 카메라라 2층 클립의 취출 방향에도 11만 보인다 — 반납은 연결하고, 2층에서
    꺼낸 505g은 영상 근거가 없으니 청구하지 않고 review로 남긴다."""
    result = settle([_take(3, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5),), (vc(11, 7),))])
    assert class_counts(result) == {}
    assert "zone2:freezer_unbilled_weight_drop:unexplained=505.0g" in result.notes
    assert next(z for z in result.zones if z.zone == 2).status == "review"


def test_take_direction_with_another_sku_still_bills_that_sku():
    """취출 방향에 반납 SKU 말고 다른 상품(8)도 보이면 기존 분해를 그대로 쓴다."""
    result = settle([_take(3, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5), vc(8, 6)), (vc(11, 7),))])
    assert class_counts(result) == {"8": 1}
    assert any("zone2:freezer_mixed_resolved:" in note for note in result.notes)


def test_same_floor_lot_is_not_treated_as_carried_in():
    """반납 SKU를 같은 층에서 꺼냈다면 들고 온 상품이 아니다 — 같은 SKU의 반복·개수
    혼합은 지금처럼 분해하지 않는다."""
    result = settle([_take(2, 0.0, 11, -530.0),
                     _mixed(2, 10.0, (vc(11, 5),), (vc(11, 7),))])
    assert not any("freezer_mixed_resolved" in note for note in result.notes)


# ---- 두 트레이에 걸친 상품 (ses-1-1795668469 5층 11) ----

class _EmptyDetector:
    def detect(self, frame, allowed_class_ids=None):
        return []


def _spanning_outcome(monkeypatch, ch1_drop_at):
    """5층 ch0 +37.5 → −448.3, ch1 +50 → −35 → −70 (끝 −410.8 / −55, 합 −465.8g).
    ch1_drop_at: ch1이 −70 빠지는 샘플 위치 (90이면 ch0 −448.3과 같은 순간)."""
    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.directional_candidates["take"] = (vc(8, 7),)  # 공유 카메라에 잡힌 2층 너겟
        return (vc(11, 28), vc(8, 11), vc(4, 3))

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        _EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    ch0 = [1000] * 30 + [1037.5] * 60 + [589.2] * 70
    ch1 = [500] * 30 + [550] * 30 + [515] * (ch1_drop_at - 60) + [445] * (160 - ch1_drop_at)
    levels = zip(ch0, ch1, strict=True)
    samples = [LoadcellSample(i * .1, level) for i, level in enumerate(levels)]
    return pipeline.process("s", TriggerRequest(5, {}, samples, 7))


def test_product_spanning_two_trays_is_judged_by_the_drop_they_share(monkeypatch):
    """두 트레이가 같은 순간 함께 빠진 무게(−518.3g)로 11을 판정한다. 트레이별로는 둘 다
    11 PARTIAL이라 대칭 오염 가드가 모두 뺐고, 시작→끝 합(−465.8g)으로는 4(475g)가 맞는다."""
    outcome = _spanning_outcome(monkeypatch, ch1_drop_at=90)
    judgment = outcome.event.judgment
    assert judgment.status is JudgmentStatus.COMPLETE
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(11, 1)]
    assert judgment.reason.startswith("multi_tray_spanning[")
    assert "freezer_tray_spanning" in outcome.trace.reason_codes
    assert outcome.event.delta_weight == pytest.approx(-465.8)  # 보고용 무게는 그대로


def test_trays_dropping_at_different_moments_are_not_merged(monkeypatch):
    outcome = _spanning_outcome(monkeypatch, ch1_drop_at=120)
    assert outcome.event.judgment.products == ()
    assert "freezer_tray_spanning" not in outcome.trace.reason_codes


def test_spanning_needs_the_same_identity_on_every_tray():
    ctx = JudgmentContext(zone=5, profile=FREEZER, delta_weight=-465.8, segments=(),
                          vision_candidates=(vc(11, 28), vc(8, 11)), active_products=PRODUCTS,
                          vision_only=False)
    segments = (WeightSegment(8.9, 9.0, -448.3), WeightSegment(8.9, 9.0, -70.0))

    def partial(cid):
        return JudgmentResult(JudgmentStatus.PARTIAL, (ProductCount(BY_CLASS[cid], 1),), .9)

    router = JudgmentRouter()
    assert tray_spanning_judgment(router, ctx, [partial(11), partial(11)], segments) is not None
    assert tray_spanning_judgment(router, ctx, [partial(11), partial(8)], segments) is None


def test_take_lot_uses_the_shared_drop_of_a_spanning_product():
    """11 취출 이력은 함께 빠진 −518.3g으로 기록돼 2층 반납 525g과 맞는다(순변화 −467.5g)."""
    segments = (WeightSegment(2.3, 3.2, 37.5), WeightSegment(2.3, 3.2, 50.0),
                WeightSegment(3.9, 4.8, -35.0), WeightSegment(5.5, 6.4, -448.3),
                WeightSegment(5.5, 6.4, -70.0))
    movement = FreezerMovementEvidence((), (), True, (-412.5, -55.0))
    one = (ProductCount(BY_CLASS[11], 1),)
    assert take_measurement(-467.5, one, movement, 15, 5, 100, segments)[0] == pytest.approx(518.3)
    assert take_measurement(-467.5, one, movement, 15, 5, 100) == (467.5, None)


# ---- 손에 든 후보를 뺀 취출 재판정 (ses-7-1795671117 1층 만두) ----

_CARRIED_HELD = {"top": {4: [62, 83]}, "side": {4: [51, 80]}}


def _carried_outcome(monkeypatch, *, drop=370, held=_CARRIED_HELD):
    """4층에서 꺼낸 핫도그(4)를 손에 든 채 1층에서 만두를 꺼냈다. 핫도그가 163표로
    클립을 덮어 만두(5: 5표, 6: 6표)는 share 하한(1위 x 0.1)에 탈락했다."""
    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.vote_summary = {
            "classes": {
                4: {"votes": 163, "ratio": .38, "weighted_conf": 1.0, "rejected_by": None},
                12: {"votes": 23, "ratio": .054, "weighted_conf": 1.0, "rejected_by": None},
                5: {"votes": 5, "ratio": .012, "weighted_conf": 1.0, "rejected_by": "share"},
                6: {"votes": 6, "ratio": .014, "weighted_conf": 1.0, "rejected_by": "share"},
            },
            "held_shadow": held,
        }
        trace.directional_candidates["take"] = (vc(4, 59), vc(12, 10))
        return (VisionCandidate(4, 1.0, 163, .38, 56, .54, 0.0),
                VisionCandidate(12, 1.0, 23, .054, 0, .34, .42))

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        _EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
        voting_params={"min_vote_share": 0.1},
    )
    samples = [LoadcellSample(i * .1, (1000 if i < 30 else 1000 - drop, 500)) for i in range(70)]
    return pipeline.process("s", TriggerRequest(1, {}, samples, 7))


def test_take_hidden_by_a_carried_item_is_rejudged_without_it(monkeypatch):
    outcome = _carried_outcome(monkeypatch)
    judgment = outcome.event.judgment
    assert judgment.status is JudgmentStatus.COMPLETE
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(6, 1)]
    assert judgment.reason.startswith("carried_retry[")
    assert "freezer_carried_retry" in outcome.trace.reason_codes
    assert {c.class_id for c in outcome.event.vision_candidates} >= {4, 12, 5, 6}


def test_carried_rejudge_needs_held_evidence(monkeypatch):
    outcome = _carried_outcome(monkeypatch, held=None)  # HELD_TRACK_DEMOTION=off
    assert outcome.event.judgment.status is not JudgmentStatus.COMPLETE
    assert "freezer_carried_retry" not in outcome.trace.reason_codes


def test_carried_rejudge_keeps_the_original_unless_it_completes(monkeypatch):
    outcome = _carried_outcome(monkeypatch, drop=260)  # 어떤 남은 후보로도 무게가 안 맞음
    assert not outcome.event.judgment.reason.startswith("carried_retry[")
    assert "freezer_carried_retry" not in outcome.trace.reason_codes


def test_rejudged_take_is_billed_with_review():
    event = TriggerEvent(
        "s", 1, 1.0, -370.0, (WeightSegment(1, 2, -370.0),),
        JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(BY_CLASS[6], 1),), 1.0,
                       reason="carried_retry[freezer_vision_first_single]"),
        vision_candidates=(vc(6, 6),),
    )
    result = settle([event])
    assert class_counts(result) == {"6": 1}
    assert result.zones[0].status == "review"
    assert "zone1:freezer_carried_retry:trigger_ts=1.000" in result.notes


def test_close_does_not_swap_a_confident_take_for_a_weak_weight_fit():
    # ses-1-1795676588 5층: 트리거가 용가리 7(29표·conf 1.0)을 채널 −475g으로 확정했다.
    # 존 순변화 −480g(작은 채널 −5g 포함)이 스냅 게이트를 넘어도, 8표·conf 0.31인 핫도그 4가
    # 무게만 더 맞는다는 이유로 마감 정산이 7을 4로 바꾸면 안 된다.
    case = next(c for c in FIXTURE["sessions"] if c["source"] == "ses-1-1795676588")
    result = settle([event_from_dict(e) for e in case["events"]])
    zone5 = next(z for z in result.zones if z.zone == 5)
    assert [(pc.product.class_id, pc.count) for pc in zone5.products] == [(7, 1)]
    assert not any("freezer_close_resolve_swap" in n for n in result.notes)
    assert "freezer_close_gate_failed:zone5:keep_incremental" in result.notes


# ---- 다른 층에서 이 트리거가 열린 동안 꺼낸 상품 (ses-3-1795673059 2층) ----

def _lifted_events(*, source_end=67.2, source_delta=-740.0, target_delta=-210.0,
                   target_segments=None):
    """1층에서 2(750g)를 꺼낸 순간(하강 66.3~67.2초)이 2층 트리거(65.6초 시작) 안이라 2층
    클립에 2가 꺼내는 방향으로 찍혔다(head_votes 0). 2층 무게 −210g은 2와 맞지 않는다."""
    source = TriggerEvent(
        "s", 1, 61.0, source_delta, (WeightSegment(66.3, source_end, source_delta),),
        JudgmentResult(JudgmentStatus.COMPLETE, (ProductCount(BY_CLASS[2], 1),), .9),
        vision_candidates=(vc(2, 148),), change_timestamps=(source_end,),
    )
    target = TriggerEvent(
        "s", 2, 65.6, target_delta,
        target_segments or (WeightSegment(67.9, 68.8, target_delta),),
        JudgmentResult(JudgmentStatus.PARTIAL, (ProductCount(BY_CLASS[2], 1),), .5,
                       reason="vision_first_identity_partial"),
        vision_candidates=(vc(2, 182),), change_timestamps=(68.8,),
    )
    return [source, target]


def test_item_lifted_on_another_floor_during_this_trigger_is_not_billed_again():
    result = settle(_lifted_events())
    zones = {z.zone: z for z in result.zones}
    assert [(pc.product.class_id, pc.count) for pc in zones[1].products] == [(2, 1)]
    assert not zones[2].products
    assert zones[2].status == "review"
    assert any(n.startswith("zone2:freezer_carried_over_suppressed:")
               and n.endswith(":lifted_during_trigger") for n in result.notes)


@pytest.mark.parametrize("kwargs", [
    {"source_end": 65.0},  # 1층 하강이 2층 트리거보다 먼저 끝났다 (손에 든 채 시작 = head 지문 몫)
    {"target_segments": (WeightSegment(67.0, 67.5, 300.0), WeightSegment(67.9, 68.8, -510.0))},
    {"target_delta": -65.0},  # 상품 크기 미만 — 다른 CLOSE 규칙 몫
    {"source_delta": -400.0},  # 출처 청구 자체가 무게와 어긋난다
    {"target_delta": -745.0},  # 2층 무게가 2와 맞는다 — 같은 상품을 하나 더 꺼냈을 수 있다
])
def test_lifted_fingerprint_needs_every_condition(kwargs):
    result = settle(_lifted_events(**kwargs))
    assert not any("lifted_during_trigger" in n for n in result.notes)


@pytest.mark.parametrize("target_delta", [
    -745.0,   # 무게가 2와 맞음
    -715.0,   # 35g 덜 읽힘 (근접 밴드 30g 밖)
    -680.0,   # 70g 덜 읽힘 (일부가 로드셀 밖에 걸침)
    -1000.0,  # 2 + 다른 상품을 함께 꺼냄
])
def test_same_product_taken_on_two_floors_is_billed_on_both(target_delta):
    """1층과 2층에서 2를 하나씩 꺼냈다. 2층 하강이 2 1개보다 상품 크기(100g) 이상 가볍지
    않으면 2층에서도 2가 빠졌을 수 있으므로 지우지 않는다."""
    result = settle(_lifted_events(target_delta=target_delta))
    assert class_counts(result) == {"2": 2}
    assert not any("lifted_during_trigger" in n for n in result.notes)


# ---- 두 트레이가 같은 한 상품만 보였을 때 (ses-4-1795673607 3층) ----

def _shared_outcome(monkeypatch, *, ch0=405, ch1=490, held=None, cands=None):
    """3층 ch0 −405, ch1 −490이 다른 순간 빠졌다. 영상은 6(370g)만 후보로 남아 두 트레이
    모두 6 PARTIAL — 가드 ②가 둘 다 뺀다. 실제로 함께 꺼낸 10은 영상이 거의 못 봤다."""
    def vision(pipeline, request, profile, snapshot, delta, trace):
        trace.vote_summary = {"classes": {}, "held_shadow": held} if held else {"classes": {}}
        return cands or (VisionCandidate(6, 1.0, 99, .2, 0, .26, .44),)

    monkeypatch.setattr(TriggerPipeline, "_run_vision", vision)
    store = ActiveProductStore()
    store.update(PRODUCTS)
    pipeline = TriggerPipeline(
        _EmptyDetector(), PROFILES, store, analyzer_factory=BocpdLoadcellAnalyzer,
    )
    levels = [(1000 if i < 30 else 1000 - ch0, 1000 if i < 60 else 1000 - ch1) for i in range(100)]
    samples = [LoadcellSample(i * .1, level) for i, level in enumerate(levels)]
    return pipeline.process("s", TriggerRequest(3, {}, samples, 7))


def test_two_trays_seeing_only_one_product_bill_it_once(monkeypatch):
    outcome = _shared_outcome(monkeypatch)
    judgment = outcome.event.judgment
    assert judgment.status is JudgmentStatus.PARTIAL
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(6, 1)]
    assert judgment.reason.startswith("multi_tray_shared_identity[")
    assert judgment.reason.endswith(":unexplained=525.0g")
    assert "freezer_tray_shared_identity" in outcome.trace.reason_codes


@pytest.mark.parametrize("ch0, ch1, unexplained", [
    (390, 395, 45.0),    # 트레이마다 6 1개에서 20·25g 더 빠짐 (near-gate PARTIAL 둘)
    (345, 340, -55.0),   # 트레이마다 25·30g 덜 읽힘
])
def test_same_product_from_two_trays_is_billed_twice(monkeypatch, ch0, ch1, unexplained):
    """두 트레이에서 6을 하나씩 꺼냈는데 트레이마다 15~30g 어긋나 둘 다 PARTIAL이 됐다.
    트레이 무게가 각각 6 1개의 근접 밴드(2×gate) 안이므로 2개다."""
    judgment = _shared_outcome(monkeypatch, ch0=ch0, ch1=ch1).event.judgment
    assert judgment.status is JudgmentStatus.PARTIAL
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(6, 2)]
    assert judgment.reason.endswith(f":unexplained={unexplained:.1f}g")


@pytest.mark.parametrize("cid, ch0, ch1", [
    (8, 520, 523),  # 너겟(500g) 20g 더 빠짐 + 오리로스 1(523g)을 모델이 못 봄
    (2, 770, 720),  # 매운오리(750g) 20g 더 빠짐 + 대패오리 3(720g)을 모델이 못 봄
    (4, 495, 500),  # 핫도그(475g) 20g 더 빠짐 + 너겟 8(500g)을 모델이 못 봄
])
def test_tray_explained_by_a_different_weight_product_is_not_counted(monkeypatch, cid, ch0, ch1):
    """영상은 한 상품만 봤지만, 무게가 그 상품과 확실히 다른(15g 넘게 차이) 다른 상품이 트레이
    무게를 허용오차 안에서 설명하면 그 트레이는 세지 않는다 — 1개만 청구하고 나머지는 partial."""
    cands = (VisionCandidate(cid, 1.0, 60, .2, 0, .3, .4),)
    judgment = _shared_outcome(monkeypatch, ch0=ch0, ch1=ch1, cands=cands).event.judgment
    assert judgment.status is JudgmentStatus.PARTIAL
    assert [(pc.product.class_id, pc.count) for pc in judgment.products] == [(cid, 1)]


def test_shared_identity_needs_vision_to_see_only_that_product(monkeypatch):
    # 10이 7표로 영상에 보였다 — 다른 트레이가 10일 수 있으므로 쓰지 않는다
    cands = (VisionCandidate(6, 1.0, 99, .2, 0, .26, .44), VisionCandidate(10, 1.0, 7, .01))
    outcome = _shared_outcome(monkeypatch, cands=cands)
    assert outcome.event.judgment.products == ()
    assert "freezer_tray_shared_identity" not in outcome.trace.reason_codes


def test_shared_identity_ignores_noise_level_candidates(monkeypatch):
    # 다른 상품 2표(노이즈 수준)는 "영상에 보였다"로 치지 않는다
    cands = (VisionCandidate(6, 1.0, 99, .2, 0, .26, .44), VisionCandidate(12, 1.0, 2, .005))
    outcome = _shared_outcome(monkeypatch, cands=cands)
    assert [(pc.product.class_id, pc.count) for pc in outcome.event.judgment.products] == [(6, 1)]


def test_two_exact_trays_of_the_same_product_stay_complete(monkeypatch):
    # 무게가 맞으면 원래 경로(트레이별 COMPLETE 합산)로 2개 — 이 규칙은 관여하지 않는다
    outcome = _shared_outcome(monkeypatch, ch0=370, ch1=372)
    assert outcome.event.judgment.status is JudgmentStatus.COMPLETE
    assert [(pc.product.class_id, pc.count) for pc in outcome.event.judgment.products] == [(6, 2)]
    assert "freezer_tray_shared_identity" not in outcome.trace.reason_codes


def test_shared_identity_skips_a_carried_item(monkeypatch):
    outcome = _shared_outcome(monkeypatch, held={"top": {6: [60, 70]}})
    assert outcome.event.judgment.products == ()
    assert "freezer_tray_shared_identity" not in outcome.trace.reason_codes


def test_shared_identity_needs_a_tray_dropping_one_unit(monkeypatch):
    # 어느 트레이도 6(370g) 1개만큼 빠지지 않았다 — 가드 ②의 대칭 오염 모양
    outcome = _shared_outcome(monkeypatch, ch0=300, ch1=300)
    assert outcome.event.judgment.products == ()
    assert "freezer_tray_shared_identity" not in outcome.trace.reason_codes


def test_shared_identity_bill_is_partial_with_a_note():
    event = TriggerEvent(
        "s", 3, 1.0, -895.0, (WeightSegment(1, 2, -405.0), WeightSegment(3, 4, -490.0)),
        JudgmentResult(JudgmentStatus.PARTIAL, (ProductCount(BY_CLASS[6], 1),), .5,
                       reason="multi_tray_shared_identity[vision_first_identity_partial"
                              "+vision_first_identity_partial]:unexplained=525.0g"),
        vision_candidates=(vc(6, 99),),
    )
    result = settle([event])
    assert class_counts(result) == {"6": 1}
    assert result.zones[0].status == "partial"
    assert "zone3:freezer_tray_shared_identity:trigger_ts=1.000" in result.notes


def test_item_lifted_during_this_trigger_and_returned_here_later_nets_out():
    """1층 2를 2층 트리거 중 꺼내(2층 −210g은 닭발) 나중에 2층에 되돌려 놓았다. 2층 2 재청구는
    지워지고, 반납은 1층 2 취출 이력과 맞아 2는 청구되지 않는다(닭발은 영상 미검출 → review)."""
    source, target = _lifted_events()
    back = TriggerEvent(
        "s", 2, 80.0, 750.0, (WeightSegment(80.5, 81.4, 750.0),),
        JudgmentResult(JudgmentStatus.NO_DETECTION), vision_candidates=(vc(2, 80),),
        freezer_return=FreezerReturnEvidence((vc(2, 80),), True, "video"),
        change_timestamps=(81.4,),
    )
    result = settle([source, target, back])
    assert class_counts(result) == {}
    assert next(z for z in result.zones if z.zone == 2).status == "review"
    assert any(n.endswith(":lifted_during_trigger") for n in result.notes)


# ---- 층 무게로 확인된 PARTIAL 취출도 혼합 분해의 취출 이력 (ses-1-1795681459) ----

def _session(source):
    return [event_from_dict(e) for e in
            next(c for c in FIXTURE["sessions"] if c["source"] == source)["events"]]


def test_floor_verified_partial_take_is_matched_by_a_return_elsewhere():
    """3층 11은 작은 채널(+31g)을 빼고 ch1 −557.5g만으로 판정해 PARTIAL이었지만 층 순변화
    −526.25g은 11(530g)과 맞는다. 이 취출을 이력에 넣어야 2층 반납 +510g이 11로 맞고,
    2층 −525/+510 움직임이 8 취출 + 11 반납으로 분해된다."""
    result = settle(_session("ses-1-1795681459"))
    assert class_counts(result) == {"8": 1}
    assert "zone2:freezer_mixed_resolved:take=-490.0g:return=510.0g:net=20.0g" in result.notes


def test_partial_take_not_verified_by_floor_weight_stays_out_of_the_mixed_ledger():
    # 같은 세션에서 3층 순변화가 11과 60g 어긋나면(−470g) 층 무게로 확인된 취출이 아니다
    events = [replace(e, delta_weight=-470.0) if e.zone == 3 else e
              for e in _session("ses-1-1795681459")]
    result = settle(events)
    assert not any("freezer_mixed_resolved" in n for n in result.notes)


# ---- 근접 밴드로 청구된 PARTIAL 취출도 혼합 분해의 취출 이력 (ses-1-1795682705) ----

def test_near_gate_take_is_cancelled_by_a_return_inside_a_mixed_trigger():
    """2층 −510g은 11(530g)과 20g 차 — gate_n(15g) 밖·근접 밴드(30g) 안이라 트리거가
    11을 PARTIAL로 청구했다. 이 취출이 이력에 있어야 3층 −370/+530 움직임이 6 취출 +
    11 반납으로 분해되고, 2층 11이 취소된다 (실제 2층 상품은 10 — 영상은 취출·반납 모두
    11로 봤다)."""
    result = settle(_session("ses-1-1795682705"))
    assert class_counts(result) == {"6": 1}
    assert "zone3:freezer_mixed_resolved:take=-375.0g:return=530.0g:net=155.0g" in result.notes


def test_take_outside_the_near_band_stays_out_of_the_mixed_ledger():
    # 같은 세션에서 2층 순변화가 11과 35g 어긋나면(−495g) 근접 밴드(30g) 밖이다
    events = [replace(e, delta_weight=-495.0) if e.zone == 2 else e
              for e in _session("ses-1-1795682705")]
    result = settle(events)
    assert not any("freezer_mixed_resolved" in n for n in result.notes)


# ---- 다음 층으로 들려 나간 상품 (yoona.md #freezer-carried-out-20261126) ----

def _with_candidate(events, zone, cid, **fields):
    """zone 이벤트의 비전 후보 cid 값을 바꾼다 (조건 하나씩 빼 보는 확인용)."""
    return [replace(e, vision_candidates=tuple(
        replace(c, **fields) if c.class_id == cid else c for c in e.vision_candidates))
        if e.zone == zone else e for e in events]


def test_item_carried_to_the_next_floor_is_not_a_ghost_where_it_was_taken():
    """1층 −681g = 12(200g) + 8(500g). 8은 1층 영상 첫 프레임엔 없고 취출 방향 5표,
    5층 영상엔 첫 프레임부터 손에 있었다(head 5). 두 층에 보였다고 유령으로 빼면 조합
    12+8이 막혀 12만 청구된다."""
    result = settle(_session("ses-8-1795685730"))
    assert class_counts(result) == {"12": 1, "8": 1, "7": 1}
    assert "zone1:freezer_carried_out_not_ghost:class8" in result.notes


def test_item_seen_on_another_floor_without_a_hand_stays_a_ghost():
    # 5층에서 8이 첫 프레임에 손에 없었다면(head 0) 들고 간 흔적이 아니다 — 기존대로 12만
    events = _with_candidate(_session("ses-8-1795685730"), 5, 8, head_votes=0)
    result = settle(events)
    assert class_counts(result) == {"12": 1, "7": 1}
    assert not any("freezer_carried_out" in n for n in result.notes)


@pytest.mark.parametrize("source, zone, expected, note", [
    # 1층 −732.5g: 3(720g)이 무게로 더 가깝지만 2(750g)가 243표·취출 방향 47표이고
    # 2층 영상 첫 프레임부터 손에 있었다(head 22). 3은 2층에 보이지 않았다.
    ("ses-9-1795685842", 1, {"2": 1, "1": 1},
     "zone1:freezer_carried_out_identity:billed=class3:adopted=class2:trigger_ts=1795685843.414"),
    # 1층 −480g: 4(475g)·8(500g) 표 48 동률. 8은 4층(head 6)·5층(head 8)에 들려 갔고
    # 4는 4층에서 첫 프레임엔 없었다(head 0, 4층 4 취출).
    ("ses-10-1795685937", 1, {"8": 1, "4": 1, "9": 1},
     "zone1:freezer_carried_out_identity:billed=class4:adopted=class8:trigger_ts=1795685938.181"),
])
def test_same_weight_take_is_billed_as_the_item_carried_to_the_next_floor(
        source, zone, expected, note):
    result = settle(_session(source))
    assert class_counts(result) == expected
    assert note in result.notes
    assert next(z for z in result.zones if z.zone == zone).status == "partial"


def _ses9_adopted(events):
    return any("freezer_carried_out_identity" in n for n in settle(events).notes)


def test_carried_out_identity_needs_every_condition():
    events = _session("ses-9-1795685842")
    assert _ses9_adopted(events)
    # 2층에서 2가 첫 프레임에 손에 없었다 → 들려 나간 흔적 없음
    assert not _ses9_adopted(_with_candidate(events, 2, 2, head_votes=0))
    # 1층에서 2가 이미 첫 프레임부터 손에 있었다 → 이 층에서 꺼낸 게 아니다
    assert not _ses9_adopted(_with_candidate(events, 1, 2, head_votes=5))
    # 2의 표가 청구된 3(71표)보다 적다
    assert not _ses9_adopted(_with_candidate(events, 1, 2, vote_count=50))
    # 청구된 3도 2층에 손에 든 채 들어갔다 → 어느 쪽이 나갔는지 못 가른다
    held3 = VisionCandidate(3, 1.0, 10, .1, head_votes=10)
    assert not _ses9_adopted([replace(e, vision_candidates=(*e.vision_candidates, held3))
                              if e.zone == 2 else e for e in events])
    # 무게 거부권: 꺼낸 무게가 2(750g)와 근처 범위(30g) 밖
    assert not _ses9_adopted([replace(e, delta_weight=-715.0) if e.zone == 1 else e
                              for e in events])


def test_carried_out_identity_does_not_bill_an_item_billed_elsewhere():
    # 2가 이미 2층에 청구돼 있으면 1층을 2로 바꾸지 않는다 (같은 상품을 겹쳐 세지 않음)
    events = [replace(e, judgment=replace(e.judgment, products=(ProductCount(BY_CLASS[2], 1),)))
              if e.zone == 2 and e.delta_weight < 0 else e for e in _session("ses-9-1795685842")]
    assert not _ses9_adopted(events)
