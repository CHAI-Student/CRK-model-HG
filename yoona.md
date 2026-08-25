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
