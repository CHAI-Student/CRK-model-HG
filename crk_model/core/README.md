# `core/` — 도메인 타입·센서 프로파일·정책·env 설정

> 계층 위치: 최하위. 다른 8개 패키지가 모두 core를 향해 의존하고, core는 어떤 패키지도
> 참조하지 않는다 (내부 의존도 `config → policy` 한 방향뿐) · 상태성: 무상태
> (frozen dataclass · Enum · 모듈 상수)
> 런타임 의존성: 없음(표준 라이브러리 `dataclasses` / `enum` / `os`만)

---

## 1. 책임과 경계

core는 **이 시스템의 어휘**를 정의한다. 무엇을 몇 개 팔았는지 계산하지 않고,
파일을 읽지 않고, 로그를 남기지 않는다.

| 하는 일 | 근거 |
|---|---|
| 도메인 타입 정의 — 불변식을 예외 처리가 아니라 **타입**으로 표현 | I10 |
| 캐비닛(냉장/냉동) 물리 상수를 `SensorProfile` 파라미터로 고정 | D3, 제약 C3 |
| 에러 세션 결제 정책의 선택지 열거 | D9 / I13 |
| `os.environ` → `Settings` 파싱 + fail-closed 검증 | 원본 `MODEL__*` 운영 관행 계승 |

이 계층이 **하지 않는** 일 — 혼동이 잦은 지점이라 명시한다.

| 하지 않는 일 | 실제 담당 |
|---|---|
| `.env` 파일 로드 | `adapters/serve.py`의 `load_dotenv()` (stdlib 파서 → `os.environ.setdefault`) |
| 존 → 프로파일 배선, 게이트 env 오버라이드 적용 | `service/model_service.py`의 `_default_profile_from_settings` / `_profiles_from_settings` / `_apply_gate_overrides` |
| 프로세스 수준 env (`MODEL__SERVER__*`, `MODEL__LOG_LEVEL`, `MODEL__VIDEO__DECODER`, 엔진 경로) | `adapters/serve.py`, `adapters/avi_frames.py` — core `Settings`에는 없다 |
| 타입 불변식의 **집행** (누가 무엇을 거부하는가) | 소비자 측 — 예: `gateway/state_machine.build_payment_payload()` |
| 일부 값의 도메인 검증 | `perception/filters.py`(수직 ROI region), `adapters/avi_frames.py`(crop 원점) |

## 2. 구성 파일

| 파일 | 역할 | 핵심 진입점 |
|---|---|---|
| `types.py` (136행) | 도메인 타입 9종(열거형 1 + frozen dataclass 8). 잠정/확정 분리(I10)의 타입 경계 | `InterimSummary`, `FinalizedSettlement`, `JudgmentResult`, `VisionCandidate` |
| `profiles.py` (76행) | 냉장/냉동 센서 물리를 파라미터로 고정 | `SensorProfile`, `REFRIGERATOR`, `FREEZER` |
| `config.py` (444행) | env → 설정 객체. 필드 75개 = env 키 75개(1:1) | `Settings`, `Settings.from_env()` |
| `policy.py` (16행) | 에러 세션 결제 정책 열거형 | `ErrorSessionPolicy` |
| `__init__.py` (30행) | 타입·프로파일·정책 재수출 | `from crk_model.core import ...` |

주의: `Settings`는 `core/__init__.py`의 `__all__`에 **없다**. 설정은 항상
`from crk_model.core.config import Settings`로 가져온다 — 도메인 타입(어디서나 쓰는 것)과
설정(조립 지점에서만 쓰는 것)을 수입 경로로 구분하기 위한 의도적 분리다.

## 3. 파일별 상세

### `types.py`

설계 원칙은 하나다: **불변식을 런타임 검사로 지키려 하면 언젠가 검사를 빼먹는다.**
그래서 "절대 결제로 가면 안 되는 잠정 집계"와 "결제 입력이 되는 확정 정산"을
서로 다른 타입으로 만들었다.

- `InterimSummary` — OPEN 중 재폴링에 응답하는 잠정 집계. 필드 `provisional`은 항상
  `True`이며 문서화 목적으로만 존재한다.
- `FinalizedSettlement` — CLOSE 정산기의 확정 출력. 결제 페이로드 빌더가 받는 **유일한** 타입.

`build_payment_payload()`는 `isinstance` 검사 하나로 I10을 집행한다. `InterimSummary`를
넘기면 `TypeError`, `blocked=True`인 확정을 넘기면 `ValueError`(I13)다. 두 타입에 공통
상위 클래스를 두거나 필드로 통합하면 이 보호가 사라진다.

모든 타입은 `@dataclass(frozen=True)`다. 이벤트 소싱(`ledger/`)이 이벤트를 값으로
비교·재생하고, 정산 멱등 캐시(I11)가 동일 객체 반환에 의존하기 때문이다.

| 타입 | 용도 | 비고 |
|---|---|---|
| `JudgmentStatus` | `complete` / `partial` / `no_detection` / `suppressed` / `error` | wire 문자열과 동일 값(str Enum) |
| `ActiveProduct` | Node 재고 스냅샷 항목 | 매칭의 **유일한 권위 소스** (제약 C7) — 로컬 상품 DB 없음 |
| `VisionCandidate` | 투표 집계를 통과한 비전 후보 | `vote_ratio` 분모 정의는 단일(§4) |
| `WeightSegment` | ingest가 정규화한 무게 변화 구간 | `delta_grams` **부호 유지**(취출 −, 반품 +) |
| `ProductCount` | 상품 × 개수 | `total_price` / `total_weight` 파생 |
| `JudgmentResult` | 트리거 1건의 판정 | `reason`(I8 사유 코드), `strategy`(텔레메트리), `explained_weight` |
| `ZoneBasket` | 존 단위 정산 결과 | `confidence`는 결제용 zone 판정 평균, `weight_delta` / `trigger_count` / `notes`는 OPS 로그용 |
| `InterimSummary` | 잠정 집계 (I10) | 결제 불가 |
| `FinalizedSettlement` | 확정 정산 (I10) | `blocked` / `block_reason`(I13), `notes`(I8) |

`VisionCandidate`의 `head_votes` / `span_ratio` / `first_pos_ratio`는 **계측 전용**이다
(carried-in held-object 신호). 판정은 이 값을 읽지 않으며 기본값이 하위호환을 보장한다 —
아카이브로 임계를 확정한 뒤 소비하려고 남긴 자리다.

### `profiles.py`

냉장/냉동 차이를 `if cabinet_type == "freezer"` 분기로 흩뿌리면, 새 존 타입이 생길 때마다
분기가 늘고 어떤 임계가 어디서 갈리는지 추적 불가능해진다. 그래서 **코드 포크가 아닌
파라미터 포크**로 만들었다(D3): 게이트 임계·구간화 임계·조기 종료 허용 여부까지 전부
프로파일 필드다.

물리적 근거(제약 C3): 로드셀 **LABD-B3/K3의 보증 분해능은 5g**(division 1g)이고,
IO-BOARD 엣지 단이 **5g 양자화**를 적용한다(CRK-IO-BOARD 2.0.2). 따라서 냉장에서도 5g
미만 임계는 물리적으로 무의미하다. 냉동고 노이즈는 **5~15g**이라 무게로 "무엇인지"를
가릴 수 없다.

| 필드 | `REFRIGERATOR` | `FREEZER` | 의미 |
|---|---|---|---|
| `tolerance_grams` | 5.0 | 15.0 | 판정·조기 종료가 공유하는 단일 tolerance (이중 기준 금지) |
| `weight_is_discriminative` | `True` | `False` | 무게가 정체성 판별자 자격이 있는가 |
| `count_gate_tolerance_grams` | `None`(→ tolerance) | 15.0 | 개수 검증 게이트 (I3) |
| `min_weight_change_grams` | 5.0 | 5.0 | 저무게 스킵 게이트 (QA Q8) |
| `segment_step_grams` | 5.0 | 20.0 | ingest 구간화 스텝 임계 (D4) |
| `motion_gate_threshold` | 0.02 | 0.005 | 모션 게이트 변화 픽셀 비율 (D6) |
| `motion_gate_keepalive` | 8 | 4 | 연속 스킵 상한 |
| `early_termination_allowed` | `True` | `False` | I15 — 냉동은 조기 종료 금지 |
| `motion_evidence_floor_px` | 10.0 | 12.0 | 변위 증거 하한(원본 `*_MOTION_MIN_DISPLACEMENT_PX` 대응) |

`weight_is_discriminative=False`가 냉동에서 뜻하는 것: 무게는 **거부권만** 갖는다.
"무엇을 가져갔나"는 비전이 정하고, 무게는 `count_gate`(±15g) 안에서 개수만 거칠게 검증한다.
냉동 전용 판정 체인은 이 플래그의 precondition으로 스스로 켜지고 꺼진다 —
호출 측에 캐비닛 분기가 없다.

`count_gate` 프로퍼티는 `count_gate_tolerance_grams or tolerance_grams` 폴백이다.
냉장은 두 값이 같아 폴백으로 두고, 냉동만 명시한다.

프로파일 상수는 **코드 기본값**이고, 현장 조정은 env 오버라이드로만 한다
(`MODEL__VISION__MOTION_GATE_THRESHOLD` / `_KEEPALIVE` → `dataclasses.replace`로 덮어씀).
그 배선은 `service/model_service.py` 소관이다.

### `config.py`

파싱 원칙 세 가지.

1. **의존성 0** — `os.environ`만 읽는다. pydantic·dotenv 같은 파서를 core에 넣으면
   Jetson 배포에서 코어가 서드파티에 묶인다. `.env` 로드는 어댑터가 한다.
2. **fail-closed 검증** — 오타가 조용히 기본값이 되면, 의도하지 않은 구성으로
   운영되고 있음을 아무도 모른다. `_env_choice()` / `_env_cabinet_type()`은 목록 밖 값에
   `ValueError`를 던져 **기동을 거부**한다. 실사고 근거: `cabinet_type` 미이식으로 냉동
   기기가 냉장 ±5g 프로파일로 동작한 이슈 #6.
3. **레버별 독립 플래그 + 롤백 env** — 새 동작은 항상 자기 스위치를 갖고 들어오며,
   기본값은 "기존 동작"이다. 관측만 하는 단계는 `shadow`, 판정에 개입하면 `active`,
   되돌릴 길은 `off`/구현 이름(예: `MODEL__LOADCELL__ANALYZER=plateau`).

| 헬퍼 | 동작 | 빈 문자열 |
|---|---|---|
| `_env_float` / `_env_int` | 값이 있으면 변환 | 기본값 |
| `_env_opt_float` / `_env_opt_int` | 미설정이면 `None` = "프로파일 기본 유지" | `None` |
| `_env_bool` | `1/true/yes/on` → True | 기본값 |
| `_env_zones` | `"9,10"` → `(9, 10)` | `()` |
| `_env_choice` | 허용 목록 밖이면 `ValueError` | 기본값 |
| `_env_cabinet_type` | `refrigerated`/`freezer` 외 `ValueError` | 기본값 |

fail-closed 검증이 걸린 키: `MODEL__MACHINE__CABINET_TYPE`, `MODEL__LOADCELL__ANALYZER`,
`MODEL__VISION__CAMERA_LAYOUT`, `MODEL__VIDEO__SIDE_CROP`,
`MODEL__VISION__VOTE_RATIO_DENOMINATOR`, `MODEL__VISION__MOTION_UNMEASURABLE`,
`MODEL__VISION__HELD_TRACK_DEMOTION`, `MODEL__GHOST__MODE`. `MODEL__SESSION__ERROR_POLICY`는
`ErrorSessionPolicy(raw)` 열거형 생성이 같은 역할을 한다(잘못된 값 → `ValueError`).

예외 하나: `MODEL__VISION__FREEZER_ROI_VERTICAL_REGION`은 `os.environ.get(...).strip().lower()`로
그대로 통과하고, 검증은 `DetectionFilterChain` 생성 시점에서 일어난다. 냉동 dual-top
구성에서는 기동이 거부되지만, `dual` 레이아웃에서는 값이 필터로 전달되지 않아 **오타가
조용히 무시된다**. 이 키를 만질 때는 알고 있어야 한다.

**환경변수 전체 카탈로그는 이 문서에 두지 않는다** → [04. 설정 레퍼런스](../../docs/04-configuration.md).
여기서는 "core가 어떻게 읽는가"만 다룬다.

### `policy.py`

`ErrorSessionPolicy`는 16행짜리 열거형이지만 **기술 결정이 아니라 사업 결정**이다(D9).
"에러 trigger를 안은 세션의 결제를 확정해도 되는가"는 Node 팀·운영과의 계약 항목(P4)이고,
합의 전 기본값은 fail-closed다.

| 값 | 의미 | 상태 |
|---|---|---|
| `BLOCK_PAYMENT` | 에러 trigger가 하나라도 있으면 세션 전체 결제 차단 | **기본값** (합의 전) |
| `FINALIZE_ERROR_FREE_ZONES` | 에러 없는 존만 확정, 에러 존은 제외 + 기록 | 합의 시 선택지 |

소비자는 `ledger/settler.py`의 `CloseSettler`다. 차단 결과는 조용히 사라지지 않고
`FinalizedSettlement(blocked=True, block_reason=...)`로 표현되며(I13), 결제 빌더가
`ValueError`로 다시 막는다 — 무성(silent) 확정이 불가능한 이중 방어.

## 4. 계약과 불변식

| # | 내용 | core에서의 표현 |
|---|---|---|
| I10 | 잠정 결과는 절대 결제로 가지 않는다 | `InterimSummary` / `FinalizedSettlement` 타입 분리 |
| I13 | 에러 세션 무성 확정 금지 | `FinalizedSettlement.blocked`/`block_reason` + `ErrorSessionPolicy` 기본 BLOCK |
| I8 | 판정·정산 사유 코드는 현장 디버깅 계약 | `JudgmentResult.reason`, `ZoneBasket.notes`, `FinalizedSettlement.notes` |
| I3 | 냉동 다품목 출력은 무게 게이트 통과 필수 | `SensorProfile.count_gate` (냉동 15g) |
| I15 | 반품·냉동에는 조기 종료 미적용 | `early_termination_allowed=False` (FREEZER) |
| I7 | 트리거 멱등성 TTL | `Settings.idempotency_ttl_s` (기본 5.0s) |
| C3 | 센서 물리 — 5g 분해능·5g 양자화·냉동 5~15g 노이즈 | 프로파일 상수 전부의 하한 근거 |
| C7 | 상품 매칭의 권위 소스는 Node 스냅샷 | `ActiveProduct`만이 상품 표현 (로컬 상품 DB 없음) |
| — | `vote_ratio` 분모는 항상 "게이트 통과 프레임 수" (단일 정의, 함정 #4) | `VisionCandidate` docstring이 정본. `MODEL__VISION__VOTE_RATIO_DENOMINATOR=hand_window`로 바꾸면 아카이브에 분모 표시가 남는다 |
| — | env 오타는 기본값 폴백이 아니라 기동 실패 | `_env_choice` / `_env_cabinet_type` |

## 5. 설정

전체 목록은 [04. 설정 레퍼런스](../../docs/04-configuration.md)에 있다. 아래는 **의미론이
core에 있는** 키(다른 패키지가 아니라 core의 타입/프로파일/정책을 직접 고르는 키)만이다.

| 환경변수 | 기본값 | 영향 |
|---|---|---|
| `MODEL__MACHINE__CABINET_TYPE` | `refrigerated` | 기기 단위 기본 `SensorProfile` 선택. 냉동기는 **반드시 명시** — 미설정이 이슈 #6의 공동 원인이었다. 오타 시 기동 거부 |
| `MODEL__ZONES__FREEZER` | (없음) | 기본 프로파일에 대한 **존 단위 오버라이드** (예: `9,10`). 냉장 기기에서 특정 존만 냉동으로 |
| `MODEL__SESSION__ERROR_POLICY` | `block_payment` | `ErrorSessionPolicy` 선택 (D9). 변경은 Node·운영 합의 사항 |
| `MODEL__VISION__MOTION_GATE_THRESHOLD` | (없음 → 프로파일) | 설정 시 기기 전 존 프로파일의 게이트 임계를 덮어씀 |
| `MODEL__VISION__MOTION_GATE_KEEPALIVE` | (없음 → 프로파일) | 동일 — 연속 스킵 상한 |
| `MODEL__TRIGGER__IDEMPOTENCY_TTL_S` | `5.0` | I7 멱등 캐시 TTL |

## 6. 테스트

core 전용 테스트 파일은 없다. **소비자 경로에서 고정**하는 편이 "타입이 실제로 무엇을
막는가"를 검증하기 때문이다.

| 테스트 파일 | 무엇을 고정하는가 |
|---|---|
| `tests/test_ledger.py` — `TestInterim`, `test_error_blocks_payment_fail_closed`, `test_error_free_zone_policy_excludes_only_error_zones` | I10(잠정 → `TypeError`), I13/D9(blocked → `ValueError`), 정책 전환 시 에러 존만 제외 |
| `tests/test_gateway.py` — `TestPaymentContract` | ACTIVE 중 폴링 응답이 잠정 타입이라 결제 빌더가 거부 (I10을 wire 경로에서 재확인) |
| `tests/test_lifecycle.py` — `TestCabinetTypeDefaultProfile` | `cabinet_type=freezer`가 존 미지정에서도 기본 프로파일이 되는지(판정 결과로 검증), 잘못된 `cabinet_type`·`loadcell_analyzer` env → `ValueError` |
| `tests/test_lifecycle.py` — `TestVisionTuningWiring`, `TestConfWeightWiring`, `TestOutcomesBound` | env → `Settings` → 파이프라인/투표/프로파일까지의 배선. 미설정 시 `None` → 프로파일 기본 유지 |
| `tests/test_t2_batch.py` — `TestSettingsWiring`, `TestTensorInputSwitch::test_env_wiring` | 성능 레버 env(`PREFETCH`/`TENSOR_INPUT`)의 기본값이 "기존 동작"임 |

## 7. 수정 시 주의

- **`Settings`에 필드만 추가하고 `from_env()` 배선을 잊으면 "유령 노브"가 된다** — 값을
  바꿔도 아무 일도 일어나지 않는다. 과거 모션 게이트 임계가 정확히 이 상태였다(env 경로
  부재). 필드 추가 시 `from_env()` + env 템플릿 3종(`.env.example`, `refrg.env.example`,
  `freezer.env.example`) + [04 문서](../../docs/04-configuration.md)를 같은 커밋에서 고친다.
- **기본값 변경은 배포 동작 변경이다.** 기존 동작을 바꾸는 값은 롤백 env를 함께 남긴다
  (`MODEL__LOADCELL__ANALYZER=plateau`가 그 형태).
- **새 문자열 옵션은 반드시 `_env_choice`로.** `os.environ.get(...).lower()` 직읽기는
  fail-closed 원칙 위반이다(현재 유일한 예외는 위에 적은 `FREEZER_ROI_VERTICAL_REGION`).
- **`frozen=True`를 풀지 말 것.** 이벤트 재생 등가성(저널 replay)과 정산 멱등 캐시(I11)가
  값 동등성·불변성에 의존한다.
- **`InterimSummary`와 `FinalizedSettlement`를 합치지 말 것.** I10은 문서가 아니라
  이 타입 분리로만 강제된다. 공통 필드를 빼서 상위 클래스를 만들면 `isinstance` 가드가 무력화된다.
- **프로파일 상수를 바꾸면 판정이 바뀐다.** `tolerance_grams`는 판정과 조기 종료가 공유하는
  단일 소스라 한쪽만 보고 조정하면 안 된다. 또한 env 템플릿·일부 주석에 **"냉장 ±3g"라는
  낡은 표기가 남아 있다** — 현행 코드값은 `tolerance_grams=5.0`이다(5g 분해능 대응 시
  상향됐고 문구가 따라오지 않았다). 값을 읽을 때는 항상 `profiles.py`가 정본이다.
- **`stock_qty`·`unit_weight`·`class_id`는 Node가 준 값 그대로 쓴다.** core에 상품 마스터를
  두는 방향(planogram/로컬 DB)은 배제된 접근이다 →
  [07. 배제·폐기 결정 기록](../../docs/07-rejected-and-retired.md).

관련 문서: [02. 시스템 아키텍처](../../docs/02-system-architecture.md) ·
[03. 판정과 정산](../../docs/03-judgment-and-settlement.md) ·
형제 패키지 [`ingest/`](../ingest/README.md), [`frames/`](../frames/README.md),
[`judgment/`](../judgment/README.md), [`ledger/`](../ledger/README.md)
