"""무게 이벤트 확률화 — 우도비 상한 score 계산기 (Phase 1: shadow 전용).

docs/0722_weight_likelihood_design.md 의 §2 모델을 그대로 구현한다:

    score(a) = log P_vision(a) + clamp(log L_weight(a), −log k, +log k)
    log P_vision(a) = Σ_i [ α·log(votes_i / top_votes) + β·log conf_i ]
    log L_weight(a) = −(d − Σ n_i·w_i)² / (2·σ_eff²)
    σ_eff² = σ_d² + Σ_i n_i·σ_db²          # DB 개당 편차의 개수 비례 누적

clamp(±log k)가 I-V의 연속판이다: 무게 우도는 vision 사전비를 최대 k배까지만
움직인다 = "무게는 거부권, 선택권은 vision". k→1이면 무게 무력, k→∞면 무게가
정체성을 선택(금지된 것).

Phase 1 계약 (판정 무변경): 라우터가 판정을 낸 **뒤** 후보 배정 후보군
(단일 정체성 n개 × identity_pool + 기존 판정 결과)의 score 순위를 계산해,
현행 판정과 1위가 다르면 trace에 diff를 기록한다 — BOCPD shadow와 동일 패턴.
승격(Phase 2: ① 중재 대체, Phase 3: ①·④ 대체)은 아카이브 실측(정답 라벨
대비 score 1위 정오 비율)이 우세할 때만 진행한다.

σ_d는 BOCPD shadow의 delta_std가 있으면 그것을(승격 시 자연 연결), 없으면
상수(양자화 2.5g × √2 ≈ 3.5g)를 쓴다. σ_db(기본 5g/개)는 DB unit_weight의
개당 실측 편차 — 현행 gate_n = 15 + 5×(n−1)이 정확히 이 항의 이산 근사다.
"""
from __future__ import annotations

import math

from crk_model.core.types import JudgmentResult, VisionCandidate
from crk_model.judgment.interfaces import JudgmentContext

# log(0) 방어 하한 — 판정 결과에 포함됐지만 vision에 관측되지 않은 정체성
# (weight_only 계열 등)의 vision 항을 유한하게 유지한다. 값 자체는 순위
# 비교에만 쓰이므로 절대 크기는 중요하지 않다.
_CONF_FLOOR = 1e-3
_VOTE_FLOOR = 0.5


class WeightLikelihoodScorer:
    """단일 score로 vision 사전비와 무게 우도(상한 clamp)를 결합한다.

    적용 조건은 FreezerVisionFirst와 동형 — freezer(무게가 정체성 판별자
    자격이 없는 프로파일) removal에 vision 후보가 있을 때. 냉장은 현행
    무게-판별 전략들이 담당하므로 Phase 1 스코프 밖이다.
    """

    def __init__(
        self,
        *,
        k: float = 20.0,
        # 우도비 상한 — conf 한 단계(0.15) 격차와 등가가 되도록 실측 보정
        # 대상 (conformal). env로 노출: 사고 시 k=1로 즉시 무력화(거부권만 남음).
        sigma_db: float = 5.0,
        # DB unit_weight 개당 편차(g) — 아카이브 (delta, 확정 배정) 잔차로 보정.
        sigma_d_default: float = 3.5,
        # BOCPD delta_std 부재 시의 로드셀 delta 표준편차 (양자화 2.5g × √2).
        alpha: float = 1.0,
        beta: float = 1.0,
        identity_pool: int = 6,
        # 단일 배정 후보군 크기 — FreezerVisionFirst identity_pool과 동형.
    ):
        if k < 1.0:
            raise ValueError(f"likelihood clamp k must be >= 1, got {k}")
        self._log_k = math.log(k)
        self._k = k
        self._sigma_db = sigma_db
        self._sigma_d_default = sigma_d_default
        self._alpha = alpha
        self._beta = beta
        self._identity_pool = identity_pool

    def applicable(self, ctx: JudgmentContext) -> bool:
        return (
            not ctx.profile.weight_is_discriminative
            and ctx.delta_weight < 0
            and bool(ctx.vision_candidates)
        )

    def shadow(
        self,
        ctx: JudgmentContext,
        judgment: JudgmentResult,
        *,
        sigma_d: float | None = None,
        tray_prior: dict[int, float] | None = None,
    ) -> dict | None:
        """현행 판정과 score 순위의 diff 기록. 비적용 컨텍스트면 None.

        tray_prior: 세션 트레이 메모리(ledger/tray_memory.py)가 산출한
        class_id별 로그 prior — 같은 트레이 확정 이력이면 +, 같은 세션의
        다른 트레이에서 이미 설명된 정체성이면 −. score의 세 번째 항
        (log_p_tray)으로 들어간다. None/빈 dict이면 중립(현행과 동일)."""
        if not self.applicable(ctx):
            return None
        sd = sigma_d if sigma_d is not None and sigma_d > 0 else self._sigma_d_default
        by_class = {
            p.class_id: p
            for p in ctx.active_products
            if p.stock_qty > 0 and p.class_id > 0 and p.unit_weight > 0
        }
        cand_by_class = {c.class_id: c for c in ctx.vision_candidates}
        top_votes = max(
            (c.vote_count for c in ctx.vision_candidates), default=0
        )
        if top_votes <= 0 or not by_class:
            return None
        target = abs(ctx.delta_weight)

        # 배정 후보군: 단일 정체성 n개 (identity_pool 상위) + 현행 판정 결과
        ranked = sorted(
            ctx.vision_candidates, key=lambda c: (-c.vote_count, -c.confidence)
        )
        assignments: dict[tuple[tuple[int, int], ...], None] = {}
        for c in ranked:
            p = by_class.get(c.class_id)
            if p is None:
                continue
            n = min(max(1, round(target / p.unit_weight)), p.stock_qty)  # I12
            assignments[((p.class_id, n),)] = None
            if len(assignments) >= self._identity_pool:
                break
        current_items = tuple(
            sorted((pc.product.class_id, pc.count) for pc in judgment.products)
        )
        if current_items and all(cid in by_class for cid, _ in current_items):
            assignments[current_items] = None
        if not assignments:
            return None

        scored = [
            self._score(
                items, target, sd, by_class, cand_by_class, top_votes,
                tray_prior or {},
            )
            for items in assignments
        ]
        scored.sort(key=lambda e: -e["score"])
        top = scored[0]
        current_entry = next(
            (e for e in scored if tuple(map(tuple, e["items"])) == current_items),
            None,
        )
        return {
            "scorer": "weight_likelihood",
            "k": self._k,
            "sigma_d": round(sd, 2),
            "sigma_db": self._sigma_db,
            **({"tray_prior": dict(tray_prior)} if tray_prior else {}),
            "current": {
                "items": [list(it) for it in current_items],
                "score": current_entry["score"] if current_entry else None,
            },
            "top": top,
            "mismatch": tuple(map(tuple, top["items"])) != current_items,
            "ranking": scored[:5],
        }

    def _score(
        self,
        items: tuple[tuple[int, int], ...],
        target: float,
        sigma_d: float,
        by_class: dict,
        cand_by_class: dict[int, VisionCandidate],
        top_votes: int,
        tray_prior: dict[int, float] | None = None,
    ) -> dict:
        tray_prior = tray_prior or {}
        log_p_vision = 0.0
        log_p_tray = 0.0
        expected = 0.0
        total_count = 0
        for class_id, count in items:
            p = by_class[class_id]
            expected += count * p.unit_weight
            total_count += count
            cand = cand_by_class.get(class_id)
            votes = max(cand.vote_count if cand else 0, _VOTE_FLOOR)
            conf = max(cand.confidence if cand else 0.0, _CONF_FLOOR)
            log_p_vision += self._alpha * math.log(votes / top_votes)
            log_p_vision += self._beta * math.log(conf)
            # 정체성당 1회 (개수 비례 아님) — prior는 "이 상품이 이 트레이
            # 사건의 설명에 등장하는가"에 대한 증거이지 개수 증거가 아니다.
            log_p_tray += tray_prior.get(class_id, 0.0)
        sigma_eff_sq = sigma_d**2 + total_count * self._sigma_db**2
        residual = target - expected
        log_l_weight = -(residual**2) / (2.0 * sigma_eff_sq)
        clamped_l = max(-self._log_k, min(self._log_k, log_l_weight))
        entry = {
            "items": [list(it) for it in items],
            "score": round(log_p_vision + log_p_tray + clamped_l, 3),
            "log_p_vision": round(log_p_vision, 3),
            "log_l_weight": round(log_l_weight, 3),
            "clamped": clamped_l != log_l_weight,
            "residual": round(residual, 1),
        }
        if log_p_tray != 0.0:
            entry["log_p_tray"] = round(log_p_tray, 3)
        return entry
