# Yoona 변경 사항 정리

## 작업 목적

냉장 장비 테스트에서 확인된 두 가지 오판정을 보완했다.

1. 토레타(class 59)와 트레비(class 11)를 함께 취출했을 때 토레타 2개로 판정되는 문제
2. 꽃게랑(class 43) 취출 후 소고기죽(class 53)을 취출했을 때 꽃게랑 5개로 판정되는 문제

판정 순서와 기존 냉장·냉동 프로파일은 최대한 유지하고, 후보가 공유되거나 순차 취출 영상이 오염되는 경우에 한정해 재판정하도록 했다.

## 해결한 문제

### 1. 토레타 2개 오판정

#### 원인

동시 multi-tray 취출에서는 두 트레이가 하나의 비전 후보 목록을 공유한다. 두 상품의 무게가 비슷하면 `same_weight_collision_guard`가 비전 confidence가 더 높은 class를 양쪽 트레이에서 반복 선택할 수 있다.

기존 흐름은 다음과 같았다.

```text
트레이 0 -> class 59 COMPLETE
트레이 1 -> class 59 COMPLETE
최종 병합 -> class 59 x 2
```

기존 `_pool_exhaustion_retry()`는 `PARTIAL` 또는 `NO_DETECTION` 이벤트만 재판정했기 때문에, 두 이벤트가 모두 COMPLETE이면 class 11을 다시 검토하지 않았다.

#### 보완 내용

[crk_model/service/pipeline.py](crk_model/service/pipeline.py)에 `_collision_complete_retry()`를 추가했다.

- 동일 class가 `same_weight_collision_guard`로 여러 트레이에서 중복 COMPLETE된 경우만 대상
- 먼저 확정된 class를 후보 풀에서 제외
- 후속 트레이만 남은 후보로 1회 재판정
- 대체 후보가 COMPLETE가 되지 않으면 기존 판정을 유지
- 일반적인 동일 상품 양쪽 트레이 취출은 기존처럼 수량을 병합

검증 결과:

```text
트레이 0: class 59 x 1
트레이 1: class 11 x 1
```

### 2. 꽃게랑 5개 오판정

#### 원인

class 53의 DB 기준 무게는 312g이지만 실측값은 약 315~322g으로 편차가 있다. 냉장 tolerance는 ±5g이다.

꽃게랑은 79g이므로:

```text
꽃게랑 x 4 = 316g
```

따라서 관측 delta가 320~325g일 때 기존 매처는 꽃게랑 x 4를 선택하기 쉽다.

또한 앞선 꽃게랑 취출 영상이 다음 trigger의 프리롤에 포함될 수 있어 class 43의 vote가 누적된다. class 53이 비전 후보에 있어도 aggregate 무게 매칭에서 탈락하면 `relaxed` 경로가 class 43 x 4를 선택한다.

기존 cross-zone 패널티는 후보를 낮춘 뒤 router로 재판정하지만, 다음 이유로 원래 결과를 유지할 수 있었다.

- class 53의 312g이 320~325g과 냉장 tolerance를 초과
- soft penalty 후에도 class 43 x 4가 다시 선택됨
- 무게 잔차가 같은 경우 상호 면제 가드가 원 판정을 보호

#### 보완 내용

[crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py)에 냉장 다량 오판정 전용 `_robust_alternative_partial()`을 추가했다.

다음 조건을 모두 만족할 때만 동작한다.

- 냉장처럼 `weight_is_discriminative=True`인 프로파일
- 기존 판정이 COMPLETE
- 기존 상품 판정이 다량 취출(`count > 1`)
- 앞선 trigger의 class가 cross-zone 오염 후보로 식별됨
- 다른 비전 후보가 active product에 존재
- 다른 후보의 단품 무게가 공통 tolerance의 확장 범위 안에 있음

조건을 만족하면 다른 상품을 COMPLETE로 강제하지 않고, 정체성만 보존하는 `PARTIAL`로 처리한다.

```text
꽃게랑 x 4 -> 소고기죽 x 1 PARTIAL
```

냉동 판정, 단품 판정, 정상적인 다량 취출에는 이 보정이 적용되지 않는다. 따라서 비전에서 특정 상품이 압도적으로 지지되는 정상적인 3개·4개 취출을 일괄 차단하지 않는다.

## 무게 편차 테스트

class 53의 DB 무게 312g과 다음 실측값을 사용해 검증했다.

```text
315g -> 기존 결과 보호
319g -> 소고기죽 x 1 PARTIAL
322g -> 소고기죽 x 1 PARTIAL
```

315g은 꽃게랑 x 4의 계산값 316g과 잔차가 1g으로 동일하게 타당하기 때문에 무게만으로 정체성을 구분할 수 없다. 이 경우 기존 무게 잔차 보호 로직이 유지된다.

앞선 꽃게랑 trigger가 있고 cross-zone 보정이 가능한 조건에서는 315g, 319g, 322g 모두 소고기죽 x 1 PARTIAL 후보로 보정되는 대표 시나리오를 별도로 확인했다.

## YAML 로그 검증

처음 제공된 6개 YAML을 읽어 trigger, 후보, 무게, 기존 판정 결과를 복원해 진단 replay했다.

### 확인 결과

- `ses-1`, `ses-2`: YAML의 최종 `zones[].products`에 class 43만 기록되어 class 53의 active product 정보는 파일만으로 복원할 수 없음. class 53을 `312g`으로 보충해 대표 조건을 재현하면 소고기죽 보정 경로를 확인할 수 있음.
- `ses-3`: class 53 정보를 포함해 재구성했을 때 소고기죽 x 1 PARTIAL로 보정됨.
- `ses-77`: 기록된 두 채널의 동일 무게 충돌 조건을 재구성했을 때 토레타 x 1 + 트레비 x 1로 분리됨.
- `ses-78`: 원래 결과가 토레타 x 1이어서 토레타 x 2 오판정은 재현되지 않음.
- `ses-83`: 두 trigger 간격이 약 6.3초로 cross-zone 오염 시간창 밖이므로 기존 보정 대상이 아님.

주의: YAML에는 당시 `active_products` 전체 스냅샷과 원본 AVI가 포함되어 있지 않으므로, 위 검증은 원본 영상까지 재생한 완전 replay가 아니라 YAML 진단 데이터 기반 검증이다.

## 테스트 결과

추가한 회귀 테스트:

- 동일 무게 충돌 COMPLETE 중복 재판정
- cross-zone 냉장 다량 오판정의 robust 대체 후보 보정

기존 정상 동작 회귀 테스트:

- 서로 다른 상품의 multi-tray 병합
- 동일 상품을 양쪽 트레이에서 취출했을 때 수량 병합
- 냉동 vote-dominated second tray 복구
- 기존 cross-zone 무게 모호성·상호 면제·시간창 테스트
- strict, relaxed, freezer, settlement 관련 판정 테스트

최종 실행 결과:

```text
428 passed, 24 skipped
```

추가 정적 검증:

```text
python3 -m compileall -q crk_model tests
 git diff --check
```

두 검사 모두 통과했다.

## 변경 파일

- [crk_model/service/pipeline.py](crk_model/service/pipeline.py)
  - 동시 multi-tray 중복 COMPLETE 후보 재판정
- [crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py)
  - 냉장 순차 취출 다량 오판정의 제한적 robust 대체 후보 보정
- [tests/test_service.py](tests/test_service.py)
  - 59/11 충돌 회귀 테스트
- [tests/test_cross_zone.py](tests/test_cross_zone.py)
  - 43/53 순차 취출 보정 회귀 테스트
- [tests/test_judgment.py](tests/test_judgment.py)
  - 기존 판정 회귀 보강

## Git 정보

- 브랜치: `cold-yoona`
- 커밋: `093ecd3 Improve sequential product judgment`
- 원격: `https://github.com/CHAI-Student/CRK-model-HG`
- 원격 브랜치: `cold-yoona`

PR 생성 주소:

https://github.com/CHAI-Student/CRK-model-HG/pull/new/cold-yoona

---

# 2026-08-28 냉장 ses-22 코카콜라 누락과 5g 분해능 보정

## 현상

`ses-22-1787897374`의 마지막 zone 5에서 코카콜라(class 60) 1개를
취출했지만 다음 조합으로 판정됐다.

```text
관측 delta: -550g
기존 판정: 매일우유(class 45) x1 + 빼빼로(class 18) x5 COMPLETE
비전 1위: 코카콜라(class 60), 15표, confidence 0.915
```

YAML의 카메라별 프레임 검출은 코카콜라가 top 4표 + side 11표였고,
빼빼로와 매일우유는 각각 top에서만 13표와 3표였다. 비전은 코카콜라를
정상 검출했지만 strict 무게 후보에서 탈락했다.

## 원인

냉장 로드셀의 물리 분해능과 IO-BOARD 양자화 간격은 5g이다. 기존 코드도
이를 다음 위치에는 반영하고 있었다.

- plateau 안정성 임계와 BOCPD 관측 노이즈: 2.5g
- 최소 무게 변화와 냉장 segment 임계: 5g
- 냉장 상품 매칭 tolerance: ±5g

하지만 strict 상품 매칭은 관측값을 ±2.5g 구간으로 해석하지 않고 DB
무게와의 숫자 차이를 그대로 비교했다.

```text
코카콜라 DB 무게: 543g
관측 무게:        550g
기존 raw 오차:      7g > tolerance 5g -> strict 후보 탈락

우유 + 빼빼로x5: 219 + 66x5 = 549g
raw 오차:           1g <= tolerance 5g -> strict COMPLETE
```

`tolerance_grams=5`는 5g 미만 임계가 무의미하다는 정책은 표현했지만,
현재 관측값 자체의 반 분해능을 DB 비교에 명시적으로 반영하지는 않았다.

## 보완 내용

`SensorProfile`에 `measurement_resolution_grams=5.0`과
`quantization_adjusted_error()`를 추가했다.

```text
effective_error = max(0, raw_error - resolution/2)
```

ses-22의 코카콜라는 다음과 같이 경계 안으로 들어온다.

```text
raw error       = |550 - 543| = 7g
effective error = 7 - 2.5 = 4.5g
4.5g <= 냉장 tolerance 5g
```

기존 strict 탐색과 점수식을 전역 변경하지 않고, 정상 판정 뒤의 제한적인
`_quantized_top_single_retry()`만 추가했다. 다음 조건을 모두 만족할 때만
누락된 비전 1위 단품을 `PARTIAL`로 보존한다.

- 냉장(`weight_is_discriminative=True`)
- 원 판정이 strict COMPLETE
- 원 판정이 다품종이고 총 수량 3개 이상
- removal segment가 정확히 1개
- 비전 1위가 원 판정에서 누락됨
- 비전 1위 득표가 기존 과금 후보보다 낮지 않음
- raw 오차는 기존 tolerance 밖이지만 반 분해능 보정 후 tolerance 안임

따라서 일반 strict, relaxed, multi-tray, 냉동, 반품 및 CLOSE 계산은 기존
동작을 유지한다. 보정 결과도 무게 경계의 불확실성을 인정해 COMPLETE가
아니라 다음처럼 기록한다.

```text
코카콜라(class 60) x1 PARTIAL
strategy: quantized_top_single_retry
reason: quantized_top_single_partial
trace: quantized_top_single_retry
```

## DB 무게가 이미 5g 단위인 경우

DB 값이 545g처럼 이미 5g 단위여도 새 보정이 중복 개입하지 않는다.

```text
DB 545g / 관측 550g -> raw 오차 5g
```

이는 원래 strict tolerance 안이므로 `_quantized_top_single_retry()`가 즉시
기존 판정을 유지한다. DB 값이 5의 배수인지 보고 출처를 추정하지 않고,
모든 DB 값은 명목 무게로 동일하게 취급하며 현재 관측의 반 분해능만
계산한다.

여기서 중요한 점은 DB 무게에 다시 ±2.5g을 붙이는 것이 아니라는 것이다.
IO-BOARD에서 들어온 현재 관측값 `550g`이 실제로는 약
`547.5g~552.5g` 구간을 대표하므로, 그 관측 구간과 DB의 명목 무게 사이의
최소 오차를 계산한다.

```text
effective_error = max(0, |observed - db_weight| - 2.5g)
```

따라서 DB 무게가 5g 단위로 측정됐는지 여부와 상관없이 같은 계산을 적용할
수 있다. DB가 이미 5g 단위인 경우에도 이 계산 자체는 유효하지만, 다음처럼
기존 tolerance를 먼저 통과하면 새 재검토는 발동하지 않는다.

```text
DB 545g / 관측 550g
raw 오차 5g <= tolerance 5g
-> 기존 strict 판정 유지 (추가 보정 미개입)
```

반대로 5g 단위가 아닌 DB 값이 양자화 경계 때문에 기존 후보에서 탈락한
경우에만 반 분해능 계산이 의미 있게 작동한다.

```text
DB 543g / 관측 550g
raw 오차 7g > tolerance 5g
effective 오차 4.5g <= tolerance 5g
-> 제한적 재검토 대상
```

즉 DB의 측정 출처나 5의 배수 여부를 추측해 분기하지 않으며, DB를 중복
보정하지도 않는다. 모든 DB 값은 동일한 명목 무게로 두고 현재 관측값의
물리적 분해능만 일관되게 반영한다.

## ses-22 YAML 진단 replay

제공된 `ses-22-1787897374.yaml`의 zone 5 값을 직접 읽고 당시 active
product 중 관련 3개 상품의 DB 무게를 보충해 `JudgmentRouter`와 새 보정을
순서대로 실행했다.

```text
YAML delta:      -550.0g
YAML candidates: 60(15표, 0.915), 18(13표, 0.547), 45(3표, 0.545)
raw error(60):    7.0g
adjusted error:   4.5g

기존 router: COMPLETE strict [(45, 1), (18, 5)]
보정 결과:   PARTIAL quantized_top_single_retry [(60, 1)]
```

YAML에는 OPEN 당시 `active_products` 전체 스냅샷이 없으므로 원본 영상과
상품 목록을 모두 포함한 완전 replay는 아니다. 다만 zone 5의 실제 delta,
segments, vision candidates 및 기존 판정은 YAML 값을 그대로 사용했다.

## 회귀 테스트

추가 검증은 다음을 포함한다.

- ses-22 실제 수치로 기존 router의 `45x1 + 18x5 COMPLETE` 재현
- 같은 입력에 새 보정을 적용해 `60x1 PARTIAL` 확인
- DB 무게가 이미 545g이면 새 보정이 개입하지 않음
- 비전 1위 득표가 기존 후보보다 약하면 기존 strict 유지
- 분해능 보정 후에도 tolerance 밖이면 기존 strict 유지
- 전체 냉장·냉동·정산·cross-zone·multi-tray 회귀 테스트

검증 결과:

```text
436 passed, 24 skipped
ruff check 통과
compileall 통과
git diff --check 통과
```

## 변경 파일

- `crk_model/core/profiles.py`
  - 5g 관측 분해능과 유효 오차 계산
- `crk_model/service/pipeline.py`
  - 제한적인 quantized top-single 재검토
- `tests/test_service.py`
  - ses-22 재현 및 기존 판정 보호 테스트
- `yoona.md`
  - 원인, 기존 분해능 반영 범위, 보정 범위와 검증 기록

---

# 2026-09-01 냉장 ses-20 저증거 ×N 증폭 보정

## 현상과 원인

`ses-20-1788243166`에서 소고기죽(class 53) 한 개를 취출했지만 꽃게랑
(class 43) 네 개로 판정됐다.

```text
관측 delta:       -320g
정답 53 단품:      312g, 오차 8g, 7표, confidence 1.0
기존 strict 결과: 43 x4 = 316g, 오차 4g, 2표, confidence 0.395
```

냉장 strict 범위는 ±5g이므로 53 단품은 strict 후보에서 빠졌고, 비전에
잠깐 나타난 43을 무게 역산한 `43 x4`만 strict 후보로 남았다. vote 수는
상품 개수가 아니므로 영상이 네 개를 확인한 결과가 아니라, 약한 클래스
정체성이 무게 조합으로 다량 증폭된 결과다.

## 보완 내용

단일 로드셀 이벤트에 한정해 `_dominant_top_single_retry()`를 추가했다.
다음 조건을 모두 만족할 때만 기존 strict ×N과 비전 1위 단품을 재비교한다.

- 냉장처럼 `weight_is_discriminative=True`인 프로파일
- 기존 판정이 strict COMPLETE
- 기존 결과가 동일 상품 한 종류의 3개 이상
- removal segment가 정확히 1개
- 기존 과금 상품과 다른 비전 득표 1위가 존재
- 비전 1위 confidence가 0.95 이상
- 비전 1위 단품 오차가 기존 relaxed 범위(±10g) 안
- 50% 무게 + 40% 비전 + 10% 단순성 점수로 비전 1위가 승리

ses-20 재점수는 다음과 같다.

```text
53 x1: 0.60
43 x4: 약 0.558
결과: 53 x1 PARTIAL
```

확장 범위에서 선택한 정체성이므로 COMPLETE가 아니라 PARTIAL로 기록한다.

```text
strategy: dominant_top_single_retry
reason: dominant_top_single_relaxed_weight
trace: dominant_top_single_retry
```

## 적용 범위와 한계

이 보정은 `analysis.events < 2`인 단일 이벤트 파이프라인에서만 호출된다.
따라서 `ses-3-1788247030`처럼 multi-tray 후보 풀 공유로 `18 x3`이 `26 x2`로
바뀐 문제에는 적용되지 않으며, 해당 문제를 해결한다고 가정하지 않는다.

전역 냉장 tolerance와 `StrictWeightMatcher` 점수도 변경하지 않았다. 일반
strict 판정은 기존 60% 무게 + 30% 비전 + 10% 단순성을 유지하고, 위의 좁은
재검토에서만 40% 비전 가중치를 사용한다.

## 회귀 테스트

- ses-20 실제 수치에서 `43 x4 COMPLETE`를 `53 x1 PARTIAL`로 교정
- 비전 1위 confidence가 0.95 미만이면 기존 strict 유지
- 비전 1위 단품이 relaxed ±10g 밖이면 기존 strict 유지
- 냉장·냉동·multi-tray·정산 전체 테스트 실행

검증 결과:

```text
439 passed, 24 skipped
ruff check 통과
compileall 통과
git diff --check 통과
```
---

# 2026-09-11 결제 완전/불완전 판정을 confidence threshold 대신 judgment 상태로 전환

## 뭘 했나

edge-environment로 보내는 결제 페이로드가 지금까지는 zone별 `confidence`
평균값에 별도의 threshold를 걸어 완전/불완전 결제를 판단해야 했다. 이 값은
전략마다 스케일이 달라(`vision_only`는 confidence×0.7 상한, `freezer_vision_first`는
conf_override/margin 등 별도 로직) 임계값을 하나로 정하기 어려웠다.

이미 판정 단계에서 나오는 `JudgmentStatus`(`COMPLETE`/`PARTIAL`)가 각 전략이
튜닝한 기준(conf_override=0.9, partial_min_confidence=0.18 등)을 통과해 나온
결론이므로, confidence 재임계값 대신 이 상태를 그대로 zone 단위로 집계해
전달하도록 바꿨다.

## 판정 기준

zone 안에 결제 확정(COMPLETE/PARTIAL) 상태로 남은 이벤트 중 **하나라도
PARTIAL이면 그 zone 전체를 불완전결제(`partial`)로, 전부 COMPLETE(또는
무판정)면 완전결제(`complete`)로 처리한다.** 기존 confidence 평균 계산과
동일한 이벤트 집합(같은 zone·상품 있음·COMPLETE/PARTIAL)을 그대로 재사용해서
집계 기준이 흩어지지 않게 했다.

## 바뀐 내용

- [crk_model/core/types.py](crk_model/core/types.py) — `ZoneBasket`에
  `status: str`(기본값 `"complete"`) 필드 추가.
- [crk_model/ledger/settler.py](crk_model/ledger/settler.py) — zone별
  confidence 평균을 내던 이벤트 목록에서 상태도 함께 뽑아, PARTIAL이 하나라도
  있으면 `"partial"`을 zone status로 정한다. `_Basket.to_zone()`에 `status`
  파라미터 추가.
- [crk_model/gateway/state_machine.py](crk_model/gateway/state_machine.py) —
  `build_payment_payload()`의 zone별 딕셔너리에 `"status"` 필드 추가
  (기존 top-level `"status"`(`success`/`complete_no_products`)와는 별개 키라
  충돌 없음). 기존 `confidence` 필드는 그대로 유지 — edge가 점진적으로
  전환할 수 있게 했다.

## 냉장/냉동 공통 반영

`CloseSettler.solve()`와 `build_payment_payload()`는 냉동/냉장 전용 분기가
없고 `profiles`(FREEZER/REFRIGERATOR)만 주입받는 단일 경로라, 코드 한 곳만
고쳐도 두 프로파일 모두에 자동 적용된다.

## 검증

```text
461 passed, 24 skipped
```

기존 테스트 전부 통과(회귀 없음). zone 딕셔너리에 새 키를 추가하는 방식이라
기존 필드를 읽는 테스트/consumer는 영향받지 않는다.

---

# 2026-09-11 (2차) 냉장 CLOSE 조기 확정 — 워터마크가 유예를 생략해 매출 누락 (ses-63)

## 증상

냉장(cold) 실기 테스트 중, 추론은 전부 정상이었는데 **final close 시점에
product 정보가 비어 확정되고, 그 직후에야 추론 결과가 도착**하는 상황이
있었다. 사용자가 제공한 실제 서버 로그(2026-09-03)로 원인을 특정했다.

## 로그로 확인한 타임라인 (session ses-63-1788419901, zone 1)

```text
16:18:36.529  trg-92 수신 (트리거 1) — 무게 변화 미미
16:18:36.555  trg-92 처리 완료: judgment=no_detection (low_weight_skip), products=[]
16:18:38.710  CLOSE 도착 → queue_pending=0
16:18:38.712  FINALIZED: totalPrice=0 products=0   ← CLOSE로부터 단 2ms 만에 확정
16:18:41.785  trg-93 수신 (트리거 2, 진짜 상품)
16:18:46.698  trg-93 처리 완료: judgment=partial, products=[('BOX_LOTTE_PEPERO_ORIGINAL_46G', 3)]
16:18:46.698  "event rejected (session ses-63-1788419901 already finalized)"
```

CLOSE와 실제 확정 사이에 유예(grace) 없이 2ms 만에 끝났다는 것이 첫 단서였다.
설정상 `close_grace_s` 기본값은 3.0초인데 전혀 적용되지 않았다.

## 원인

[crk_model/gateway/state_machine.py](crk_model/gateway/state_machine.py)의
`MultiZoneGateway`는 CLOSE에 `expected_triggers`(엣지 워터마크 — Node가 존별
녹화 파일 수를 세어 보내는 값)가 오면, 그 값이 있다는 사실만으로 **CLOSE
유예 창(`close_grace_s`) 전체를 세션 단위로 생략**하도록 되어 있었다
(`if not self._watermark_set and self._close_grace > 0: ...`).

이 세션에서는 zone 1에 트리거가 1건(`trg-92`, no_detection)만 도착한
상태에서 Node가 CLOSE에 `expected_triggers={1: 1}`을 실어 보낸 것으로 보인다.
당시 트리거 2(`trg-93`, 진짜 상품)는 아직 인코딩 중이라 Node의 녹화 디렉터리
카운트에 잡히지 않았다. 배리어 입장에서는 "기대한 1건이 이미 다 도착했다"로
보여 그 즉시 워터마크가 유예를 생략시켰고, 인코딩이 끝나 3.075초 뒤 도착한
진짜 트리거는 "이미 확정된 세션"으로 rejected됐다 — products가 있는 상품이
결제에서 통째로 빠졌다(이슈 #8의 재발, 원인은 카메라 업로드 지연이 아니라
**Node의 워터마크 자체가 인코딩 중인 트리거를 셀 수 없다는 점**).

## 고친 내용

**워터마크는 이제 "도착 대기"만 좁히고, CLOSE 유예는 워터마크 유무와 무관하게
항상 별도로 적용한다.** 워터마크는 Node가 아는 만큼만 정확하고, 아직 도착하지
않은 트리거의 존재는 시간만이 알 수 있다는 원칙으로 되돌렸다.

- [crk_model/gateway/state_machine.py](crk_model/gateway/state_machine.py)
  - `poll()`의 유예 판단에서 `not self._watermark_set` 조건을 제거 —
    `close_grace_s > 0`이면 워터마크 유무와 무관하게 항상 유예를 거친다.
  - 더 이상 쓰이지 않는 `_watermark_set` 필드와 그 대입 코드를 제거.
  - `handle_close()`/`poll()` 주석을 이 사고(2026-09-03 ses-63) 기준으로 갱신.
- [crk_model/core/config.py](crk_model/core/config.py) — `close_grace_s`
  기본값을 `3.0` → **`5.0`**으로 상향. 실측 갭이 3.075초였는데 기존 3.0초
  기본값으로는(설령 유예가 정상 작동했더라도) 약 75ms 차이로 아슬아슬하게
  놓쳤을 상황이라 여유를 더 뒀다.
- `.env.example`, `refrg.env.example`, `freezer.env.example` —
  `MODEL__CLOSE__GRACE_S`를 동일하게 `5.0`으로 반영.
- [crk_model/gateway/README.md](crk_model/gateway/README.md),
  [docs/04-configuration.md](docs/04-configuration.md),
  [docs/05-operations.md](docs/05-operations.md) — "워터마크가 오면 유예
  생략" 서술을 "유예는 워터마크와 무관하게 항상 적용" 으로 갱신.

## 냉장/냉동 공통 반영

`MultiZoneGateway`는 냉동/냉장 전용 분기가 없는 단일 게이트웨이 코드 경로라
이번 수정도 코드 변경 없이 두 프로파일 모두에 자동 적용된다.

## 테스트

[tests/test_gateway.py](tests/test_gateway.py)에서:

- `test_seq_watermark_skips_grace` → 워터마크가 있어도 `close_grace_pending`을
  거친 뒤에야 확정되도록 수정.
- `test_expected_triggers_holds_until_arrival_then_finalizes` → 기대 트리거
  도착 후에도 유예가 남아 있음을 확인하도록 수정.
- 신규: `test_expected_triggers_undercount_second_trigger_still_finalizes_with_it`
  — ses-63 로그를 그대로 재현(1번째 no_detection만으로 워터마크 충족 →
  2번째 진짜 트리거가 유예 안에 도착 → 매출 누락 없이 포함).

```text
tests/test_gateway.py: 18 passed
전체: 462 passed, 24 skipped
```

정적 검사(`compileall`, `git diff --check`) 통과.

## 변경 파일

- [crk_model/gateway/state_machine.py](crk_model/gateway/state_machine.py) — 워터마크의 유예 생략 제거
- [crk_model/core/config.py](crk_model/core/config.py) — `close_grace_s` 기본값 3.0 → 5.0
- `.env.example`, `refrg.env.example`, `freezer.env.example` — `MODEL__CLOSE__GRACE_S=5.0`
- [tests/test_gateway.py](tests/test_gateway.py) — 워터마크/유예 관련 테스트 2건 수정 + 회귀 테스트 1건 추가
- [crk_model/gateway/README.md](crk_model/gateway/README.md), [docs/04-configuration.md](docs/04-configuration.md), [docs/05-operations.md](docs/05-operations.md) — 문서 갱신

## 참고: 근본 해결은 Node 쪽에도 남아 있음

`docs/08-handover.md`의 P2 항목("엣지 워터마크 Node 측 구현")이 이 사고와
직결된다 — Node가 CLOSE 시점에 "아직 인코딩 중인 파일"까지 포함해서 셀 수
있어야 `expected_triggers`가 진짜로 신뢰 가능한 신호가 된다. 이번 수정은
모델 쪽에서 워터마크를 과신하지 않도록 만든 방어이고, Node 쪽 카운팅 정확도
개선은 별도 협의가 필요하다.

---

# 2026-09-12 냉장 ses-47·ses-60 비전 1위 누락과 저증거 다량 과금 방어

## 현상

두 세션 모두 비전은 토레타(class 59)를 가장 유력하게 관측했지만, 냉장
무게 우선 판정이 더 작은 무게 오차를 만드는 다른 상품의 다량 조합을
선택했다.

### ses-60 — 반품 없는 단일 취출

```text
관측 delta:       -525g
비전 1위:         토레타(class 59), 6표, confidence 0.3672
약한 경쟁 후보:   빼빼로(class 18), 1표, confidence 0.3036
토레타 x1:        535g, 오차 10g -> 냉장 strict ±5g 밖
빼빼로 x8:        528g, 오차 3g  -> same_product_count COMPLETE
기존 결과:        빼빼로 x8
```

`same_product_count`는 후보별 무게 오차가 tolerance 안에 들어오면 동일 상품
수량을 역산한다. 이때 1표짜리 배경 후보도 후보 풀에 남아 있으면 무게가 더
잘 맞는다는 이유로 8개 과금까지 확대될 수 있었다.

### ses-47 — 취출 후 일부 반품

```text
첫 removal:  -1091.7g
비전 1위:    토레타(class 59) 35표
기존 판정:   매일우유(class 45) x5 = 1095g, 오차 약 3.3g

후속 return: +535g
비전 1위:    토레타(class 59) 23표
반품 무게:   토레타 DB 무게 535g과 일치
```

기존 동존 반품 정산은 장바구니에 있는 상품의 무게와 return 무게를 비교한다.
첫 removal이 우유 x5로 잘못 기록됐기 때문에 토레타 반품을 차감하지 못했고,
`net_delta_correction`으로 우유 두 개만 제거한 뒤 다음 결과가 남았다.

```text
기존 최종 결과: 매일우유 x3
notes:
- net_delta_correction x2
- unmatched_return:zone1:+535.0g
```

## 수정 원칙

냉장 전체를 냉동의 vision-first 방식으로 바꾸거나 냉장 strict tolerance를
전역으로 넓히지 않았다. 기존에 정상 동작하는 strict, 실제 다량 취출,
multi-tray, 냉동 판정과 반품 정산을 보호하기 위해 두 실패 형태에 각각 좁은
후처리 분기를 추가했다.

```text
일반 판정 tolerance: 기존 ±5g 유지
신규 분기 결과:      COMPLETE로 승격하지 않고 PARTIAL
냉동 프로파일:       신규 냉장 분기 미적용
모호한 대응 관계:    기존 결과 유지
```

## 1. ses-60 저증거 동일상품 다량 확대 방어

[crk_model/service/pipeline.py](crk_model/service/pipeline.py)에
`_weak_multi_count_conflict_guard()`를 추가했다.

다음 조건을 모두 만족할 때만 기존 `same_product_count` 결과를 비전 1위
단품 PARTIAL로 제한한다.

- 냉장(`weight_is_discriminative=True`)
- 원 판정이 `same_product_count` COMPLETE
- 원 판정이 한 상품 3개 이상
- removal segment가 정확히 1개
- 비전 1위와 과금 class가 다름
- 비전 1위가 최소 3표
- 과금 후보는 최대 2표
- 비전 1위 득표가 과금 후보의 3배 이상
- 비전 1위 상품이 판매 중이고 재고·무게 정보가 유효
- 비전 1위 단품 오차가 strict ±5g 밖이면서 검토 범위 ±10g 안

ses-60은 위 조건을 전부 만족하므로 다음처럼 바뀐다.

```text
기존: 빼빼로(class 18) x8 COMPLETE
보정: 토레타(class 59) x1 PARTIAL
strategy: weak_multi_count_conflict_guard
reason: weak_multi_count_conflict_partial
trace: weak_multi_count_conflict_guard
```

±10g은 새로운 COMPLETE tolerance가 아니다. 약한 후보의 다량 증폭을 막고
비전 1위 단품을 PARTIAL로 보존할지를 판단하는 이 분기 전용 검토 범위다.

정상적인 동일 상품 다량 취출처럼 과금 class 자체가 비전 1위인 경우와
냉동 프로파일에는 개입하지 않는다.

## 2. ses-47 취출–반품 정체성 재정산

[crk_model/ledger/settler.py](crk_model/ledger/settler.py)에
`_reconcile_take_return_identity()`를 추가했다. 실행 위치는 기존
`pass_same_zone()` 앞이다.

신규 분기는 return을 직접 장바구니에서 차감하지 않는다. 강한 증거로 잘못
판정된 이전 removal의 정체성만 PARTIAL로 재구성하고, 실제 반품 차감은 기존
`pass_same_zone()`이 그대로 담당한다.

다음 조건을 모두 만족할 때만 removal을 재구성한다.

- 냉장 프로파일의 같은 존
- return보다 앞선 음수 removal
- return 비전 1위 상품의 단품 무게와 return delta가 기존 ±5g 안에서 일치
- removal과 return의 비전 1위 class가 동일
- 기존 removal은 다른 한 상품을 3개 이상 과금
- removal 비전 1위 득표가 기존 과금 후보의 3배 이상
- removal segment가 정확히 1개
- 비전 1위 상품으로 계산한 취출 수량이 2개 이상이고 재고 이하
- 최초 removal 무게와 반품 후 잔여 무게가 모두 제한된 검토 범위 안
- 해당 return에 대응 가능한 이전 removal이 정확히 하나

취출–반품 검토 범위는 상품별로 다음처럼 제한한다.

```text
review_limit = min(25g, unit_weight의 5%)
```

이 범위도 일반 상품 판정 tolerance를 바꾸지 않으며, 정확한 후속 반품과
동일 비전 정체성이 함께 있는 CLOSE 재검토에서만 사용한다.

ses-47은 다음처럼 처리된다.

```text
removal 재구성: 토레타 x2 PARTIAL
기존 same-zone return: 토레타 x1 차감
최종 결과: 토레타 x1 PARTIAL
note: take_return_identity_reconciled:zone1:class59:lifted2-returned1
```

같은 class를 가리키는 이전 removal이 여러 개라 어느 취출에 대한 반품인지
모호하면 재구성하지 않는다. `CloseSettler`의 멱등성, 음수 수량 방지,
교차존 반품과 net-delta 후속 정산은 기존 경로를 유지한다.

## 회귀 테스트

추가한 테스트:

- ses-60 실제 수치로 기존 router의 빼빼로 x8 COMPLETE 재현
- 같은 입력에 신규 가드를 적용해 토레타 x1 PARTIAL 확인
- 정상 다량 취출에서 과금 class가 비전 1위이면 기존 결과 유지
- 냉동 프로파일에서 신규 가드 미발동
- ses-47 실제 수치로 우유 x5 removal과 토레타 +535g return 재현
- CLOSE 재정산 후 토레타 x1 PARTIAL 확인
- 대응 가능한 removal이 둘 이상이면 재구성하지 않는 모호성 보호

전체 테스트 결과:

```text
444 passed, 24 skipped
```

추가 검증:

```text
수정 파일 대상 ruff check 통과
git diff --check 통과
```

전체 `ruff check .`에서는 이번 변경과 무관한 기존 파일
`crk_model/core/types.py`, `crk_model/gateway/state_machine.py`의 긴 주석 두
곳(E501)이 남아 있다.

## 변경 파일

- [crk_model/service/pipeline.py](crk_model/service/pipeline.py)
  - `_weak_multi_count_conflict_guard()` 추가
- [crk_model/ledger/settler.py](crk_model/ledger/settler.py)
  - `_reconcile_take_return_identity()` 추가
- [tests/test_service.py](tests/test_service.py)
  - ses-60 및 정상 다량·냉동 보호 테스트 추가
- [tests/test_ledger.py](tests/test_ledger.py)
  - ses-47 및 모호한 removal 보호 테스트 추가
- [yoona.md](yoona.md)
  - 원인, 안전 분기 조건, 검증 결과 기록
