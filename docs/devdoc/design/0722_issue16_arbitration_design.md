# 냉동 판정 무게 중재 재설계 + 멀티트레이 PARTIAL 과금 (issue #16 설계안)

상태: **구현 완료** (2026-07-22, 같은 날 구현 — fix_logs.md 해당 항목 참조)
구현 중 확정된 설계 수정 2건:
- **③ 조합은 flat 게이트 유지** (원안은 gate_n 적용): 조합은 우연 적합 공간이
  조합적으로 커지고 실사고(#10 filler)가 조합형 — gate_n을 넣자 3종 정답
  케이스에서 k=2 오적합이 선점하는 회귀가 테스트로 확인됐다. n-스케일은
  ①(동일 정체성 n개)과 ④(유일-적합)에만 적용한다.
- **I6(enforce_full_delta_match)도 freezer 한정 n-스케일**: gate_n으로 적합을
  인정해 놓고 I6이 flat tolerance로 강등하면 두 게이트가 모순된다(370g
  케이스가 ① 통과 후 I6에서 강등되는 것으로 실측). 냉장(weight_is_
  discriminative)은 flat 유지. 라우터가 프로파일을 보고 slack을 전달한다.
근거 사고: GitHub issue #16 본문 + 코멘트 4건 (2026-07-22 실기, P0 배포본)
선행 반영: `_pool_exhaustion_retry`(2-pass 소진 재판정), ④ unique-refit 2계층화

## 1. 배경 — 사고 4건의 공통 구조

| 케이스 | 시나리오 | 정답 (vision 증거) | 실제 과금 | 직접 원인 |
| --- | --- | --- | --- | --- |
| A | 동시 2트레이 (155g+135g) | 27×1 + 40×1 (40: conf 1.0, 12표) | 27×1 | single_share가 40 배제 → near-gate가 27 보존 PARTIAL (소진 재판정+④ 2계층화로 **해결됨**) |
| B | 순차 2트레이 (185g+~105g) | 13×1 + 30×1 (30: top, conf 0.86) | 13×1 | 30 잔차 ~20-25g(DB 편차) → near-gate PARTIAL — **정체성은 맞는데 병합이 PARTIAL을 버림** |
| C | 동일 상품 5개 연속 (베이글×5) | 27×5 (conf 1.0, 34표) | **13×4 (오상품·오수량)** | 27 자기 적합 잔차 32g(눌림 오염+누적 편차) > 게이트 → 득표 2위 13이 4×185=740 우연 적합(잔차 3) **선착 COMPLETE** |
| D | 단품 취출 (class 23) | 23×1 (conf 1.0, 19표) | **13×1 (오상품)** | 진열 만두가 63표로 득표 1위 → 23은 share(50%) 미달로 시도 불가, 13이 잔차 10 우연 적합 COMPLETE |

공통: **정답 상품은 매번 vision에 선명히 보였다(conf 0.95~1.0). 오과금을 만든 건
무게 산술이 확정권을 쥐는 구조다.** 그리고 그 무게의 두 입력이 모두 부정확하다:

- DB unit_weight는 실측과 10~30g 편차 (정책상 고정 — 성에·포장 편차를 tolerance로
  흡수하는 운영. 예: 만두 라벨 168g/DB 185g, 베이글 DB 155g/실측 개당 ~148g)
- 로드셀 delta는 접촉 오염(눌림 8~32g 실측)과 5g 양자화·0.8s 캐던스 절단을 겪는다

## 2. 원칙 — I-V의 재확인이자 완성

I-V(이슈 #15): "무게로 정체성을 **선택**하는 것은 금지, 무게의 권한은 개수
산정·검증과 반증에 한정." 그런데 현행 ① 단일 경로는 *득표순 선착 적합*이라,
1위가 무게로 탈락하는 순간 **무게가 2위를 선택**한다 — I-V가 금지한 바로 그
동작이 예외 경로로 존재한다 (C·D의 뿌리).

재설계 원칙:

> **무게는 거부권(veto)만 갖는다. 복수 후보가 거부권을 통과하면 선택은 반드시
> vision 증거(득표·conf)가 한다. conf는 P1-4(max 결합) 이후 "얼마나 선명하게
> 보였나"의 신뢰 가능한 신호다.**

## 3. 설계 3 — FreezerVisionFirst ① 단일 경로 재설계

### 3a. n-스케일 게이트 (C의 절반)

DB 편차와 접촉 오염은 **개수·픽 횟수에 비례해 누적**되는데 게이트는 flat ±15g다.

```
gate_n(n) = count_gate + COUNT_UNIT_SLACK × (n − 1)      # 기본 slack 5.0 g/개
near_n(n) = near_factor × gate_n(n)
```

- n=1이면 기존과 완전 동일 (±15 유지 — 단품 의미론 무변경).
- C: n=5 → gate_n=35 ≥ 잔차 32 → 베이글×5가 자기 적합에 성공.
- 원본 대응물: `SAME_PRODUCT_COUNT_TOLERANCE_GRAMS`(개당 5g) — 원본도 동일
  상품 반복은 개당 스케일이었다. HG 이식 시 누락된 축.
- 적용 범위: ① 적합, ② near 판정, ③ 조합 allocations(총 개수 기준),
  ④ refit 2계층(fits_gate/fits_near), **settler 냉동 close 재solve의 I3 게이트**
  (settler.py `abs(-net - count*w) <= gate` → `<= gate_n(count)`). I3 게이트를
  언급하는 두 지점(판정·정산)을 함께 바꿔 일관성 유지.

### 3b. 선착 폐지 — 복수 적합 중재 (C의 나머지 절반)

①은 자격 후보 전원의 적합을 **모두 수집**한 뒤 결정한다. 득표순 순회 중
첫 적합 반환(선착)을 폐지한다.

```
fits = [(p, cand, n, r)] : eligible 후보 중 r ≤ gate_n(n)

len(fits)==1 → COMPLETE (기존 동형, 이제 순서 무관)
len(fits)≥2 → 중재:
    vt = fits 중 최다 득표 적합
    bc = fits 중 최고 conf 적합
    if bc is vt:                       → vt COMPLETE            # 증거 일치
    elif bc.conf ≥ vt.conf + CONF_MARGIN:
                                       → bc COMPLETE            # conf 결정적 우세
                                         reason "…single_arbitrated"
    elif vt가 전역 득표 1위:            → vt COMPLETE            # 종전 서열 존중
    else:                              → 모호 → ② near로 진행    # ④와 같은 태도
len(fits)==0 → ② near (기존)
```

- C: fits = {27×5(r=32, 34표, conf 1.0), 13×4(r=3, 25표, 0.80)} → vt=bc=27 →
  **27×5 COMPLETE** (14,000원). 잔차가 훨씬 작은 13이 지는 것이 요점 —
  잔차는 중재 기준이 아니다(무게=거부권 원칙).

### 3c. conf 자격 확장 (D, A의 근본 해결)

single_share(top 득표의 50%)는 "표가 충분히 갈린 후보만 무게 중재 대상"이라는
전제인데, 득표 자체가 진열 오염·baseline으로 왜곡되면 진짜 상품이 자격을 잃는다
(D: 19표 vs 오염 63표 = 30%). conf는 이 왜곡과 독립적이다.

```
eligible = { votes ≥ single_share × top_votes }                      # 기존
         ∪ { conf ≥ CONF_OVERRIDE  AND  votes ≥ refit_share × top_votes }   # 신설
```

- 기본 CONF_OVERRIDE=0.9: "양 카메라에서 선명하게 보였다" 수준만 통과.
- 득표 하한은 ④ refit과 같은 refit_share(10%) 재사용 — "vision이 사실상 못 본"
  후보(이슈 #10 멜로나 3표=1.75%)는 여전히 배제.
- D: 23(conf 1.0, 30%) 자격 획득 → fits={13(r=10, 63표, 0.79), 23(r≈0, 19표, 1.0)}
  → 중재: bc=23, 1.0 ≥ 0.79+0.15 → **23×1 COMPLETE**.
  ※ 전제: |175 − w23| ≤ 15 (w23 DB 확인 필요 — 읽기만, 변경 아님. 부적합이면
  이 케이스는 어떤 규칙으로도 못 살리며 fail 방향은 현행과 동일).
- A: 40(conf 1.0, 19%) 자격 획득 → 유일 적합(r=0) → **①에서 직접 COMPLETE**.
  소진 재판정·④는 2차 방어선으로 유지.
- 안전성 비교: 이미 출하된 ④ unique-refit은 "top 반증 시 refit_share(10%)+유일
  적합"만으로 conf×0.8 COMPLETE를 준다. 3c는 그보다 **높은 문턱**(conf 0.9)에
  중재 가드까지 있어 엄격히 더 보수적이다.

### 적대적 검증 — 기존 사고 픽스처 통과 확인

`test_near_gate_keeps_top_identity_and_count`(−370g, 후보: 23번 176g/65표/0.86,
13번 185g/16표/0.66 — **13이 185×2=370 잔차 0으로 정확 적합하는 함정**):

- 13의 자격: share 25% < 50% ✗, conf 0.66 < 0.9 ✗ → 배제 (양쪽 문 모두 차단)
- 23: gate_n(2)=20 ≥ 잔차 18 → **유일 적합 → 23×2 COMPLETE** (현행은 near-gate
  PARTIAL — 과금 동일, 상태만 격상. 테스트 기대값 1건 갱신 필요)

`test_gate_near_miss_keeps_identity_as_partial`(−178, 100g×2, 잔차 22):
gate_n(2)=20 < 22 → 여전히 near 밴드(40 내) → PARTIAL 유지. 무변경.

이슈 #10 멜로나(79×3, 3표): refit_share 하한으로 자격·모호성 판단 모두에서
배제 — 무변경.

## 4. 설계 4 — 멀티트레이 병합에 "고유 정체성 PARTIAL" 포함

근거: 정산기는 에러가 아닌 **모든** 판정의 products를 집계한다(settler.py:84,123).
즉 단일 트리거의 near-gate PARTIAL은 과금된다(#15 정답 경로). 멀티트레이 병합만
COMPLETE 한정이라, 두 취출이 한 영상(≈4초 이내)에 담기면 같은 증거로 덜
과금된다 (B: 5초만 늦게 집었으면 둘 다 과금됐다).

```
(소진 재판정 후)
complete_ids = ∪ COMPLETE 이벤트 products의 class_id
billable_partials = PARTIAL 이벤트 중 products가 있고,
    ① class_id가 complete_ids와 겹치지 않고            # A형 표-그림자 가드
    ② 다른 billable partial과도 겹치지 않는 것          # 상호 중복 → 전부 제외
merged = COMPLETE products + billable_partials products
status = 전건 COMPLETE면 COMPLETE, merged 있으면 PARTIAL, 없으면 NO_DETECTION
confidence = 기여 이벤트들의 min
reason에 "partial_billed:ch{N}" 표기 (아카이브 식별)
```

- 가드 ①: 형제 COMPLETE의 상품을 물고 있는 PARTIAL은 표-그림자 오염 산물
  (A의 1차 판정 형태) — 현행대로 제외.
- 가드 ②: PARTIAL끼리 같은 정체성이면 대칭 오염 가능성 — 과청구가 미청구보다
  나쁘다(I13/D9)는 원칙대로 전부 제외 (현행 동작 유지).
- B: ch0 COMPLETE(13) + ch1 PARTIAL(30, 고유) → **13×1 + 30×1 과금** ✓.
- 설계 3이 들어가면 A·C·D형 PARTIAL 다수가 COMPLETE로 격상되므로, 설계 4가
  실제로 커버하는 것은 B형(DB 편차 near-gate 잔존)이다 — 겹침 없이 상보적.

## 5. 케이스 시뮬레이션 (설계 3+4 동시 적용 시)

| 케이스 | 현행 | 설계 후 | 경로 |
| --- | --- | --- | --- |
| A 동시 | 27+40 (재판정으로 해결) | 27+40 | 3c로 ①에서 직접, 재판정 불요 |
| B 순차 | 13만 | **13+30** | 4 (고유 PARTIAL 과금) |
| C 5연속 | 13×4 오과금 | **27×5** | 3a(gate_n 35≥32) + 3b(중재 vt=bc=27) |
| D 단품 | 13 오과금 | **23** | 3c(conf 자격) + 3b(margin 중재) |
| #15 −370 | 23×2 PARTIAL | 23×2 **COMPLETE** | 3a 격상 (과금 동일) |
| #10 멜로나 | 차단 | 차단 | refit_share 하한 유지 |

## 6. 신규 노브 + 롤백

| env | 기본 | 의미 | 사실상 비활성값 |
| --- | --- | --- | --- |
| `MODEL__JUDGMENT__COUNT_UNIT_SLACK` | 5.0 | 개수당 게이트 가산(g) — 3a | 0 |
| `MODEL__JUDGMENT__CONF_OVERRIDE` | 0.9 | conf 자격 문턱 — 3c | 2.0 |
| `MODEL__JUDGMENT__CONF_MARGIN` | 0.15 | 중재 시 conf 우세 최소 격차 — 3b | 2.0 |

셋 다 비활성값이면 ①은 "적합 전수 수집 후 vote-top 우선"이 되는데, 이는 선착과
결과 동일(유일 적합=동일, 복수 적합=vote-top이 선착보다 앞서거나 같은 순위).
즉 env만으로 현행 동작 복원 가능 — 기존 judgment 노브들과 같은 배선
(Settings → `default_pipeline(freezer_strategy=…)`).

## 7. 구현 지점

| # | 파일 | 내용 |
| --- | --- | --- |
| 3a | `judgment/strategies.py` | `fit()` → `(n, r, gate_n)` 반환, ①②③④ 게이트 치환 |
| 3a | `ledger/settler.py:326-347` | 냉동 close 재solve 게이트 `gate_n(count)` |
| 3b/3c | `judgment/strategies.py` ① | 자격 확장 + 적합 수집 + 중재, reason 태그 |
| 4 | `service/pipeline.py _judge_tray_events` | billable_partials 병합 + reason |
| env | `core/config.py`, `service/model_service.py`, `.env.example` | 노브 3종 배선 |

## 8. 테스트 계획

- 전략 단위: C(중재 vt=bc), D(conf 자격+margin 중재), 중재 모호→② 폴스루,
  slack=0 롤백 동형성, gate_n 경계(32 vs 35), #15 함정 픽스처(13 정확 적합 배제)
- 파이프라인 단위: B(고유 PARTIAL 병합 과금), 상호 중복 PARTIAL 제외,
  A 회귀(기존 2건) 유지
- 기대값 변경 1건: `test_near_gate_keeps_top_identity_and_count` PARTIAL→COMPLETE
  (과금 동일, §3a 격상 — 의도된 변경으로 문서화)
- settler: close 재solve gate_n 케이스 (C형: |753−775|=21.7 ≤ gate_n(5)=35 확정)

## 9. 리스크

| 리스크 | 방어 |
| --- | --- |
| conf 높은 유령이 무게 우연 적합으로 채택 | conf 0.9 + refit_share 10% + margin 0.15 + 무게 거부권 4중 문턱. ④(이미 출하)보다 보수적 |
| gate_n 확대로 우연 적합 증가 | n≥2에만 적용, 개당 5g(양자화 1단위). 중재가 선택권을 vision에 묶어 우연 적합의 "승리"를 차단 |
| PARTIAL 과금으로 과청구 | 고유 정체성 한정 + 상호 중복 제외 + 단일 트리거와 동일한 기존 정산 의미론 |
| 동작 변화 관측 불가 | reason 태그(`single_arbitrated`, `partial_billed:chN`) + 기존 vote_summary로 아카이브 사후 검증 |
