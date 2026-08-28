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
