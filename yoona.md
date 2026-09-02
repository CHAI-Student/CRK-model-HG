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
