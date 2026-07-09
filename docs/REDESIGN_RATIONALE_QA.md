# REDESIGN_RATIONALE_QA

# 왜 이렇게 복잡한가 — 설계 의도 규명 Q&A (재작성 판단 자료)

> 목적: 현 로직의 복잡한 지점마다 **① 코드/문서/히스토리 근거 → ② 근거가 없으면 설계 의도 추론 → ③ 더 나은 해법**을 기록.
각 항목에 근거 등급 표기: **[확인]** = 코드·문서·피드백으로 입증 / **[추론]** = 정황 기반 추정 (검증 필요).
> 
> 
> 기준: 커밋 `d104bca` · 히스토리는 2026-06-16 스쿼시(`2bb6cba`) 이후 37개 커밋 전수 확인 · 실측 트레이스 3건 분석.
> 

---

## 핵심 결론 요약

1. **복잡도의 정체는 “설계”가 아니라 “현장 실패 대응의 누적(accretion)”** — v4.x 체인지로그, 2026-06-29 운영 피드백, 스쿼시 이후 커밋 37개 전부가 개별 실패 케이스 패치임. [확인]
2. **freezer 경로가 따로 존재하는 근본 원인은 센서 물리학** — 냉동고 로드셀 오차 5~15g vs 냉장고 ±3g. 무게가 “정체성 판별자” 자격을 잃어 vision-first로 역전됨. [확인]
3. **속도 병목은 판단 엔진이 아니라 YOLO 호출 횟수** — 실측: 트리거 처리 18.4s 중 YOLO 16.2s(88%), 판단엔진은 924케이스 일괄 42ms. 판단엔진 재작성은 **유지보수성** 문제이지 성능 문제가 아님. [확인]

---

## Q1. 왜 freezer는 vision-first이고 냉장고는 weight-first인가?

**[확인]** `docs/llm-wiki/source-docs/freezer-weight-feedback-2026-06-29.md`:
- 냉동고 로드셀 오차는 보통 ~5g, 최대 **10~15g** (냉장고 ±3g 대비 5배).
- 실패 사례: 178g 하나 꺼냈는데 후보 1·2·3위를 **모두 합쳐서** 청구 — 170g 근접 단일 후보가 리스트에 있었음에도.
- 결론: freezer에서 무게는 정체성 판별자가 못 되고, **개수 검증용 거친 게이트(±15g, `MODEL__WEIGHT__FREEZER_WEIGHT_TOLERANCE_GRAMS=15.0`)**로만 사용.

**더 나은 해법**: 현재는 freezer/일반이 코드 포크(judge 1순위 분기 + 별도 close resolver + 별도 필터 세트)로 갈라져 10.4k줄의 주범이 됨.
→ **“센서 신뢰도 프로파일” 추상화**: `SensorProfile{tolerance, weight_is_discriminative: bool, count_gate_only: bool}`을 주입하면 매칭 코어는 하나로 통합 가능. freezer는 프로파일 값만 다른 동일 파이프라인이 됨. 코드 포크 → 파라미터 포크.

---

## Q2. 왜 `judge()`에 10개 이상의 순차 폴백이 있는가?

**[확인]** 각 분기의 도입 시점이 추적됨:
- `detected_single_item_fallback` ← 커밋 `3a5c306` “strict 미스지만 단일 감지 품목이 무게 허용치에 맞는 경우 구제”
- `stage_count_combination` / `same_product_count` ← freezer repeat-count 실패 커밋군 (`07518f7`, `d86e879`, `b923e16`)
- `same_weight_candidate_collision_guard` ← 동일무게 후보 충돌 시 정규 후보 우선 (178g 사건 계열)
- v4.8 changelog: `has_loadcell` vision-only 분기, stock 상한 재적용 — 모두 개별 결함 대응.

**[추론]** 즉, 폴백 체인은 “이 순서가 최적”이어서가 아니라 **추가된 순서·심각도 순**. 순서 자체의 근거 문서는 없음.

> **전문가 리뷰 보정 (2026-07-04)**: “순서 근거 없음”은 과장 — freezer 1순위(센서 물리)와
segment>aggregate(정보 보존)는 필연적 순서다. 정확한 해석: **“누적(accretion) +
특이도 우선(specific-first)”의 혼합**. 특수한 전제를 가진 전략이 앞에, 일반 폴백이 뒤에
온다는 원칙 자체는 유효하므로, 라우터 이식 시 순서를 그대로 보존하면 된다.
> 

**더 나은 해법**:
1. **전략 라우터**: 각 분기를 `Strategy{name, precondition(), solve(), guarantees}` 객체로 추출, 우선순위 리스트를 **데이터**(설정)로 선언. 다이어그램이 곧 코드가 되고, 분기 추가가 diff 한 줄이 됨.
2. **등가성 검증**: 재작성 전후를 기존 **924케이스 시나리오 계약**(`docs/scenario-readiness/`, 실패 0)으로 비교하면 현장 테스트 없이 회귀 검증 가능.
3. 각 전략에 히트 카운터 텔레메트리 → 실전에서 안 맞는 전략은 데이터로 제거 근거 확보.

---

## Q3. 왜 segment 매칭이 aggregate 무게 매칭보다 먼저인가?

**[확인]** docstring: “Match separable loadcell removal segments before aggregate weight” (`decision_engine.py:6227`).

**[추론]** 근거 문서는 없으나 수학적으로 자명: 한 트리거 안에서 2개 상품을 순차로 꺼내면 합계 무게(예: 348g)는 부분집합 합 문제로 조합이 모호하지만, 로드셀 **시계열 구간**(170g 하락 → 178g 하락)은 각각 단일 매칭으로 유일해짐. 시계열 정보를 버리기 전에 쓰는 것. 178g 다중청구 사건의 직접적 재발 방지책으로 추정.

**더 나은 해법**: 방향은 유지하되 **위치를 옮길 것** — segment 검출을 트리거 수신부(로드셀 분석)로 이동해, 판단엔진 입력을 처음부터 `[WeightSegment]` 리스트로 정규화. 엔진은 “구간별 매칭”만 알면 되고 aggregate는 구간이 1개인 특수 케이스가 됨. 현재는 엔진 내부에서 구간 분해를 재시도하는 역방향 구조.

> **전문가 리뷰 추가 조건 (2026-07-04)**: ingest 구간화 이동 시 두 가지 미검토 사항 해결 필수 —
① `_stabilize_return_delta`(1.0s 재수집)와의 **시점 충돌**: 구간화 확정 후 로드셀 샘플이
더 유입될 수 있으므로 구간화는 stabilization 완료 이후로 순서 고정.
② freezer의 5~15g 노이즈 + 컴프레서 사이클·온도 드리프트가 **가짜 세그먼트**를 만들 수
있음 → 구간 검출 임계도 SensorProfile 소속으로 하고, drift-aware baseline(구간 검출 전
재영점)을 검토.
> 

---

## Q4. 왜 YOLO를 conf=0.01로 돌리고 나중에 0.4로 자르는가?

**[확인]** `docs/llm-wiki/synthesis/latency-and-frame-stride.md`: “low-confidence products rescued by accumulated votes” — 프레임당 신뢰도는 낮아도 수십 프레임 투표가 누적되면 증거가 됨. 조기에 0.4로 자르면 투표 증거가 소멸.

**평가**: 합리적. NMS 추가 비용은 GPU에서 무시 가능. **유지 권장.** 단, `max_det=20`과 결합해 노이즈 박스가 Motion/HandPath 필터 CPU 비용을 늘리는지는 프로파일링 가치 있음(현재 CPU 측 비용은 전체의 ~12%라 우선순위 낮음).

---

## Q5. 왜 단일 워커 큐(트리거 직렬 처리)인가?

**[확인]** ① v4.10 주석 “TensorRT 동시 추론 충돌 방지” ② 2026-06-29 CRK 피드백 “single-GPU resource serialization” 명시 ③ Jetson Orin Nano **4GB** 메모리 제약.

**평가**: 제약이 실재하므로 병렬화는 답이 아님. 진짜 문제는 **직렬이라서가 아니라 트리거 1건이 18.4s 걸려서** 다음 트리거가 밀리는 것. 사람의 연속 행동 간격(수 초)보다 처리시간이 길다 → 큐 적체 → CLOSE 대기 연장 → 결제 지연.
→ 해법은 큐 구조 변경이 아니라 **건당 처리시간 단축** (OPTIMIZED_ARCHITECTURE.md L1~L3).

---

## Q6. 왜 freezer 반품을 close까지 지연 처리하는가?

**[확인]** 커밋 시퀀스가 학습 과정을 그대로 보여줌:
`c3c27de` close 집계 리졸버 신설 → `488a2cc` **부호 있는 net delta로 단순화** → `c80f0ef` “Defer freezer returns until close” → `181248a` 동일위치 반품 정산 수정 → `d104bca` best-channel 지연.

**[추론]** 도입 이유 문서는 없으나 Q1과 결합하면 필연: 오차 15g 센서로 “꺼냈다 넣었다”의 **중간 시점** 매칭을 하면 오판이 누적됨. 문 닫힘 시점의 net delta는 안정적이므로, 중간 판단을 보류하고 close에서 **부호 있는 순변화량으로 장바구니를 재해석**하는 것이 정확도 우위.

**더 나은 해법**: 이 방향을 **전 존으로 일반화** — “이벤트 소싱 + close-time 단일 정산”. 트리거는 전부 불변 이벤트로 축적만 하고(`_reaggregate_products`가 이미 이 구조), interim 응답은 잠정치로 명시, 확정은 close 리졸버 한 곳에서. 그러면 반품 복구 3계층(Q7)도 흡수됨.

---

## Q7. 왜 반품 복구가 3계층(동존 즉시 / net-delta / 교차존)인가?

**[확인]** `TRIGGER_INFERENCE_RECOVERY_NOTES_2026-03-31.md` — 실제 사용자 행동 3종에 1:1 대응: 같은 자리에 되돌림 / 세션 합계가 안 맞음(로드셀 정착 지연) / **다른 존에 되돌림**.

**평가**: 케이스는 실재하나 3계층이 **각각 다른 시점·다른 파일**에서 실행되어 상호작용 추적이 어려움 (aggregator 내부 → store net-delta → store cross-zone → freezer close resolver, 4번째 층까지 생김).

**더 나은 해법**: Q6과 동일 결론 — close 시점 **단일 글로벌 정산기**로 통합. 입력: 전 존의 서명된 트리거 이벤트 전체. 출력: 존별 확정 장바구니. 3계층은 정산기 내부의 매칭 우선순위(동존>net>교차존)로 강등. 실시간 interim 표시는 1계층 결과만 사용하고 “잠정”으로 명시.

---

## Q8. 저무게 스킵 5g의 근거는? freezer에서도 유효한가?

**[확인]** 냉장고 로드셀 노이즈 바닥(±3g) 위에 마진을 둔 값. freezer용은 별도 tolerance(15g)와 별도 신뢰도 게이트가 존재.
**[추론]** 그러나 freezer에서 5~15g짜리 실제 제거가 “저무게 스킵”에 걸릴 위험은 코드 검토 결과 stabilization 재수집(`_stabilize_return_delta`, `RETURN_STABILIZATION_WAIT_SECONDS=1.0`)으로 완화 중으로 보임. **재작성 시 존 타입별 게이트를 명시적으로 분리할 것** — 현재는 전역 5g에 freezer 예외가 흩어져 있음.

---

## Q9. 프레임 stride가 왜 1|2만 허용되는가?

**[확인]** `config.py:629` validator가 1|2 외 거부. `latency-and-frame-stride.md`: stride=2는 “latency rollback” — 정확도 우선 기본값 1, 검증된 후퇴 레버만 허용하는 보수적 설계. 실측: stride 1→2 시 18.4s→~7.5s.

**더 나은 해법**: 균일 stride는 **눈먼 스킵**(빠른 손동작 프레임도 버림). 자판기 영상 특성상 대부분 프레임이 정지 상태이므로, **모션 게이트 샘플링**(다운스케일 absdiff ~2ms/frame CPU)으로 “움직임 있는 프레임만 추론”하면 스킵률은 상황 적응적(정지 구간 90%↑ 스킵, 동작 구간 0% 스킵)이 되어 stride=2보다 빠르면서 recall 손실 없음. 상세는 OPTIMIZED_ARCHITECTURE.md L1. 사전 검증은 **현장 AVI/프레임 검출 덤프 코퍼스 회수 후** 오프라인 재생으로 수행한다 — 레포 내 트레이스의 `raw_vision_candidates`는 트리거 단위 집계라 프레임 단위 재생에는 불충분함이 확인됨 (OPTIMIZED_ARCHITECTURE §6 G2 전제).

---

## Q10. 빈 allowlist에서 추론을 fail-closed하는 이유는?

**[확인]** `latency-and-frame-stride.md`: `ActiveProductStore` 재고 컨텍스트 상실 시(`allowed_class_ids_count=0`) 추론 강행하면 **판매 중이 아닌 상품을 청구**할 수 있음 → 의도적 차단(`empty_allowlist_fail_closed`) + `snapshot_source=last_valid` 폴백. 2026-05-21/27 무검출 트레이스가 stride 문제로 오인됐던 사건이 근거.

**평가**: 결제 시스템으로서 올바른 안전 설계. **재작성 시 불변식으로 보존.** (동일 계열: 비디오 처리 실패 fail-closed `c1af3e5` — 처리 실패를 “무검출=0원”으로 조용히 바꾸면 매출 누락.)

---

## Q11. 세션 ID 초 단위 충돌 리스크(2026-03-04 리뷰 #2)는 해결됐는가?

**[확인]** 해결됨 — `door_session.py:839`, `global_door_session.py:196` 모두 `%H%M%S_%f`(마이크로초)로 확장. 단, 리뷰 #1(YOLO 로드 실패 시 무증상 기동)과 #3(에러가 processing으로 잔류)은 **재작성 시 반드시 재확인** 필요 항목으로 이관.

---

## Q12. CLOSE 대기 20s/5s는 왜 그렇게 길었나? (현재는?)

**[확인]** 구 문서(`PRODUCT_DETECTION_FLOW.md` v5.4)의 20s/5s는 **낡은 정보**. 현재 기본값은 `close_initial_wait_seconds=3.0`, `close_subsequent_wait_seconds=1.0` (`config.py:699-706`, 커밋 `bf4cddf` 계열 지연 개선). 결제 대기 체감의 주범은 이제 CLOSE 디바운스가 아니라 **큐에 남은 트리거의 처리시간**(=Q5) — 마지막 행동 직후 문을 닫으면 18.4s짜리 추론이 끝나야 확정됨.

---

## 재작성 시 보존해야 할 불변식 (정확도 보호장치 목록)

| # | 불변식 | 근거 |
| --- | --- | --- |
| I1 | 비디오 처리 실패는 무검출이 아니라 에러로 전파 (fail-closed) | `c1af3e5`, CRK 피드백 |
| I2 | 빈 active-product allowlist에서 추론 차단 + last_valid 폴백 | Q10 |
| I3 | freezer 다품목 출력은 무게 게이트(±15g) 통과 필수 | 178g 사건 |
| I4 | 저신뢰 감지도 투표 누적까지는 보존 (conf 0.01→0.4 2단계) | Q4 |
| I5 | stock_qty=0 상품은 매칭 제외 (품절 하드필터) | v4.8 |
| I6 | delta 전량 설명 강제 (`_enforce_full_delta_match`) — 부분 설명으로 과금 금지 | judge() 전 분기 |
| I7 | 트리거 멱등성 (5s TTL) + 단일 GPU 직렬 추론 | Q5 |
| I8 | 판정 사유 코드 로깅 (`strict_mismatch` 등) — 현장 디버깅 계약 | recovery notes |
| I9 | 시나리오 계약 924케이스 0 실패 유지 | scenario-readiness |
| I10 | interim 결과는 절대 결제로 전달되지 않음 — 결제 입력은 close-finalized 결과 유일 | 전문가 리뷰 (2026-07-04) |
| I11 | CLOSE finalize 멱등성 — 재폴링/재시도로 이중 확정·이중 과금 불가 (I7은 trigger 멱등성만 커버) | 전문가 리뷰 |
| I12 | count ≤ stock_qty 상한 | v4.8 (목록 누락분 보강) |
| I13 | 에러 trigger 존재 시 무성(silent) 확정 금지 — `status="error"` 전파 + “에러 포함 세션의 결제 확정 가능 여부”를 계약으로 정의 | 2026-03-04 리뷰 #3 + 전문가 리뷰 |
| I14 | 반품 정산이 존별 count를 음수로 만들 수 없음 (환수 > 청구 금지) | 교차존 복구·L6 통합 시 신규 위험 |
| I15 | +delta(반품) 트리거·freezer에는 조기 종료(L2) 미적용 | OPTIMIZED_ARCHITECTURE L2 |
| I16 | 손 상태 래치가 활성인 동안 모션 게이트(L1) 스킵 금지 — 조작적 정의: “직전 추론 통과 프레임에서 손이 ROI 내였거나, 손의 ROI 퇴장이 아직 미확인이면 스킵 불가” (손 bbox는 YOLO 산출물이므로 미추론 프레임에는 존재하지 않음 → 래치 형태로만 검증 가능) | OPTIMIZED_ARCHITECTURE L1 + 전문가 재판별 N3 |