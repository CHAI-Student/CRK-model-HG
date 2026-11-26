# Yoona 변경 사항 정리

냉동의 현재 처리 기준은 [2026-11-25 로드셀 무게 안정화 판단](#freezer-20261125)이다.
로드셀 무게를 신뢰하기로 하면서 [2026-11-18 비전 증거와 세션 전체 반납 연결](#freezer-20261118)(커밋 `4390e7a`·`8433acf`)을
되돌렸다. 코드는 [2026-11-16 기록](#freezer-20261116)(커밋 `7bb4f25`) 상태를 바탕으로 한다.
그 위에 [2026-11-19 CLOSE 정산 보완](#freezer-20261119), [2026-11-25 코드 경량화](#refactor-20261125),
11-25 무게 신뢰 기반 보완(같은 층 반납, 청구 없이 빠진 무게의 review),
[2026-11-26 반납 보완](#freezer-20261126)(반납 허용오차 25g, 세션 무게 반납, 정산 입력 보완)을 더했다.
Edge 쪽 로드셀 영점 확인 규칙은 [2026-11-26 Edge 로드셀 영점 확인](#zeroset-20261126)에 있다.
11-26 실기에서 나온 반납 층 오청구는 [2026-11-26 들고 온 상품 규칙](#freezer-carried-in-20261126),
두 트레이에 걸친 상품의 미청구는 [2026-11-26 두 트레이에 걸친 상품](#freezer-tray-spanning-20261126),
다른 층 상품을 들고 와 반납하며 이 층 상품을 꺼낸 경우는 [2026-11-26 들고 온 상품 후보 제외](#freezer-carried-take-20261126)에 있다.
아래 날짜별 기록은 당시 구현 이력이며, 이후 철회·보완된 설명이 포함되어 있다.
최신 절의 구현 완료, 저장 데이터 검증, 실제 장비에서 확인할 항목을 구분해서 읽는다.

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

---

# 2026-08-26 (2차) 재테스트 결과 검토 — 새로 드러난 4가지 문제

## 뭘 확인했나

freeze-yoona 브랜치 수정 후 재테스트한 결과, 65↔69 케이스(session 40 계열)는 해결됐다. 그런데 처음에 고쳐야 했던 것과 같은 계열의 문제 4건이 다시 발견됐다. 코드/로그를 다시 뜯어봤고, 아직 코드는 고치지 않고 원인만 정리한다(사용자가 "검토해봐"라고 요청).

## 1) 71 → 69: 69×3 + 71×1로 잘못 쪼개짐

`crk_model/ledger/settler.py`의 콤보(2종류 조합) 계산 로직이, 개수 배분을 고를 때 "원래(잘못된) 단일 종 판정 개수"에 가까운 쪽을 우선시하도록 되어있다. 71이 무시되던 예전 문제(session49/session ses-36 대응 수정)는 고쳐졌지만, 정작 몇 개씩 나눌지 정하는 기준이 여전히 틀린 원판정을 참고하는 구조라 잘못된 분배가 나온다.

- 관련 코드: `crk_model/ledger/settler.py` `_vision_combo()` 의 `deviation` 계산(트리거 판정 개수와의 차이를 우선시하는 tie-break)

## 2) 68 → 70 → 75: 68이 사라지고 70×2 + 75×1로 나옴

zone2가 원래 68×1+70×1로 정확히 나눠 잡았는데, CLOSE 시점 크로스존 재판정(`crk_model/ledger/cross_zone.py`의 `_repass_event`)이 이 zone을 다시 판정할 때 "68/70을 각각 다른 상품으로 나눠 인식했다"는 정보를 버리고, zone 전체 무게를 하나의 덩어리로 처음부터 다시 계산해버린다. 그 결과 무게가 안 맞는 68은 탈락하고 표가 제일 많은 70만 2개로 확정된다.

- 관련 코드: `crk_model/ledger/cross_zone.py` `_repass_event()`의 재판정 호출부(`router.judge(ctx)` — 원래의 다중 채널/세그먼트 분리 결과를 사용하지 않고 단일 컨텍스트로 재계산)

## 3) 68 → 69 → 69 반납: 68이 나와야 하는데 69×2로 나옴

비전은 68을 표 28개·확신도 1.0으로 강하게 잡았다(로그에 `runner_up=class68(conf=1.00,votes=28)`로 남음). 하지만 정산 로직(`_freezer_resolve`)은 "원래 상품 개수 다시 세기" 또는 "원래 상품 + 다른 상품 하나씩 섞기(2종류 조합)" 두 가지만 시도하고, "사실 처음부터 다른 한 가지 상품이었다"처럼 통째로 바꿔치기하는 경로가 없다. 68 하나만으로는 무게가 거의 정확히 맞는데도(95g vs 실측 92.5g), 조합 로직은 반드시 2종류를 요구해서 이 답을 찾지 못한다.

- 관련 코드: `crk_model/ledger/settler.py` `_freezer_resolve()`의 `len(kinds) == 1` 분기 (단일 종 스냅/2종 조합만 존재, 단일 종 교체 경로 없음)

## 4) 70 → 74: 70×2로 나옴

이번 4건 중 유일하게 인식(perception) 쪽 문제. 74가 표 4개만 받고 상대적으로 너무 적어 걸러졌다(사실상 미탐지). 두 상품을 꺼내는 동작도 모션이 분리 인식되지 않아 하나로 뭉뚱그려졌고, 인접 zone에서도 우연히 같은 70이 잡히면서 "인접 zone도 진짜 같은 상품 팔았을 수 있다"는 기존 규칙으로 70이 두 번 청구됐다. 74가 원천적으로 거의 안 보인 게 핵심이라 판정 로직보다 인식 쪽(모션 분리, 클래스 74 검출률) 점검이 필요하다.

## 다음에 할 일 (아직 미착수)

- 1), 2), 3)은 판정/정산 로직에서 고칠 수 있는 문제 — 다음 단계에서 구현 예정
- 4)는 인식 모델/모션 분리 쪽 점검이 필요 (판정 로직으로 해결 불가)

---

# 2026-08-26 (3차) 1·2·3번 문제 로직 개선

## 뭘 했나

앞서 검토한 4가지 문제 중 로직으로 고칠 수 있는 1·2·3번을 고쳤다. 기존 코드/테스트는 최대한 안 건드리는 방향으로, 딱 문제가 되는 지점만 좁게 수정했다.

### 1) 71 → 69: 개수 분배가 틀린 원판정 쪽으로 편향되던 문제

`crk_model/ledger/settler.py`의 2종류 조합 계산(`_vision_combo`)이 몇 개씩 나눌지 고를 때 "원래(신뢰가 깨진) 단일 종 판정 개수"에 가까운 쪽을 우선시하던 걸, **원래 단일 종 판정이 이미 무게 게이트를 통과했었던 경우에만** 잔차(무게가 실제로 얼마나 잘 맞는지)를 먼저 보도록 순서를 바꿨다. 무게 게이트 자체가 실패해서 조합으로 구제하는 기존 케이스(`27×3+30×1` 같은)는 그대로 증분 우선을 유지해서 회귀가 없다.

- 확인: 71→69 재구성 시 69×3+71×1(잔차 26.67g) → 69×2+71×2(잔차 18.33g)로 개선

### 2) 68 → 70 → 75: 크로스존 재판정이 다중 상품 인식을 뭉개던 문제

`crk_model/ledger/cross_zone.py`의 재판정 로직에, **오염된 후보 클래스가 실제로 청구된 상품과 아예 무관하면 재판정 자체를 건너뛰는** 검사를 추가했다. zone2가 68+70을 정확히 나눠 잡았는데 오염 후보가 75(그 둘과 무관한 클래스)였던 경우, 예전엔 무조건 zone 전체를 다시 계산해서 68이 사라졌는데 이제는 손대지 않는다. COMPLETE 판정에만 적용했다(PARTIAL은 기존 경로가 이미 안전하게 처리하고 있어서 그대로 뒀다).

### 3) 68 → 69 → 반납: "상품을 통째로 바꿔치기"하는 경로 추가

`crk_model/ledger/settler.py`에 마지막 구제 단계를 하나 추가했다. 단일 종 스냅도 실패하고 2종류 조합도 실패했을 때, "사실 처음부터 완전히 다른 한 종류였다"는 가능성을 마지막으로 검토한다. 이때도 아무 표나 인정하는 게 아니라 2종 조합과 똑같은 증거 기준(표 개수·확신도)을 통과해야만 바꿔친다 — 노이즈로 잘못 바뀌는 일은 없도록 했다.

- 확인: 68→69→반납 재구성 시 69×2(잘못됨) → 68×1(정답)로 개선

## 새 파라미터는?

없다. 기존에 있던 `combo_min_vote_ratio`, `combo_min_conf`, `count_unit_slack` 같은 값들만 재사용했다.

## 테스트 결과

```text
448 passed, 24 skipped
```

기존 테스트 전부 통과(회귀 없음) + 신규 6건 추가. 정적 검사(compileall, git diff --check)도 통과.

## 4)는 여전히 미해결

70 → 74 케이스는 74가 거의 인식이 안 된 문제라 판정 로직으로는 손댈 수 없다. perception(모션 분리·클래스 74 검출) 쪽 점검이 필요하다.

## 바뀐 파일

- `crk_model/ledger/settler.py` — 콤보 tie-break 순서 조건부 변경(`prefer_residual`), 단일 종 교체 구제(`_single_species_swap`)
- `crk_model/ledger/cross_zone.py` — 오염 클래스가 과금 상품과 무관하면 COMPLETE 재판정 스킵
- `tests/test_ledger.py`, `tests/test_cross_zone.py` — 회귀 테스트 6건 추가

---

# 2026-08-28 냉동 재테스트 3건 추가 진단 및 수정

## 뭘 확인했나

8월 27일 냉동 장비 재테스트에서 같은 시나리오를 두 번씩 반복했는데, 매번 세 번째 상품이 통째로 빠지고 다른 상품이 2개로 겹쳐 청구되는 문제 3건(ses-34/35, ses-36/37, ses-38/39)이 나왔다.

- **34-35 (71→75 취출, 결과 71×2)**: zone3·zone4가 각각 71을 표 36개·확신도 1.0으로 완전히 똑같이 청구. 75(진짜 두 번째 상품)도 표는 받았지만(11표), 75의 단위무게가 두 zone의 실측 무게 어디에도 깔끔히 안 맞아 애초에 후보 경쟁에도 못 들어갔다.
- **36-37 (68→70→75 취출, 결과 68×1+70×2)**: zone2·zone4가 70을 확신도 0.9902377456426621(소수점 13자리까지 완전히 동일)로 청구. 75는 두 zone 모두에서 표는 받았지만(16표, 두 zone 표까지 동일) 마찬가지로 무게가 안 맞아 후보에도 못 들어갔다.
- **38-39 (67→68→71 취출)**: 68이 이 세션 전체 vision 후보에 단 한 번도 안 잡힘 — 판정 로직이 아니라 순수 인식(perception) 미탐지라 로직으로 손댈 수 없다.

공통 원인: 카메라 화각이 겹쳐서 같은 클래스가 서로 다른 zone에 완전히 똑같은 득표·확신도로 찍힌다(유출). 이 로직은 "무게는 게이트만 정하고 실제 정체성은 표가 정한다"는 원칙이라, 유출로 부풀려진 표를 이기지 못하는 진짜 상품(75)은 있어도 무시된다.

## 왜 대체 상품을 추정해 끼워넣지 않았나

처음엔 "안 잡힌 상품을 게이트 완화해서 끼워 넣는" 방식을 검토했지만, 실제로 안 팔렸는데 우연히 표만 받은 오탐 상품까지 잘못 끼워 넣을 위험이 있어 기각했다. 대신 **새 상품을 추정하지 않고, 이미 청구된 것 중 중복만 제거**하는 훨씬 안전한 방식으로 정했다 — 미청구가 과청구보다 낫다는 이 시스템의 기존 원칙과 일치한다.

## 고친 내용

`crk_model/ledger/cross_zone.py`에 `_fingerprint_duplicate_suppression()`을 추가했다.

- 서로 다른 zone에서 **같은 클래스가, 같은 개수로, 표·확신도까지 거의 완전히 일치**하게 청구된 경우만 대상 (같은 zone 반복 취출·트레이 분리는 무관 — cross-zone 전용)
- 확신도는 거의 완전히 일치(`1e-6` 이내)해야 한다 — 여러 소수 자리까지 우연히 같을 수 없는 강한 유출 증거
- 표는 카메라별 프레임 수 차이로 자연스럽게 벌어질 수 있어 상대오차 10%까지 허용
- 개수까지 같아야 한다 — 두 zone이 개수가 다르면(예: 1개 vs 2개) 진짜 각자 다른 판매였을 수 있어 손대지 않는다(회귀 테스트 ses-8 상호 강등 fixture에서 확인)
- 무게 잔차가 더 정확한 zone만 남기고, 나머지 zone의 그 클래스 청구만 제거한다 — 대체 상품을 추정하지 않는다
- 기존 재판정(`_repass_event`) 로직은 전혀 안 건드리고, 그 결과에 마지막 한 번만 추가로 적용

```text
34-35: zone3(71×1, 무게 오차 0g) 유지 / zone4(71×1) 제거 → 71×1
36-37: zone4(70×1, 무게 오차 5g) 유지 / zone2의 70×1 제거 → 68×1 + 70×1
```

## 남은 한계

75(진짜 두 번째/세 번째 상품)는 두 시나리오 모두 여전히 청구되지 않는다. 무게가 안 맞아 애초에 후보 경쟁에 못 들어간 상품을 되살리는 건, 오탐 위험 없이 하려면 훨씬 신중한 설계가 더 필요해서 이번엔 "중복 제거"까지만 했다. 38-39의 68 미탐지는 인식 모델 쪽 문제라 이번 로직 수정 범위 밖이다.

## 테스트 결과

```text
451 passed, 24 skipped
```

기존 테스트 전부 통과(회귀 없음) + 신규 1건 추가. 정적 검사(compileall, git diff --check)도 통과.

## 바뀐 파일

- `crk_model/ledger/cross_zone.py` — `_fingerprint_duplicate_suppression()` 추가, `apply_cross_zone_penalty()` 마지막 단계에 연결

---

# 2026-08-28 (2차) 냉장 59/60 혼동 조사 — 로직 결함 아님, 코드 변경 되돌림

## 뭘 조사했나

냉장 장비에서 토레타(59, 525g)와 코카콜라(60, 543g)를 함께 취출했는데 한쪽이 사라지고 60만 청구되는 문제(ses-4/ses-28 계열)를 조사했다. 실제 세션 값(두 트레이 delta -550g/-540g)으로 전체 판정 파이프라인(`_judge_tray_events`)을 직접 재생하며 원인을 끝까지 추적했다.

## 처음 시도: `_partial_class_collision_retry` 추가 (되돌림)

PARTIAL끼리 같은 상품으로 충돌하면(가드②) 그 클래스를 제외하고 재판정해 COMPLETE가 되면 채택하는 함수를 추가했었다. 그런데 실제 ses-4 수치로 검증한 결과:

1. 이 사고의 실제 패턴은 가드②(PARTIAL끼리 충돌)가 아니라 **가드①(한쪽 트레이가 COMPLETE로 확정한 상품과 같은 클래스로 겹친 PARTIAL을 형제 오염으로 보고 버림)** 이었다 — 그래서 새로 만든 함수는 애초에 이 케이스에서 한 번도 발동하지 않았다.
2. 가드①에 대응하는 "클래스 제외 후 재판정, COMPLETE될 때만 채택" 로직은 **이미 `_pool_exhaustion_retry`로 존재**했고, 실제로 이 케이스에서 이미 시도됐지만 실패했다(제외 후 남은 후보의 무게 잔차가 25g로 relaxed 허용치 10g을 훨씬 초과).
3. 토레타 실측 무게(525g→534~535g)를 반영해도 결과는 동일했다 — 이 특정 델타 값들에서는 60이 구조적으로 더 무게가 잘 맞아서, 무게 보정만으로는 해결되지 않는다.

즉 새로 추가한 함수는 **안전하지만(회귀 없음) 이 문제에 대해 아무 효과가 없는 죽은 코드**였다. 그래서 `crk_model/service/pipeline.py`의 `_partial_class_collision_retry()`와 그 호출부, 그리고 `tests/test_service.py`의 관련 테스트 2건(`test_partial_class_collision_retry_is_inert_for_close_weight_pair`, `test_partial_class_collision_retry_keeps_legitimate_double_complete`)을 전부 제거했다. 한 트레이에 서로 다른 두 상품이 있어도 다종 조합 매칭이 유지되는지 확인하는 회귀 테스트(`test_one_tray_with_two_different_products_still_combos`)는 이 문제와 무관하게 유효한 안전망이라 남겨뒀다.

## 진짜 원인: 로직이 아니라 vision 확신도

`StrictWeightMatcher`의 채점식(`weight_score*0.6 + vision_score*0.3 + simplicity*0.1`)을 근거로, 문제 트레이(ch1, delta=-540g)에서 60의 vision 확신도를 낮춰가며 실제로 재생해봤다.

```text
conf(60)=0.76(현재 실측치) → PARTIAL, [(60, 1)]           — 59가 사라짐
conf(60)=0.20                → PARTIAL, [(60, 1)]           — 그대로
conf(60)=0.15                → PARTIAL, [(59, 1), (60, 1)]  — 둘 다 정상 청구
```

`conf(60)`이 약 0.2 밑으로 떨어지는 순간 strict가 60 대신 59로 정확히 재확정되고, 그 결과 다른 트레이의 PARTIAL(60)도 더 이상 형제와 충돌하지 않아 정상 청구된다. 코드를 전혀 바꾸지 않아도 비전이 "이 트레이엔 60이 없다"는 걸 정확히 잡아내는 순간 문제가 해결된다는 뜻이다.

**결론**: 이전 장비에서는 잘 되던 게 지금 장비 환경에서만 어긋난다는 정황과 일치 — 이건 판정 로직의 결함이 아니라 새 장비 환경에서 60의 vision 확신도가 실제보다 높게 나오는 인식(perception) 쪽 문제다. 판정 로직으로는 고칠 수 없고, 카메라 각도·조명 등 인식 쪽 점검이 필요하다.

## 검증

- 전체 테스트: `452 passed, 24 skipped` (코드 제거 후 회귀 없음)
- 정적 검사(compileall, git diff --check) 통과
- `825 error logs`의 34건 YAML 재생: 기존에 이미 검증된 5건(43→53 개선)만 변경, 그 외 회귀 없음 — 코드 추가 전/후 완전히 동일한 결과

## 바뀐 파일

- `crk_model/service/pipeline.py` — `_partial_class_collision_retry()` 및 호출부 제거(원복)
- `tests/test_service.py` — 위 함수 전용 테스트 2건 제거(원복), 다종 조합 회귀 테스트는 유지

## 미해결로 남긴 것

냉장 59/60 혼동은 판정 로직으로는 해결되지 않는다. 새 장비 환경에서 vision이 60을 과도한 확신도로 오탐하지 않도록 인식(perception) 쪽(카메라 각도/조명/모델) 점검이 필요하다.

---

# 2026-09-02 냉동 교차 존 보정 및 로드셀 종료 안정화

## 1. 교차 존 중복 억제 뒤의 대체 상품 복구

### 문제

동일한 상품이 인접 zone에서 같은 표 수와 confidence로 청구되면,
`_fingerprint_duplicate_suppression()`은 카메라 화각 유출로 판단해 무게 잔차가
더 큰 zone의 중복 청구를 제거한다. 기존 동작은 제거 뒤 결과를 무조건
`NO_DETECTION`으로 만들었다.

`ses-44-1788333361`에서는 zone3/zone4가 모두 class 75로 잠정 청구됐고,
zone3의 중복 class 75가 제거됐다. 하지만 zone3에는 class 72(단위중량 115g)가
후보로 있었으며, zone3 delta `-110g`에 대한 잔차는 5g이었다. 즉 중복 class 75를
제거한 뒤에도 확정 가능한 대체 상품이 있었는데 기존 로직은 이를 사용하지 못했다.

### 수정

[crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py)의 fingerprint
중복 억제 단계에서 다음을 수행하도록 변경했다.

1. 제거할 중복 class를 후보 목록에서 제외한다.
2. 남은 후보와 원래 zone delta로 기존 `JudgmentRouter`를 다시 실행한다.
3. 재판정 결과가 `COMPLETE`이고 상품이 있을 때만 대체 결과를 채택한다.
4. 대체가 불가능하거나 `PARTIAL`이면 기존 정책대로 중복 상품만 제거한다.

따라서 임의 상품을 새로 추정해 넣지 않는다. 원래 영상에서 이미 관측된 후보만
사용하고, 기존 router의 재고·단위중량·냉동 count gate 검증을 모두 통과한 경우만
복구한다.

관측 note:

```text
zoneN:cross_zone_fingerprint_duplicate_replaced:
removed=class75:adopted=P...x1
```

`ses-36-1788332797`의 class 69 반납 후 class 76만 남는 흐름과,
`ses-44-1788333361`의 class 75 중복 제거 뒤 class 72 복구를 회귀 테스트로
고정했다.

## 2. 냉동 로드셀 최종 delta 안정화

### 관측된 문제

냉동 장비에서는 문이 열린 동안 손으로 상품을 집고 옮기는 과정에서 중간 하중이
여러 번 바뀐다. 중간 변화는 비전 분석 구간을 찾는 데 유용하지만, 결제용 최종
상품 변화량으로 신뢰하면 안 된다.

대표 사례:

```text
시작 안정값:       0g
중간 변화:   +110g, +210g, -315g
종료 안정값:    -105g
```

결제 delta는 `-105g`여야 한다. 중간 변화의 합 또는 중간 상태만으로 결과를
확정하면 `+5g`처럼 반품으로 보이는 값이 나와 `unmatched_return`으로 처리될 수
있다.

### 기존 BOCPD 경로

기본 분석기는 `BocpdLoadcellAnalyzer`다. 기존에도 채널별로 첫 BOCPD level과
마지막 BOCPD level의 차이를 `delta_weight`로 사용했으며, `WeightSegment`는
이 level들 사이의 단계 변화를 기록했다. 즉 코드가 단순히 `segments`의 수치를
더해 delta를 만들던 구조는 아니었다.

다만 BOCPD가 짧거나 흔들리는 마지막 level을 종료값으로 선택할 수 있었고,
removal(delta 음수)은 return과 달리 마지막 안정 지속시간을 별도로 보호하지
않았다. 따라서 종료부가 안정됐는지 명시적으로 확인하고, 결제 delta의 근거를
아카이브에 남길 필요가 있었다.

### 수정 내용

냉동 프로파일에 다음 최종 안정성 기준을 추가했다.

```text
종료/시작 window: 채널별 3개 샘플
대표값:           median
허용 흔들림:       max(samples) - min(samples) <= 10g
```

IO Board polling이 약 `0.8s`이므로 3개 샘플은 약 2.4초다. median을 사용해
한 프레임의 튐이나 손 접촉값이 종료값을 바꾸지 않도록 했다.

구체적인 흐름은 다음과 같다.

1. BOCPD가 기존과 동일하게 채널별 변화 segment와 시작 plateau를 만든다.
2. 냉동 removal 채널에서 trigger 종료 3개 sample의 median/span을 계산한다.
3. 종료 span이 10g 이하면 `종료 median - BOCPD 시작 plateau`를 해당 채널의
  최종 delta로 사용한다. trigger가 이미 변화 중에 시작될 수 있어 시작 3개 sample의
  span은 안정성 차단 조건으로 쓰지 않는다.
4. 중간 `WeightSegment`는 제거하거나 합산하지 않고, 비전 분석 시간창과 진단용으로
   그대로 유지한다.
5. 움직인 removal 채널의 종료 span이 10g을 넘으면 `final_delta_unstable`로
   판단한다. 이때 delta를 0으로 보내므로 불안정한 부호가 `unmatched_return` 또는
   반품 차감으로 확정되는 것을 막는다.
6. 반품(delta 양수)은 기존 `needs_return_stabilization` 계약을 그대로 우선한다.
   이번 보정으로 반품 재수집 흐름을 바꾸지 않았다.

### 저장되는 진단값

`TriggerTrace.loadcell_terminal_levels`와 세션 archive의
`triggers[].trace.loadcell_terminal_levels`에 채널별 다음 정보가 남는다.

```text
channel
start_median
end_median
start_span
end_span
```

종료 안정화에 실패하면 trace의 `reason_codes`에는 아래 값도 남는다.

```text
loadcell_final_delta_unstable
```

따라서 이후 `unmatched_return` 또는 `below_min_weight_change`가 나와도,
중간 segment뿐 아니라 실제 최종 delta가 어떤 시작/종료 프레임에서 계산됐는지
세션 YAML만으로 확인할 수 있다.

### 범위와 제약

- 적용 범위는 기본 `BOCPD` 냉동 경로다. 냉장 프로파일과 `plateau` 롤백 분석기는
  기존 동작을 유지한다.
- 추가 샘플 수집이나 동기 대기는 추가하지 않는다. 이미 trigger payload에 포함된
  샘플만 사용하므로 결제 지연은 늘지 않는다.
- 3개 시작/종료 샘플이 모두 있어야 median 안정성 판단을 한다. 빠른 취출처럼
  종료 샘플이 충분하지 않은 기존 BOCPD 경로는 기존 결과를 유지한다.
- `final_delta_unstable`은 잘못된 반품 확정을 막는 보호 장치다. 자동 재수집은
  현재 IO Board/상위 서비스 계약에 없으므로 이번 변경 범위에는 포함하지 않았다.

### 검증

로드셀 회귀 테스트를 추가했다.

- `0 -> +110 -> +320 -> -105`처럼 중간 변화가 있어도 BOCPD 시작 plateau와 종료
  median으로 `delta=-105g`가 되는지
- 종료 3개 샘플의 span이 10g을 넘으면 `final_delta_unstable`과 `delta=0`이 되는지
- 기존 빠른 취출 BOCPD 처리와 반품 stabilization 계약이 유지되는지

실행 결과:

```text
tests/test_ingest.py: 20 passed
tests/test_service.py tests/test_session_archive.py: 66 passed
```

## 변경 파일 및 Git 정보

- [crk_model/core/profiles.py](crk_model/core/profiles.py) — 냉동 종료 안정 span(10g) 설정
- [crk_model/ingest/loadcell.py](crk_model/ingest/loadcell.py) — 채널별 terminal median/span 자료형과 계산
- [crk_model/ingest/bocpd.py](crk_model/ingest/bocpd.py) — 냉동 removal final delta 보정 및 불안정 차단
- [crk_model/service/pipeline.py](crk_model/service/pipeline.py) — loadcell terminal 진단 trace 기록
- [crk_model/ledger/archive.py](crk_model/ledger/archive.py) — terminal 진단 YAML 저장
- [crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py) — fingerprint 중복 삭제 후 COMPLETE 대체 재판정
- [tests/test_ingest.py](tests/test_ingest.py), [tests/test_cross_zone.py](tests/test_cross_zone.py) — 현장 세션 및 로드셀 회귀 테스트

브랜치: `freeze-yoona`

최근 커밋:

```text
bd8f9ce cross_zone: restore replacement after duplicate suppression
da1ca5a test: preserve freezer field session regressions
57fd957 ingest: stabilize freezer final loadcell delta
```

---

# 2026-09-03 BOCPD 종료 안정성 오탐 수정 및 콤보 판정 미세 조정

## 1. BOCPD 종료 안정성 판단이 정상 취출을 오탐하던 문제

### 문제

앞선(2026-09-02) 종료 안정성 수정의 초기 구현은 시작 3개 샘플의 span도 `<= 10g`을
요구하고, 종료값을 `end_median - start_median`으로 계산했다. 그런데 trigger는
이미 무게가 변하는 도중에 시작될 수 있어서, 시작 3개 샘플 자체가 `0, -100, -110`처럼
아직 흔들리는 경우가 정상적으로 발생한다.

실제 로그(ses-13/ses-16/ses-17)에서 class 72 단독 취출(-110g)이 이 조건에 걸려
`final_delta_unstable`로 처리됐고, `delta=0`이 되어 비전 호출 자체가 스킵됐다.

### 수정

[crk_model/ingest/bocpd.py](crk_model/ingest/bocpd.py)에서 시작 span 조건을
제거했다. 이제 안정성 판단은 **종료 3개 샘플의 span만** 본다. 시작 기준값은
terminal 시작 median이 아니라 BOCPD가 찾은 채널의 첫 segment level
(`c.segments[0].level`)을 그대로 사용한다.

```text
0, -100, -110, ... (안정), 종료 median -110
-> start_span은 보지 않음, end_span만 <=10g면 delta=-110 확정
```

시작 구간의 흔들림은 원래부터 BOCPD segment 경계가 흡수하는 값이라, 별도
안정성 요건으로 다시 검사할 필요가 없었다.

### 검증

`tests/test_ingest.py`에 trigger가 무게 변화 도중 시작해도 오탐하지 않는지 확인하는
회귀 테스트(`test_freezer_accepts_a_trigger_that_starts_during_the_weight_change`)를
추가했다. 종료부만 불안정한 기존 회귀 테스트는 그대로 유지된다.

## 2. 확신 스냅 가드 문턱 조정 및 낮은 확신 챌린저 구제

### 문턱 하향: 0.95 -> 0.92

콤보가 확신 스냅(존 판정 COMPLETE + 고confidence)을 뒤집지 못하게 막는
`combo_override_max_conf` 기본값을 `0.95`에서 `0.92`로 낮췄다. 실측 판정
confidence 분포를 볼 때 0.92~0.95 구간의 스냅도 이미 충분히 확신 구간이라
판단해, 이 구간에서 콤보가 함부로 뒤집지 못하도록 보호 범위를 넓혔다.

### 낮은confidence 챌린저를 위한 좁은 구제 (`_strong_combo_override`)

문턱을 낮추면서 발생할 수 있는 부작용 — 실제로는 맞는 조합인데 챌린저
confidence가 문턱 바로 아래(예: 0.877)라 막히는 경우 — 를 위해
[crk_model/ledger/settler.py](crk_model/ledger/settler.py)에 좁은 예외를 추가했다.

다음을 모두 만족해야만 확신 스냅 가드를 우회한다.

- 콤보 총 개수가 스냅 개수와 같다
- 콤보 잔차가 스냅 잔차보다 최소 5g 더 낫다
- 콤보 안의 non-snap 챌린저 confidence가 `>= 0.85`
- 챌린저 득표가 스냅 클래스 득표의 `35%` 이상

네 조건을 모두 만족하면 `freezer_combo_override_strong_evidence` note를 남기고
콤보를 허용한다. 새 전역 문턱을 추가하지 않고 기존 파라미터만으로 판단한다.

- 검증: 76×2(스냅, 확신도 1.0) 취출이 실제로는 76+75였던 사례(잔차 5g -> 0g,
  75 confidence 0.877, 득표 7 vs top 19)를 재구성한 회귀 테스트로 확인했다.

## 3. 판정 기각 조합 제외 조건의 동률 표 예외

### 문제

`_vision_combo()`의 `rejected_by_judgment` 제외는 "이 존의 COMPLETE 판정이
이미 과금한 클래스보다 표가 많거나 같은 클래스"를 전부 걷어냈다
(`votes[cid] >= billed_votes`). 그런데 실제로는 판정이 그 클래스를 이겼다는
근거 없이, 우연히 과금 클래스와 **표가 같을 뿐**인 정당한 대체 후보까지
같이 제거되는 사례가 나왔다.

실사고(`ses-1-1788399283`): zone3가 cross-zone 재판정으로 class 71(표 8)을
COMPLETE 과금한 뒤, 실제 두 번째 상품인 class 72도 표가 똑같이 8이라는
이유만으로 `rejected_by_judgment`에 걸려 조합 후보에서 빠졌다. 그 결과
class 72가 누락되고 71×2로 확정됐다(정답은 71×1+72×1).

### 수정

`votes[cid] >= billed_votes` 조건을 `votes[cid] > billed_votes`로 좁혔다.
이제 과금 클래스보다 **더 많은** 표를 받은 클래스만 판정 기각으로 간주해
제외하고, 동률인 클래스는 나머지 자격 검사(ghost 제외, 다른 존 무게 뒷받침
제외, 최소 증거 하한, 무게 게이트)를 그대로 통과해야 콤보 후보로 남는다.
기존에 진짜로 판정이 이긴 클래스를 걸러내던 보호(`더 많은 표` 케이스)는
그대로 유지된다.

- 검증: 동률 표 후보가 다른 존 뒷받침 제외 이후에도 살아남아 정답 조합을
  구성하는 회귀 테스트를 추가했다(`test_combo_keeps_equal_vote_candidate_after_other_zone_exclusion`).
  `ses-1-1788399283`의 71/72 동률 표 실사고도 이 수정으로 해결된다.

## 검증

```text
tests/test_ledger.py: 36 passed
전체: 460 passed, 24 skipped
```

정적 검사(`get_errors`, `git diff --check`) 통과.

## 변경 파일

- [crk_model/ingest/bocpd.py](crk_model/ingest/bocpd.py) — 종료 안정성 판단에서 시작 span 조건 제거
- [crk_model/ledger/settler.py](crk_model/ledger/settler.py) — `_strong_combo_override()` 추가, `rejected_by_judgment` 동률 표 예외
- [crk_model/core/config.py](crk_model/core/config.py) — `combo_override_max_conf` 기본값 0.95 -> 0.92
- `freezer.env.example`, `refrg.env.example` — `MODEL__CLOSE__COMBO_OVERRIDE_MAX_CONF` 0.92로 변경
- [tests/test_ingest.py](tests/test_ingest.py), [tests/test_ledger.py](tests/test_ledger.py) — 회귀 테스트 추가
- [docs/04-configuration.md](docs/04-configuration.md), [crk_model/ledger/README.md](crk_model/ledger/README.md) — 새 문턱·예외 문서화

브랜치: `freeze-yoona` (커밋 전 로컬 변경 포함)

---

# 2026-09-03 (2차) 미해결 — 같은 zone 멀티트레이 공유 표 풀의 구조적 배제

## 문제 (ses-5)

zone2가 실제로는 70(70g)·69(95g) 두 상품을 각 트레이에서 하나씩 꺼냈는데,
`70×2`로 확정되고 69가 완전히 누락됐다. 무게는 게이트 안(69 DB 95g, 실측
~90g)이라 원인이 아니다.

## 원인

`_judge_tray_events()`는 트레이(채널)별로 자기 무게만 다르게 넣고, **비전
후보 풀(vote_summary)은 zone 전체가 공유**한다(영상 1개, YOLO 재실행 없음
— 기존 설계). `FreezerVisionFirstStrategy.solve()`의 자격 검사는

```python
eligible(cand): cand.vote_count >= single_share(0.5) * top_c.vote_count
                or (cand.confidence >= conf_override(0.9)
                    and cand.vote_count >= refit_share(0.1) * top_c.vote_count)
```

이 zone 공유 풀의 **전체 top 표수**를 기준선으로 삼는다. ses-5 zone2 공유
풀은 `74(71표, zone5 유출)·70(49표)·69(8표,conf0.84)`였는데, 74를 유출로
쳐서 페널티(α0.5)를 줘도 남는 기준선은 70의 49표다. `69`는 8표로
`0.5×49=24.5`에도, override 조건(`conf0.84 < 0.9`)에도 못 미쳐 **두 트레이
어디서도 애초에 후보 자격을 얻지 못한다** — 트레이가 물리적으로 분리돼
있어도 자격 판정 기준선은 zone 전체 공유라 한쪽 트레이의 강한 신호(70)가
다른 트레이의 약한 진짜 신호(69)를 무게와 무관하게 배제한다.

## 왜 안 고쳤나

- cross-zone 스킵 조건("오염 클래스가 과금 상품과 무관하면 재판정 생략")을
  완화해도 무의미하다 — 74를 제거해도 69는 70의 표에 눌려 여전히 자격
  미달이라, 이 zone2 사고의 진짜 원인은 74 유출이 아니라 **동일 zone 내
  표 풀 공유 구조 자체**였다.
- `eligible()`의 기준선(top_c)을 바꾸는 건 냉동 단일-종 판정 전체(멀티트레이
  아닌 일반 트리거 포함)가 공유하는 핵심 게이트라, 지금까지 고친
  session49·ses-1~ses-36 등 수십 건의 회귀 테스트가 이 경로를 통과한다 —
  건드리면 회귀 위험이 크다.
- 진짜 구조적 해법(트레이별 후보 풀을 시간/위치로 분리)은 신규 아키텍처가
  필요해 이번 범위를 벗어난다.

## 결론

코드 수정 없이 미해결로 남긴다. 두 상품이 한 zone의 서로 다른 트레이에서
동시에 나올 때, 표 수 차이가 크면(이번 8 vs 49) 약한 쪽이 무게와 무관하게
누락될 수 있다는 아키텍처 한계로 기록한다. 트레이별 후보 분리 기능을
설계할 여유가 생기면 다시 다룬다.

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

# 2026-10-02 냉동 교차존 — 잔차 동률/열세 시 오염 후보가 조용히 살아남던 문제

## 뭘 확인했나

`ses-2`, `ses-4` 재테스트 로그를 다시 분석한 결과, 두 세션 모두 같은 계열의
문제였다 — 인접 zone의 실제 취출 장면이 카메라에 섞여 한 zone의 비전 후보
득표를 오염시켰고, 공교롭게 무게까지 맞아떨어져 기존 교차존 안전장치
(`_mutual_exemptions`, ④ 무게 모호성 게이트)가 둘 다 개입하지 않았다.

- **ses-2 zone2**: zone1·zone2가 같은 클래스(주먹밥)를 각각 -115g/-230g로
  청구했는데 둘 다 잔차가 정확히 0g(동률)이었다. `_mutual_exemptions`의
  동률 분기는 "무게로 못 가르면 둘 다 면제"라 zone2도 그대로 보호됐다.
  실제로는 zone2의 진짜 상품(청양만두, class74)이 20표로 자체 후보 풀에
  남아 있었는데도 손대지 않았다.
- **ses-4 zone5**: zone4(잔차 5g)·zone5(잔차 20g)가 같은 클래스(하겐다즈)를
  청구했다. zone5는 잔차 비교에서 이미 "면제 안 됨"으로 정확히 판별됐지만,
  진짜 상품(치즈버거, class74)이 모션 증거 부족으로 투표 자체에서 탈락해
  zone5의 vision_candidates에 아예 없었다. 그래서 ④ 무게 모호성 게이트가
  "경쟁 후보 없음 → 무게가 유일 해"로 보고 조용히 KEEP했다(관측 note도 없음).

## 이전 수정이 원인이었는지

`yoona.md` 전체 이력을 검토했다. 2026-08-28/2026-09-02에 추가된
`_fingerprint_duplicate_suppression()`(동일 지문·동일 개수 전용)과
2026-09-02 "대체 재판정" 기능은 이번 두 사례의 원인이 아니다 — 둘 다 개수가
다르거나(zone5 count2 vs zone4 count1) 지문이 정확히 일치하지 않아 애초에
그 경로가 작동 범위 밖이었다. 두 사례는 `_mutual_exemptions`/④ 게이트의
원래 설계("무게로 못 가르면 개입하지 않는다")가 의도한 보수적 동작의
연장선에서 드러난, 기존부터 있던 사각지대였다 — 2026-09-03(2차)에 기록한
"공유 표 풀 구조적 배제"와 같은 계열의 한계다.

## 고친 내용

[crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py)에 두 가지를
좁게 추가했다. 기존 `_mutual_exemptions`·`_repass_event`·
`_fingerprint_duplicate_suppression` 경로는 전혀 건드리지 않았다.

### 1. 잔차 동률 판별에 "자기 후보로 대체 가능한가" 추가

`_mutual_exemptions()`의 동률(`r1 == r2`, 또는 비교 불가) 분기에
`_tie_break_by_replaceability()`를 추가했다. cid를 뺀 자기 후보만으로 신뢰
가능한(confidence ≥ `source_conf_min`) COMPLETE 대체가 나오는 쪽은 "그
클래스가 자기 것이 아닐 수 있다"는 독립 증거를 가진 쪽이다. 정확히 한쪽만
대체되면 반대쪽(대체 불가)을 진짜 소스로 면제하고, 대체되는 쪽은 면제하지
않는다 — 둘 다 대체되거나 둘 다 안 되면 기존처럼 양쪽 다 면제(동작 무변경).

### 2. 마지막 안전망 `_residual_loser_resolution()`

`apply_cross_zone_penalty()`의 마지막 단계(기존 fingerprint 중복 제거 뒤)에
추가했다. 상호 강등 가드에서 면제받지 못한 zone이 모든 기존 패스를 거친
뒤에도 여전히 공유 클래스를 청구 중이면:

- cid를 뺀 자기 후보로 직접 재판정 — 신뢰 가능한 COMPLETE 대체가 있으면
  채택(ses-2 zone2 패턴: 대체 후보가 자기 풀에 살아있는 경우).
- 대체가 없으면, 자기 잔차가 base `count_gate`(n-scale 슬랙 미적용)를
  초과할 때만 그 청구를 제거한다 — 슬랙 없이는 원래 통과 못했을 약한
  적합만 제거 대상으로 삼는다(ses-4 zone5 패턴: 대체 후보 자체가 투표에서
  탈락해 없는 경우 — 새 상품을 추정하지 않고 오청구만 제거).

두 경로 모두 "대체 후보"를 `_credible_trial_replacement()`로 검증한다 —
라우터 COMPLETE 통과 + `source_conf_min` 이상 confidence + base count_gate
이내 잔차를 모두 요구해, 저confidence 노이즈 후보 하나가 합법적인 양쪽
판매(인접 zone이 실제로 같은 상품을 파는 경우)를 갈라놓지 않게 했다.

## 왜 안전한가

- 새 함수는 기존 `_trial_replacement_valid`/`windows_mutually_overlap`/
  `_judgment_residual`을 재사용한다 — 새 임계값은 `source_conf_min`(기존
  파라미터) 재사용뿐, 신규 파라미터는 추가하지 않았다.
- 1차 구현에서 `test_penalized_winner_still_wins`(합법적 양쪽 판매 보호)와
  `test_ses8_mutual_topology_field_fixture`(대체 불가 사례 무변경)가 모두
  실패했다 — 저confidence 단일 후보가 "대체 가능"으로 오판되던 게 원인이라,
  `_credible_trial_replacement()`에 `source_conf_min` 이상 요구 조건을
  추가해 두 테스트 모두 원래 동작대로 복구했다(ses8 사례는 흥미롭게도 원래
  GT가 class40이었는데도, conf 0.31이 기준 미달이라 여전히 "구제 경로
  없음"으로 보수적으로 남는다 — 과도한 교정보다 안전을 우선).
- 제거만 하고 새 상품을 추정하지 않는 기존 철학(R1 "하드 제외 금지",
  "미청구가 과청구보다 낫다")을 그대로 따른다.

## 검증

```text
tests/test_cross_zone.py: 40 passed
전체: 464 passed, 24 skipped (기존 462 passed 대비 신규 2건만 추가, 회귀 없음)
```

신규 회귀 테스트:

- `test_tie_break_exposes_zone_with_credible_own_alternative` — ses-2 zone2
  패턴 재구성, 청양만두로 올바르게 보정되는지 확인
- `test_residual_loser_without_credible_alternative_is_suppressed` — ses-4
  zone5 패턴 재구성, 대체 불가 시 오청구만 제거되는지 확인

정적 검사(`compileall`, `git diff --check`) 통과.

## 남은 한계

ses-4 zone5의 진짜 정답(치즈버거)은 이번 수정으로도 복구되지 않는다 — 모션
증거 부족으로 투표 자체에서 탈락한 상품은 판정/정산 계층에서 안전하게
추정해 끼워 넣을 근거가 없다(기존 "안 잡힌 상품을 억지로 끼워넣지 않는다"
원칙 유지). 이번 수정은 **잘못된 상품(하겐다즈)이 과청구되는 것만** 막는다
— 진짜 원인 해결은 perception 계층(모션 변위 증거/투표 집계)의 별도 점검이
필요하다.

## 바뀐 파일

- [crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py) —
  `_tie_break_by_replaceability()`, `_credible_trial_replacement()`,
  `_residual_loser_resolution()` 추가, `_mutual_exemptions()`/
  `apply_cross_zone_penalty()` 시그니처에 이들을 배선
- [tests/test_cross_zone.py](tests/test_cross_zone.py) — 회귀 테스트 2건 추가

---

# 2026-10-02 (2차) Node 결제 status — "애매한 COMPLETE"도 불완전결제로 보고

## 요청 배경

2026-09-11에 zone 결제 status를 confidence threshold 대신 judgment 상태
(COMPLETE/PARTIAL)로 집계하도록 바꿨는데, COMPLETE 전략이 채택했더라도 근거가
애매했던 경우(예: ses-24 zone1 `freezer_vision_first_single(conf=0.72)
runner_up=class66(conf=0.72,votes=2)` — 채택 후보와 경쟁 후보의 confidence가
사실상 동률)까지 그대로 `"complete"`로 Node에 보고되고 있었다. "비전·무게가
1단계부터 모두 명확히 일치한 경우에만 complete, 조금이라도 애매하면 다른
값으로" 요청에 따라 보완했다.

## 고친 내용

[crk_model/ledger/settler.py](crk_model/ledger/settler.py)에 `_is_ambiguous_complete()`를
추가하고, `CloseSettler.settle()`의 zone status 집계에 연결했다.

- COMPLETE로 채택된 상품의 vision_candidates confidence와, 채택되지 않은
  최상위 경쟁 후보(runner-up)의 confidence 차이가 `ambiguous_runner_up_margin`
  (기본 0.03) 미만이면 "애매한 COMPLETE"로 본다.
- 판정 자체(상품/개수, `judgment.status`)는 전혀 바꾸지 않는다 — Node로 나가는
  `zones[].status`만 내려보낸다(결제 보고 전용 신호. 이후 2차 수정으로
  `"partial"`이 아니라 `"review"`라는 별도 중간 등급으로 분리됐다 — 아래
  2026-10-02(3차) 참조).
- 경쟁 후보가 아예 없으면(vision_candidates에 대안 자체가 없음) 동률을 판단할
  근거가 없으므로 애매함으로 보지 않는다 — 기존 "complete" 유지.
- 발동 시 `zoneN:ambiguous_runner_up_complete:trigger_ts=...` note를 남겨
  아카이브에서 왜 불완전결제로 내려갔는지 추적 가능하게 했다.
- 새 파라미터 `ambiguous_runner_up_margin`(`CloseSettler` 생성자) +
  `MODEL__CLOSE__AMBIGUOUS_RUNNER_UP_MARGIN`(기본 0.03) — 기존
  `combo_override_max_conf` 등과 같은 배선 방식.

## 검증

```text
tests/test_ledger.py: 신규 2건(확신 COMPLETE는 complete 유지 / 동률 COMPLETE는
partial로 보고 + 상품·개수 불변) 추가
전체: 466 passed, 24 skipped (기존 464 대비 신규 2건만 추가, 회귀 없음)
```

정적 검사(`compileall`, `git diff --check`) 통과.

## 바뀐 파일

- [crk_model/ledger/settler.py](crk_model/ledger/settler.py) — `_is_ambiguous_complete()`
  추가, `CloseSettler` 생성자에 `ambiguous_runner_up_margin` 파라미터 추가,
  zone status 집계에 연결
- [crk_model/core/config.py](crk_model/core/config.py) — `close_ambiguous_runner_up_margin`
  설정 + `MODEL__CLOSE__AMBIGUOUS_RUNNER_UP_MARGIN` env 배선
- [crk_model/service/model_service.py](crk_model/service/model_service.py) —
  `CloseSettler` 생성 시 새 설정 전달
- [tests/test_ledger.py](tests/test_ledger.py) — 회귀 테스트 2건 추가

---

# 2026-10-02 (3차) 결제 status 3단계 세분화 — complete/review/partial

## 요청 배경

"애매하면 partial이나 다른 값으로" 요청에 이어, "status를 더 다양하게 해도
좋을 거 같다 — 정도에 따라 구분"이라는 후속 요청이 왔다. 2차 수정에서는
애매한 COMPLETE를 그냥 `"partial"`로 합쳐 보냈는데, 이러면 "개수/무게 자체가
미검증인 진짜 PARTIAL"과 "판정은 COMPLETE인데 비전만 근소하게 애매했던
경우"가 Node 입장에서 구분이 안 됐다. 둘은 운영상 심각도가 다르므로 등급을
나눴다.

## 고친 내용

[crk_model/ledger/settler.py](crk_model/ledger/settler.py)에 결제 보고 전용
상수 `PAYMENT_STATUS_REVIEW = "review"`를 추가하고, zone status를 3단계
우선순위로 재집계했다(심각한 쪽이 우선):

1. **`"partial"`** — zone 내 결론난 판정 중 하나라도 실제 `JudgmentStatus.PARTIAL`
   (개수/무게 자체가 미검증)
2. **`"review"`**(신규) — 전부 COMPLETE지만, 그중 하나 이상이 애매한 COMPLETE
   (`_is_ambiguous_complete`로 판별 — 채택 후보와 runner-up의 confidence 차이가
   `ambiguous_runner_up_margin` 미만)
3. **`"complete"`** — 그 외(비전·무게가 1단계부터 명확히 일치)

같은 zone에 1)과 2)가 동시에 있으면 더 근본적인 문제인 `"partial"`이 우선한다.
`PAYMENT_STATUS_REVIEW`는 `JudgmentStatus` enum 멤버가 아니라 결제 보고
전용 문자열 상수로 분리했다 — 내부 판정 로직 어디에서도 이 값으로 분기하지
않는다(순수 Node 보고용).

## 검증

```text
tests/test_ledger.py: 기존 "동률 COMPLETE → partial" 테스트를 "→ review"로
갱신 + 우선순위 테스트(PARTIAL과 review가 같은 zone에 있으면 partial 승리) 신규 추가
전체: 467 passed, 24 skipped (기존 466 대비 신규 1건, 회귀 없음)
```

정적 검사(`compileall`, `git diff --check`) 통과.

## 바뀐 파일

- [crk_model/ledger/settler.py](crk_model/ledger/settler.py) — `PAYMENT_STATUS_REVIEW`
  상수 추가, zone status 3단계 우선순위 재집계
- [crk_model/core/types.py](crk_model/core/types.py) — `ZoneBasket.status` 문서화 갱신
- [crk_model/gateway/state_machine.py](crk_model/gateway/state_machine.py) —
  결제 payload 주석 갱신
- [tests/test_ledger.py](tests/test_ledger.py) — 기존 테스트 1건 갱신(review
  기대값) + 우선순위 회귀 테스트 1건 추가

---

# 2026-10-19 냉동 교차존 — 멀티아이템 오염 재판정이 비오염 상품까지 소거하던 문제

## 뭘 확인했나

실기 세션 `ses-7-1791346368` 분석 요청(오라클: 1층 주먹밥(72)+한맥불벅(66),
2층 하겐다즈(69), 3층 제로아이스(70), 4층 청양만두(74))에서, 1층(zone1)만
최종 0건(`products: []`, `confidence: 0.0`)으로 내려갔다. vision은 72를
conf 0.91·21표로 정상 탐지했는데도 결과가 비었다 — 교차존 보정 단계의
연쇄 작용이 원인이었다.

1. zone1 1차 판정(multi_tray, PARTIAL)은 66×1 + 70×2로 확정됐다. 70은 사실
   zone3가 거의 동시에 취출한 제로아이스의 카메라 유출표(99표, conf 1.0)였고,
   `FreezerVisionFirstStrategy`는 설계상 "무게로 정체성을 선택하지 않고
   득표 서열을 신뢰"하므로 72(21표)가 70(99표)에 밀려 처음부터 후보에서
   빠졌다.
2. `_repass_event()`의 soft 페널티(α=0.5)가 70의 득표를 99→49.5로 깎았지만
   여전히 72(21)·66(32)보다 높아 서열이 안 바뀌었다. 재판정은 "같은 70
   클래스, 수량만 2→4"로 재확정했는데, 이 결과를 그대로 채택하면서 원
   판정에 같이 있던 66까지 통째로 덮어써 사라졌다.
3. 마지막 안전망 `_residual_loser_resolution()`이 "zone1의 70 청구는
   zone3가 진짜 출처"라고 정확히 판별해 제거했지만, 이 시점엔 70 하나만
   남아 있어 제거 후 아무것도 남지 않았다 — 최종 0건.

## 고친 내용

[crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py)의
`_repass_event()` 한 곳에만 분기를 추가했다. `FreezerVisionFirstStrategy`
(투표·조합 선정 로직), 실시간 멀티트레이 파이프라인, 다른 교차존 안전장치
(`_mutual_exemptions`, `_fingerprint_duplicate_suppression`,
`_residual_loser_resolution`)는 전혀 건드리지 않았다.

soft 페널티 재판정(`rejudged`)이 COMPLETE로 성공해도, 결과가 **여전히
페널티 대상 클래스(`penalized`)를 포함**하면("오염 클래스가 득표 우위를
유지한 채 수량만 바뀐 것") 그 결과를 통째로 채택하지 않는다. 대신 기존에
있던 "재판정 실패 시" 경로와 동일한 방식으로, 원 판정(`e.judgment`)에서
`penalized`에 속한 멤버만 제거하고 나머지(66)는 보존한다
(`+cross_zone_contamination_pruned`, 남는 게 없으면
`cross_zone_contaminated_partial_suppressed`로 기존처럼 NO_DETECTION).

## 왜 안전한가

- 신원을 "다시 선택"하는 로직이 아니라 "이미 다른 zone의 확정 판정과 겹친
  것으로 검증된 특정 class_id만 소거"하는, 기존에 이미 쓰이던 패턴을 한
  분기 더 적용한 것뿐이다. top 투표자가 아닌 후보를 conf만으로 조합에
  새로 들이는 로직은 추가하지 않았다 — 그런 방식은 오염 후보도 흔히
  conf 1.0으로 나오기 때문에(카메라 유출이어도 YOLO 인식 자체는 정확하다)
  오히려 이번 사고와 같은 유형의 오탐을 재도입할 위험이 있어 의도적으로
  배제했다.
- `demoted=X:adopted=Y` 패턴을 쓰는 기존 테스트 3건(`test_doc_scenario_
  rejudges_zone2`, `test_ses_36_...`, `test_partial_original_is_rejudged_
  to_weight_fit`)은 모두 Y가 X와 다른 클래스라 새 분기(`still_contaminated`)
  조건에 걸리지 않아 영향이 없다 — 개별 확인함.
- 새 분기는 실기 수치(zone1 PARTIAL 66×1+70×2, zone3/4/2 COMPLETE)로
  재구성한 회귀 테스트로 먼저 "수정 전 → 동일하게 0건 재현"을 확인한 뒤,
  수정을 적용해 "66 보존"으로 바뀌는 것까지 검증했다.

## 검증

```text
tests/test_cross_zone.py: 신규 1건(TestSes7Zone1MultiItemPreservedAfterPenalty) 추가,
수정 되돌리면 동일 테스트가 실기와 같은 0건(NO_DETECTION)으로 재현되는 것 확인
전체: 468 passed, 24 skipped (기존 467 대비 신규 1건만 추가, 회귀 없음)
```

정적 검사(ruff) 통과 — 신규 코드에서 추가된 lint 오류 없음(기존부터 있던
무관 항목 2건 제외).

## 남은 한계

이번 수정은 "0건 → 66만 과금"까지만 복구한다. 72(주먹밥)까지 완전히
되살리려면 CLOSE 시점 교차존 보정이 실시간 트리거의 멀티트레이 세그먼트
분할(`crk_model/service/pipeline.py`의 `TriggerPipeline`, 채널별 재시도·풀
상태와 강하게 결합)을 재구성해야 하는데, 범위가 크고 리스크가 높아 이번
"기존 동작 최대한 유지" 요청 범위에서는 보류했다. 사용자 확인 후 이번
안전한 수정 선에서 마무리하기로 함.

## 바뀐 파일

- [crk_model/ledger/cross_zone.py](crk_model/ledger/cross_zone.py) —
  `_repass_event()`에 `still_contaminated` 분기 추가(오염 멤버만 소거,
  비오염 상품 보존)
- [tests/test_cross_zone.py](tests/test_cross_zone.py) —
  `TestSes7Zone1MultiItemPreservedAfterPenalty` 회귀 테스트 1건 추가

---

# 2026-11-10 냉동 다른 층 반납 정산과 Y축 방향 진단 1차 구현

작업 브랜치는 `freeze-yoona`다. 이번 단계는 기존 무게 기반 반납 정산을
보완하고, 제안한 Y축 방향을 실제 영상과 대조할 수 있도록 기록하는 범위다.
Y축 방향만으로 반납을 확정하거나 결제 수량을 변경하지 않는다.

## 다른 층 반납 정산

기존에는 다른 층 반납을 차감한 뒤 냉동 CLOSE 재solve가 출발 층의 관측
net만 보고 수량을 복구했다. 2개 취출 후 다른 층에 1개를 반납해도 2개가
청구되는 합성 입력으로 재현했다. 또한 반납층에서 다시 취출하면 그 층의
관측 net이 0이 되어 해당 취출이 소거될 수 있었다.

이제 냉동끼리는 시간순으로 동존 반납을 먼저 처리하고, 실패하면 이미
취출된 다른 층의 장바구니에서 무게가 맞는 단품 또는 2품 합을 차감한다.
이동이 성립하면 다음 보정을 2층 net 보정과 4층 냉동 재solve에 함께 전달한다.

```text
정산용 net = 관측 net + 다른 층에 반납한 무게 - 다른 층에서 반납받은 무게

1층: 200g 취출, 2층으로 100g 반납 -> 관측 -200g, 정산용 -100g
2층: 100g 반납받음                -> 관측 +100g, 정산용    0g
2층: 반납받은 상품을 다시 취출    -> 관측    0g, 정산용 -100g
```

보고용 `weight_delta`와 원본 이벤트는 관측값을 유지한다. 냉동끼리의 미매칭
반납을 마지막 교차존 패스에서 미래 취출과 다시 연결하지 않는다. 냉장만의
정산 순서와 잠정 집계의 동존 매칭은 기존 동작을 유지한다.

## Y축 방향 진단

`motion_direction.py`를 추가해 냉동 상품 트랙의 bbox 중심 Y좌표를 구간별로
분석한다. 기본값은 영상 보정 전의 진단용 값이다.

- 관측 3회 이상, 구간 변위 20px 이상
- Y 감소는 `take_candidate`, Y 증가는 `return_candidate`
- 양방향 구간이 있으면 `mixed`, 충분한 증거가 없으면 `unknown`
- 3px 흔들림 허용, 극점에서 10px 역행하면 반대 방향 구간 시작
- 게이트 스킵 포함 디코드 위치에서 12프레임 넘게 끊기면 구간 분리
- 카메라와 상품 트랙별로 독립 기록, 손 자체에는 상품 방향을 부여하지 않음

아카이브의 기록 위치:

```text
trace.vote_summary.motion_evidence.<camera>.<class_id>.track_detail[].y_direction
```

방향별 `first/last/obs/start_y/end_y/delta_y`를 남긴다. 왕복은 처음과 마지막
좌표가 같아도 2구간으로 기록한다. 트랙 상세는 기존처럼 관측 상위 8개이고,
트랙당 구간은 8개까지 기록하며 전체 개수는 `segment_count`에 남긴다.
모션 증거가 꺼져 있거나 저무게 스킵으로 영상 추론이 생략되면 방향 진단도 없다.

## 검증

전체 회귀 테스트: `500 passed, 24 skipped`.

추가 검증은 다른 층 전량/일부/2개 반납, 반납층 재취출, 연속 이동, 미래
취출과의 오매칭 방지, 동존 우선, 무게 노이즈, 취출/반납/왕복 방향,
흔들림·짧은 관측·가림 공백, 빈손, 카메라별 독립성, 방향 진단의 투표·판정
무영향을 포함한다. 검증 입력은 합성 데이터이며 실제 장치 영상 재생은 아니다.

## 다음 영상 검증

취출만, 같은 층 반납, 다른 층 반납, 2개 중 1개 반납, 빈손 진입, 꺼냈다가
다시 넣기, 들고 지나가기, 가림 후 재등장을 카메라별로 확인한다.
실제 출입구/선반 위치에 맞춰 0/250px 경계와 이동 임계값을 보정해야 한다.

현재 다른 층 반납은 기존 무게 매칭을 사용하므로 무게가 유사한 다품종의
정체성을 보장하지 않는다. 여러 출발 층에 같은 SKU가 있으면 물리 인스턴스의
원래 층도 무게만으로 구분할 수 없다. 손과 상품의 동행, 놓임·안정 무게 증가,
방향 구간과 세션 취출 상품 이력을 연결하는 판정은 후속 단계다.
한 트리거의 취출·반납 합이 저무게 스킵 범위인 경우도 현재 방향 진단 대상이
아니므로, 영상 기반 동작 판정을 활성화할 때 함께 다뤄야 한다.

## 실영상 검증을 위한 분석 도구 연결

`analyze-sessions --session` 기본 출력에서도 Y축 후보와 방향별 프레임 구간,
시작/종료 Y좌표, 변위, 관측 수를 확인할 수 있게 했다. 전체 리포트에는 카메라별
후보 상태를 집계하고, JSON의 `y_direction.records`에는 세션·존·트리거 인덱스와
영상 경로를 함께 기록한다. 입력 아카이브와 결제 결과는 변경하지 않는다.

공유 영상을 받은 여러 트리거와 양 카메라의 관측은 중복을 포함한다.
이 관측 수를 상품 개수로 사용하지 않는다. 최종 청구 0개인 세션에도 취출 후
반납 동작이 있을 수 있으므로 기존 과금 정답 라벨로 방향 정확도를 계산하지 않는다.
`unknown`은 방향 진단을 했으나 증거가 부족한 상태이며, 진단 필드가 없는
미기록 트리거(구버전/기능 꺼짐/추론 생략 등)와 구분한다.

### 장치에서 확인할 준비

이번 변경 코드가 적용된 장치에서 아래 설정을 확인하고 테스트를 기록한다.
이 작업에서는 장치 배포나 원격 실행을 하지 않았다.

```text
MODEL__MACHINE__CABINET_TYPE=freezer
MODEL__VISION__MOTION_EVIDENCE=1
MODEL__SESSION__SAVE_DETECTIONS=1
```

`SAVE_DETECTIONS`는 bbox 오버레이 검증용이다. 기존 `freezer.env.example`에는
이미 활성화되어 있다. 영상과 YAML/JSON 아카이브를 함께 보관한다.

### 동작별 대조 순서

| 동작 | 확인할 Y축 구간 | 기대 최종 청구 |
|---|---|---|
| A 1개 취출 후 종료 | `take` | A 1개 |
| A 1개 취출 후 같은 층 반납 | 취출·반납 구간, 관측되면 `mixed` | A 0개 |
| A 1개 취출 후 다른 층 반납 | 출발 영상 `take`, 반납 영상 `return` | A 0개 |
| A 2개 취출 후 다른 층에 1개 반납 | 취출·반납 방향과 이력 상쇄 | A 1개 |
| 빈손을 넣었다 뺌 | 상품의 취출/반납 후보가 생기는지 확인 | 0개 |
| 꺼내려다가 다시 넣음 | 왕복 구간 또는 저무게 스킵 여부 | 0개 |

표의 구간은 영상 대조 대상이며, 저무게 스킵·가림·미검출이면 기록되지 않을 수 있다.
카메라별로 확인하고, 잘못된 방향/왕복 누락/빈손 오탐을 구분해 메모한다.

### 확인 명령

아래 `SESSION_ID`를 실제 세션 ID로 바꾼다. `--dir`에는 날짜 폴더들의 상위인
아카이브 루트를 지정한다. 영상 경로가 장치 경로라면 `render-session --map`으로
로컬 경로에 연결한다.

```bash
.venv/bin/analyze-sessions --dir data/sessions --session SESSION_ID
.venv/bin/analyze-sessions --dir data/sessions --json
.venv/bin/render-session SESSION_ID --dir data/sessions
```

이 단계의 추가 분석 도구 테스트는 6개이며, 분석 도구 테스트 28개가 통과했다.
전체 회귀는 `506 passed, 24 skipped`, 변경 코드 정적 검사와 `git diff --check`도 통과했다.
현재 저장소에는 실영상/세션 아카이브가 없어 실제 동작 검증과 임계 보정은 아직
수행하지 않았다. 새 코드의 진단이 없는 과거 로그는 원본 영상·프레임 검출 등
보유 자료를 먼저 확인한 후 별도 재생 범위를 정한다.

---

# 2026-11-12 냉동 상단 top + 45도 경사 side의 시점별 설정

아래는 최초 분리 구현 기록이다. env 항목의 최종 목록은 뒤의 "설정 간소화"를 따른다.

냉동 side는 맨 상단 top 카메라의 바로 옆에서 약 45도 각도로 같은 동작을
촬영한다는 장착 정보를 반영했다. 기존에도 side 상품은 추론했지만 두 영상에
동일한 Y 이동 방향·20px 임계·공통 ROI를 적용했다. 이제 카메라별로 설정한다.
장착 각도 45도로부터 영상 좌표의 45도 회전을 추정하거나 강제 보정하지 않는다.

## 동작 방향과 반납 경계

`MODEL__VISION__FREEZER_TOP_*`와 `MODEL__VISION__FREEZER_SIDE_*`에 다음 항목을 추가했다.

| 항목 | 기본값 | 의미 |
|---|---|---|
| `DIRECTION_AXIS` | `y` | bbox 중심에서 분석할 축 `x` 또는 `y` |
| `RETURN_DIRECTION` | `increasing` | 선택 좌표가 증가하면 반납 후보. `decreasing`이면 반대로 해석 |
| `DIRECTION_MIN_DISPLACEMENT_PX` | `20.0` | 해당 시점의 최소 이동량 |
| `RETURN_BOUNDARY_PX` | 미설정 | 해당 시점의 선택 축에서 동작 경계 통과 진단 |

공통 `MODEL__VISION__FREEZER_DIRECTION_MIN_OBSERVATIONS=3`과
`MODEL__VISION__FREEZER_DIRECTION_BOUNDARY_MARGIN_PX=10.0`도 추가했다.
예를 들어 경계240/여유10/반납 increasing이면 중심 좌표가 230 이하에서
250 이상으로 이동한 구간에 `boundary_crossed=true`가 기록된다.
반대로 이동하면 취출 통과다. 경계 미설정은 통과 여부 `null`이며 방향 관측은 계속한다.
방향 후보는 충분히 움직였으면 경계 미통과(`false`)여도 기록될 수 있다.

새 냉동 예시는 top 경계를 `240.0`으로 두고 side 경계는 빈 값으로 남겼다.
240은 초기 검증 후보이며 실측으로 확정한 값이 아니다. side 영상에서 동일한
반납이 Y 감소로 보이면 `SIDE_RETURN_DIRECTION=decreasing`으로 조정하고,
주 이동이 X축일 때만 `SIDE_DIRECTION_AXIS=x`를 쓴다. 장착 각도만 보고 선택하지 않는다.

방향/경계 진단은 투표·상품 판정·반납 정산·결제 수량에 사용하지 않는다.
기존 무게 기반 반납 상쇄는 계속 사용한다. 다품종의 유사 무게 문제와 손·상품
동행/놓임을 이용한 반납 확정은 이 변경으로 해결됐다고 간주하지 않는다.

## 시점별 ROI

`FREEZER_TOP_ROI_VERTICAL_REGION`, `FREEZER_SIDE_ROI_VERTICAL_REGION`과
각각의 `ROI_Y_SPLIT`을 추가했다. 모두 `MODEL__VISION__` 접두사를 붙인다.
기본 `inherit`/빈 값이면 기존 공통 `FREEZER_ROI_VERTICAL_REGION=upper`,
`FREEZER_ROI_Y_SPLIT=350.0`을 상속한다. 따라서 새 override를 설정하지 않으면
상품 검출을 남기는 영역은 기존과 같다.

카메라별 region은 `upper/lower/off`로 바꿀 수 있다. side 수직 ROI가 켜져
있으면 기존처럼 side X ROI는 생략한다. side 수직 ROI를 off로 바꾸면
`SIDE_ROI_MAX_CENTER_X`가 다시 적용된다(냉동 예시값 480).
이 기능은 냉동 `dual_top_proxy`에서만 적용하며 냉장 ROI 경로는 유지한다.

**ROI 경계는 검출을 제거하고, RETURN_BOUNDARY는 이동 구간을 진단한다.**
ROI를 좁히면 방향 분석 전에 필요한 궤적이 사라질 수 있으므로 각각 조정해야 한다.

## side 손 추론과 기록

냉동 예시의 `MODEL__VISION__SIDE_HAND_ENABLED=1`로 side에서도 손을 추론한다.
이 옵션은 기존 손 래치와 손 근접 `hand_path` 필터도 활성화하므로, 방향 진단과
달리 검출 후보/판정에 영향을 줄 수 있다. 손 가림·오검출 시 상품 누락 여부를
영상으로 확인하고 필요하면 `0`으로 되돌린다. 코드의 기본값은 기존 `False`다.

Y축 기록은 기존 `track_detail[].y_direction`을 유지하고, X축은
`track_detail[].motion_direction`에 기록한다. `analyze-sessions` 기본 출력은
선택 축·반납 방향·경계·구간 변위·경계 통과를 표시한다. JSON 리포트의
`y_direction.records` 키는 기존 소비자 호환을 위해 유지하며 내부 `axis`로 X/Y를 구분한다.
양쪽 시점의 관측은 물리 상품 개수로 사용하지 않는다.

## 검증

합성 장면에서 top의 Y 증가와 side의 X 감소를 같은 반납 후보로 인식하고,
각 경계를 통과하는지 확인했다. env → Settings → ModelService → 필터/트랙
전체 배선, 양 카메라 추론과 side 손 기록, 시점별 ROI/임계/방향, 경계 여유,
냉장 경로 보존, 잘못된 설정 기동 실패, 방향 설정 변경의 판정 무영향을 검증했다.

```text
534 passed, 24 skipped
변경 코드 ruff 검사 통과
git diff --check 통과
```

실제 장치 영상으로 side 축·반납 방향·경계·ROI를 보정한 결과는 아니다.
장치 배포와 원격 실행은 수행하지 않았다.

## 설정 간소화 — 기존 ROI와 반납 경계 구분

카메라 보정용 신규 env를 14개에서 6개로 줄였다. 각 항목 바로 위에 적용 시점,
축·단위, 빈 값 의미와 예시를 주석으로 적었다. 기존 설정과의 차이는 다음과 같다.

| 변수 | 하는 일 | 현재 냉동 예시에서 적용 |
|---|---|---|
| `FREEZER_ROI_Y_SPLIT` | 냉동 상품 검출의 공통 Y 영역 제한 | upper/350으로 top·side의 중심 Y ≤ 350 유지 |
| `TOP_ROI_Y_SPLIT` | TOP_ROI_ENABLED이고 무게 변화가 0이 아닐 때 top 중심 Y ≥ 경계만 유지 | TOP ROI가 꺼져 있고 냉동 ROI가 우선하므로 미사용 |
| `SIDE_ROI_MAX_CENTER_X` | side 중심 X ≥ 경계인 상품 검출 제외 | 냉동 side 수직 ROI가 켜져 있어 미사용. 수직 ROI off 시 대체값480 유지 |
| 새 `FREEZER_SIDE_ROI_Y_SPLIT` | side 검출 Y 경계만 따로 보정 | 빈 값이면 공통350 사용 |
| 새 `FREEZER_*_RETURN_BOUNDARY_PX` | 추적 구간의 반납/취출 경계 통과 진단 | 검출을 제거하지 않으며 결제에도 미사용 |

모든 변수에는 `MODEL__VISION__` 접두사가 붙는다.

남긴 신규 설정:

```text
FREEZER_SIDE_ROI_Y_SPLIT
FREEZER_SIDE_DIRECTION_AXIS
FREEZER_SIDE_RETURN_DIRECTION
FREEZER_SIDE_DIRECTION_MIN_DISPLACEMENT_PX
FREEZER_TOP_RETURN_BOUNDARY_PX
FREEZER_SIDE_RETURN_BOUNDARY_PX
```

제거한 신규 env 8개는 top/side 각각의 `ROI_VERTICAL_REGION`, top의 `ROI_Y_SPLIT`,
top의 `DIRECTION_AXIS`·`RETURN_DIRECTION`·`DIRECTION_MIN_DISPLACEMENT_PX`, 공통
`FREEZER_DIRECTION_MIN_OBSERVATIONS`·`FREEZER_DIRECTION_BOUNDARY_MARGIN_PX`다.
env 로더·Settings·서비스 배선에서도 제거했다. top은 기존 공통 ROI와 Y축 증가=반납,
최소 이동20px를 사용한다. 3회 관측과 경계 여유10px는 `MotionDirectionConfig`의
코드 기본값으로 관리한다. side ROI 유지 방향은 기존 공통 upper/lower/off를 사용한다.

냉동 예시에서 꺼져 있어 사용하지 않는 `TOP_ROI_Y_SPLIT` 할당도 제거했다.
기존 TOP ROI 기능과 냉장 `.env.example`의 해당 변수는 유지했다. side X ROI는
냉동 수직 ROI off 시의 기존 대체 동작을 보존하기 위해 예시값480을 유지했다.
예시만 바꿔도 기기 설정이 바뀌지는 않으므로, 기존 기기 `.env`에 제거한 신규
변수가 있다면 함께 정리해야 한다.

간소화 후 전체 검증은 `532 passed, 24 skipped`다. 제거한 env 항목의 검증 사례를
정리했고, 코드의 방향/경계 검증은 유지했다. 냉장·냉동 env 예시를 각각 읽어
서비스가 초기화되는지 확인했으며, side ROI만 200으로 바꾸면 top은 유지하고
side만 해당 영역 검출을 제외하는 것도 확인했다. 변경 코드 ruff와
`git diff --check`가 통과했다.

# 2026-11-12 냉동 반납 상품 추론과 행동별 정산 연결

이 절은 당시의 행동별 정산 구현 기록이다. 뒤의 기존 무게 처리 복원으로 일부 철회됐으며,
현재 처리 기준은 [2026-11-18 기록](#freezer-20261118)을 따른다.

## 변경한 처리

- 반납 방향으로 이동한 상품 영상에서 종류를 판정하고, 안정된 무게 증가로 수량을 검증한다.
- 같은 물체에서 클래스가 깜빡이는 것을 별도 상품으로 세지 않도록 클래스 무관 튜브의
  구간별 종류 우세를 확인한다. 기본 관측 3회와 2:1 우세를 코드에서 관리한다.
- 카메라 경계가 지정돼 있으면 실제 경계 통과를 요구한다. top/side는 정체성 증거를
  함께 제공하며 두 카메라/트랙 수를 상품 수량으로 세지 않는다.
- 여러 종류 취출이 경계 통과로 확인되면 각 종류를 포함해 수량을 검증한다.
  A100g+B105g를 A×2로 축약하지 않는다. 가능한 수량 배분이 여럿이면 보류한다.
- `FreezerAction`에 부호별 무게와 판정을 보존한다. CLOSE는 시간순 취출 이력에서
  같은 SKU만 차감한다. 같은 층의 다른 SKU를 무게로 대신 차감하지 않는다.
- 다품목/3개 이상 반납과 여러 출발 층에 나뉜 같은 SKU의 반납을 처리한다.
  반납한 SKU가 취출 이력에 없으면 다른 상품을 지우지 않는다.
- 상품별로 정산한 층은 뒤의 net-delta/냉동 재solve가 종류·수량을 뒤집지 못하게 한다.
  미확정 반납은 영향을 받을 수 있는 기존 냉동 층도 보호한다.
- 냉동은 순무게 0도 영상 분석한다. plateau/운영 기본 BOCPD 모두 왕복 구간을 보존하고,
  순변화 0/음수인 혼합 에피소드에도 반납 안정화를 확인한다. 냉장 경로는 기존대로다.
- 저널과 세션 YAML에 행동별 판정과 방향별 후보를 저장해 재생/사후 검증을 지원한다.

## 보류하는 경우와 한계

한 영상의 취출→반납/반납→취출은 양쪽 경계 증거와 안정된 부호별 무게 구간이 모두
필요하다. 무게 구간 누락, 반대 부호 구간 겹침, 여러 번 교대하는 복잡한 왕복,
카메라 종류 충돌, 같은 영상 쌍의 여러 층 공유는 행동을 임의 확정하지 않는다.
이 경우 notes와 `review` 또는 `partial`로 표시한다. 이 상태는 자동 결제 차단을
의미하지 않는다. 실제 손 놓임이나 미등록 외부 상품 종류를 직접 인식하는 기능도 아니다.

같은 SKU가 여러 층에서 취출되면 물리 인스턴스의 원래 층은 확정할 수 없다.
같은 SKU 안에서 동일 층 이력을 먼저 소비하고 세션 전체 잔여 수량을 보존한다.
행동 필드가 없는 구 저널은 기존 정산을 유지하며, 기록되지 않은 반납 종류를 새로
복원하지 않는다. 단방향 일반 취출은 기존 라우터/정산 경로를 유지한다.

## env와 검증

기존에 간소화한 카메라 보정용 신규 env 6개를 유지하고 이번 기능용 env는 추가하지 않았다.
관측 수, 경계 여유, 정체성 우세, 매칭 규칙은 코드 기본값이다. `freezer.env.example`의
주석을 실제 반납 판정에 사용한다는 설명으로 갱신했다. 예시 경계240px는 현장 영상에서
보정해야 하며 실제 기기 `.env`는 수정하지 않았다.

새 회귀 사례 38개를 포함한 전체 결과: `570 passed, 24 skipped`.
유사 무게/다품목/층간/초과·선행 반납, 재취출, 왕복·동일 무게 교환, 두 카메라 합치기와
충돌, 경사 side의 X축 반납, 클래스 플리커, 경계 미통과, 공유 영상, 안정화,
두 로드셀 분석기, 저널 재생과 YAML 직렬화를 검증했다. 변경 코드 ruff와
`git diff --check`가 통과했다. 합성 입력 검증이며 실제 장치 영상 재생은 아직 하지 않았다.

# 2026-11-12 render-session의 python/tuple YAML 읽기 오류 수정

반납 정보의 `judgment.products` 튜플이 `action_to_dict()`에 남아 일반 YAML 더머가
`!!python/tuple`을 기록했다. 안전 로더를 사용하는 `render-session --latest`에서
`ConstructorError`가 발생하는 것을 실제 `SessionArchive.save()` 경로로 재현했다.
앞선 검증은 `safe_dump()`를 직접 사용해 실제 writer의 타입 태그를 놓쳤다.

행동 직렬화에서 상품 튜플을 목록으로 변환하고, 아카이브 신규 저장/라벨 재저장 모두
`CSafeDumper`/`SafeDumper`로 통일했다. 기존 파일의 튜플 태그는 아카이브 전용 안전
로더에서 목록으로만 읽는다. 임의 Python 객체 태그는 계속 거부하며, 읽기만으로
기존 파일을 변경하지 않는다. 기존 세션은 새 파일로 다시 생성할 필요 없이 읽을 수 있다.

회귀 사례 12개를 추가했다. 실제 반납 이벤트 저장→표준 안전 로더 읽기, 중첩 튜플
태그의 기존 파일 읽기, 라벨 수정 후 표준 YAML 저장, 임의 Python 태그 거부,
`render-session --latest`의 파일 로드→렌더 호출 경로를 C/순수 Python 백엔드에서
검증했다. CLI 검증에서 영상 인코딩은 스텁을 사용했고 실제 장치 영상은 재생하지 않았다.
전체 결과는 `582 passed, 24 skipped`이며 변경 코드 ruff와 `git diff --check`가 통과했다.

# 2026-11-12 카메라 방향 변경: 위쪽 반납 / 아래쪽 취출

사용자가 변경된 카메라 방향을 확인했다. 영상 좌표의 Y=0은 화면 위쪽 그대로이며,
현재 행동 의미는 Y 감소(위쪽 이동)=반납, Y 증가(아래쪽 이동)=취출이다.
앞선 단계에 기록된 증가=반납 설명은 이전 카메라 방향 기준이다.

`MotionDirectionConfig`와 side Settings/env 로더 기본값을 `decreasing`으로 변경했다.
top 기본 방향과 side 기본 방향 모두 Y 감소=반납으로 맞추고 env 예시·주석·문서를
수정했다. 경계240/여유10이면 Y 250 이상→230 이하가 반납 통과이며 역방향은 취출이다.
추가 env는 만들지 않았다. side의 설치별 X/Y·방향 보정 기능은 유지한다.

기기의 `.env`에 `MODEL__VISION__FREEZER_SIDE_RETURN_DIRECTION=increasing`이 명시돼
있으면 코드 기본값보다 우선하므로, 현재 Y축 방향에 맞춰 `decreasing`으로 바꾸고
서비스를 재시작해야 한다. 과거 아카이브에 기록된 방향 판정은 다시 해석하지 않는다.

왕복/다품목/층간 반납 입력을 새 방향에 맞춰 검증했다. 기본 Settings·env 로더에서
서비스까지 같은 방향이 전달되는 것과, 아래쪽 이동을 반납으로 잘못 확정하지 않는
회귀 사례를 추가했다. 전체 `585 passed, 24 skipped`, 변경 코드 ruff와
`git diff --check`가 통과했다.

# 2026-11-12 냉동 좌표 원점을 아래로 통일: Y=0~350 검출

사용자가 `(0,0)`을 영상 왼쪽 아래로 지정하고, 아래 원점 Y=350까지를 검출
구간으로 확인했다. 이 절이 현재 냉동 좌표 기준이며, 위 원점·감소=반납으로 적힌
직전 기록은 변경 전 기준이다.

- 480×480 크롭의 냉동 판단 좌표는 `Y=480-영상 bbox 중심 Y`다.
- `FREEZER_ROI_VERTICAL_REGION=lower`, `FREEZER_ROI_Y_SPLIT=350.0`이면 상품
  중심 Y=0~350을 포함한다. 영상 아래쪽 350px를 유지하고 위쪽 130px를 제외한다.
- top과 경사 side에 모두 적용한다. side 별도 ROI 경계는 아래 원점 기준이며,
  빈 값이면 공통350을 사용한다. 수직 ROI가 켜져 있으면 기존 side X ROI는 생략한다.
- 위로 이동하면 판단 Y가 증가하므로 반납, 아래로 이동하면 감소하므로 취출이다.
  side Y축 기본 반납 방향도 `increasing`으로 맞췄다. 실제 동작 방향은 직전 단계와 같다.
- 반납 경계도 같은 아래 원점이다. 경계240/여유10의 반납은 Y≤230→Y≥250이며,
  취출은 반대다. 경계와 검출 ROI는 서로 다른 역할이다. 신규 env는 추가하지 않았다.

상품/손 bbox 자체는 영상 위 원점 좌표로 보존한다. 손 교차·추적·원본 영상 위 박스
위치를 유지하면서 ROI·방향·경계 판단에서만 변환한다. 새 냉동 아카이브에는
`decision_coordinate_origin=bottom_left`를 남긴다. 렌더 라벨은 아래 원점 중심 XY와
`(0,0) Y+ UP`을 표시하고, 분석 CLI는 원점과 변환된 방향 구간 좌표를 출력한다.
원점 스탬프가 없는 과거 세션은 기존 표시를 유지한다. 냉장 ROI는 기존 위 원점 기준이다.

`freezer.env.example`와 코드 기본값을 맞췄다. 실제 기기 `.env`에 예전 값이 명시돼
있으면 다음 값으로 맞춘 뒤 서비스를 재시작해야 한다. 실제 기기 설정은 수정하지 않았다.

```dotenv
MODEL__VISION__FREEZER_ROI_VERTICAL_REGION=lower
MODEL__VISION__FREEZER_ROI_Y_SPLIT=350.0
MODEL__VISION__FREEZER_SIDE_RETURN_DIRECTION=increasing
```

아래350 경계의 포함/제외(top·side), 비대칭 Y 경계 통과와 역방향, X축 유지,
손/bbox 보존, ROI→반납 판정→아카이브 연결과 렌더 좌표 표시를 검증했다.
전체 `602 passed, 25 skipped`, 변경 코드 ruff와 `git diff --check`가 통과했다.
numpy/ffmpeg 등 환경 의존 사례는 건너뛰었으며 렌더 픽셀 검증 1건도 numpy 부재로
건너뛰었다. 실제 장치 영상 재생은 아직 하지 않았다.

# 2026-11-12 기존 무게 처리 복원: 방향 진단으로 취출·반납을 막지 않음

사용자가 무게 처리와 반납 판단은 기존대로 유지해야 한다고 명시해 당시 처리 기준을
복원했다. 이후 비전 SKU 연결과 제한적인 혼합 해석을 보완했으므로, 현재 처리 기준은
[2026-11-18 기록](#freezer-20261118)을 따른다.

`ses-1-1794471430`에서 검출 후보가 남았는데도 2층 +80→−450g, 3층 +235→−705g을
혼합 행동으로 분류했다. 양쪽 방향의 경계 통과를 요구하면서 `crossed_directions=[]`가
되자 `freezer_mixed_actions_unresolved`로 상품을 비우고 최종 0원으로 종료했다.
중간 무게 증가를 새 반납 행동으로 해석해 기존 취출 경로를 막은 변경이었다.

이번 수정은 다음을 복원했다.

- 최종 무게 감소는 취출, 증가는 반납으로 기존 라우터·정산기에 전달한다.
  +80→−450g은 최종 −370g 취출, +235→−705g은 −470g 취출로 일반 판정한다.
- 중간 양수/음수 구간을 별도 행동으로 쪼개는 `_judge_freezer_actions`와 전용 수량
  추론을 제거했다. 원래의 채널별 무게 판정, 세그먼트 재시도와 수량 라우터를 사용한다.
- plateau/BOCPD를 행동별 추론 추가 전의 무게 분석으로 복원했다. 반납 안정화는
  기존처럼 최종 무게 증가에 적용하며, 중간 양수 구간만으로 취출에 반납 대기를 추가하지 않는다.
- 냉동도 기존 저무게 스킵을 적용한다. 순무게 0인 왕복 구간을 별도 행동으로 보존하거나
  영상으로 교환 판정을 강제하지 않는다. 로드셀 신뢰 불가 시 기존 vision_only 경로는 유지한다.
- 신규 이벤트는 `freezer_actions=None`으로 기존 ghost/cross-zone/무게 정산에 들어간다.
  같은 층에서 무게 매칭을 먼저 하고, 실패하면 앞서 취출한 다른 냉동 층에서 찾는다.
- 아래 원점 Y=0~350 ROI, 원본 bbox 보존, top·side 이동 방향과 경계 진단은 유지했다.
  방향 충돌·미검출·경계 미통과로 기존 무게 판정을 취소하지 않는다. env 값은 추가하지 않았다.

이전 행동별 저널/아카이브 형식은 읽기·당시 의미의 재생 호환을 위해 남겼다.
이미 저장된 0원 세션을 자동으로 재판정하지 않는다. 현재 반납은 기존 무게 매칭이므로
같은 무게의 다른 SKU 교환과 유사 무게의 복수 후보를 영상으로 확정하는 기능은 적용하지 않는다.

실측 무게 패턴에 합성 상품 정보를 넣은 취출 회귀, 영상 방향 반대/미검출/경계 미통과
반납, 카메라 종류 충돌, 같은 층·다른 층 반납, 두 분석기의 기존 안정화/저무게 게이트,
ROI·진단 아카이브와 과거 행동별 재생을 검증했다. 전체 `611 passed, 25 skipped`이며
변경 코드 ruff와 `git diff --check`가 통과했다. 실제 영상과 당시 상품 스냅샷을
사용한 원 세션 전체 재추론은 하지 않았다.

# 2026-11-13 다른 층 반납: 비전 SKU와 실측 취출 delta 연결

사용자가 제공한 11월12일 실기4세션의 실제 동작은 다음과 같다.

| 세션 | 실제 동작 | 기대 청구 |
|---|---|---|
| ses-1-1794472933 | 3층 핫도그, 5층 용가리 치킨 취출 | 핫도그1 + 용가리1 |
| ses-2-1794472980 | 3층 핫도그, 4층 오리고기 취출 후 핫도그를 2층 반납 | 오리고기1 |
| ses-3-1794473065 | 2층 핫도그 취출 후 5층 반납 | 없음 |
| ses-4-1794473128 | 5층 핫도그 취출 후 같은 층 반납 | 없음 |

실패 원인은 반납을 등록 무게와 비교하던 경로였다. 핫도그 등록 무게는490g인데
ses-2 취출−465g/반납+466.7g, ses-3 취출−462.5g/반납+465g이라 등록값과는
±15g 밖이었다. 취출은 near-gate PARTIAL로 핫도그1개를 유지했지만 반납은
`unmatched_return`이 되어 기존 청구가 남았다.

BOCPD/plateau의 계산과 일반 취출 라우터는 변경하지 않았다. `0→+100→−370`은
최종 delta−370g이며, 중간−470g을 취출량으로 쓰지 않는다. 동일 층 왕복이
한 트리거의 delta0이면 기존 저무게 스킵도 유지한다.

새 냉동 양수 트리거에 `freezer_return`을 저장한다. 반납 방향 영상 후보를 먼저
사용하고 없으면 전체 영상 후보를 사용하며, 무게 안정 여부도 함께 기록한다.
정산은 기존 ghost/cross-zone 보정 뒤 시간순으로 진행한다. 비전 우세 후보의 SKU를
먼저 결정하고 해당 SKU의 잔여 취출 이력만 대상으로 측정 delta를 검증한다.
한 종류 취출의 단위 실측값은 `abs(delta)/수량`이다. 여러 종류를 함께 취출한 경우는
개별 측정량을 분리할 수 없어 등록 단위 무게를 사용한다.

후보는3표 이상·차순위1.5배 이상 우세를 요구한다. 다른 잔여 SKU도 선두의 절반 이상
관측되면 복수 종류 반납일 수 있어 보류한다. 같은 SKU의 여러 출발 층/취출 시점에서
최대12개까지 매칭하되, ±15g 안의 수량이 유일해야 한다. 같은 SKU 안에서 동일 층을
우선한다. 다른 무게의 같은 SKU 이력도 적합한 조합을 찾는다. 이미 반납한 수량은
재사용하지 않고 미래 취출과 과거 반납도 연결하지 않는다.

비전 미검출/모호성, 무게 불일치, 선행 취출 없음, 반납 안정화 미완료는 차감하지
않고 notes와 `review`/`partial`로 남긴다. 뒤의 net 보정이나 재solve가 다른 SKU를
대신 지우거나 반납한 상품을 다시 추가하지 않도록 관련 원장을 보호한다.
이 상태가 자동 결제 차단을 뜻하지는 않는다. 같은 무게의 다른 SKU 교환이 한 트리거의
delta0으로 끝나는 경우와 여러 종류의 동시 반납은 여전히 별도 검토가 필요하다.

저널/아카이브에 새 증거를 표준 JSON/YAML로 보존한다. 새 필드가 없는 구 기록은
기존 무게 경로를 유지하며 과거 `freezer_actions` 재생도 보존한다. 신규 env는 없다.

네 세션의 저장된 잠정 판정·영상 후보를 재정산해 위 기대 결과를 모두 확인했다.
회귀 fixture는 아카이브에 있는3개 상품 메타데이터를 사용한다. OPEN 당시 전체12개
스냅샷과 재고는 없어 fixture 재고 상한을1로 제한했으며, 반납 증거는 당시 방향 후보와
안정화 대기 없는 무게 구간에서 복원했다. 실제 AVI의 YOLO 재추론은 하지 않았다.

새 매칭 회귀37개와 기존 무게 회귀의 최종 delta 사례2개를 포함해 전체
`650 passed, 25 skipped`, 변경 코드 ruff와 `git diff --check`가 통과했다.
실기에서 서버 코드를 반영하고 서비스를 재시작해야 적용된다. 실제 기기 `.env`,
원본 세션 YAML과 결제 이력은 변경하지 않았다.

---

<a id="freezer-20261116"></a>

# 2026-11-16 누락 기록 보충: 혼합 취출·반납, 채널 잔차, 같은 기준의 무게 비교

이 절은 기존 문서에서 빠져 있던 11월 16일 작업을 보충한다.
이미 반영된 커밋 `1c69444`, `fed0e38`, `7bb4f25`와 저장 세션 회귀 테스트를 기준으로 한다.
11월 18일에 추가한 손 추적·취출 이력 보존과는 작업 시점을 구분한다.

## 1. 작은 무게 변화에도 영상 후보로 상품이 추가되던 문제

냉동 상품은 200g 이상인데, 약 5~20g의 변화에도 영상 추론과 상품 판정이 실행됐다.
실제 상품 취출이 다른 층 영상에 함께 찍히면 이 작은 변화에도 상품 하나가 붙을 수 있었다.

파일럿 설정으로 `FREEZER.min_trigger_delta_grams=50.0`을 추가했다.
전체 트리거의 `abs(delta) < 50g`이면 안정 여부와 관계없이 영상 추론을 생략한다.
이 값은 코드의 냉동 프로파일에 있으며 신규 env는 없다.

**50g은 추론을 시작할 최소 변화량이고, 상품 무게의 매칭 허용오차는 기존 15g이다.**
센서의 기본 변화 기준 5g과 세그먼트 구분 기준 20g도 별도로 남아 있다.
이 변경만으로는 큰 상품 채널에 섞인 작은 부채널까지 막지 못했으며, 그 부분은 11월 18일에 보완했다.

## 2. 11월 16일 오전 ses-2: 청양(101) 대신 너겟(99)을 판정한 문제

대상은 `ses-2-1794789461.yaml`이다.
실제 동작은 1층 너겟(99), 3층 오리지널(102), 5층 청양(101) 취출이다.
사용자가 확인한 청양 등록 무게는 530g이며 재고도 있었다.

저장 데이터의 5층 기록은 다음과 같다.

| 항목 | 저장 내용 |
|---|---|
| 최종 delta | −502.5g |
| 영상 전체 주요 후보 | 102:34표, 103:21표, 101:8표, 99:7표 |
| 취출 방향 후보 | 102:16표, 101은 방향 후보에서 빠짐 |
| 당시 판정 | 너겟(99) 1개 COMPLETE |

너겟 등록 500g은 관측값과 2.5g 차이지만, 청양 등록 530g은 27.5g 차이다.
따라서 실제로 보인 상품보다 무게가 맞는 다른 후보가 선택되는 문제와,
비전의 취출 방향 후보 자체가 101 대신 102를 지지한 문제가 함께 있다.
재판정이 있다고 해서 방향 후보에 빠진 정답을 자동 복원할 수 있는 상황은 아니다.

이 건은 101의 등록 무게 누락이나 품절로 설명하지 않는다.
11월 18일 변경은 무게 때문에 배경 SKU로 바뀌는 경로를 줄이지만,
이 세션의 정답 101까지 복구했다고 검증한 것은 아니다. 실제 영상의 101/102 혼동은 남은 확인 항목이다.

## 3. 양수 반납인데 no_detection으로 끝난 ses-4·5·6

양수 트리거의 `judgment=no_detection`만 보고 반납 실패로 단정할 수 없다.
신규 취출 상품을 추가하지 않는 양수 트리거도 CLOSE에서 앞선 취출을 차감할 수 있다.
실제 실패 여부는 최종 상품 목록과 `freezer_vision_return` 또는 `freezer_return_unresolved`로 확인한다.

| 세션 | 실제 동작 | 저장 증거를 재정산한 결과 |
|---|---|---|
| ses-4-1794795619 | 1층 99, 2층 103·98 취출 후 103을 1층 반납 | 99 1개 + 98 1개 |
| ses-5-1794795995 | 1층 103, 3층 102 취출 후 103을 3층 반납 | 102 1개 |
| ses-6-1794796052 | 4층 95, 2층 98 취출 후 98을 5층 반납 | 95 1개 |

ses-5의 출발 층은 사용자가 **1층 취출**로 정정했다. 2층이라는 최초 설명은 오기다.

해결을 위해 다음 경로를 보완했다.

- 전체 영상 선두가 실제 반납품과 다를 때, 영상에 실제 관측된 다른 후보의 실측 무게를 검증한다.
  적합한 대안이 하나일 때만 사용하고, 보이지 않은 상품이나 여러 적합 후보 중 하나를 임의로 고르지 않는다.
- 같은 실측 무게로 다른 층의 상품이 같은 SKU로 중복 판정될 때, 양 시점의 취출 관측 순서가 일치하고
  서로 다른 행동 시각이 확인되면 교차존 판정을 보완한다. 순서 누락·충돌·동시 관측은 모호하게 남긴다.
- 취출과 반납이 한 트리거로 병합되면 최종 순변화만으로 두 행동을 잃지 않도록 CLOSE에서 제한적으로 해석한다.
- 단일 상품 채널과 작은 부채널을 분리해 반납 연결을 시도하되, 전체 delta와 잔차는 보존한다.

## 4. 한 트리거 안에 다른 상품의 취출과 반납이 섞이는 경우

ses-5의 3층은 오리지널을 꺼내고 닭발을 반납해 최종 delta가 −320g이었다.
이 음수 값을 닭발 또는 오리지널 하나의 무게로만 보면 실제 행동을 놓친다.

현재 혼합 해석은 마지막 음수 변화 **뒤에 남는 안정된 증가 구간**을 사용한다.
앞선 닭발 취출 이력, 닭발 반납 방향 후보, 오리지널 취출 방향 후보가 함께 있어야 한다.

```text
앞선 1층 닭발 취출: −205g
3층의 반납 증가:   +205g
3층 원본 최종 delta: −320g

3층 취출량 검증: 205 − (−320) = 525g
정산 내부 해석: 오리지널 −525g + 닭발 반납 +205g = 원본 −320g
최종 상품: 오리지널 1개
```

취출 SKU와 반납 SKU가 확인되고, 앞선 이력이 아직 남아 있고, 반납이 안정되고,
분해한 취출량이 기존 라우터에서 COMPLETE가 될 때만 적용한다.
원본 트리거의 delta와 보고용 층별 합은 바꾸지 않는다.
손으로 누른 중간 양수 봉우리를 모두 상품 반납으로 만드는 방식은 아니다.

## 5. ses-6의 +489.2g과 +465g, +24.2g은 서로 다른 용도

```text
반납층 원본 전체 delta: 약 +489.2g
상품 채널의 증가:       +465.0g
나머지 부채널 변화:     약 +24.2g
```

채널 합이 원본 전체 delta와 일치하고, 상품 크기 채널이 하나이며,
다른 채널 변화의 절댓값 합이 기존 50g 기준 미만일 때만 +465g으로 반납 연결을 시도한다.
나머지 +24.2g을 충격이라고 확정하거나 전체 delta에서 지우지 않는다.

정산 사유는 `freezer_return_channel_residual:+24.2g:total=+489.2g`에 남고,
연결 사유에는 `take=465.0g:return=465.0g`이 기록된다.
최종 OPS 로그는 기존처럼 전체 `weight_delta`를 표시하고 관련 notes를 함께 출력한다.
이름이 별도의 센서 측정값이라는 뜻은 아니며, 같은 기록에서 전체량·연결량·잔차를 구분한 것이다.

당시 ses-6 회귀 자료에는 개별 BOCPD 채널 delta가 모두 저장되지 않아,
기록된 주 채널 값과 전체 합으로 부채널 잔차를 복원했다. 원시 센서를 다시 측정한 값은 아니다.

## 6. 오후 ses-3: 삼계탕은 돌아왔는데 무게 비교에서 탈락한 문제

대상은 `ses-3-1794808045`다.
실제 동작은 3층 삼계탕(100)을 5층으로, 1층 오리로스(92)를 4층으로 옮기고
1층 너겟(99)만 꺼낸 것이므로 기대 최종 상품은 너겟 1개다.

취출은 마지막 3샘플 중앙값을 사용하는 경우가 있고, 양수 반납은 마지막 BOCPD 구간 평균을 사용한다.
계산 기준이 다른 값을 비교하면 같은 상품도 15g 밖으로 벗어날 수 있었다.

시작과 종료가 모두 안정되고, 취출·반납 각각에서 기여한 채널의 시작·종료 기록이 완비돼 있으면,
반납 매칭은 양쪽 모두 `종료 3샘플 중앙값 − 시작 3샘플 중앙값`으로 비교하도록 보완했다.
이 비교용 값은 `stable_channel_deltas`에 저장한다.
한쪽 기록이 없으면 양쪽 모두 기존 delta로 비교하며, 잘 맞는 계산식만 골라 쓰지 않는다.

삼계탕은 취출 890g과 반납 비교값 905g으로 기존 ±15g 안에 들어가 연결됐다.
반납층 원본 전체 delta +912.5g은 그대로 보존했다.

오리로스는 취출 약 520g에 비해 반납층 전체 증가가 약 400g 수준이고,
실제 기록의 비교량도 부족했다. 사용자는 벽이나 다른 상품에 걸쳤을 수 있다고 확인했다.
따라서 이 저장 자료는 **삼계탕 연결만 해결됐고, 오리로스는 review로 남는다.**
과거 기록에 없는 손 이탈·상품 잔류 증거를 11월 18일 기능으로 소급 생성하지 않는다.

## 7. 무게와 비전은 고정 5:5 점수가 아니다

냉동은 `weight_is_discriminative=False`로, 비전 후보에서 종류를 고르고 무게로 수량과 적합성을 검증한다.
분기별 가드와 재판정이 있으므로 전체 동작을 비전:무게 5:5 같은 비율로 표현하지 않는다.

BOCPD 내부에는 정규분포를 사용하는 변화점 추정이 있다.
`sigma=2.5`는 센서 관측의 흔들림 가정이며, 매 delta에서 2.5g을 빼는 보정치가 아니다.
상품 등록 무게마다 하나의 정규분포를 두고 최종 결제 SKU를 고르는 단일 공식도 아니다.

벽에 기대어 실제 하중이 적게 실리는 양은 상품과 상황마다 달라,
고정 100~150g 가산, 일괄 비율 보정, 무게 그룹만으로의 SKU 확정은 적용하지 않았다.

---

<a id="freezer-20261118"></a>

# 2026-11-18 비전 증거와 세션 전체 취출·반납 연결 (2026-11-25 되돌림)

> **2026-11-25 되돌림**: 로드셀 무게를 안정화된 것으로 판단해 이 절의 구현(커밋 `4390e7a`·`8433acf`)을
> 코드에서 되돌렸다. 손 ID 추적, 방향 영상 후보 우선, 무게 불일치 반납의 비전 연결, 채널 검증 취출,
> 작은 부채널 제외, 세션 전체 반납 공동 배정은 현재 코드에 없다. 아래는 당시 기록이다.
> 현재 기준은 [2026-11-25 절](#freezer-20261125).

## 작업 목적과 현재 상태

냉동 상품이 벽이나 다른 상품에 기대면 취출 때 로드셀에서 빠지는 하중과
반납 때 실리는 하중이 다를 수 있다. 단순 무게 매칭을 넓히면 비슷한 무게의 다른 상품까지
차감할 위험이 있어, 실제 상품의 움직임과 손 추적 증거를 더 적극적으로 사용하도록 보완했다.

| 현재 문제 | 반영한 변경 | 남은 조건·한계 |
|---|---|---|
| 큰 상품 채널 옆 6g 변화에 상품 1개가 붙음 | 작은 부채널을 독립 상품 판정에서 제외 | 원본 전체 delta에는 포함 |
| 상품 채널에서 확인한 취출이 전체 PARTIAL 때문에 반납 분석에서 빠짐 | 채널별 검증 취출 이력을 별도 보존 | 원래 판정에 없는 상품·수량을 추가하지 않음 |
| 실제 오리로스를 무게가 비슷한 배경 상품으로 바꿈 | 취출 방향 후보와 해당 후보의 정체성을 우선 보존 | 비전 자체가 다른 SKU를 잘못 지지하면 정답 보장 불가 |
| 손이 매 프레임 별개 검출이라 잡음·놓임 연결이 약함 | `model.track()`으로 손 ID를 연결 | 실제 Jetson 엔진 실행 검증 필요 |
| 전체 영상의 다른 행동 표가 섞임 | 영상 내 이동 방향과 손 연결 궤적을 우선 사용 | 무게 시각에 따른 후보 제한은 시계열 연동 정보가 없어 제외 |
| 먼저 들어온 반납만 따로 처리해 전체 연결이 모호함 | CLOSE에서 복수 반납을 함께 검토 | 동점 해는 미확정 유지 |
| 벽에 기댄 상품의 취출·반납 실측이 크게 다름 | 엄격한 놓임 증거와 잔여 1개 조건에서 시각적 반납 연결 | 여러 개 수량·동시 같은 SKU 취출 문제는 미해결 |

## 1. 작은 부채널을 별도 상품으로 만들지 않는다

기존의 전체 트리거 50g 게이트에 더해, 냉동의 채널별 상품 판정에서도
기존 기준인 50g 미만 변화를 제외한다. 예를 들면 다음과 같다.

```text
채널 A: −523.33g → 상품 판정 대상
채널 B:   +6.25g → 별도 상품 판정 제외
전체 delta: 약 −517.08g → 기존 값 보존
```

두 채널이 각각 상품 크기의 변화를 보이면 둘 다 기존 개별 판정 대상이다.
50g 미만이라는 이유만으로 그 변화의 원인을 충격·몸 접촉·영점 이동 중 하나로 확정하지 않는다.
trace에 `freezer_small_channel:chN:+6.25g` 같은 제외 사유를 남긴다.

## 2. 확인된 취출을 반납 연결 이력에서 보존한다

이전 혼합 해석은 전체 판정이 COMPLETE인 취출만 이력으로 받아들였다.
상품 채널은 맞게 판정했어도 다른 채널 때문에 전체 PARTIAL이 되면,
앞서 꺼낸 오리로스가 반납 매칭에는 없는 상품이 되어 `no_prior_take`가 발생할 수 있었다.

이제 상품 채널이 COMPLETE로 검증한 SKU·수량·실측 delta를
`freezer_movement.verified_takes`에 따로 저장한다.
전체 상태가 PARTIAL이어도 해당 상품이 원래 판정 목록에 포함되어 있으면 반납 연결에 사용할 수 있다.
구 기록은 단일 SKU의 실제 전체 무게가 등록 총량과 허용오차 안에서 맞는 PARTIAL도 제한적으로 보존한다.

**취출 이력을 보존한다는 말은 상품을 추가 결제하라는 뜻이 아니다.**
기존 상품 목록에 이미 있는 수량 안에서만 이력을 만들고, 반납되면 그 잔여 수량을 차감한다.
모든 PARTIAL을 검증된 취출로 바꾸거나 COMPLETE로 승격하는 변경은 아니다.

## 3. 무게가 가깝다는 이유로 행동에서 본 상품을 배경 SKU로 바꾸는 일을 줄인다

취출 방향 후보가 있으면 냉동 음수 트리거의 판정 후보를 그 목록으로 제한한다.
예를 들어 전체 영상에는 오리로스(92)와 만두(96)가 있어도 취출 구간에 96이 없으면,
관측 −370g이 만두 등록 373g과 가깝다는 이유만으로 96을 끼워 넣지 않는다.

판정이 불완전하고 행동 선두 SKU가 결과에서 빠졌다면,
그 선두 후보만으로 재판정해 정체성을 보존하는 경로도 추가했다.
행동 증거를 보존한 취출이 무관한 교차존 후보의 재판정 때문에 다시 바뀌는 것도 제한했다.

모든 COMPLETE 판정을 비전 1위로 일괄 교체하지는 않는다.
기존 수량 라우터와 적합성 검증이 남아 있고, 여러 행동 후보 간 충돌·수량 불확실성도 남을 수 있다.
이번 수정은 고정 점수 비율 변경이 아니라 **후보 선택과 보존 권한을 행동 증거에 더 부여한 변경**이다.

행동 후보를 우선하는 신규 경로에서는 중간 세그먼트 합을 다른 무게 타깃으로 삼는 재시도를 하지 않는다.
현재 논의에서 몸 접촉·상품 이동의 중간 봉우리를 보정 무게로 쓰지 않겠다는 기준을 반영했다.
행동 증거가 없는 구 경로와 냉장 경로는 기존 동작을 유지한다.

## 4. `model.track()`은 손 검출에 ID를 붙인다

새 설정은 하나다.

```dotenv
MODEL__VISION__TRACKER=bytetrack
```

`off / bytetrack / botsort`를 지원한다.
코드 및 일반 `.env.example`의 기본값은 `off`, `freezer.env.example`은 `bytetrack`이다.
실제 서비스의 환경 설정에 이 값이 없으면 추적이 자동 활성화되는 것은 아니다.
기존 `SIDE_HAND_ENABLED=1`은 경사 side에서도 손을 추론하도록 유지한다.

활성화되면 `model.track(persist=True, tracker="bytetrack.yaml")`을 사용한다.
손 검출에만 추적 ID를 붙이고, 상품 bbox·저신뢰 검출·기존 상품 투표와 튜브는 보존한다.
정적 batch 엔진의 패딩 프레임은 손 추적 시간에 포함하지 않는다.
이는 손 검출 모델을 새로 학습하거나 손 검출 정확도가 검증됐다는 뜻이 아니다.

냉동은 고정 top과 약 45도 경사 side가 같은 동작을 촬영한다.
현재 엔진은 저장된 top 전체를 처리한 뒤 side 전체를 처리하므로,
각 촬영 영상 시작에서만 `begin_clip(camera)`로 추적 구간을 새로 연다.
같은 영상의 프레임·배치 사이에서는 손 ID를 유지한다.
top의 손 ID 7과 side의 손 ID 7, 다른 층·다른 영상의 손 ID 7은 같은 손이라는 의미가 아니다.
카메라가 물리적으로 전환된다는 가정이나 장치 전환 이벤트는 없다.

## 5. 손과 함께 움직인 상품, 손을 뗀 뒤 남은 상품을 구분한다

상품의 행동 구간에서는 최소 3회 관측과 튜브 내 종류 2:1 우세를 확인한다.
같은 손 ID가 상품 bbox 주변에 3회 이상 함께 관측되면 손과 상품을 연결한다.
같은 행동에 이런 연결이 있으면 손과 연결된 궤적을 우선 사용하고,
추적 ID를 얻지 못하면 기존 이동 방향 증거를 유지한다.

반납 완료 증거는 다음을 함께 관측했을 때만 만든다.

1. 반납 방향으로 이동한 상품과 연결된 손 ID가 있다.
2. 그 손이 상품에서 떨어진 위치에서 실제로 다시 관측된다.
3. 다른 손도 상품 근처에 남아 있지 않다. ID 없는 손 검출도 확인한다.
4. 손 이탈 후 상품이 3회 이상 남아 있고, 해당 관측들의 중심 이동이 기존 정지 허용 범위 안이다.

손이 가림이나 프레임 밖 이동으로 검출되지 않았다는 이유만으로 반납 완료를 만들지 않는다.
이 조건은 화면에서의 손 이탈·상품 정지 증거이며, 실제 하중이 전부 트레이에 실렸다는 보장은 아니다.

## 6. 무게가 다른 반납도 연결하는 범위

기본은 같은 SKU의 앞선 실측 취출량과 반납량을 기존 ±15g으로 검증하는 경로다.
안정된 같은 기준의 시작·종료 기록이 양쪽에 있으면 앞서 설명한 끝점 비교를 사용한다.

그 검증이 `weight_mismatch`로 실패해도 다음 조건을 모두 만족하면 1개 반납을 연결할 수 있다.

- 반납 방향에서 확인된 SKU가 하나이고 3표 이상이다. 전체 영상 후보만 있는 경우는 제외한다.
- 반납과 움직임의 안정화가 확인돼 있다.
- 위의 손 이탈·상품 잔류 증거가 실제로 확보됐다.
- 앞선 취출 이력에 같은 SKU의 잔여 이력이 하나이며, 그 이력의 남은 수량이 정확히 1개다.
- 반납 비교량이 유한한 양수이고 등록 단위 무게 +15g을 넘지 않는다.

예를 들어 같은 상품이 벽에 기대어 취출은 370g, 반납은 523g으로 측정됐다면,
이 조건을 갖춘 신규 영상에서는 잔여 1개를 반납으로 연결할 수 있다.
`freezer_return_vision_presence:take=370.0g:return=523.0g`를 남기고 검토 사유를 유지한다.
이 예시의 세션 원본 합은 −370+523=+153g 그대로다. 누락된 하중을 보정해 0으로 만들지 않는다.

같은 SKU가 여러 이력에 남아 있거나, 여러 개 중 몇 개를 놓았는지 모호하거나,
상품이 계속 움직이거나 손 증거가 없으면 이 예외를 사용하지 않는다.

## 7. 무게 시각에 맞춘 영상 후보 제한은 제외

처음에는 무게 변화 구간 앞뒤 1초 안의 영상 관측만 사용하는 기능을 추가했으나,
사용자가 시계열 연동 정보가 없다고 확인해 11월 18일에 제외했다.
촬영 시작 시각·FPS의 추가 입력, 시간창 생성과 프레임 선별,
관련 아카이브 필드와 시각 미정렬 사유 로그도 함께 제거했다.

현재 행동 후보는 영상 안에서 관측한 상품의 이동 방향과 손·상품 연결을 기준으로 만든다.
무게 변화 시각을 영상 프레임에 대입하지 않으며, 요청 시각·파일명으로 촬영 시각을 추정하지 않는다.
기존 로드셀 분석과 최종 delta, 무게 세그먼트를 사용하는 혼합 취출·반납 해석은 유지한다.

## 8. 문을 닫을 때 여러 반납을 함께 연결한다

기존 혼합 해석 다음에 `freezer_session.resolve_session_returns()`를 추가했다.
복수의 안정된 반납과 신규 행동 증거가 있으면, 앞선 상품별 잔여 취출과의 가능한 연결을 함께 검토한다.

```text
취출 이력: A 1개, B 1개
첫 반납 영상: A/B가 비슷하게 관측됨
둘째 반납 영상: B만 확인됨

전체 증거가 일관되게 설명되면: 첫 반납 A, 둘째 반납 B
두 배정이 똑같이 가능하면: 모호한 상태 유지
```

연결은 실제 관측 후보와 앞선 취출에 한정한다. 미래 취출이나 이미 소비한 수량은 사용하지 않는다.
전체 해 가운데 같은 SKU로 일관되게 결정되는 반납만 후보를 좁힌다.
이미 명확하게 연결된 비전 결과를 더 많은 반납을 성립시키려고 약한 다른 후보로 바꾸지 않는다.
원본 delta와 등록 무게를 바꿔 답을 맞추지도 않는다.

구 행동별 정산을 안전하게 재구성할 수 없거나 무게 비교 기준이 섞이면 기존 정산으로 돌아간다.
반납 12건 초과 또는 탐색 4096회 초과도 기존 경로로 돌아가며,
`freezer_session_assignment_search_limit`를 기록한다.
이 공동 배정이 가능한 모든 복잡한 혼합 트리거를 한꺼번에 풀어주는 것은 아니다.

## 9. 현재 판단 흐름과 로그의 의미

```text
로드셀 분석 → 원본 최종 delta 유지 → 전체 50g 게이트
  → top·side 추론 / 선택적으로 손 ID 추적
  → 영상 내 방향 구간 + 손 연결
  → 상품 채널 판정 / 작은 부채널 제외 / 검증 취출 이력 보존
  → CLOSE의 기존 ghost·교차존 보정
  → 조건을 갖춘 혼합 취출·반납 해석
  → 복수 반납 공동 배정
  → 같은 SKU의 잔여 취출 차감 / 미확정 사유 보존
```

로그의 `weight_delta`는 기존 분석기가 만든 전체 순변화이며, 연결용 상품 채널량으로 대체하지 않는다.
`0 → +100 → −370`이면 최종 순변화는 −370g이고, 중간 +100에서 −370으로 내려간 470g과 구분한다.
원시 시계열 전체의 이동 절댓값을 상품 무게로 합산하지 않는다.

| 기록 | 의미 |
|---|---|
| `freezer_small_channel` | 작은 채널을 독립 상품 판정에서 제외 |
| `freezer_return_channel_residual` | 상품 채널과 원본 전체 delta 사이에 남은 채널 잔차 |
| `freezer_return_measurement:method=stable_endpoints` | 양쪽이 같은 안정 끝점 측정 기준으로 비교됨 |
| `freezer_return_vision_presence` | 무게 불일치 상태에서 실제 손 이탈·상품 잔류와 잔여 1개 조건으로 연결 |
| `freezer_vision_return` | 어느 출발 층의 어떤 SKU를 몇 개 차감했는지 |
| `freezer_mixed_resolved` | 원본 순변화 안의 서로 다른 취출·반납을 조건부 분해 |
| `freezer_session_return_assignment` | 세션 전체 증거로 모호한 반납 SKU를 좁힘 |
| `freezer_return_unresolved` | 무게·종류·이력·안정화 등의 이유로 반납을 차감하지 못함 |

notes는 기존 OPS CLOSE 출력에 포함된다. 모든 항목이 트리거 처리 직후 별도 INFO 한 줄로
출력된다는 뜻은 아니며, 문을 닫아 정산한 결과를 확인해야 하는 항목도 있다.
YAML/JSON에는 `action_details`, `verified_takes`, `vision_priority`,
`return_completed`와 선택적 손 `track_id`를 남긴다.
프레임별 bbox/손 ID의 저장은 기존 `MODEL__SESSION__SAVE_DETECTIONS=1`을 사용한다.

## 10. 11월 16일 저녁 ses-1·2 재검증 결과

| 세션 | 실제 동작과 기대 | 현재 저장 데이터 기반 결과 |
|---|---|---|
| ses-1-1794812134 | 2층 102·103, 4층 92 취출 후 92를 1층 반납. 기대 102+103 | 102+103+92. 96으로 바뀌던 문제는 제거했지만 92 반납은 미확정 |
| ses-2-1794812242 | 1층 92, 3층 94 취출 후 92를 3층 반납. 기대 94 | 94 1개 |

ses-1의 4층 원본 delta는 −368.75g이며 채널별로 −362.5g, −6.25g이다.
과거에는 무게가 비슷한 96 또는 작은 채널의 별도 상품 판정으로 결과가 오염됐다.
새 경로는 행동 후보를 보존하고 작은 채널을 제외해 정체성을 개선한다.
다만 약 370g 취출과 520g 반납의 차이를 기존 무게 기준만으로 연결할 수 없고,
옛 기록에는 실제 손 ID와 놓임 완료 증거가 없으므로 92까지 차감했다고 주장하지 않는다.

ses-2는 1층 −523.33g 상품 채널과 +6.25g 부채널이 합쳐져 약 −517.08g이었다.
상품 채널 이력을 보존하고, 3층의 취출 94와 반납 92를 함께 해석해
기대 최종 상품 94 한 개가 남는 것을 확인했다.

검증은 저장된 후보·방향 증거·delta·끝점 정보로 파이프라인 판단과 정산을 재실행한 것이다.
원본 AVI의 YOLO 재추론이나 실제 손 추적을 실행한 결과가 아니다.
당시 전체 OPEN 재고가 없어 fixture에는 재고 상한 12를 사용했고,
등록 무게는 사용자가 정정한 값을 적용했다. 없는 손 증거와 촬영 시각은 만들지 않았다.

## 11. 현재 등록 무게 기준

아래는 사용자가 마지막으로 제공한 등록 무게다. 실측 취출량과 같은 값이라는 뜻은 아니다.
실제 운영 판정은 전달된 상품 목록의 `unit_weight`를 사용하며, 이 표를 제품 코드에 하드코딩하지 않았다.
기존 회귀 자료에는 당시의 490g 핫도그, 525g 오리로스, 200g 닭발 등이 남아 있을 수 있다.

| class | 등록 무게(g) | class | 등록 무게(g) |
|---|---:|---|---:|
| 92 | 523 | 98 | 460 |
| 93 | 750 | 99 | 500 |
| 94 | 720 | 100 | 915 |
| 95 | 475 | 101 | 530 |
| 96 | 373 | 102 | 530 |
| 97 | 370 | 103 | 201 |

## 12. 남은 한계와 실제 장비에서 확인할 항목

- 같은 SKU 두 개를 동시에 꺼냈는데 영상에서 하나의 묶음으로만 검출되고 실측 하중도 불완전하면,
  현재 정보만으로 1개/2개를 확정할 수 없다. 손 ID·검출 박스·카메라 수를 수량으로 세지 않는다.
- 같은 층 왕복이나 다른 SKU 교환의 최종 delta가 0 또는 50g 미만이면 기존 전체 저무게 스킵이 남아 있다.
  그 영상이 추론되지 않는 경우를 이번 변경이 새로 해결한 것은 아니다.
- 101/102처럼 비전의 종류·방향 후보가 잘못된 경우는 별도 영상 검증이 필요하다.
- 과거 세션의 누락된 손 추적·완료 증거는 자동 복구하지 않는다. 저장 ses-1과 오후 오리로스는 미해결이다.
- Jetson의 설치된 Ultralytics 버전과 TensorRT 엔진으로 손 추적, 배치·패딩,
  side 손 가림, 추적 ID 유지, 놓임 후 관측 확보를 확인해야 한다.
  현재 로컬 추적 테스트는 공식 콜백 구조를 모사한 계약 검증이다.
- 같은 SKU가 여러 층에서 취출되면 총 잔여 수량과 물리 인스턴스의 원래 층을 구분해야 한다.
  같은 SKU 안의 동일 층 우선 규칙은 물리적 원위치 식별을 보장하지 않는다.
- `review`/`partial`은 자동 결제 차단을 뜻하지 않는다. 미확정 반납이 있으면 기존 상품이 남아 청구될 수 있다.

## 13. 변경 파일과 검증 상태

| 파일 | 역할 |
|---|---|
| [service/pipeline.py](crk_model/service/pipeline.py) | 작은 채널 제외, 행동 후보 우선, 검증 취출·손 증거 수집 |
| [perception/motion_evidence.py](crk_model/perception/motion_evidence.py) | 손·상품 연결, 놓임 완료, 영상 내 방향별 행동 후보 |
| [adapters/yolo_detector.py](crk_model/adapters/yolo_detector.py) | 선택적 `model.track()`, 손 ID, 영상별 추적 구간, 패딩 제외 |
| [ledger/freezer_returns.py](crk_model/ledger/freezer_returns.py) | 채널별 이력 보존과 제한적인 시각적 반납 연결 |
| [ledger/freezer_session.py](crk_model/ledger/freezer_session.py) | CLOSE의 복수 반납 공동 배정 |
| [ledger/freezer_movements.py](crk_model/ledger/freezer_movements.py) | 혼합 취출·반납에 검증된 채널 이력 사용 |
| [ledger/cross_zone.py](crk_model/ledger/cross_zone.py) | 무관한 교차존 재판정으로 행동 정체성이 사라지는 문제 방지 |
| [ledger/settler.py](crk_model/ledger/settler.py) | 새 연결 경로 적용, 이력 소비와 사유 기록 |
| [ledger/events.py](crk_model/ledger/events.py), [journal.py](crk_model/ledger/journal.py), [archive.py](crk_model/ledger/archive.py) | 신규 증거 저장 및 구 기록 호환 |
| [core/config.py](crk_model/core/config.py), [adapters/serve.py](crk_model/adapters/serve.py) | 추적 설정 로딩과 검출기 연결 |
| [freezer.env.example](freezer.env.example), [.env.example](.env.example), [adapters/README.md](crk_model/adapters/README.md) | 설정과 운영 설명 |

새 회귀는 `test_freezer_vision_priority.py`, `test_hand_tracker.py`,
`test_freezer_camera_views.py`와 저녁 세션 fixture에 추가했다.
작은 채널·PARTIAL 이력·상품 보존, 미래 취출/중복 반납 차단, 잘못된 SKU·수량·불안정 반납 거부,
세션 배정의 동점과 명확한 비전 보존, 카메라별 손 ID 분리, 패딩 제외,
실제 손 이탈·정지 확인, 시각 정렬 없이 영상 내 행동 구간의 관측을 보존하는 것을 검증했다.

무게 시각 기반 영상 후보 제한을 제외한 뒤의 검증 결과:

```text
.venv/bin/python -m pytest -q
740 passed, 25 skipped

변경 Python 파일 ruff 검사 통과
git diff --check 통과
```

이 기록은 로컬 코드와 저장 데이터 검증 결과다. 실제 기기 배포·실영상 추적은 수행하지 않았다.
이 절의 최초 구현은 커밋 `4390e7a`에 포함돼 있다.
이후 요청한 시각 정렬 기능 제거는 현재 작업 트리에 있으며 이번 작업에서 커밋·push하지 않았다.
실제 기기 `.env`, 등록 상품 DB, 원본 YAML 및 결제 이력도 수정하지 않았다.
이번 문서 보충은 링크·섹션 앵커·코드 블록과 `git diff --check`로 확인했다.

<a id="freezer-20261119"></a>

# 2026-11-19 냉동 6세션 오판정 분석과 CLOSE 정산 보완

## 작업 목적과 결과 요약

11월 19일 6세션의 실제 취출과 추론 결과를 비교해 원인을 로직·모델 인식으로 나눴다.
로직 문제 4가지는 CLOSE 정산에서 고쳤다. 모델 혼동은 로직으로 고칠 수 없어 기록만 남긴다.
새 env는 추가하지 않았고, 무게 처리 기본 경로와 트리거 판정은 바꾸지 않았다.

| 세션 | 실제 취출 | 기록된 결과 | 변경 후 (저장 데이터 재정산) | 원인 |
|---|---|---|---|---|
| ses-1-1795055066 | 4층 핫도그·4층 청양·3층 오리로스 | 3층 오리지널×1, 4층 오리지널×2 | 상품 동일, 3·4층 status `review` | 모델 혼동(101→102, 92→102) + refit |
| ses-2-1795055845 | 1층 대패오리·2층 너겟·5층 삼계탕 | 정답 | 동일 | — |
| ses-1-1795061395 | 1층 땡초교자·3층 육즙교자·2층 닭발 | 1층 육즙×1, 2층 육즙×1 | 1층 육즙×1, 3층 육즙×1, 2층 미청구(`review`) | 모델 혼동(96→97) + 교차존 강제 억제 + 손에 든 상품 재청구 |
| ses-2-1795062111 | 5층 용가리·1층 매운오리·2층 너겟 | 매운오리·너겟·닭발 | 동일 | 인식 부족(용가리 3표). 보류 항목 |
| ses-3-1795062865 | 4층 청양×2 | 청양×1 + 오리지널×1 | 청양×2 | 로직(무게가 같은 조합으로 교체) |
| ses-4-1795064208 | 5층 용가리×2 | 정답 | 동일 | — |

교자 세션은 청구 개수가 2개로 같다. 다만 3층 육즙교자는 실제 취출을 맞게 청구했고,
2층의 잘못된 육즙교자 청구는 빠졌다. 1층은 여전히 땡초교자를 육즙교자로 인식한 결과이고,
2층의 실제 닭발은 청구 근거가 없어 미청구다(과청구보다 미청구 원칙).

## 1. 무게가 같은 조합이 맞는 스냅을 바꾸던 문제 (ses-3 청양×2)

트리거 판정은 청양(101)×2 COMPLETE였다(−1055g, 1060g과의 잔차 5g).
CLOSE의 비전 조합이 청양+오리지널(530+530=1060g, 잔차 5g)을 찾아 결과를 바꿨다.
두 상품의 등록 무게가 같아 무게는 아무것도 가르지 못했고, 비전만으로 정체성을 바꾼 것이다.
기존 확신 스냅 보호(⑤)는 상대 후보 102의 conf도 1.0(≥0.92)이라 적용되지 않았다.

조합의 모든 상품 단위 무게가 스냅 상품과 count gate(15g) 안에 있고,
잔차 개선이 5g 미만이면 게이트를 통과한 스냅을 유지한다.

```text
freezer_combo_rejected_equal_weight:zone4:P102=1,P101=1:keep=P101=2
```

무게가 조합을 실제로 더 잘 설명하는 경우는 기존대로 조합을 쓴다.
예: ses-8의 76+75(100/95g)는 잔차가 5g→0g으로 줄어 조합 유지.
이 세션의 결과 status는 `review`다. 101/102 conf가 동률이라 기존 `ambiguous_runner_up_complete` 규칙이 적용된다.

## 2. 손에 든 상품 때문에 진짜 취출이 지워지던 문제 (교자 세션 3층)

교차존의 session42 강제 억제는 "소스 외에 같은 클래스를 유일 후보로 든 피해 zone이 2곳 이상이면 순수 유출"로 본다.
교자 세션에서는 97을 유일 후보로 든 zone이 2층·3층이라 3층의 실제 육즙교자(−335g)가 지워졌다.

2층의 97은 FOV 유출이 아니라 1층에서 꺼낸 상품을 손에 든 채 2층을 연 것이었다.

| zone | 전체 후보 97 | head_votes | first_pos_ratio |
|---|---|---:|---:|
| 1층 (소스) | 185표 | 0 | 0.54 |
| 3층 (실제 취출) | 229표 | 0 | 0.27 |
| 2층 (손에 든 상품) | 196표 | 52 | 0.0 |

피해 zone을 셀 때 carried-in 지문 zone(전체 후보 `head_votes ≥ 5`)은 제외한다.
문턱 5는 `MotionEvidence.held_min_head` 기본값과 같은 값이고, env를 추가하지 않았다.
그러면 3층은 2-zone 규칙(soft 페널티 후에도 이기면 인정)으로 돌아가 97 partial이 유지된다.
`head_votes`가 기록되기 전 세션(값 0)은 기존 session42 동작과 같다.

## 3. 손에 든 상품이 다른 층에 다시 청구되던 문제 (교자 세션 2층)

2층 −205g에 97(370g)이 identity partial로 청구됐다. 실제는 닭발(200g)이었지만,
닭발은 7표로 share 기준에서 탈락해 판정 후보에 없었다.

`freezer_session.suppress_carried_over_partials()`를 CLOSE에 추가했다
(2026-11-25 11-18 되돌림으로 `freezer_session.py`가 없어져 `freezer_carried_over.py`로 옮겼다. 내용은 같다).
교차존 보정 뒤, 혼합 취출·반납 해석 전에 실행한다.
아래 세 조건이 모두 맞을 때만 PARTIAL 판정에서 해당 클래스를 뺀다.

1. 더 앞선 이벤트가 같은 클래스를 이미 청구했다 (손에 든 상품의 출처).
2. 이 이벤트의 전체 비전 후보가 carried-in 지문(`head_votes ≥ 5`)을 가진다.
3. 어떤 개수 n으로도 무게가 근접 밴드 2×gate_n(n) 안에 들지 않는다.
   gate_n(n) = 15g + 5g×(n−1).
   교자 세션 예시: n=1 잔차 165g > 30g, n=2 잔차 535g > 40g.

뺀 자리에 다른 상품을 채우지 않는다. 남은 무게는 설명되지 않은 취출일 수 있으므로,
해당 zone의 결제 status를 `review`로 올린다.

```text
zone2:freezer_carried_over_suppressed:P97:delta=-205.0g:trigger_ts=1795061404.454
```

무게가 그 상품과 맞거나(같은 상품을 하나 더 꺼냈을 수 있음), `head_votes`가 0이거나,
앞선 청구가 없으면 아무것도 바꾸지 않는다. COMPLETE 판정은 대상이 아니다.

## 4. refit으로 바뀐 정체성을 complete로 내보내던 문제 (ses-1-1795055066)

`freezer_vision_first_unique_refit`과 `refit_arbitrated`는 비전 1위 후보를 무게가 맞는 다른 상품으로 바꾼 COMPLETE다.
이 세션에서는 101→102 교체가 3번 있었고, 무게 차이는 0g이었다.
청구는 그대로 두고 해당 zone의 결제 status를 `review`로 올린다.

```text
zone4:refit_identity_replaced:trigger_ts=1795055068.669
```

청구 상품은 여전히 오리지널이다. 모델 혼동 자체는 이 변경으로 고쳐지지 않는다.

## 5. 보류: 무게가 맞지 않고 반납 증거가 더 많은 후보 미청구

ses-2-1795062111 5층은 닭발(103)이 청구됐다. 이 판정에는 다음 문제가 있다.
- 실제 취출은 용가리다.
- 닭발은 반납 방향 13표, 취출 방향 5표였다.
- 무게도 어떤 개수와도 맞지 않았다.

"이런 후보는 청구하지 않는다"는 규칙은 로드셀 무게가 안정될 때까지 보류하기로 했다(사용자 결정).
현재는 5층 닭발 partial 청구가 그대로 남는다.
- 2026-11-25: 로드셀 무게를 안정화된 것으로 판단해 보류 조건은 풀렸지만, 이 규칙은 구현하지 않기로 했다(사용자 결정).

## 6. 로직으로 해결하지 않은 원인

- **모델 혼동 쌍**: 101↔102, 96↔97, 92↔102. 무게 차이가 7g 이하라 무게로 거부할 수 없다.
  학습 데이터 보강이 필요하다. 이번 변경은 이 경우를 `review`로 드러내는 데까지만 한다.
- **ses-1-1795055066 4층 ch0 −535g**: 핫도그 등록 무게 475g과 60g 차이가 난다.
  아직 설명이 안 돼 원시 로드셀 기록 확인이 필요하다. 고정 보정은 적용하지 않았다.
- **인식 부족**: ses-2-1795062111 용가리 3표, 교자 세션 2층 닭발 7표(share 탈락).
- 11-19 YAML의 닭발 등록 무게는 200g이다(11-18 절의 표는 201g).

## 7. 판단 흐름에서 바뀐 위치

```text
CLOSE: ghost 강등
  → 교차존 보정 (피해 zone 집계에서 carried-in zone 제외)        ← 2
  → carried-over partial 제거                                   ← 3 (신규)
  → 혼합 취출·반납 해석 → 복수 반납 공동 배정 → 반납 차감
  → freezer 재solve (무게가 같은 조합 교체 거부)                  ← 1
  → zone status (refit·carried-over 제거 → review)             ← 4, 3
```

(2026-11-25: "복수 반납 공동 배정"은 11-18 되돌림으로 빠졌다. 현재 흐름은 [11-25 절](#freezer-20261125) 참고.)

| 기록 | 의미 |
|---|---|
| `freezer_combo_rejected_equal_weight` | 무게가 같은 조합으로 스냅을 바꾸지 않고 유지 |
| `freezer_carried_over_suppressed` | 손에 든 상품의 무게 불일치 재청구를 제거하고 zone을 review로 표시 |
| `refit_identity_replaced` | refit이 비전 1위와 다른 상품으로 바꾼 COMPLETE. zone을 review로 표시 |

## 8. 검증

저장된 YAML 트리거의 판정은 그대로 두고 CLOSE 정산만 다시 실행했다.
YOLO 재추론이나 실영상 처리는 하지 않았다.
- 정산 설정: `Settings` 기본값, 모든 zone FREEZER 프로파일.
- 재고: YAML에 없어 490개로 가정했다.
- 재정산 도구 확인: 같은 도구를 HEAD(`8433acf`) 코드로 돌리면 11-19 6세션 모두 기록된 청구 결과와 같다.

| 범위 | 변경 전후 차이 |
|---|---|
| 11-19 6세션 | 위 요약 표의 3세션만 변경 (ses-3 청양×2, 교자 세션 2·3층, ses-1-1795055066 status) |
| 11-12~11-16 YAML 11세션 | 청구 상품 변경 없음. ses-1-1794812134 4층에 refit note만 추가(status는 원래 review). ses-2-1794789461 4층 −17.5g에 붙어 있던 오리지널 partial 제거로 status partial→review |
| ses-1-1794471430 | 구 행동별 정산(`freezer_actions`) 기록이라 비교에서 제외 |

새 회귀 테스트:
- `tests/test_ledger.py`
  - `test_equal_weight_combo_keeps_gate_passed_snap`
  - `TestFreezerCarriedOver`: 제거 1건, 조건 하나라도 빠지면 유지 3건
  - `TestRefitReviewStatus`: unique_refit·refit_arbitrated는 review, single은 complete
- `tests/test_cross_zone.py`
  - `test_carried_in_zone_is_not_counted_as_bleed_victim`: head 52면 3층 유지, head 0이면 기존 session42 억제

변경 전 코드에서는 위 테스트 중 변경을 검증하는 5건이 실패하고 대조 케이스 5건은 통과한다.

```text
.venv/bin/python -m pytest -q
750 passed, 25 skipped

변경 Python 파일 ruff: tests/test_cross_zone.py:1029 F841 1건 (HEAD부터 있던 기존 경고, 이번 변경과 무관)
git diff --check 통과
```

## 9. 변경 파일

| 파일 | 역할 |
|---|---|
| [ledger/settler.py](crk_model/ledger/settler.py) | 무게가 같은 조합 거부, carried-over 패스 연결, refit·carried-over zone의 review status |
| [ledger/cross_zone.py](crk_model/ledger/cross_zone.py) | `CARRIED_IN_MIN_HEAD`, `_carried_in_zones`, session42 피해 zone 집계 제외 |
| `ledger/freezer_session.py` (11-25부터 [ledger/freezer_carried_over.py](crk_model/ledger/freezer_carried_over.py)) | `suppress_carried_over_partials` |
| [tests/test_ledger.py](tests/test_ledger.py), [tests/test_cross_zone.py](tests/test_cross_zone.py) | 11-19 세션 값 기반 회귀 |

이 절의 변경은 작업 트리에만 있고 커밋·push하지 않았다.
실제 기기 `.env`, 등록 상품 DB, 원본 YAML, `README.md`는 수정하지 않았다.

<a id="refactor-20261125"></a>

# 2026-11-25 코드 경량화: CLOSE 정산 정리와 꺼진 경로 제거

11-19 절 이후 전체 코드 경량화 검토 결과 중 아래 항목을 반영했다.
청구 결과는 바꾸지 않는 정리가 목적이다. 동작이 바뀌는 곳은 5절에 따로 적었다.

| 구분 | 내용 | 청구 영향 |
|---|---|---|
| A | cross_zone·ghost_ledger·settler 중복 정리 | 없음 (18세션 재정산 동일) |
| C-1 | zone notes를 ambiguous/refit note 기록 뒤에 모음 | 없음 (`ZoneBasket.notes`만 보강) |
| C-3 | render CLI 마무리 인코드 실패 격리 | 없음 (분석 도구) |
| B-1 | 옛 냉동 행동별 정산(`freezer_actions`) 제거 | 신규 트리거 없음. 11-12 기록 재처리만 영향 |
| B-2 | 조기 종료(early termination) 제거 | 없음 (env 3종 모두 0이던 기능) |

## 1. A — CLOSE 정산 중복 정리

| 위치 | 변경 |
|---|---|
| `cross_zone._rejudge` | 후보만 바꾼 재판정 호출 5곳(cross_zone 4, ghost_ledger 1)을 하나로 |
| `cross_zone._strip_classes` | "오염 클래스 제거, 남는 상품 없으면 NO_DETECTION" 4곳을 하나로 |
| `_credible_trial_replacement` | `_trial_replacement_valid`를 흡수. 지문 중복 억제는 신뢰도 하한 0, 동률 판별·잔차 패스는 `source_conf_min` |
| 지문 중복 억제 | 같은 이벤트를 두 번 재판정하던 것을 한 번 계산해 재사용 |
| `_tie_break_by_replaceability` | 실행되지 않던 첫 번째 정의 삭제 |
| `_mutual_exemptions`, `_repass_event` | 유일 호출자가 항상 넘기던 인자의 기본값·`or {}` 폴백 삭제 |
| `ghost_ledger.weight_backed_zones` | ghost `_weight_backed_classes`와 settler `_backed_zones_by_class`(같은 게이트)를 하나로 |
| settler 콤보·단일 종 교체 | 후보 수집(`_zone_evidence`)·자격 제외 ①②(`_exclude_session_explained`)·실존 하한 ③(`_exclude_low_evidence`) 공용화, 콤보 note 문자열 `_fmt_combo` |

재판정 캐시는 CLOSE 라우터가 정산마다 새로 만들어지고 telemetry·miss_log를 읽는 곳이 없어 결과에 영향이 없다.

## 2. C-1 — zone notes 누락

`_notes_for_zone`이 `ambiguous_runner_up_complete`·`refit_identity_replaced` 기록보다 먼저 실행돼,
두 note가 세션 notes에는 남지만 zone notes(`ZoneBasket.notes`)에는 빠졌다. 기록 뒤로 옮겼다.
18세션 재정산에서 바뀐 것은 4세션의 zone notes뿐이고, 청구·status·세션 notes는 같다.

## 3. C-3 — render CLI 실패 격리

`sink.close()`(마무리 인코드)가 `finally` 안에 있어 ffmpeg 실패 예외가 `except OSError` 밖으로 나가
남은 카메라·트리거 렌더가 모두 중단됐다. close를 try 안으로 옮기고, `OSError`·인코드 대기 timeout은
그 카메라만 건너뛴다. 예상 밖 예외는 sink를 정리한 뒤 그대로 올린다.

## 4. B — 꺼진 경로 제거

**옛 행동별 정산 (`freezer_actions`)**: 11-12에 생성 코드가 들어갔다가 같은 날 제거됐고, 이후에는
옛 저널·아카이브 재생에만 쓰였다. `FreezerAction` 타입, `TriggerEvent.freezer_actions`, 저널·아카이브
직렬화, settler의 행동별 SKU 차감·보호 경로, freezer_session·freezer_movements의 예외 처리를 지웠다.
옛 저널·YAML에 `freezer_actions` 키가 있어도 읽기는 되며 키만 무시한다.

**조기 종료**: 이슈 #22(0805 냉장 20종 실기) 이후 기본 off였고 env 예시 3종 모두 0이었다.
`perception/early_termination.py`, `Settings.early_termination_enabled`, env `MODEL__VISION__EARLY_TERMINATION`,
`SensorProfile.early_termination_allowed`, `TriggerTrace.early_terminated`(아카이브 `trace.early_terminated`),
조기 종료 입력으로만 쓰이던 `HandLatch.frames_since_exit`를 지웠다. 기기 `.env`에 이 키가 남아 있어도 무시된다.

## 5. 동작이 바뀌는 곳

- **ses-1-1794471430 (11-12, `freezer_actions` 기록)**을 다시 정산하면 zone2(−370g)·zone3(−470g)가
  `review` → `complete`(청구 없음)로 바뀐다. 두 트리거 모두 상품 없는 PARTIAL 판정이라 청구는 원래도 0이다.
  행동별 경로는 이를 `freezer_action_unresolved`로 review에 올렸지만, 일반 경로의 zone status는
  상품이 있는 PARTIAL만 본다.
  - 참고: 신규 트리거에서도 "무게는 빠졌는데 상품 없는 PARTIAL"이면 zone이 `complete`(청구 없음)로 나갔다.
    같은 날 [11-25 무게 안정화 절](#freezer-20261125)에서 review로 바꿨다. 이 세션의 2·3층도 다시 review다.
- 조기 종료는 기본 off라 신규 트리거 동작 변화는 없다. 켜 두었던 기기라면 이제 항상 전 프레임을 추론한다.

## 6. 검증

저장 YAML 18세션(11-12~11-19)의 트리거 판정을 그대로 두고 CLOSE 정산만 다시 실행해, 오늘 작업 전
작업 트리 복사본과 비교했다. YOLO 재추론·실영상 처리는 하지 않았다.

| 단계 | 결과 |
|---|---|
| A 후 | 18세션 출력 전체(청구·status·세션/zone notes) 동일 |
| C-1 후 | zone notes 4세션 보강 외 동일 |
| B 후 | C-1 후와 동일 (재정산 도구는 원래 `freezer_actions`를 넣지 않음). 넣어서 비교한 ses-1-1794471430은 5절 |

```text
.venv/bin/python -m pytest -q
726 passed, 25 skipped

ruff (crk_model, tests): 기존 경고 2건만 (gateway/state_machine.py:365 E501, tests/test_cross_zone.py:1029 F841 — HEAD부터 있던 것)
```

테스트 수 750 → 726: 옛 행동별 경로 13건, 조기 종료 13건 삭제, render 실패 격리 2건 추가.
- `tests/test_render_cli_failures.py` (신규, ffmpeg·numpy 없이 실행): 마무리 인코드 실패 시 그 카메라만 건너뜀 —
  수정 전 코드에서 실패한다. 예상 밖 예외는 sink 정리 후 전파.
- `tests/test_t2_batch.py::test_pipeline_prefetch_closes_unconsumed_side_on_error`: 조기 종료 대신 top 디코드 실패로
  side 프리페처가 닫히는지 확인하도록 바꿨다.
- `tests/test_session_archive.py::test_actual_freezer_save_is_readable_by_standard_safe_loader`: `freezer_movement`
  튜플로 표준 YAML 저장을 확인하도록 바꿨다. 옛 `!!python/tuple` 아카이브 읽기 테스트는 그대로 둔다.

## 7. 변경 파일

| 파일 | 변경 |
|---|---|
| [ledger/cross_zone.py](crk_model/ledger/cross_zone.py) | A (−93줄) |
| [ledger/settler.py](crk_model/ledger/settler.py) | A, C-1, B-1 (−108줄) |
| [ledger/ghost_ledger.py](crk_model/ledger/ghost_ledger.py) | A, `weight_backed_zones` |
| [ledger/events.py](crk_model/ledger/events.py), [journal.py](crk_model/ledger/journal.py), [archive.py](crk_model/ledger/archive.py), [freezer_session.py](crk_model/ledger/freezer_session.py), [freezer_movements.py](crk_model/ledger/freezer_movements.py) | B-1 (archive는 B-2 trace 키 포함) |
| [service/pipeline.py](crk_model/service/pipeline.py), [model_service.py](crk_model/service/model_service.py), [worker.py](crk_model/service/worker.py), [core/config.py](crk_model/core/config.py), [core/profiles.py](crk_model/core/profiles.py), [perception/\_\_init\_\_.py](crk_model/perception/__init__.py), [frames/motion_gate.py](crk_model/frames/motion_gate.py), `perception/early_termination.py`(삭제) | B-2 |
| [adapters/render_cli.py](crk_model/adapters/render_cli.py) | C-3 |
| `.env.example`, `refrg.env.example`, `freezer.env.example` | `MODEL__VISION__EARLY_TERMINATION` 삭제 |
| 테스트 7개 파일 + 신규 1개 | 위 6절 |

`crk_model` 본체는 오늘 작업 전보다 424줄 줄었다.

이 절의 수치(테스트 726건, 줄 수)는 같은 날 이어진 11-18 되돌림 이전 기준이다.
`freezer_session.py`는 되돌림으로 삭제됐다. 최신 수치는 [11-25 무게 안정화 절](#freezer-20261125)에 있다.

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다. 실제 기기 `.env`, `README.md`는 수정하지 않았다.

<a id="freezer-20261125"></a>

# 2026-11-25 로드셀 무게 안정화: 11-18 비전 우선 설계 되돌림과 같은 층 반납 보완

## 1. 판단

로드셀 무게를 안정화된 것으로 판단했다(사용자 결정). 무게 신뢰가 낮다는 전제로 만든
11-18 커밋 두 개(`4390e7a` 비전 기반 추론 설계, `8433acf` 시계열 후보 제한 제거)를 코드에서 되돌려
11-16(`7bb4f25`) 기준으로 맞췄다. 11-19 절 5번은 보류 조건이 풀렸지만 구현하지 않기로 했다(사용자 결정).

## 2. 되돌린 내용

| 11-18 기능 | 되돌린 뒤 |
|---|---|
| 손 ID 추적 (`MODEL__VISION__TRACKER`=bytetrack, `Detection.track_id`, 클립별 추적 초기화, 행동 상세 `action_details`) | 없음. 기기 `.env`에 `MODEL__VISION__TRACKER`가 남아 있어도 무시된다 |
| 방향 영상 후보 우선(`vision_priority`)·행동 정체성 보존(`freezer_action_identity_preserved`) | 트리거 판정은 전체 영상 후보 + 기존 segment 재시도 |
| 무게가 안 맞는 반납의 비전 연결(`matched_vision_presence`, `return_completed`) | 없음. 반납은 앞선 취출 실측 delta와 ±15g으로 검증 |
| 채널 검증 취출(`verified_takes`)·작은 부채널 독립 판정 제외 | 취출 이력은 판정 상품 기준(`record_take`) |
| 세션 전체 반납 공동 배정(`freezer_session.resolve_session_returns`)·행동 완료 시각 정렬 | 반납은 시간순으로 하나씩 매칭 |
| 교차존 보정의 `vision_priority` 보호 | 없음 |

되돌리는 방법: 커밋은 건드리지 않고, 파일마다 (작업 트리, HEAD, `7bb4f25`) 3-way 병합으로 두 커밋의
변경만 뺐다. 커밋하지 않은 11-19·11-25 변경이 닿지 않은 파일(`yolo_detector.py`, `serve.py`,
`detector.py`, `motion_evidence.py`, `test_freezer_camera_views.py`)은 `7bb4f25`와 같다.
`freezer_session.py`, `test_freezer_vision_priority.py`, `test_hand_tracker.py`,
`fixtures/freezer_cross_floor_20261116_evening.json`은 삭제했다.

유지한 것:
- 11-19 항목 1~4. 항목 3(carried-over 억제)은 `freezer_session.py` 삭제로 `freezer_carried_over.py`로 옮겼다.
  무게가 맞지 않음을 근거로 청구를 빼는 규칙이라 무게 신뢰 전제와 맞다.
- 11-25 경량화 A·B·C.

## 3. 같은 층 반납 보완

비전이 반납 SKU를 정하지 못한 경우에만 같은 층 취출 이력과 실측 무게로 보완한다.

| 경우 | 처리 | note |
|---|---|---|
| 반납 정체성 없음·모호(`missing_identity`·`ambiguous_identity`), 같은 층 잔여 취출 중 실측 무게가 맞는 SKU가 하나 | 그 SKU 차감 | `freezer_return_same_floor_weight:vision=…` |
| 위가 안 되고, 같은 층 미청구 취출과 반납 무게가 ±15g(count gate) 안 | 서로 상쇄. 청구 변화 없음, review 아님 | `freezer_unbilled_take_returned:take=…:return=…` |
| 영상 증거 없는 무게 반납 | 같은 층 청구 상품 매칭 → 같은 층 미청구 취출 상쇄 → 다른 층 순 | 위와 같음 |

확정하지 않는 경우(기존처럼 미해결·검토):
- 무게가 맞는 SKU가 둘 이상(같은 무게 SKU)이거나 개수가 갈림
- 영상에 다른 후보만 보임
- 비전이 다른 SKU를 뚜렷하게 본 `no_prior_take`(같은 무게라도 무게로는 정체성을 못 가른다)
- 비전 SKU의 무게 불일치(`weight_mismatch`), 반납 무게 미안정(`not_stabilized`)
- 다른 층으로 간 정체성 없는 반납

## 4. 청구 없이 빠진 무게 → review

냉동 층에 50g(`min_trigger_delta_grams`) 이상 빠졌는데 청구 상품이 없는 판정이 있으면 확인한다.
상품 없는 PARTIAL과 NO_DETECTION(`forced_final_no_match`, 교차존·carried-over 억제 결과 등)을 모두 포함한다.
반납·층간 이동을 반영한 층 순변화가 청구 무게보다 50g 이상 더 빠졌으면 그 층을 `review`로 올린다
(`freezer_unbilled_weight_drop:unexplained=Xg`). 50g 미만이거나 같은 층 반납으로 상쇄되면 무시한다.
냉장은 이번 규칙을 적용하지 않았다.

## 5. 검증

저장 YAML 18세션(11-12~11-19)의 트리거 판정을 그대로 두고 CLOSE 정산만 다시 실행했다.

| 단계 | 결과 |
|---|---|
| 되돌린 직후 | 되돌리기 전과 청구·status 동일. notes 차이 3줄(ses-2-1795055845 5층에 교차존 재판정 시도 기록 추가, 결과는 원 판정 유지) |
| 새 규칙 후 | 청구 동일. status 3곳 `complete`→`review`: ses-1-1794471430 2층(370g)·3층(470g) 미청구, ses-5-1794795995 3층(320g — 교차존 보정이 닭발 청구를 1층 유출로 지운 층). 교자 세션 2층(205g)은 원래 review, note만 추가 |

한계: 저장 YAML의 트리거 판정은 기록 당시 파이프라인이 만든 것이다. 특히 11-19 6세션은 11-18 비전 우선
파이프라인의 판정이라, 되돌린 트리거 판정 로직(방향 후보 우선, 정체성 보존, 부채널 제외)의 효과는
이 재정산에 반영되지 않는다. 트리거 판정까지 보려면 실기 영상 재추론이 필요하다.

```text
.venv/bin/python -m pytest -q
706 passed, 25 skipped (731 collected)

ruff (crk_model, tests): 기존 경고 2건만 (gateway/state_machine.py:365 E501, tests/test_cross_zone.py:1029 F841)
```

테스트: 11-18 전용 테스트 삭제로 726 → 694. 새 규칙 12건 추가로 706.
기대값이 바뀐 기존 테스트 3건은 같은 층 반납이 이제 차감되는 경우다.
- `test_freezer_return_matching.py`: 정체성 없음·모호 같은 층 반납
- `test_freezer_returns.py`: 영상 정체성 없는 같은 층 반납, 카메라 정체성 충돌

같은 경우의 다른 층 반납은 계속 미해결로 남는지 테스트를 따로 추가했다.

## 6. 변경 파일

| 파일 | 변경 |
|---|---|
| `crk_model` 전반(11-18 변경 파일) | 되돌림 — 2절 |
| [ledger/freezer_returns.py](crk_model/ledger/freezer_returns.py) | `match_same_floor_return`, `UnbilledTake`, `cancel_unbilled_take` |
| [ledger/settler.py](crk_model/ledger/settler.py) | 같은 층 반납 연결, `_unbilled_weight_drop` review |
| [ledger/freezer_carried_over.py](crk_model/ledger/freezer_carried_over.py) | 11-19 항목 3 이동 |
| `.env.example`, `freezer.env.example` | `MODEL__VISION__TRACKER` 삭제(되돌림) |
| [tests/test_freezer_return_matching.py](tests/test_freezer_return_matching.py), [tests/test_freezer_returns.py](tests/test_freezer_returns.py) | 5절 |
| `docs/03`, `docs/05`, `crk_model/ledger/README.md`, `tests/README.md` | 반납 규칙·note·테스트 수 |

`crk_model` Python 코드: HEAD 13,960줄, `7bb4f25` 13,486줄, 현재 13,398줄.

## 7. 남은 것

- 11-19 5번(무게가 어떤 개수로도 안 맞고 반납 방향 표가 취출보다 많은 후보 미청구): 구현하지 않는다(사용자 결정). ses-2-1795062111 5층 닭발 partial 청구는 그대로 남는다.
- 루트 `README.md`의 조기 종료 언급 2줄(120·137행)은 사용자 수정이 있어 손대지 않았다.

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다. 되돌리기 전 작업 트리는 세션 스크래치패드에 백업했다.

<a id="dedupe-20261125"></a>

# 2026-11-25 중복 코드 정리 (경량화 검토 A의 나머지)

[11-25 코드 경량화](#refactor-20261125)에서 cross_zone·settler만 하고 남긴 A 항목(동작 변화 없는 중복)을 모두 정리했다.
같은 날 [무게 안정화 되돌림](#freezer-20261125) 이후의 코드가 기준이다.

| 영역 | 정리한 중복 | 줄 수 |
|---|---|---:|
| `core/config.py` | 설정 87개의 기본값이 필드와 `from_env()`에 두 번 적혀 있던 것을 필드 하나로. `from_env`는 (필드 → env 키, 파서) 표로 읽는다 | 526 → 482 |
| `perception/voting.py` | `combine`·`debug_summary`가 같은 게이트를 각자 계산하고 클래스별 유효표를 4번씩 다시 세던 것을 `_evaluate()` 하나로 | 412 → 405 |
| `perception/motion_evidence.py`, `ingest/bocpd.py`, `adapters/yolo_detector.py` | 트랙·튜브 매칭 루프, 임계 산식, 구간→세그먼트 변환, 허용 클래스 정규화, predict 호출 반복 | 975 → 962 (세 파일 합) |
| `service/pipeline.py` | 트레이 이벤트 재판정·재시도 3곳 반복을 `_judge_event`·`_complete_retry`로 | 975 → 970 |
| `ledger/archive.py` | 상품·후보 dict와 json/yaml 쓰기 반복 | 465 → 449 |
| `judgment/strategies.py` | 세그먼트 매칭 병합, 냉동 분리 취출 게이트, 표 순위 정렬 5곳, 개수 맞춤 3곳, 아무도 넘기지 않던 생성자 인자 5개(→ 클래스 상수, 값 동일) | 1327 → 1335 |
| `adapters/analyze_cli.py`·`render_cli.py`·`label_cli.py`·`http_app.py` | 세 CLI의 세션 지정·조회 흐름을 `adapters/_archive_cli.py`(신규)로, 효과 없는 코드 | 1996 → 2040 (신규 51줄 포함) |

`journal.py`는 helper를 만들면 오히려 길어져 그대로 뒀다. `crk_model` Python 코드 합계는 13,398 → 13,365줄이다.
strategies·CLI는 줄 수가 늘었지만 같은 로직·메시지가 한 곳에만 있게 됐다.

## 검증

정리 전 작업 트리 복사본과 새 코드를 별도 프로세스에서 같은 입력으로 돌려 출력을 바이트 단위로 비교했다.
모두 동일했다. 영역별 최종 합본에서도 다시 돌렸다.

| 영역 | 비교 범위 |
|---|---|
| config | env 7,746가지(빈 값, 예시 env 3종, 키별 65개 값, 무작위 조합) — 결과·예외 메시지 |
| perception | voting·motion 2만 시드 × 해시 시드 2, BOCPD 2만, YOLO 5천(가짜 모듈) |
| pipeline·저장 | `TriggerPipeline.process` 2,500건, 재시도 3,500건, 저널·아카이브 직렬화·YAML 저장 |
| judgment | JudgmentContext 5.6만 × 라우터 설정 8종, 약 160만 줄 + telemetry·miss_log |
| CLI·http_app | 세 CLI 인자 조합(출력·종료 코드·생성 파일), http_app TestClient |

```text
.venv/bin/python -m pytest -q
706 passed, 25 skipped
저장 YAML 18세션 재정산: 정리 전과 출력 전체 동일
ruff: 기존 경고 2건만
```

실행하지 못한 것: 실제 ultralytics/TensorRT, ffmpeg·numpy 렌더링(이 PC에 없음). 해당 코드는 바꾸지 않았다.

## 정리 중 발견한 기존 문제 (수정하지 않음)

- `judgment/strategies.py` `_strong_repeated_candidate`: 상품 조회에 `class_id > 0` 필터가 없어 손(0)·미매핑(-1) 상품이 대안으로 뽑힐 수 있다.
- `analyze_cli`: unit 잔차 폴백에서 `break`가 안쪽 루프만 빠져, 같은 클래스가 여러 층에 있으면 마지막 층 값이 앞 값을 덮는다.
  `--session X`의 파일이 매핑이 아닌 JSON/YAML이면 TypeError로 종료한다.
- `config`: `MODEL__SESSION__ERROR_POLICY`만 빈 값·대소문자·공백을 기본값으로 넘기지 않고 오류를 낸다.
  bool env는 오타(`ture`)를 조용히 False로 읽는다. 숫자 env의 공백 값은 오류다.

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.

<a id="freezer-20261126"></a>

# 2026-11-26 냉동 반납 보완: 11-25 실기 세션 검토

## 1. 배경

11-25 실기 6세션(기기 커밋 `7bb4f25`)에서 반납을 제대로 처리하지 못한 경우가 대부분이었다.
같은 날 기기에서 맞았던 ses-4-1795593217·ses-7-1795593955·ses-1-1795594411 YAML도 받아 함께 검토했다.

| 세션 | 정답 | 기기 청구 | 지금 (저장 데이터 재정산) |
|---|---|---|---|
| ses-3-1795592912 | 95 | 102 | **95** |
| ses-5-1795593345 | 94+92 | 94+93 | 94+93 (4층 92를 영상이 93으로 봄) |
| ses-6-1795593508 | 102 | 102+98 | **102** |
| ses-8-1795594034 | 93+98 | 94+98 | 94+98 (1층 93을 94로 봄 — 2개 취출·1개 반납으로 정산) |
| ses-2-1795594482 | 102+97+98 | 95+95+98 | 95+98, 1층 review — 1층 영상이 없는 저장 데이터 한계. 영상이 돌면 102+98 (4절) |
| ses-3-1795594851 | 97+101+98 | 98+97+97+98 | **97+102+98** (101을 102로 봄) |
| ses-4-1795593217 | 102 | 94+102 | **102** (1층 94를 2층에 반납 — 세션 총 −523g = 상품 1개. 사용자 확인) |
| ses-7-1795593955 | 99+102 | 99+102 | 99+102 (유지) |
| ses-1-1795594411 | 97+103+100 | 97+103+100 | 97+103+100 (유지 — 5층 닭발 중복을 교차존 대신 ③이 트리거 단계에서 뺌) |

## 2. 사용자 결정

- 반납 매칭 허용오차 25g. env가 아닌 코드 전역값 `core/profiles.FREEZER_RETURN_TOLERANCE_GRAMS`.
  취출 판정 허용오차(`tolerance_grams` 15g)는 그대로 — 25g 이상에서는 무게가 맞는 후보가 늘어
  공유 카메라에 오염된 영상 1위가 이긴다(ses-2-1795055845 94→93, ses-2-1794789461 102→99 등).
- 상품 크기 기준(이전 검토의 185g = 최소 상품 − 15g)을 **가장 가벼운 등록 상품의 절반**(현재 100g)으로.
  상품이 아닌 채널 변화는 최대 75g, 상품 채널은 최소 362g이고, 80~185g 어디든 24세션 결과가 같다(60g은 ses-3-1795594851 깨짐).
- E2(모호한 반납도 세션 무게로), D2(상쇄 움직임이면 영상 추론) 반영. F(무게로 불가능한 partial 제거)는 취출이 사라져 제외.
- 비슷한 상품(93/94, 101/102, 92/93, 96/97)은 모델 문제로 둔다. 다만 취출 자체는 청구에 나타나야 한다.
- 11-19 5번은 계속 구현하지 않는다.

## 3. 변경

정산 입력 복사본만 고친다(아카이브·보고용 `weight_delta`는 원본). 모두 냉동 프로파일에만 적용.

| 번호 | 내용 | 위치 | note | 해결 세션 |
|---|---|---|---|---|
| ① | 한 트리거 안 상품 크기 채널 하나 감소·하나 증가 → 취출·반납 두 이벤트 | `freezer_close_checks.split_tray_movements` | `freezer_tray_split` | ses-3-1795592912 4층 |
| ② | 단일 SKU 취출 이력을 상품 채널 실측으로 기록(여러 개면 등록 무게, 다른 상품이 섞여 절반 넘게 무거우면 등록 무게) | `freezer_returns.take_measurement` | — | ses-2-1795594482 2층 (−467.5 → −475g) |
| ③ | 멀티 트레이에서 50g 미만 채널은 판정하지 않음(11-18 `4390e7a` 복원) | `pipeline._judge_tray_events` | `freezer_small_channel:chN` (reason code) | ⑥과 함께여야 함 |
| ④ | 순변화 50g 미만인데 상품 크기 하강·상승이 모두 있던 무청구 층 → review | `freezer_close_checks.masked_movement_zones` | `freezer_masked_movement` | ses-2-1795594482 1층 |
| ⑤+E2 | 비전 반납 SKU가 취출 이력에 없거나(`no_prior_take`) 모호하면(`ambiguous_identity`) 모든 층 취출 중 무게가 맞는 유일한 SKU로 차감 — 그 SKU가 영상에 3표 이상, 여러 SKU 조합으로도 설명되면 거부, 반납·출발 층 review | `freezer_returns.match_session_weight_return` | `freezer_return_session_weight` | ses-6 4층, ses-8 3층, ses-3-1795594851 1층, ses-4 2층 |
| ⑥ | 다른 층 무게 검증 COMPLETE와 같은 지문인 무게 불일치 PARTIAL 재청구 제거 | `freezer_close_checks.suppress_partial_fingerprint_duplicates` | `freezer_fingerprint_partial_duplicate` | ses-3-1795594851 1층 98 |
| ⑦ | 반납 직후 같은 층 무청구 작은 감소(상품 크기 미만)를 반납 무게에 합산 | `freezer_close_checks.fold_contact_release` | `freezer_return_contact_release` | ses-3-1795594851 1층 (+786.7 −65 = +721.7g) |
| ⑧ | 정체성만 보존한 count=1 PARTIAL이 층의 유일한 청구면 층 최종 무게로 개수 복원(n≥2, 오차 ≤ n×25g, 재고 이하, PARTIAL 유지) | `freezer_close_checks.recover_partial_count` | `freezer_partial_count_from_weight` | ses-8 1층 (−1473.3g → 94×2) |
| D2 | 저무게 스킵 대상이어도 상품 크기 하강·상승이 모두 있으면 영상 추론. 트리거 판정은 무청구, 작은 순변화를 반납 증거로 만들지 않음 | `pipeline._freezer_masked_movement` | `freezer_masked_movement` (reason code·judgment reason) | ses-2-1795594482 1층 |
| 혼합 | 혼합 분해: 취출 이력 ② 적용, 반납 SKU가 이력에 없거나 반납 방향이 없으면 ⑤와 같은 조건으로 찾기, 반납 실측 − 순변화로 구한 취출 무게는 취출 방향 1위 SKU로 tolerance×2·재고 안이면 인정 | `freezer_movements._split_mixed`, `_derived_take` | `freezer_mixed_resolved` | ses-2-1795594482 1층 |
| 25g | 냉동 반납 매칭 전부(정산·혼합 분해·같은 층 보완·세션 무게)에 25g | `profiles.return_tolerance` | — | ses-3-1794808045, ses-6-1794796052, ses-4 |

정산 순서: 교차존·carried-over → ⑥ → ① → 혼합 분해 → ⑧ → ⑦ → 반납 매칭(②·⑤·25g) → ④ → net/교차존/freezer resolve.

## 4. 검증

저장 YAML 27세션(11-12~11-25, ses-4·ses-7·ses-1 포함)을 CLOSE만 다시 정산했다. 트리거 단계의 ③은 저장 후보로
멀티 트레이 판정을 근사 재판정해 적용했다(영상 재추론 아님). 시제품 결과와 repo 결과가 25세션 모두 같다(ses-7·ses-1은 repo로만 확인).

HEAD 대비 청구가 바뀐 세션:

| 세션 | HEAD | 지금 | 정답 대비 |
|---|---|---|---|
| ses-3-1795592912 | 102 | 95 | 정답 |
| ses-6-1795593508 | 102+98 | 102 | 정답 |
| ses-3-1795594851 | 98+97+97+98 | 97+102+98 | 101→102만 다름(비슷한 상품) |
| ses-4-1795593217 | 94+102 | 102 | 정답 |
| ses-3-1794808045 | 92+99+100 | 92+99 | 삼계탕 반납 연결(912.5 대 취출 890g). 92는 11-16부터 남은 문제(반납 하중 부족, 정답은 99만) |
| ses-6-1794796052 | 95×2 | 95 | 정답 |
| ses-2-1794812242 | 92+92 | 94 | 11-16 기대값 복귀 |

ses-8은 청구가 같다(94+98). HEAD는 1개 청구 + 반납 미해결이었고, 지금은 2개 취출 − 3층 반납 1개다.
ses-8을 포함한 나머지 20세션은 청구가 같고 status·notes만 바뀐다(review 추가 등).

ses-2-1795594482 1층은 기기가 영상을 건너뛰어 후보가 없다. D2로 영상이 돌았다고 가정한 6가지
(반납을 99로 봄 / 95로 혼동 / 반납 방향 없음 × 트레이 1·2개)에서 모두 102+98이 된다(3층 97은 아래 6절).
같은 상품을 꺼냈다 넣은 왕복(−10g)은 D2로 영상이 돌아도 청구하지 않고 review만 남는다.

```text
.venv/bin/python -m pytest -q
748 passed, 25 skipped (773 collected)
ruff (crk_model): 기존 경고 1건만 (gateway/state_machine.py:365 E501)
```

## 5. 기대값이 바뀐 기존 테스트 (14건)

| 원인 | 테스트 | 변경 |
|---|---|---|
| E2 | `test_freezer_return_matching.py::test_other_floor_return_without_vision_identity_stays_unresolved[ambiguous]` | 모호 반납은 세션 무게 차감 + review로 분리(`test_other_floor_ambiguous_return_matches_unique_session_weight_for_review`) |
| E2 | `test_freezer_returns.py::test_camera_identity_conflict_on_other_floor_keeps_prior_take_for_review` | 차감 + 두 층 review |
| D2 | `test_freezer_returns.py::test_overlapping_segments_keep_existing_zero_weight_skip` | 영상 추론은 돌고 트리거 무청구. 상품 크기 미만 상쇄는 기존 스킵 유지 테스트 추가 |
| 25g | `test_measured_return_tolerance_boundary` | 15.1g 거부 → 25.1g 거부 |
| 25g | `test_catalog_weight_alone_cannot_override_disagreeing_measured_delta` | 반납 490 → 491g (실측과 26g 차) |
| 25g | `test_freezer_return_measurements.py` 5건 | 끝점 경계 905.1 → 915.1g, 끝점이 필요한 경우를 총 925g·채널 920g으로, 11-16 오후 재현 테스트는 25g에서 삼계탕이 연결됨으로 |
| 25g | `test_freezer_session_returns.py` 4건 | 채널 잔차 24.2g → 30g(25g 밖) |

새 테스트: `tests/test_freezer_sessions_20261125.py` 39건(`fixtures/freezer_sessions_20261125.json` 9세션 포함).

## 6. 남은 것

- **ses-2-1795594482 3층 97**: 영상에 97·96 검출은 있었지만(top 튜브 진단에 97이 24·23회) 신뢰도가 투표 진입 기준 미만이라
  후보가 없다. 무게 365g은 96(373)·97(370) 모두 맞아 무게로 정할 수 없다 → 미청구 + review. 모델 문제.
- 비슷한 상품: ses-5 92→93, ses-8 93→94, ses-3-1795594851 101→102. 취출은 청구에 나타난다.
- ses-2-1795594482 1층 D2 결과는 가정값 검증이다. 실기에서 같은 동작(같은 층 취출 + 다른 상품 반납)을 다시 확인해야 한다.
- 25g의 대가: 25g 안쪽 상품(95 475 / 98 460 / 99 500, 92 523 / 101·102 530)은 반납 무게로 더 구분되지 않는다.
  영상이 정체성을 못 정하면 ⑤의 조합 가드로 차감하지 않고 review로 남는다.

## 7. 변경 파일

| 파일 | 변경 |
|---|---|
| [core/profiles.py](crk_model/core/profiles.py) | `FREEZER_RETURN_TOLERANCE_GRAMS`, `return_tolerance`, `product_size_floor` |
| [ledger/freezer_close_checks.py](crk_model/ledger/freezer_close_checks.py) | 신규 — ①④⑥⑦⑧ |
| [ledger/freezer_returns.py](crk_model/ledger/freezer_returns.py) | ② `take_measurement`·`record_take_measured`, ⑤ `match_session_weight_return` |
| [ledger/freezer_movements.py](crk_model/ledger/freezer_movements.py) | 혼합 분해 보완 |
| [ledger/settler.py](crk_model/ledger/settler.py) | 정산 순서 연결, 25g, ⑤, review 조건 |
| [service/pipeline.py](crk_model/service/pipeline.py) | D2, ③ |
| 테스트 | 5절, 신규 `test_freezer_sessions_20261125.py`·fixture |
| `docs/03`, `docs/05`, `crk_model/ledger/README.md`, `tests/README.md` | 규칙·note·테스트 수 |

`crk_model` Python 코드: 13,365 → 13,948줄. 새 env는 없다.

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.

<a id="zeroset-20261126"></a>

# 2026-11-26 Edge 로드셀 영점 확인 (Edge_Environment)

대상 저장소: `/Users/yoona/Desktop/Edge_Environment` (브랜치 `HG/freeze`). 이 저장소(CRK-model-HG) 코드는 바꾸지 않았다.

## 1. 배경

영점(`POST /calibrate`, IO board `MC LZ`)이 제대로 적용되지 않아 채널에 이전 무게가 남는 경우가 있다.
4층(트레이 2개)에서 왼쪽 500g을 꺼냈는데 오른쪽에 이전 세션의 −600g이 남아 최종 −1100g이 나왔다.

모델은 트리거 안의 시작·끝 무게 차이만 쓰므로 남은 값이 그대로 머물면 영향이 없다. −1100g이 나왔다는 것은
남은 −600g이 세션 도중 움직였다는 뜻이다. 유력한 경로는 음수 영역 펌웨어 문제다(값이 음수로 남으면 펌웨어가
양수 쪽으로 되돌리려 해 값이 오락가락함, CRK-IO-BOARD `docs/FIRMWARE_ZERO_TRACKING_REQUEST.md`).
해당 세션 YAML의 4층 채널 시작·끝 값으로 확정할 수 있다. 저장 27세션에서는 모든 층의 첫 트리거가
0±30g에서 시작해 영점이 정상이었다.

## 2. 영점 시점 검토

변경 전 흐름(`Payments.js`): 문 닫힘(t0) → CLOSE → IO 기록 중지 + 5초 → 카메라 종료(5초 대기 후 stop, 남은 트리거 전송)
→ 영점(t0+10~14초) → 추론 결과 → 결제 승인. 모델 확정은 CLOSE 후 6~12초(ses-7 6초, ses-4 8초, ses-1 12초).

| 후보 | 판단 |
|---|---|
| 카메라 종료 직후 (현재) | 유지. 트리거가 모두 전송돼 그 세션 정산에 영향이 없고, 영점 시간이 추론·결제 대기에 흡수된다 |
| 추론·결제 뒤 | 추론은 IO board를 쓰지 않아 정확도 이득이 없고, 다음 손님과 겹칠 가능성만 커진다 |
| 세션 시작 때마다 | 모든 손님이 3~5초 늦게 문을 연다. 사용자 선택으로 넣지 않음 |

변경 전 구멍: 영점 결과를 다시 읽어 확인하지 않음, 실패해도 마지막 성공 후 25분까지 재시도 없음,
카메라 종료·기록 중지 실패에도 영점 실행, 1분 점검의 문 상태 조회 중 세션이 시작되는 경합.

## 3. 사용자 결정

- 영점(약 4초) 직후 추가 대기 없이 확인. 모든 채널 ±15g 안이면 성공(처음 ±10g에서 조정).
- 실패하면 재영점은 1번만 → 최대 약 8초.
- 확인은 세션 종료(문 닫힘 → CLOSE → 카메라 종료) 후 한 번. 세션 시작 확인은 넣지 않음.
- IO board가 아예 응답하지 않으면 로드셀 상태 `20`(장비 이상)으로 보고.

## 4. 변경

| 항목 | 내용 |
|---|---|
| 확인 | 영점 응답 직후 `GET /loadcells` → 10채널 모두 ±15g 안이면 성공. 성공 여부는 `/calibrate` 응답이 아니라 다시 읽은 값으로 판단(응답 500이어도 값이 0이면 성공). `EEEEEE`/`VVVVVV` 채널은 확인에서 제외 |
| 재영점 | 실패 시 1번만. 그래도 실패면 다음 1분 점검에서 즉시 재시도(25분 대기 없음). 25분 타이머는 "확인된 영점"만 리셋 |
| 장비 이상 | 영점 전 판독·영점 요청·확인 판독 중 HTTP 응답 자체가 없으면(timeout·연결 끊김) 재영점 없이 멈추고 `hasDeviceError()` = true → `HealthMqtt.LoadcellStatusAPI`가 `20`. Health 보고(IF_02, 안내 표시·센서 오류 음성), 결제 시작·상품 수집 시작 점검에 반영. IO board가 다시 응답하면 해제. 응답은 오는데 값이 남는 실패는 `20` 아님. 무응답 시 최대 약 10초(영점 요청 제한 시간) |
| 생략 | IO 기록 중지나 카메라 종료가 실패하면 세션 직후 영점을 건너뛰고 1분 점검에서 재시도(`Payments.js`가 성공 여부 전달) |
| 경합 | 1분 점검에서 문 상태 조회 뒤 결제·수집 세션 시작 여부를 다시 확인 |
| 로그 | 영점 전 10채널 값, 확인 결과(`result=residual over_15g=ch7=-600g` 등), 무응답 단계 |

## 5. 문 흔들림 검토 (CRK-model-HG 추론 로직, 변경 없음)

카메라(CRK-CAMERA)는 채널 변화가 5g만 넘어도 트리거를 보내므로 문 흔들림도 트리거가 된다. 모델이 거르는 단계:
1. 트리거마다 시작·끝 안정 구간 차이만 사용 — 흔들렸다 돌아오면 0
2. 채널 변화 5g 미만은 변화 없음, 냉동은 트리거 순변화 50g 미만이면 영상 추론 없이 건너뜀(`low_weight_skip`)
3. 무게가 빠진 채널의 끝 구간이 10g 넘게 흔들리면 그 트리거 무게를 쓰지 않음(`final_delta_unstable`)
4. 트레이 2개 층의 50g 미만 채널은 상품으로 판정하지 않음(11-26 ③)
5. 정산: 50g 미만 빠진 무게는 review도 올리지 않고 작은 증가는 note만
6. D2(11-26): 100g 이상 하강·상승이 둘 다 있을 때만 영상 추론, 트리거 단계 무청구

저장 27세션: 무게가 작아 건너뛴 트리거 44건 모두 35g 이하, 흔들림으로 무게 판정을 포기한 트리거 0건.
흔들림 보정은 50g 미만이거나 왔다 돌아오는 변화만 거르므로, 남은 값이 600g 움직이는 경우는 막지 못한다 — 영점 확인이 필요한 이유.

## 6. 검증

가짜 IO board(영점 응답 4초)로 `LoadcellZeroset.js` 실제 코드를 16가지 경우로 실행, 모두 통과:
정상(4초), −600g 잔여 후 재영점 성공(8초), 두 번 다 실패(8초), 실패 후 1분 점검 재시도, 성공 직후 주기 점검 대기,
±15g 경계(15g 성공·20g 실패), 영점 응답 500이지만 값 0, 에러 채널 제외, 카메라 종료 실패 시 생략,
문 조회 중 세션 시작 시 생략, 유휴 상태 재시도, 영점 요청 무응답 → `20`, IO board 복구 → `29`,
영점 전 판독 무응답 → 영점 요청 없이 `20`, 잔여값 실패는 `20` 아님. `node --check` 통과.
실제 장비에서는 실행하지 않았다.

## 7. 남은 것

- 실기 확인: 영점 로그(`[ZEROSET]`)로 잔여값이 얼마나 자주·크게 생기는지 본다.
- 빈 트레이가 펌웨어 문제로 계속 ±15g 밖에서 흔들리면 유휴 상태에서 1분마다 영점을 반복할 수 있다 — 로그에 `calibrate failed after 2 attempts`가 이어지면 재시도 간격 조정 검토.
- 세션 직후 무응답이면 IO board가 다시 응답할 때까지(1분 점검) 결제가 막힌다.
- 기존 문제(수정 안 함): `HealthMqtt.DeadboltStatusAPI`가 `/health` 실패 시 이전 응답(`IOBoardRes`)을 지우지 않아 `LoadcellStatusAPI`가 오래된 `29`를 볼 수 있다(무응답이면 deadbolt `10`으로 결제는 막힘).
- −1100g 세션 YAML을 받으면 원인 확정.

## 8. 변경 파일 (Edge_Environment)

| 파일 | 변경 |
|---|---|
| `server/routes/RestAPI/LoadcellZeroset.js` | 확인·재영점 1회·즉시 재시도·장비 이상·경합 방지·로그 |
| `server/routes/Mqtt/HealthMqtt.js` | `LoadcellStatusAPI` 맨 앞 장비 이상 확인(`20`) |
| `server/routes/RestAPI/Payments.js` | 기록 중지·카메라 종료 성공 여부를 `fireAfterSession`에 전달(결제 흐름 변경 없음) |
| `README.md` | 결제 흐름 9단계에 영점 추가, "로드셀 영점(zeroset)" 절 |

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.

<a id="freezer-carried-in-20261126"></a>

# 2026-11-26 들고 온 상품 규칙: 11-26 실기 반납 층 오청구

상품 번호는 1~12(구 92~103 −91)로 쓴다.

## 1. 배경

| 세션 | 동작 | 정답 | 기기 청구 |
|---|---|---|---|
| ses-1-1795659665 | 3층 10 취출, 2층 8 취출 + 10 반납 | 8 | 11 (3층) |
| ses-1-1795661722 | 2층 11 취출, 5층 7 취출 + 11 반납 | 7 | 11 (2층) |

ses-1-1795659665 YAML은 새 번호(11)로, ses-1-1795661722 YAML은 구 번호(102·98·103)로 기록돼 있다 — 기기 모델·설정 확인 필요.

## 2. 원인

top·side 카메라를 모든 층이 공유해, 다른 층에서 꺼내 들고 온 상품이 반납 층 클립에 **취출 방향**으로도 잡힌다.
ses-1-1795659665 2층 혼합 분해에서 반납(+525g)은 세션 무게로 3층 11(실측 530g)에 맞았지만, 남은 취출 505g을
취출 방향 1위인 11로 판정했고, 반납과 같은 SKU라 "같은 SKU 반복은 분해하지 않는다" 가드에 걸려 분해 자체를 버렸다.
그래서 3층 11 청구가 남았다. 2층 8은 영상 2표뿐이라 후보가 없다(모델 문제).

## 3. 변경

`freezer_movements._split_mixed`: 취출 방향 후보(3표 이상)가 모두 반납 SKU이고, 그 반납이 모두 **다른 층** 취출 이력을
돌려놓은 것이면 들고 온 상품으로 본다. 반납은 연결하고, 이 층 취출은 영상 근거가 없으므로 무청구
(judgment reason `freezer_mixed_take:carried_in_unidentified`, 후보 비움). 남은 무게는 기존
`freezer_unbilled_weight_drop`으로 review가 된다. 같은 층 취출 이력의 같은 SKU는 지금처럼 분해하지 않는다.
취출 방향에 다른 SKU도 보이면 기존 분해 그대로다. 새 env·상수 없음.

## 4. 검증

저장 YAML 29세션(ses-1-1795661722 추가) CLOSE 재정산(영상 재추론 아님). HEAD 대비 바뀐 세션은 ses-1-1795659665 하나다.

| 세션 | HEAD | 지금 | 정답 대비 |
|---|---|---|---|
| ses-1-1795659665 | 11 (3층) | 청구 없음, 2층 review(미청구 505g) | 11 오청구 제거. 8은 영상 근거가 없어 review |
| ses-1-1795661722 | 11 (2층), 5층 review | 같음 | 아래 5절 |

```text
.venv/bin/python -m pytest -q
753 passed, 25 skipped (778 collected)
```

새 테스트: `tests/test_freezer_sessions_20261126.py` 5건(`fixtures/freezer_sessions_20261126.json` 2세션, 새 번호.
ses-1-1795661722는 class_id만 −91 변환). 규칙을 빼면 ses-1-1795659665 재생·들고 온 상품 테스트 2건이 실패한다.

## 5. 남은 것

- **ses-1-1795661722 5층 반납 무게 부족**: 마지막 하강(−520g) 뒤 상승이 +457.5g으로, 2층 11 실측 525g보다 67.5g 적다
  (반납 허용오차 25g 초과) → 반납 미확정이라 들고 온 상품 규칙까지 가지 않는다. 세션 총 −511.7g도 7(460g) 하나만
  빠진 것보다 50g 넘게 많아, 반납한 11이 저울에 다 실리지 않았을(벽·칸막이·다른 상품에 기댐) 가능성이 있다 — 영상 확인 필요.
  무게만 보면 "5층에서 꺼낸 것과 넣은 것이 거의 같은 무게"라 11 청구가 일관된다. 허용오차를 늘리면 4(475)·7(460)·8(500)이
  반납 무게로 구분되지 않아 바꾸지 않았다. 반납 무게가 맞았다면 결과는 청구 없음 + 5층 review다.
- 반납 층에서 꺼낸 상품의 검출 부족(8 2표, 7 3표): 후보가 없어 청구할 수 없고 review로 남는다. 모델 문제.

## 6. 변경 파일

| 파일 | 변경 |
|---|---|
| [ledger/freezer_movements.py](crk_model/ledger/freezer_movements.py) | `_split_mixed` 들고 온 상품 규칙 |
| `tests/test_freezer_sessions_20261126.py`, `tests/fixtures/freezer_sessions_20261126.json` | 신규 |
| `docs/03-judgment-and-settlement.md`, `crk_model/ledger/README.md`, `tests/README.md` | 규칙·테스트 수 |

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.

<a id="freezer-tray-spanning-20261126"></a>

# 2026-11-26 두 트레이에 걸친 상품: ses-1-1795668469

상품 번호는 1~12(구 92~103 −91)로 쓴다. YAML은 구 번호(102·99)로 기록돼 있다.

## 1. 배경

| 세션 | 동작 | 정답 | 기기 청구 |
|---|---|---|---|
| ses-1-1795668469 | 5층 11 취출, 2층 8 취출 + 11 반납 | 8 | 없음 (2·5층 review) |

## 2. 원인

5층(트레이 2개)의 11이 두 트레이에 걸쳐 놓여 있었다. 채널별로 보면 다음과 같다.

| 채널 | 움직임 | 끝값 |
|---|---|---|
| ch0 | +37.5 → −448.3 (+32.5 → −416) | −410.8 |
| ch1 | +50 → −35 → −70 | −55 |

- 꺼내는 순간 두 채널이 함께 빠졌고 합은 −518.3g으로 11(530g)과 맞는다.
- 시작→끝 합은 −465.8g이다. 놓여 있던 동안 상품 일부(약 60g)가 로드셀 밖(트레이 사이 프레임·벽 등)에 걸쳐 있었던 것으로 보인다.

판정 결과는 다음과 같았다.
1. 트레이별 판정: ch0 −410, ch1 −55 모두 무게가 맞는 상품이 없어 둘 다 11 PARTIAL이었다.
2. 같은 정체성의 PARTIAL이 겹치면 대칭 오염으로 보고 모두 빼는 가드 때문에 청구가 없었다.
3. 5층 취출 이력이 없으니 2층 혼합 분해(−500 뒤 +525)가 반납을 연결하지 못해, 2층 8도 청구되지 않았다.

사용자 제안대로 층 전체로 보되, **시작→끝 합(−465.8g)으로 판정하면 4(핫도그 475g, 3표)로 잘못 바뀐다**.
그래서 두 트레이가 같은 순간 함께 빠진 하강 합을 쓴다.

## 3. 변경

| 위치 | 내용 |
|---|---|
| `freezer_returns.spanning_drop` | 두 채널 이상의 하강이 같은 시간 창에 겹치면 그 합(상품 크기 100g 이상) 중 가장 큰 것. 한 채널의 세그먼트는 겹치지 않으므로 겹치는 하강은 다른 채널이다 |
| `pipeline.tray_spanning_judgment` | 냉동 멀티 트레이 판정이 COMPLETE가 아니고, 판정한 트레이가 **모두 같은 한 상품 PARTIAL**이며, 함께 빠진 하강이 있으면 그 합으로 1회 재판정한다. **같은 상품 1개 COMPLETE일 때만** 채택하고, 아니면 기존 판정을 유지한다. reason `multi_tray_spanning[...]:drop=…g`, reason code `freezer_tray_spanning` |
| `freezer_returns.take_measurement` | 취출 이력 무게 측정값 후보에 함께 빠진 하강 합을 추가(등록 무게와 개수 게이트 안일 때만 선택). 11 이력이 518.3g으로 기록돼 2층 반납 525g과 맞는다 |
| `freezer_movements._split_mixed` | 혼합 분해가 세션 무게로 반납 SKU를 정했으면 그 SKU만 정산의 반납 증거로 넘긴다. 정산 시점에는 분해가 만든 같은 층 취출 이력(8, 500g — 반납 525g과 정확히 25g)도 후보로 겹쳐 세션 무게 반납이 거부됐다 |
| `settler` | 함께 빠진 무게로 판정한 층은 note `freezer_tray_spanning:trigger_ts=…`과 함께 review(시작→끝 순변화와 다름) |

보고용 층 무게(`weight_delta`)는 원본(−467.5g) 그대로다. 새 env·상수는 없다.

## 4. 검증

저장 YAML 30세션 CLOSE 재정산(영상 재추론 아님)이다. 트리거 단계 규칙은 기기 코드에 없었으므로, 멀티 트레이 미확정 트리거를
저장 후보로 채널별 재판정한 뒤 같은 함수(`tray_spanning_judgment`)로 근사 적용했다. HEAD 대비 바뀐 세션은 하나다.

| 세션 | HEAD | 지금 |
|---|---|---|
| ses-1-1795668469 | 청구 없음, 2·5층 review | **8**, 2·5층 review (5층 11은 2층 반납으로 취소) |

다른 멀티 트레이 트리거에서 함께 빠진 하강이 있는 경우는 이 세션 5층뿐이다.
ses-8-1795594034 4층(+50/−780)과 ses-7-1795593955 1층(+45/+261.7)은 부호가 달라 해당하지 않는다.

```text
.venv/bin/python -m pytest -q
758 passed, 25 skipped (783 collected)
```

- **새 테스트:** `tests/test_freezer_sessions_20261126.py` +5건. fixture에 이 세션을 추가했다(5층 트리거는 위 근사 재판정, `trigger_rejudged_tray_spanning_zones`).
- **실제 파이프라인 확인:** BOCPD 채널 분석에서도 두 채널 구간이 같은 시간 창으로 나와 규칙이 동작하는 것을 확인했다. 하강 시점이 다르면 합치지 않는다.
- **규칙을 빼고 확인:** 트리거 규칙을 끄면 파이프라인 테스트가, 정한 SKU 전달을 끄면 세션 재생 테스트가 실패한다.

## 5. 남은 것

- **5층에서 11 무게가 계속 60~70g 덜 잡힌다.** 두 세션 모두 11이 두 트레이에 걸쳐 놓였다.

  | 세션 | 동작 | 측정 | 11 무게 대비 |
  |---|---|---|---|
  | ses-1-1795661722 | 반납 | +427.5/+30 = 457.5g | −67.5g |
  | 이번 세션 | 시작→끝 | −465.8g | −64.2g |

  트레이 사이나 벽에 상품이 걸치지 않게 5층 진열 위치·트레이 간격을 확인해야 한다.
- **ses-1-1795661722는 그대로다.** 반납 때 함께 오른 합도 457.5g이라 이 규칙으로 맞지 않는다(반납 허용오차 25g 초과).
- **트레이 정체성이 다르면 적용하지 않는다.** 한 상품이 두 트레이에 걸쳤는데 트레이별 정체성이 다르게 나오거나, 함께 빠진 합으로도 맞지 않으면 지금처럼 review로 남는다.

## 6. 변경 파일

| 파일 | 변경 |
|---|---|
| [service/pipeline.py](crk_model/service/pipeline.py) | `tray_spanning_judgment`, `_judge_tray_events`에서 호출 |
| [ledger/freezer_returns.py](crk_model/ledger/freezer_returns.py) | `spanning_drop`, `take_measurement`·`record_take_measured`에 `segments` |
| [ledger/freezer_movements.py](crk_model/ledger/freezer_movements.py) | 취출 이력에 `segments` 전달, 세션 무게로 정한 반납 SKU만 넘김 |
| [ledger/settler.py](crk_model/ledger/settler.py) | 취출 이력에 `segments` 전달, `freezer_tray_spanning` review |
| `tests/test_freezer_sessions_20261126.py`, fixture | 5건 추가, 세션 추가 |
| `docs/03-judgment-and-settlement.md`, `crk_model/ledger/README.md`, `tests/README.md` | 규칙·테스트 수 |

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.

<a id="freezer-carried-take-20261126"></a>

# 2026-11-26 들고 온 상품 후보 제외: ses-1-1795670038

상품 번호는 1~12(구 92~103 −91)로 쓴다. YAML은 구 번호로 기록돼 있다.

## 1. 배경

| 세션 | 동작 | 정답 | 기기 청구 |
|---|---|---|---|
| ses-1-1795670038 | 2층 11 취출, 5층 7 취출 + 11 반납 | 7 | 11 (2층) |

ses-1-1795661722와 같은 동작이다. 이번에는 반납 무게가 맞았는데도 실패했다.

## 2. 원인

- **5층 무게:** −515 뒤 +210, +320이 올라왔다. 반납 +530g은 2층 11 이력(525g)과 맞았다.
- **5층 취출 판정:** 취출 무게는 530 − 44.2 = 485.8g이다. 그런데 취출 방향 후보에 들고 온 11이 1위(11표)로 들어 있었다. 7은 5표였다.
  - 공유 카메라라 2층에서 들고 오는 모습이 5층 클립에 취출 방향으로 잡혔다.
  - 라우터는 7 PARTIAL(25.8g 차)을 냈다.
  - 무게 기반 보완(`_derived_take`)은 취출 방향 1위인 11(530g)로 맞춰 보다가 44g이 어긋나 실패했다.
- **결과:** 분해 전체가 버려지고 2층 11 청구가 남았다.

들고 온 상품 규칙(#freezer-carried-in-20261126)은 "취출 후보가 들고 온 SKU뿐"인 경우만 다뤘다.
들고 온 상품과 실제로 꺼낸 상품이 같이 보이는, 더 흔한 경우를 놓쳤다.

## 3. 변경 (`freezer_movements._split_mixed`)

| 내용 | 이유 |
|---|---|
| 다른 층 취출을 돌려놓은 반납 SKU는 이 층 취출 후보에서 빼고 판정한다. 남는 후보가 없으면 기존 들고 온 상품 규칙(무청구 review) | 이번 세션 |
| 반납 구간이 마지막 하강 뒤 상승이 아니면, 첫 하강 앞 상승(반납 → 취출)도 본다. 단 반납 방향 SKU의 앞선 취출 이력과 무게가 직접 맞을 때만(세션 무게 보완 없음) | 들고 온 상품을 먼저 놓고 꺼내는 순서. 손으로 누른 하중(+165 뒤 −690 같은)과 구분 |
| 반납 방향 후보가 없을 때의 세션 무게 분해를 순변화 50g 미만으로 한정하지 않는다(상품 크기 하강·상승은 그대로 필요) | 7 취출 + 11 반납은 순변화가 +60g 안팎이라 D2 조건을 넘는다 |

## 4. 검증

저장 YAML 31세션 CLOSE 재정산(영상 재추론 아님, 트리거 단계 두 트레이 규칙은 저장 후보로 근사)이다. 바뀐 세션은 다음과 같다.

| 세션 | 이전 | 지금 |
|---|---|---|
| ses-1-1795670038 | 11 (2층) | **7**, 2층 review |
| ses-2-1794812242 (1층 1 취출 → 3층 3 취출 + 1 반납, 정답 3) | 3, 1·3층 review | 3, 1층 review — 들고 온 1이 3층 후보에서 빠져 "2위 후보와 비슷함" review가 사라짐 |

나머지 29세션은 청구·review·note가 같다.

같은 동작의 변형 7가지는 가상 이벤트로 고정했다. 모두 2층 11 취출 뒤 5층 이벤트를 두었다.

| 변형 | 결과 |
|---|---|
| 취출 → 반납 | 7 |
| 반납 → 취출 | 7 |
| 반납 방향 없음 +30g | 7 |
| 반납 방향 없음 +60g | 7 |
| 실제 취출만 보임 | 7 |
| 7 미검출 | 청구 없음 + 5층 review |
| 누름 + 취출 (반납 아님) | 11 유지 |

수정 전 코드로 돌리면 이 세션 재생과 변형 4가지가 실패한다.

```text
.venv/bin/python -m pytest -q
766 passed, 25 skipped (791 collected)
```

## 5. 남은 것

- **ses-1-1795661722:** 5층 반납 무게가 457.5g으로 11(525g)보다 67.5g 적어 연결되지 않는다(반납 허용오차 25g 초과). 5층 진열 위치 확인이 필요하다(#freezer-tray-spanning-20261126 5절).
- **이 층에서 꺼낸 상품이 영상에 3표 미만이면** 청구할 수 없고 review로 남는다(ses-1-1795659665 2층 8).
- **반납 → 취출 순서에서 반납 앞에 누름이 섞이면** 반납 구간 무게가 커져 맞지 않는다(예: +30 +525 −480). 이때는 기존처럼 review로 남는다.

## 6. 변경 파일

| 파일 | 변경 |
|---|---|
| [ledger/freezer_movements.py](crk_model/ledger/freezer_movements.py) | 들고 온 SKU 후보 제외, 반납 → 취출 순서, 반납 방향 없는 분해 조건 |
| `tests/test_freezer_sessions_20261126.py`, fixture | 변형 7가지, 세션 추가 |
| `docs/03-judgment-and-settlement.md`, `crk_model/ledger/README.md`, `tests/README.md` | 규칙·테스트 수 |

이 절의 변경도 작업 트리에만 있고 커밋·push하지 않았다.
