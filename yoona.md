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

# 2026-08-26 냉동 장비 테스트 오판정 4건 보완 (freeze-yoona)

## 뭘 했나

8월 26일 냉동 장비 테스트에서 발견된 오판정 4건을 고쳤다. 다 "무게는 맞는데 어떤 상품인지 잘못 고르는" 문제였다.

1. **인접 칸 3개에서 같은 상품이 중복 청구되는 문제** (session 42)
2. **거의 동점인 두 후보 중 표를 조금 더 받은 쪽만 무조건 이기는 문제** (session 49)
3. **두 상품을 섞어서 꺼냈는데 무게가 우연히 딱 맞아서 한 상품으로만 확정되는 문제** (session 40)
4. **두 번째 상품도 표를 많이 받았는데, 첫 번째 상품의 확신도가 높다는 이유만으로 무시되는 문제** (session ses-36)

## 왜 이런 문제가 생겼나

### 1. 인접 칸 3개 중복 청구

카메라 화각이 옆 칸까지 겹쳐서, 실제로는 1번 칸에서만 상품을 꺼냈는데 2번·3번 칸 영상에도 같은 상품이 찍혀버렸다. 원래 로직은 "옆 칸 하나가 대안 없이 같은 후보만 들고 있으면, 그 옆 칸도 진짜로 같은 상품을 팔았을 수 있다"고 봐주게 되어 있었다. 근데 이번엔 옆 칸이 1개가 아니라 2개(2번, 3번)나 그랬다 — 두 칸이 동시에 우연히 같은 상품을 판 것보다 카메라가 겹쳐서 잘못 찍힌 것일 확률이 훨씬 높다.

**고친 내용**: 대안 없이 같은 후보만 들고 있는 "피해 칸"이 2개 이상이면, 페널티를 줘도 안 바뀌더라도 그냥 청구를 없앤다. 피해 칸이 1개뿐인 경우(원래 봐주던 경우)는 그대로 둔다.

### 2. 근소한 표 차이

득표 10표 vs 9표, 확신도 0.99 vs 0.97 — 사실상 오차 범위인데 로직은 "표가 조금이라도 많으면 무조건 승리"였다. 실제로는 표가 적은 쪽(하겐다즈)이 무게로 봤을 때 훨씬 잘 맞았다.

**고친 내용**: 표 차이가 1표 이하 + 확신도 차이가 작을 때만, 무게가 더 잘 맞는 쪽으로 넘어가게 했다. 표 차이가 크면 원래처럼 표 많은 쪽이 이긴다.

### 3. 무게가 우연히 딱 맞아서 생기는 착각

데리야끼는 영상에서 거의 안 잡혔고(2표), 쿠키앤크림 혼자 5개(350g)로 계산해도 실제 무게(354g)와 거의 딱 맞아떨어졌다. 그래서 실제로는 데리야끼+쿠키앤크림을 섞어 꺼냈는데 쿠키앤크림 5개로 확정돼버렸다.

이건 무게만으로는 답을 알 수 없는 케이스다(두 상품 무게가 비슷해서). 그래서 정답을 억지로 맞히기보다는, "여러 번 나눠서 꺼낸 흔적(무게 변화가 여러 번 나뉨)이 있고 + 다른 상품도 표를 받긴 받았다"면, 상품 개수는 그대로 두되 확정(COMPLETE) 대신 "확인 필요" 상태(PARTIAL)로 낮추게 했다. 이건 옵션으로 껐다 켰다 할 수 있게 만들어서(`mixed_kind_demotion`) 기본은 꺼져 있다.

### 4. 확신도만 보고 두 번째 후보를 무시

가장 최근 사례(ses-36)가 이 케이스다. 쿠키앤크림이 129표로 확정됐는데, 사실 한맥불벅도 63표(확신도 1.0)로 꽤 많이 잡혔다. 그런데 기존 로직은 "쿠키앤크림 판정이 확신도 1.0으로 이미 확정됐으니, 다른 조합 후보는 무조건 무시"하게 되어 있었다. 확신도만 보고 상대 후보가 얼마나 강한지는 전혀 안 본 게 문제였다.

**고친 내용**: 확정된 상품(쿠키앤크림)의 확신도가 높아도, 대안으로 나온 상품(한맥불벅)도 똑같이 확신도가 높으면 더 이상 무조건 무시하지 않고 조합(둘 다 인정)을 채택하게 했다. 대안 쪽 확신도가 낮으면 기존처럼 그대로 무시한다 — 새 숫자를 추가하지 않고 원래 쓰던 "확신 기준값"을 양쪽에 똑같이 적용한 것뿐이다.

## 어떻게 확인했나

- 관련 코드를 실제로 실행해서 재현 → 고치기 전/후 결과 비교
- 전체 테스트 442개 통과 (기존 테스트 회귀 없음), 정적 검사(compileall, git diff --check) 통과
- 새로 추가한 테스트로 "이번엔 고쳐진 케이스"와 "예전처럼 유지돼야 하는 케이스"를 둘 다 확인

## 미해결로 남긴 것

일부 케이스(41·52·57)는 애초에 카메라/모델이 상품을 아예 못 봤거나 다른 상품으로 착각한 경우라, 판정 로직을 고쳐서 해결할 수 있는 문제가 아니다. 이건 영상 인식 쪽(모델) 점검이 별도로 필요하다.

## 바뀐 파일

- `crk_model/ledger/cross_zone.py` — 3칸 이상 중복 청구 억제
- `crk_model/judgment/strategies.py` — 근소한 표 차이 무게 재판단, 혼합 상품 의심 시 확정 강등
- `crk_model/ledger/settler.py` — 확신도 대칭 검사(양쪽 다 확신도 높으면 조합 인정)
- `crk_model/core/config.py`, `crk_model/service/model_service.py` — 위 기능 설정 연결
- `tests/test_cross_zone.py`, `tests/test_judgment.py`, `tests/test_ledger.py` — 회귀 테스트 추가

## Git 정보

- 브랜치: `freeze-yoona`
- 원격: `old-repo` (`https://github.com/CHAI-Student/CRK-model-HG.git`)

PR 생성 주소:

https://github.com/CHAI-Student/CRK-model-HG/pull/new/freeze-yoona
