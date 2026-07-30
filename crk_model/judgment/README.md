# `judgment/` — 무게·비전 증거로 "무엇을 몇 개"를 확정하는 순수 판정 계층

> 계층 위치: 상위는 `service/pipeline.py`(트리거 판정)와 `ledger/`의 close 2차 패스
> (`cross_zone`·`ghost_ledger` 재판정), 하위는 `core/`(타입·`SensorProfile`)뿐 · 상태성: 무상태 — 전략은
> 부수효과가 없고, `JudgmentRouter`가 갖는 유일한 상태는 유계 진단 카운터(텔레메트리
> `Counter`, `miss_log` = `maxlen=256` deque)다
> 런타임 의존성: 없음(표준 라이브러리) · 입력 `JudgmentContext` → 출력 `JudgmentResult`

상위 문서: [03. 판정과 정산](../../docs/03-judgment-and-settlement.md)(전체 흐름 다이어그램·
설계 결정 D/불변식 I 목록) · 형제 패키지: [`perception/`](../perception/README.md)(득표 순위 생성)

---

## 1. 책임과 경계

`judge(ctx) -> JudgmentResult` 한 함수로 요약된다. 같은 입력이면 언제 어디서
호출해도 같은 결과가 나오고(순수), 그래서 트리거 판정과 close 2차 패스의
재판정(`ledger/cross_zone.py`·`ledger/ghost_ledger.py`)이 **같은 계층을 재사용**할
수 있다. 단 후자는 라우터를 주입받지 않으면 기본 인스턴스를 만든다(§7-7).

**한다**

- 우선순위대로 전략을 시도해 `(status, products, confidence, reason, strategy)` 확정
- 무게 조합 탐색(백트래킹)과 냉동 전용 vision-first 판정
- 모든 성공 결과에 **전량 설명 검사**(I6) 적용 — 부분 설명이면 `PARTIAL`로 강등
- 어느 전략이 판정했는지 텔레메트리로 축적 (실전에서 안 맞는 전략의 제거 근거)

**하지 않는다**

- **증거 생성**: 득표 순위·conf는 `perception/`이, delta·세그먼트는 `ingest/`가 만든다.
  판정층은 주어진 득표 순위를 신뢰하며 오검출 억제를 재시도하지 않는다(층별 단일 책임).
- **금액·재고 계산·영속화**: 정산과 이벤트 기록은 `ledger/`.
- **정책 결정**: tolerance·게이트 임계는 `core/profiles.py`의 `SensorProfile`이 소유하고,
  이 계층은 주입받은 값을 쓸 뿐이다(이중 기준 금지 — 조기 종료도 같은 값을 쓴다).
- **재시도·타임아웃·I/O**: 세그먼트 타깃 재판정 같은 "언제 다시 부를지"는 `service/`.

## 2. 구성 파일

| 파일 | 역할 | 핵심 진입점 |
|---|---|---|
| `interfaces.py` | `JudgmentContext` + `Stage`/`Strategy` 프로토콜 | `JudgmentContext` |
| `router.py` | 우선순위 파이프라인 선언과 실행, I6 강제, 텔레메트리 | `default_pipeline()` / `JudgmentRouter.judge` |
| `strategies.py` | 전략 16종 + Stage 1종 + `enforce_full_delta_match` | 각 `*Strategy.precondition/solve` |
| `strict.py` | 무게 우선 백트래킹 조합 탐색 | `StrictWeightMatcher.find_valid_combinations` / `.best` |
| `__init__.py` | 공개 표면 재수출 | — |

합계 1,350행 (`strategies.py`가 1,015행 — 전략별 docstring에 실기 사고 근거가 붙어
있어서다. 코드보다 "왜 이 순서·이 임계인가"가 길다면 의도된 것이다).

## 3. 파일별 상세

### `interfaces.py`

**Stage와 Strategy를 인터페이스에서 구분**하는 것이 이 파일의 유일한 설계
결정이다(L5 승인 조건 ①). 후속 분기의 **입력을 변형**하는 단계는 Strategy로
표현할 수 없다 — Strategy는 "판정하거나 다음으로 넘긴다"(`solve → Result | None`)
이지 컨텍스트를 바꿔서 통과시키는 형태가 아니기 때문이다.

| 프로토콜 | 시그니처 | 의미 |
|---|---|---|
| `Stage` | `apply(ctx) -> ctx` | 입력 변환기. 판정하지 않고 `stage_hints`만 채운다 |
| `Strategy` | `precondition(ctx) -> bool`, `solve(ctx) -> Result \| None` | 결정자. 전제 불충족이면 건너뛰고, `None`이면 다음 전략으로 |

`JudgmentContext`는 frozen dataclass다 — 존, 프로파일, `delta_weight`,
`segments`, `vision_candidates`, `active_products`, `vision_only`, `stage_hints`.
`active_products`가 매칭의 유일한 권위 소스이며(C7), 여기 없는 상품은 어떤
전략도 청구할 수 없다.

### `router.py`

**우선순위가 데이터(리스트)로 선언된다.** `default_pipeline()`이 반환하는
리스트의 순서가 곧 판정 순서이므로, 순서를 바꾸는 변경은 **diff 한 줄**이고
`tests/test_judgment.py::TestPipelineOrder`가 그 순서를 문자열로 고정한다.

`judge()` 루프의 계약 4개:

1. `Stage`면 `ctx`를 갱신하고 계속 (판정하지 않음).
2. `precondition`이 False면 조용히 건너뜀. `solve`가 `None`이면
   `miss_log`에 `"<name>_mismatch"` 기록 후 다음 전략(I8 — 사유가 남아야 한다).
3. **모든 성공 결과가 `enforce_full_delta_match`(I6)를 통과**한다. 냉동에서만
   `count_unit_slack`이 전달된다 — `gate_n`으로 적합을 인정해 놓고 I6이 flat
   tolerance로 강등하면 두 게이트가 서로 모순되기 때문이다. 냉장은 무게가
   판별자라 flat 유지.
4. 반환 직전 `telemetry[name] += 1`과 `strategy=name` 각인. 히트 분포가 "실전에서
   한 번도 안 맞는 전략"의 제거 근거이고, `miss_log`는 24h+ soak에서 무상한
   성장을 막기 위해 `deque(maxlen=256)`다.

**relaxed 하위 순서(9.1~9.4)는 원본과 의도적으로 다르다.** 원본
`_judge_relaxed`는 자체 partial(count=1, 무게 무검증)까지 반환하고 `is_success`가
COMPLETE·PARTIAL을 모두 성공으로 취급하므로, 뒤따르는 `detected_single`·
`vision_first_identity_partial`이 사실상 도달 불가한 사문화 코드가 된다.
CRK-model-HG는 결제 정확도상 **"무게로 뒷받침된 count 격상" > "무검증 count=1"**
이라고 보아 `relaxed_partial`을 9.4 최종 폴백으로 내리고, 9.1~9.3이 먼저 COMPLETE
격상을 시도하게 재배치했다. 각 precondition이 서로 겹치지 않게 좁혀져 있어
(9.1은 allowlist 완전 불일치 전용, 9.2/9.4는 프로파일로 상호 배타) 실질 충돌은 없다.

### `strategies.py`

순서 원칙은 **"누적 + 특이도 우선"** — 특수한 전제를 가진 전략이 앞, 일반 폴백이
뒤다. 냉동 1순위(센서 물리)와 `segment > aggregate`(시계열 정보 보존)는
바꿀 수 없는 필연적 순서다.

#### 불변식 I-V (이슈 #15 신설)

> `weight_is_discriminative=False`(냉동)에서 **청구 정체성은 vision 득표 순위에서만
> 유도한다.** 무게의 권한은 ⑴ 지목된 정체성의 개수 산정·검증과 ⑵ 정체성의 반증뿐이며,
> 무게 적합성이 정체성을 **선택**하는 경로는 금지한다.

실기 사고가 근거다 — 득표 1위(65표, conf 0.86)가 게이트를 3g 차이로 놓치자
16표 배경 후보가 "무게가 맞아서" COMPLETE로 채택되어, 같은 상품 2회 연속 취출이
로드셀 20g 차이로 서로 다른 두 상품으로 과금됐다. ±15g 창은 여러 상품이 우연히
걸릴 만큼 넓다.

| 구분 | 전략 |
|---|---|
| **냉동 배제** — precondition에 `weight_is_discriminative`. 앞의 7종은 무게로 후보 중 정체성을 고르기 때문이고, `relaxed_partial`은 냉동에서 더 보수적인 9.2가 그 역할을 전담하기 때문이다 | `segment_weight_matching`, `stage_count_combo`(양 인스턴스), `same_weight_collision_guard`, `strict`, `same_product_count`, `relaxed`, `relaxed_loadcell_only`, `relaxed_partial` |
| 냉동에서 정체성을 청구할 수 있음 (전부 "정체성은 vision, 무게는 개수/검증") | `vision_only`, `freezer_vision_first`(밴드·근접·조합·유일적합), `vision_first_identity_partial`, `detected_single_item_fallback`(top 고정) |
| 정체성을 청구하지 않는 가드 | `min_weight_gate`, `no_candidate_fallback`(냉동은 `loadcell_identity_suppressed`), `forced_final` |

허용된 유일한 예외는 `freezer_vision_first` ④의 **유일-적합 구제**다 — top이
결정적으로 반증됐고 나머지 중 적합이 **정확히 하나**일 때만.

#### 전략 목록 (현행 `default_pipeline()` 순서)

전체 흐름 다이어그램은 [03. 판정과 정산](../../docs/03-judgment-and-settlement.md)에
있다. 여기서는 계약만 표로 고정한다.

| # | 전략 (`name`) | 전제 (precondition) | 결정 | 실패 시 |
|---|---|---|---|---|
| 0 | `vision_only` | `ctx.vision_only` | 최상위 후보 count=1, conf×0.7 (I6 미적용 — 설명할 delta가 없음) | allowlist에 매칭되는 후보가 없으면 `NO_DETECTION(no_vision_candidates)` |
| 1 | `freezer_vision_first` | 냉동 ∧ 후보 있음 ∧ `delta < 0` | ①밴드 내 단일 → ②top 근접 PARTIAL → ③top 포함 조합 → ④유일-적합 구제 | `None` → 9.2가 정체성 보존 |
| 2 | `augment_stage_weight_gate` | **Stage** — 결정자 아님 | 제거 세그먼트 delta의 절댓값 목록을 `stage_hints["segment_targets"]`로 주입 | 제거 구간 없으면 ctx 그대로 |
| 3 | `segment_weight_matching` | 냉장 ∧ 제거 구간 ≥ 2 ∧ 후보 있음 | 구간별 최적 조합을 병합 (I12: 합산 count ≤ stock) | 한 구간이라도 실패 → `None` |
| 3.5 | `stage_count_combo` (`require_no_vision=True`) | 냉장 ∧ **후보 없음** ∧ targets ≥ 2 | 전 재고 센티널 후보로 구간별 매칭, `total_count ≥ 2`만 채택 | `None` → 4 |
| 4 | `no_candidate_fallback` | 후보 없음 ∧ not vision_only | 냉동: `loadcell_identity_suppressed`. 냉장: `(품목, n)` 전수 탐색 후 **유일**하면 `weight_only`(conf 0.3) | 없으면 `no_candidates_forced_final`, 2쌍 이상이면 `weight_only_ambiguous` — **항상 확정(None 없음)** |
| 5 | `min_weight_gate` | 후보 있음 ∧ delta 절댓값 < `min_weight_change_grams` | `NO_DETECTION(below_min_weight_change)` | — |
| 6 | `same_weight_collision_guard` | 냉장 ∧ 후보 있음 ∧ `delta < 0` | 동일 무게대(±tol) 후보 ≥ 2면 최고 conf 채택 | 충돌 아니면 `None` |
| 7 | `strict` | 냉장 ∧ 후보 있음 | `StrictWeightMatcher.best` (기본 경로) | `None` → 7.5 |
| 7.5 | `stage_count_combo` (기본) | 냉장 ∧ targets ≥ 2 | strict 실패 후 stage 조합 구제 | `None` → 8 |
| 8 | `same_product_count` | 냉장 ∧ 후보 있음 | 동일 품목 n≥2개 중 오차 최소 (`err ≤ tol`) | `None` → 9 |
| 9 | `relaxed` | 냉장 ∧ 후보 있음 | tolerance×2로 조합 재시도, conf×0.8 (I6이 원래 tol로 재검증) | `None` → 9.1 |
| 9.1 | `relaxed_loadcell_only` | 냉장 ∧ 후보 있음 ∧ **모든 후보가 allowlist 불일치** | 전 재고 nearest-single(오차 < 5g) PARTIAL, count=1 | 5g 이상이면 `None` |
| 9.2 | `vision_first_identity_partial` | 냉동 ∧ 후보 있음 ∧ `delta < 0` | 무게검증 1회 — 통과 시 `COMPLETE(vision_identity_weight_validated)`, 실패 시 count=1 PARTIAL | 청구 conf < `partial_min_confidence`면 `None` (후보 쇼핑 금지 — 하위 후보로 내려가지 않는다) |
| 9.3 | `detected_single_item_fallback` | `delta < 0` ∧ 후보 있음 (프로파일 무관) | top 후보만 보고 tolerance×3까지 구제, conf 상한 0.65 | 잔차 초과면 `None` |
| 9.4 | `relaxed_partial` | 냉장 ∧ 후보 있음 | 정체성 보존 count=1 PARTIAL (무게 무검증), conf×0.5 | conf 하한 미달이면 `None` |
| 10 | `forced_final` | 항상 True | `NO_DETECTION(forced_final_no_match)` | — |

3.5와 7.5는 원본에서 동일 헬퍼가 호출되는 **두 지점**이다. 인스턴스를 둘 두고
`require_no_vision`으로 유효 구간을 분리해 원본 순서를 보존한다.
`no_candidate_fallback`은 결코 `None`을 반환하지 않으므로 3.5는 반드시 그 **앞**에
있어야 후보없음 체인의 첫 단계로 실제 작동한다.

#### 판정 노브 — 각각이 막는 실기 사고

| 노브 | 기본 | 적용 지점 | 막는 사고 |
|---|---|---|---|
| `single_share` | 0.5 | ① 자격 (top 득표 대비) | 저득표 후보가 무게 적합만으로 정체성을 가져가는 것 |
| `conf_override` / `conf_margin` | 0.9 / 0.15 | ① 자격 보완 + 복수 적합 중재 | 진열 오염이 득표를 왜곡한 케이스 — conf 1.0 진짜 상품 19표 vs 오염 63표. margin 비교는 `min(0.99, vt+margin)`로 **포화**(정답 conf 1.0이 0.855+0.15=1.005에 패배하던 구조적 결함), 단 vt conf ≥ `conf_override`면 포화하지 않음(둘 다 천장 압축이면 conf 차이는 노이즈 → 득표 서열) |
| `count_unit_slack` | 5.0 | `gate_n(n) = count_gate + slack×(n−1)` (①·④·I6) | DB unit_weight 편차와 접촉 오염이 개수에 비례 누적되는데 flat ±15g를 쓰면 n≥4에서 정답의 자기 적합이 깨진다 — 베이글 5개(5×155≈775)가 만두 4개(4×185=740)에 확정을 넘긴 사고. **조합(③)은 의도적으로 flat 유지**(우연 적합 공간이 조합적으로 커지므로) |
| `near_factor` | 2.0 | ② 근접 실패 밴드 | 접촉 하중 오염(실측 8~18g)을 "정체성이 틀렸다"로 오독하는 것 — 정체성·개수 보존 PARTIAL |
| `combo_share` | 0.3 | ③ 조합 멤버 자격 | 배경 후보가 오염 잔차의 filler로 끼는 것(메로나 79g×3) |
| `refit_share` | 0.1 | ④ 구제 자격 | 3표(top의 1.75%)짜리 후보가 "유일 적합"으로 COMPLETE 채택되던 사고 — vision이 사실상 못 본 후보는 구제 대상도 모호성 판단 대상도 아니다 |
| `refit_arb_conf_floor` | 0.8 | ④ 복수 적합 중재의 절대 하한 | margin 우세만으로는 "덜 흐린 유령"이 이긴다 — conf 0.69가 0.35를 꺾고 오과금(정당 케이스는 0.82) |
| `partial_min_confidence` | 0.18 | 9.2 / 9.4 청구 conf 하한 | 5표/청구 conf 0.157짜리 identity partial이 잔차 65g 오상품을 과금 |

모든 노브는 "센티널로 비활성"을 지원한다(`slack=0`, `conf_override=2.0`,
`conf_margin=2.0`, `refit_arb_conf_floor=2.0`, `partial_min_confidence=0`) —
env 한 줄로 구 동작으로 롤백할 수 있고, 그 롤백 경로 자체가 테스트로 고정돼 있다.

### `strict.py`

**무게 우선 백트래킹 조합 탐색.** 로드셀이 tolerance 내로 정확하다는 가정에서
출발해 **무게로 가능한 조합을 먼저 뽑고, 그 중 YOLO가 본 것만 남겨** vision
confidence로 최종 선택한다(냉장 기본 경로).

- **탐색 공간에서 불변식을 강제**한다: `stock_qty > 0`이 아닌 상품 제외(I5),
  `class_id`가 vision 후보에 없는 상품 제외, 개수 상한 `min(stock, max_items)`(I12).
  판정 후 검사가 아니라 **탐색 자체에서** 배제하므로 위반 조합이 만들어지지 않는다.
- 가지치기: `weight ≥ target + tolerance`면 중단, `max_items=6`·`max_kinds=3` 상한,
  단위 무게 내림차순 정렬. `target < tolerance`면 즉시 `[]`(설명할 무게가 없음).
- 정렬 키: `-match_score → 종류 수 → 무게 오차`. `match_score =
  weight_score×0.6 + vision_score×0.3 + simplicity×0.1`이며 simplicity가
  "같은 오차·conf면 종류가 적은 조합"을 선호하게 만든다(모호한 다품종 회피).
- **tolerance는 인자로 받는다** — `SensorProfile.tolerance_grams` 단일 소스이고,
  조기 종료(`perception/early_termination.py`)도 같은 함수에 같은 값을 넘긴다.
  이중 기준이 생기면 "조기 종료는 설명됐다고 판단했는데 judge는 아니라고 하는"
  모순이 발생한다.

## 4. 계약과 불변식

| # | 계약 | 근거·위반 시 |
|---|---|---|
| 순수성 | 전략은 부수효과·내부 상태 없음. 같은 ctx → 같은 결과 | close 2차 패스가 같은 판정 계층으로 재판정할 수 있는 전제 |
| I-V | 냉동에서 무게는 개수 산정·검증·반증만 (정체성 선택 금지) | 위반 시 이슈 #15 재발: 로드셀 20g 차이가 상품을 바꿔버린다 |
| I6 | 모든 COMPLETE는 delta 전량 설명 (냉동은 `gate_n` 스케일) | 부분 설명 과금 금지 → `PARTIAL(+full_delta_unexplained)` |
| I5 / I12 | `stock=0` 제외 · `count ≤ stock` | 재고보다 많이 청구하는 물리적 불가 판정 차단 |
| I8 | 모든 결과에 `reason` 코드, 미스는 `miss_log` | 사유 없는 판정은 현장에서 디버깅 불가 |
| I13/D9 | 모호하면 청구하지 않는다 (`weight_only_ambiguous`, ④ 복수 적합 불발) | 과청구 > 미청구가 더 나쁜 실패 |
| 후보 쇼핑 금지 | conf 하한 미달 시 하위 후보로 내려가지 않고 `None` | 하한 통과 여부가 "청구되는 후보"를 고르게 되면 증거 순위가 무의미해짐 |
| 단일 tolerance | 임계는 `SensorProfile`에서만 옴 | 조기 종료·판정·정산의 기준 불일치 방지 |
| `class_id > 0` | `_product_by_class`가 hand(0)·미매핑(−1)을 정체성 조회에서 배제 | 미매핑 상품 여러 개가 −1로 뭉쳐 하나로 충돌하던 결함 |

## 5. 설정

전부 `MODEL__JUDGMENT__*`이며 `service/model_service.py`가 `FreezerVisionFirstStrategy`
인스턴스와 `default_pipeline(partial_min_confidence=...)`, `JudgmentRouter(count_unit_slack=...)`
로 주입한다. 카탈로그 정본은 [04. 설정 레퍼런스](../../docs/04-configuration.md).

| 환경변수 | 기본값 | 영향 |
|---|---|---|
| `SINGLE_SHARE` | 0.5 | ① 밴드 내 단일 시도 자격 (top 득표 대비) |
| `COMBO_SHARE` | 0.3 | ③ 조합 멤버 자격 |
| `NEAR_FACTOR` | 2.0 | ②·④ 근접 밴드 배수 (`gate_n × factor`) |
| `REFIT_SHARE` | 0.1 | ④ 유일-적합 구제 자격 |
| `COUNT_UNIT_SLACK` | 5.0 | `gate_n` 개수당 가산 + I6 강등 검사 스케일 (0 = flat) |
| `CONF_OVERRIDE` | 0.9 | ① share 미달 보완 자격 + 포화 예외 문턱 (2.0 = 비활성) |
| `CONF_MARGIN` | 0.15 | ① 복수 적합에서 conf가 득표 서열을 뒤집는 최소 격차 (≥1.0 = 비활성 센티널) |
| `REFIT_ARB_CONF_FLOOR` | 0.8 | ④ 중재 승자의 절대 conf 하한 (2.0 = 중재 비활성) |
| `PARTIAL_MIN_CONFIDENCE` | 0.18 | 9.2·9.4 무게 미검증 count=1 청구의 conf 하한 (0 = 비활성) |

**env로 노출되지 않은 코드 상수**: `tolerance_grams`·`count_gate`·
`min_weight_change_grams`(전부 `core/profiles.py`의 프로파일 상수),
`StrictWeightMatcher(max_items=6, max_kinds=3)`, `relax_factor=2.0`,
`detected_single`의 `tolerance_factor=3.0`, `FreezerVisionFirst`의
`max_kinds=4`·`identity_pool=6`·`max_total_items=12`.

## 6. 테스트

`tests/test_judgment.py` 55건. 픽스처(`cola`/`water`/`bar170`/`bar178`, `cand()`)는
`tests/conftest.py`.

| 테스트 클래스 | 건수 | 무엇을 고정하는가 |
|---|---|---|
| `TestFreezer` | 9 | I3 게이트로 후보 합산 금지(178g 사건), 근접 실패의 정체성·개수 보존 PARTIAL, 3종 조합과 "적은 종류 우선", 유일-적합 구제, refit 중재의 성립/절대 하한/모호 유지, 모호 시 strict·relaxed로의 **체인 누수 없음**(텔레메트리 0 확인) |
| `TestGuards` | 8 | 저무게 게이트 사유 코드, 동일 무게 충돌의 conf 우선, vision_only count=1·conf×0.7, weight_only 단일 매치·다품목 조합 금지·모호 거부, 텔레메트리 카운트 |
| `TestIssue16WeightArbitration` | 7 | n-스케일 게이트로 우연 적합 방어, conf_override+margin 중재, 격차 부족 시 폴스루, 천장 포화 발동/비발동(양쪽 천장), margin 비활성 센티널, 노브 롤백으로 구 선착 동작 재현 |
| `TestStrictMatcher` | 6 | 단순 조합 선호, stock 제한 시 종류 조합, I5 품절 제외, I12 count 상한, `target < tolerance` 빈 결과, vision 미검출 제외 |
| `TestVisionFirstIdentityPartial` | 5 | 냉동 relaxed 미스 후 정체성 보존 PARTIAL, 무게검증 시 COMPLETE 격상, 저conf 청구 차단, 하한 0 롤백, COMPLETE 경로는 하한 무관 |
| `TestWeightOnlySameProductCount` | 3 | 동일 상품 n개 유일 매칭 채택, 서로 다른 (품목, n) 2쌍이면 모호 거부, stock 초과 n 제외 |
| `TestIssue10MelonaFiller` | 3 | share 하한 없이도 판정층이 filler를 거부하고 정답 복원, 3표 filler를 `refit_share`가 차단, share 제거 후 정답 복원 |
| `TestFullDeltaMatch` | 2 | I6 부분 설명 강등, relaxed 과잉 도달을 라우터가 강등 |
| `TestSegmentMatching` | 2 | 구간별 매칭이 합계 모호성을 해소, 단일 구간은 strict로 폴백 |
| `TestStageCountCombination` | 2 | 후보 없이 `segment_targets`로 개수 조합 성립, 단일 매치는 이 전략 몫이 아님 |
| `TestNoCandidateFreezerSuppression` | 2 | 냉동은 `loadcell_identity_suppressed`, 냉장은 `weight_only` 유지 |
| `TestRelaxedLoadcellOnly` | 2 | allowlist 완전 불일치에서만 발동(냉장), 냉동 억제 |
| `TestDetectedSingleItemFallback` | 2 | strict·relaxed가 놓친 잔차 구제, 앞선 전략이 잡으면 도달하지 않음 |
| `TestPipelineOrder` | 1 | `default_pipeline()`의 17개 `name` 순서 문자열 고정 |
| `TestIssue15IdentityConsistency` | 1 | 무게 갈아타기 금지 — 잔차 0인 배경 후보(만두 185×2)가 자격 양문 미달로 탈락 |

## 7. 수정 시 주의

1. **순서를 바꿀 때는 `TestPipelineOrder`가 먼저 깨진다.** 그 테스트를 고치기
   전에 "왜 이 순서인가"(누적 + 특이도 우선, §3)를 다시 확인할 것. 특히
   `no_candidate_fallback`은 항상 확정하므로 그 앞에 무엇을 두는지가 곧 후보없음
   체인의 정의다.
2. **새 전략을 추가하면 I-V 표(§3)를 갱신할 것.** 무게로 후보 중 정체성을
   고르는 전략이라면 `precondition`에 `ctx.profile.weight_is_discriminative`를
   반드시 넣어야 한다 — 빠뜨리면 냉동에서 178g 사건 계열이 재발한다.
3. **tolerance를 로컬 상수로 만들지 말 것.** 항상 `ctx.profile`에서 받고,
   완화가 필요하면 배수(`relax_factor`, `tolerance_factor`)로 표현해 단일 소스
   추적이 유지되게 한다.
4. **conf 하한에서 `continue`하지 말 것.** 하한 미달은 `return None`이다.
   하위 후보로 내려가면 "하한을 통과한 후보"가 청구 대상이 되어 증거 순위가
   무의미해진다(후보 쇼핑 금지).
5. `enforce_full_delta_match`는 `COMPLETE`만 건드린다. PARTIAL을 COMPLETE로
   올리는 로직을 여기 추가하면 I6의 방향(강등만)이 뒤집힌다.
6. `judgment/likelihood.py`(무게 우도 score shadow)는 2026-07-30 삭제됐다.
   유사 접근을 다시 꺼내기 전에
   [07. 배제·폐기 결정 기록](../../docs/07-rejected-and-retired.md)을 읽을 것.
7. **9.2의 conf 하한은 9.3으로 우회될 수 있다(현행 동작).** 냉동에서 9.2가
   `partial_min_confidence` 미달로 `None`을 반환해도, 잔차가 `tolerance×3`(45g) 안이면
   9.3 `detected_single_item_fallback`이 같은 top 정체성을 청구한다(I6이 PARTIAL로
   강등하지만 청구 자체는 성립). 9.3은 프로파일을 가리지 않고 자체 conf 하한도 없다 —
   저증거 청구를 완전히 막으려면 9.3에도 하한을 두거나 냉동에서 9.2 뒤로 두지 않는
   배치를 검토해야 한다.
8. **`MODEL__JUDGMENT__*` 노브는 현재 트리거 판정 경로에만 도달한다.**
   `ledger/cross_zone.py`·`ledger/ghost_ledger.py`는 `router` 인자를 받지만
   `CloseSettler`가 넘기지 않아 `JudgmentRouter()` 기본 인스턴스로 재판정한다
   (기본 env에서는 값이 같아 동작이 일치하지만, 노브를 조정하면 CLOSE 2차 패스만
   구 임계로 도는 불일치가 생긴다). 노브를 실제로 튜닝해 운영할 때는 라우터
   주입 배선을 함께 검토할 것.
