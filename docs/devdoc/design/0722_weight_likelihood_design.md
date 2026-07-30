# 무게 이벤트의 확률화 — 우도비 상한 방식 설계 (research §1-2 승인분)

상태: **Phase 1 (shadow) 구현 완료** (2026-07-23, `judgment/likelihood.py` —
fix_logs.md 해당 항목 참조). Phase 2/3은 아카이브 실측 후.

> **주 (2026-07-24):** 본문이 σ_d 소스로 지목한 `trace.loadcell_shadow.delta_std`
> 는 BOCPD primary 승격 확정에 따른 shadow 장치 삭제로 더 이상 존재하지
> 않는다 — 현재 σ_d는 None(라이브러리 기본 σ) 고정이며, Phase 2 승격 시
> primary `BocpdLoadcellAnalyzer`에서 직접 뽑는 경로로 재연결한다
> (`service/pipeline.py` `_likelihood_shadow` 주석).
근거: claudedocs/research_judgment_performance_20260722.md
§1(FAIM/Grab), issue #16 실기 4건, docs/0722_issue16_arbitration_design.md.

## 1. 문제 — 경첩(hinge)의 불연속

현행 냉동 판정의 무게 규칙은 전부 이산 경계다: 하드 게이트 gate_n, near 밴드
(×2), single_share 50%, conf_override 0.9, conf_margin 0.15. 경계 바로 안팎에서
판정이 코인플립처럼 뒤집힌다 — #15(3g 차이), #16 로그 3(near 밴드를 2g 차로
초과) 모두 경계 사고였고, 그때마다 경계를 옮기거나(slack) 새 경계를 추가하는
(margin) 식으로 대응해 왔다. 경계는 늘어날수록 상호작용이 어려워진다.

## 2. 모델 — 점수 하나로

트레이 이벤트(delta d, 표준편차 σ_d)와 후보 배정 a = {(상품 p_i, 개수 n_i)}에
대해:

```
score(a) = log P_vision(a) + clamp(log L_weight(a), −log k, +log k)

log P_vision(a) = Σ_i [ α·log(votes_i / top_votes) + β·log conf_i ]
log L_weight(a) = −(d − Σ n_i·w_i)² / (2·σ_eff²)
σ_eff² = σ_d² + Σ_i n_i·σ_db²          # DB 개당 편차의 개수 비례 누적
```

- **σ_db ≈ 5g**: DB unit_weight는 정책상 고정이고 실측과 개당 5~15g 편차
  (라벨 168/DB 185, 베이글 DB 155/실측 ~148.6). 현행 `gate_n = 15 + 5×(n−1)`
  이 정확히 이 항의 이산 근사다.
- **σ_d**: BOCPD shadow가 이미 산출한다 (`trace.loadcell_shadow.delta_std`) —
  승격 시 자연 연결. 그 전에는 상수(양자화 2.5g × √2).
- **clamp(±log k)가 I-V의 연속판**: 무게 우도가 vision 사전비를 최대 k배까지만
  움직인다 = "무게는 거부권(가산점 아님), 선택권은 vision". k→1이면 무게 무력,
  k→∞면 무게가 정체성을 선택(금지된 것). 제안 기본 k = 20 (≈ conf 한 단계
  0.15 격차와 등가가 되도록 실측 보정 — conformal 절차 대상).

현행 이산 규칙과의 대응 (전부 이 모델의 특수해):

| 현행 | 모델에서의 해석 |
| --- | --- |
| 하드 게이트 gate_n | L_weight ≥ e^{−2} 수준의 컷 (잔차 ≈ 2σ_eff) |
| near 밴드 (top 보존 PARTIAL) | top의 L_weight가 낮지만 clamp 덕에 score 우위 유지 → 낮은 conf로 채택 |
| single_share / conf_override | log P_vision의 두 항 (득표비 + conf) |
| conf_margin 중재 | 두 후보 score 차의 유의성 검정 |
| I6 전량 설명 | 최종 채택 a의 L_weight 하한 (COMPLETE/PARTIAL 경계) |

## 3. 이행 계획 — 3단계, 사고 스위트가 게이트

**Phase 1 (shadow)**: `judgment/likelihood.py`에 score 계산기. 라우터가 판정을
낸 뒤, 후보 배정 후보군(단일 n개 × identity_pool + 기존 결과)의 score 순위를
계산해 **현행 판정과 1위가 다르면** `trace`에 diff 기록 (BOCPD shadow와 동일
패턴). 판정 무변경.

**Phase 2 (중재 대체)**: ① 복수 적합 중재(vt/bc/margin 삼단 규칙)를 score
비교로 교체 — 가장 국소적이고, 중재는 이미 "복수 후보가 거부권을 통과한 뒤"라
I-V 충돌이 없다.

**Phase 3 (①·④ 대체)**: 단일/refit 경로를 score 최대화로 통합. near-gate는
"top이 score 1위지만 L_weight가 하한 미달 → PARTIAL"로 자연 흡수.

승격 게이트 (전 단계 공통):
- 사고 재현 테스트 전건 green: #10 멜로나 필러 차단, #15 함정(185×2 잔차 0)
  차단, #16 A~D 4건, 370g 격상, 모호 폴스루.
- shadow diff 실측: 1위 불일치 세션을 아카이브에서 수집해 전수 검토 —
  score가 맞힌 비율이 우세할 때만 다음 Phase.

## 4. 파라미터와 보정

| 파라미터 | 초기값 | 보정 방법 |
| --- | --- | --- |
| σ_db | 5.0 g/개 | 아카이브의 (delta, 확정 배정) 쌍에서 잔차 분포 실측 |
| σ_d | BOCPD delta_std 또는 3.5g | BOCPD 승격과 연동 |
| k (clamp) | 20 | conformal: 정답 배정이 1위가 되는 최소 k의 분위수 |
| α, β (vision 가중) | 1.0, 1.0 | 동일 conformal 절차 |

전제: **아카이브 정답 라벨** (research §6과 공유되는 선행 조건) — 실험 시
실제 취출 품목/수량을 세션 아카이브에 기록하는 필드. 이것 없이는 Phase 1
diff의 정오 판정을 수기로 해야 한다.

## 5. 리스크

| 리스크 | 방어 |
| --- | --- |
| 연속 점수의 감사성 저하 (현행 라우터는 "어느 전략이 왜"가 명시적) | Phase별 국소 대체 + score 분해(log P_vision, log L, clamp 발동 여부)를 reason에 기록 |
| k 오보정 → 무게가 사실상 선택권 획득 | clamp는 코드 상수 아닌 env — 사고 시 k=1로 즉시 무력화 (거부권만 남음) |
| BOCPD 미승격 상태의 σ_d 부정확 | Phase 1은 diff 기록만이라 무해 — σ 민감도도 shadow에서 실측 |
