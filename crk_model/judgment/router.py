"""판정 라우터 v2 — 셀 단위 4경로 (README "추론 설계 v2").

경로:
- cell_delta   : 활성 셀 전부가 "정체성 p의 n×w"로 설명됨 (주 경로)
- cell_pending : 일부/전부 셀이 정체성 또는 개수 모호 — close 셀 net으로 이월
- no_signal    : 활성 셀 없음 (게이트 미달) — NO_DETECTION (fail-closed)
- vision_only  : 로드셀 신뢰 불가 — 최다득표 후보 count=1, conf×0.7 (v1 유지)

정체성 규칙 (셀별):
- 알려진 셀(신념 확신): p 고정, 무게는 개수만 결정. 비전 불일치는 1회성이면
  note, 무게까지 다른 상품을 지목하면 contradiction으로 보고(강등 증거).
- 미지 셀: W(무게 후보) ∩ V(비전 후보)가 유일하면 채택. 비전 무득표 시 W가
  유일하고 프로파일이 허용(weight_is_discriminative, 냉장 ±3g)하면 무게 단독
  채택 — 냉동 ±15g은 보류 (178g 사건·이슈 #6의 억제 원리).

confidence는 진단용이다 — 결제 페이로드는 confidence를 쓰지 않는다
(이슈 #6 4차 wire 계약). 알려진 셀의 무게 판정 0.9 / 비전 교차 채택은 해당
후보 conf / 무게 단독 채택 0.6.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from crk_model.core.profiles import SensorProfile
from crk_model.core.types import (
    ActiveProduct,
    CellOutcome,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.judgment.interfaces import JudgmentContext, JudgmentDecision

# 진단용 confidence 상수 (결제 미사용 — 세션 아카이브/로그 해석용)
_CONF_KNOWN_IDENTITY = 0.9
_CONF_WEIGHT_ONLY = 0.6
_VISION_ONLY_FACTOR = 0.7


def _weight_pairs(
    delta_abs: float,
    products: Sequence[ActiveProduct],
    tol: float,
    *,
    removal: bool,
) -> list[tuple[ActiveProduct, int, bool]]:
    """W: delta를 n×unit_weight로 설명하는 (상품, n, 개수모호) 후보 전수.

    상품 ≤ 10 (전제 1)이므로 전수 탐색이 곧 상수 시간이다. removal은
    품절(stock=0) 상품과 stock 초과 개수를 배제한다 (셀에 없는 것은 꺼낼 수
    없음 — v1 I5/I12 승계). 개수모호 = 이웃 정수 n±1도 tol 이내 (w ≤ 2·tol).
    """
    pairs: list[tuple[ActiveProduct, int, bool]] = []
    for p in products:
        w = p.unit_weight
        if w <= 0:
            continue
        n = round(delta_abs / w)
        if n < 1:
            continue
        if abs(delta_abs - n * w) > tol:
            continue
        if removal and n > p.stock_qty:
            continue
        ambiguous = abs(delta_abs - (n + 1) * w) <= tol or (
            n > 1 and abs(delta_abs - (n - 1) * w) <= tol
        )
        pairs.append((p, n, ambiguous))
    return pairs


def _vision_leader(candidates: Sequence[VisionCandidate]) -> VisionCandidate | None:
    ranked = sorted(candidates, key=lambda c: (-c.vote_count, -c.confidence))
    return ranked[0] if ranked else None


def _resolve(cell: CellOutcome, product_id: str, count: int, reason: str) -> CellOutcome:
    return CellOutcome(
        channel=cell.channel,
        delta_weight=cell.delta_weight,
        segments=cell.segments,
        stabilized=cell.stabilized,
        resolved=True,
        product_id=product_id,
        count=count,
        reason=reason,
    )


def _pending(cell: CellOutcome, reason: str, product_id: str = "") -> CellOutcome:
    return CellOutcome(
        channel=cell.channel,
        delta_weight=cell.delta_weight,
        segments=cell.segments,
        stabilized=cell.stabilized,
        resolved=False,
        product_id=product_id,  # 정체성만 확정된 개수 보류(count_pending)에 사용
        count=0,
        reason=reason,
    )


class JudgmentRouter:
    """v1과 이름·telemetry 계약 유지 — 내부는 셀 단위 4경로."""

    def __init__(self) -> None:
        self.telemetry: Counter[str] = Counter()  # 경로별 히트율

    def judge(self, ctx: JudgmentContext) -> JudgmentDecision:
        if ctx.vision_only:
            decision = self._vision_only(ctx)
        else:
            decision = self._judge_cells(ctx)
        self.telemetry[decision.result.strategy] += 1
        return decision

    # ---- ④ vision_only: 로드셀 신뢰 불가 (v1 의미론 유지) ----
    @staticmethod
    def _vision_only(ctx: JudgmentContext) -> JudgmentDecision:
        cells = tuple(_pending(c, "loadcell_unstable") for c in ctx.cells)
        by_class = {
            p.class_id: p
            for p in ctx.active_products
            if p.stock_qty > 0 and p.class_id > 0
        }
        ranked = sorted(
            ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence)
        )
        for cand in ranked:
            if cand.class_id in by_class:
                p = by_class[cand.class_id]
                result = JudgmentResult(
                    JudgmentStatus.COMPLETE,
                    (ProductCount(p, 1),),
                    confidence=cand.confidence * _VISION_ONLY_FACTOR,
                    reason="vision_only",
                    strategy="vision_only",
                )
                return JudgmentDecision(result, cells)
        result = JudgmentResult(
            JudgmentStatus.NO_DETECTION,
            reason="no_vision_candidates",
            strategy="vision_only",
        )
        return JudgmentDecision(result, cells)

    # ---- ①②③ 셀 단위 판정 ----
    def _judge_cells(self, ctx: JudgmentContext) -> JudgmentDecision:
        profile = ctx.profile
        out_cells: list[CellOutcome] = []
        contradictions: list[tuple[int, str]] = []
        resolved: list[CellOutcome] = []
        pending: list[CellOutcome] = []
        confidences: list[float] = []

        active = [
            c
            for c in ctx.cells
            if c.stabilized and abs(c.delta_weight) >= profile.min_weight_change_grams
        ]
        for cell in ctx.cells:
            if cell not in active:
                out_cells.append(
                    _pending(cell, cell.reason if not cell.stabilized else "below_min_gate")
                )
                if not cell.stabilized and abs(cell.delta_weight) > 0:
                    pending.append(out_cells[-1])
                continue
            judged, conf, contra = self._judge_cell(cell, ctx, profile)
            out_cells.append(judged)
            if contra is not None:
                contradictions.append(contra)
            if judged.resolved:
                resolved.append(judged)
                confidences.append(conf)
            else:
                pending.append(judged)

        if not active and not pending:
            # ③ no_signal: 게이트 미달 (fail-closed — 무신호 과금 금지)
            result = JudgmentResult(
                JudgmentStatus.NO_DETECTION,
                reason="below_min_weight_change",
                strategy="no_signal",
            )
            return JudgmentDecision(result, tuple(out_cells))

        products = _merge_removals(resolved, ctx.active_products)
        if resolved and not pending:
            # ① cell_delta: 활성 셀 전부 설명됨
            reason = "cell_delta" if products else "return_resolved"
            result = JudgmentResult(
                JudgmentStatus.COMPLETE,
                products,
                confidence=min(confidences) if confidences else 0.0,
                reason=reason,
                strategy="cell_delta",
            )
        elif resolved:
            # ② 일부만 설명 — 설명된 만큼만 PARTIAL, 나머지는 close 이월
            result = JudgmentResult(
                JudgmentStatus.PARTIAL,
                products,
                confidence=min(confidences) if confidences else 0.0,
                reason="cell_pending:" + ",".join(str(c.channel) for c in pending),
                strategy="cell_pending",
            )
        else:
            # ② 전부 보류 — 과금 없음, close 셀 net이 확정 (fail-closed)
            result = JudgmentResult(
                JudgmentStatus.NO_DETECTION,
                reason="cell_pending:" + ",".join(str(c.channel) for c in pending),
                strategy="cell_pending",
            )
        return JudgmentDecision(result, tuple(out_cells), tuple(contradictions))

    def _judge_cell(
        self, cell: CellOutcome, ctx: JudgmentContext, profile: SensorProfile
    ) -> tuple[CellOutcome, float, tuple[int, str] | None]:
        delta_abs = abs(cell.delta_weight)
        removal = cell.delta_weight < 0
        tol = profile.tolerance_grams
        by_id = {p.product_id: p for p in ctx.active_products}
        leader = _vision_leader(ctx.vision_candidates)

        known_id = ctx.identities.get(cell.channel)
        if known_id is not None and known_id in by_id:
            return self._judge_known(
                cell, by_id[known_id], ctx, delta_abs, removal, tol, leader
            )

        # ---- 미지 셀: V ∩ W 판별 ----
        pairs = _weight_pairs(delta_abs, ctx.active_products, tol, removal=removal)
        vision_classes = {c.class_id for c in ctx.vision_candidates}
        vw = [(p, n, amb) for p, n, amb in pairs if p.class_id > 0 and p.class_id in vision_classes]

        if len({p.product_id for p, _n, _a in vw}) == 1:
            p, n, amb = vw[0]
            conf = max(
                (c.confidence for c in ctx.vision_candidates if c.class_id == p.class_id),
                default=0.0,
            )
            if amb:
                # 정체성은 확정(신념 갱신 대상), 개수는 close net으로
                return _pending(cell, "count_pending", product_id=p.product_id), conf, None
            return _resolve(cell, p.product_id, n, "vision_weight_match"), conf, None

        if not vision_classes and len({p.product_id for p, _n, _a in pairs}) == 1:
            if profile.weight_is_discriminative:
                p, n, amb = pairs[0]
                if amb:
                    return (
                        _pending(cell, "count_pending", product_id=p.product_id),
                        _CONF_WEIGHT_ONLY,
                        None,
                    )
                return (
                    _resolve(cell, p.product_id, n, "weight_unique"),
                    _CONF_WEIGHT_ONLY,
                    None,
                )
            # 냉동: 무게 단독 정체성 채택 보류 (오차 5~15g — 이슈 #6 억제 원리)
            return _pending(cell, "weight_identity_suppressed"), 0.0, None

        reason = "identity_ambiguous" if pairs else "no_identity_evidence"
        return _pending(cell, reason), 0.0, None

    @staticmethod
    def _judge_known(
        cell: CellOutcome,
        p: ActiveProduct,
        ctx: JudgmentContext,
        delta_abs: float,
        removal: bool,
        tol: float,
        leader: VisionCandidate | None,
    ) -> tuple[CellOutcome, float, tuple[int, str] | None]:
        # 비전 교차검증 — 불일치는 note, 무게까지 다른 상품을 지목하면 강등 증거
        mismatch = ""
        contra: tuple[int, str] | None = None
        if leader is not None and leader.class_id > 0 and leader.class_id != p.class_id:
            rivals = [
                q
                for q in ctx.active_products
                if q.class_id == leader.class_id and q.product_id != p.product_id
            ]
            if rivals:
                mismatch = f"+vision_mismatch:{rivals[0].product_id}"
                if removal and _weight_pairs(delta_abs, rivals, tol, removal=True):
                    contra = (cell.channel, rivals[0].product_id)

        # known_* 접두어 계약: 신념을 근거로 한 결과는 pipeline이 신념에
        # 재반영하지 않는다 (자기 강화 루프 방지 — pipeline._observe_beliefs)
        w = p.unit_weight
        if w <= 0:
            return (
                _pending(cell, "known_unit_weight_missing", product_id=p.product_id),
                0.0,
                contra,
            )
        n = round(delta_abs / w)
        if n < 1 or abs(delta_abs - n * w) > tol or (removal and n > p.stock_qty):
            return (
                _pending(cell, "known_count_gate_failed" + mismatch, product_id=p.product_id),
                0.0,
                contra,
            )
        ambiguous = abs(delta_abs - (n + 1) * w) <= tol or (
            n > 1 and abs(delta_abs - (n - 1) * w) <= tol
        )
        if ambiguous:
            return (
                _pending(cell, "known_count_pending" + mismatch, product_id=p.product_id),
                0.0,
                contra,
            )
        conf = _CONF_KNOWN_IDENTITY
        if leader is not None and leader.class_id == p.class_id:
            conf = max(conf, leader.confidence)
        return (
            _resolve(cell, p.product_id, n, "known_identity" + mismatch),
            conf,
            contra,
        )


def _merge_removals(
    resolved: Sequence[CellOutcome], products: Sequence[ActiveProduct]
) -> tuple[ProductCount, ...]:
    """확정된 제거 셀들의 과금 품목 병합 (반품 셀은 과금하지 않음 — 정산 소관)."""
    by_id = {p.product_id: p for p in products}
    counts: dict[str, int] = {}
    for c in resolved:
        if c.delta_weight < 0 and c.product_id in by_id:
            counts[c.product_id] = counts.get(c.product_id, 0) + c.count
    return tuple(
        ProductCount(by_id[pid], n) for pid, n in sorted(counts.items()) if n > 0
    )
