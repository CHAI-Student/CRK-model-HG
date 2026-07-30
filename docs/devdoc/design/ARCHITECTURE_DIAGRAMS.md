# ARCHITECTURE_DIAGRAMS

# CRK-Model 추론·결제 로직 시각화 (재작성용 참조 문서)

> **상태 (2026-07-24):** 원본 CRK-model 구조의 참조용 시각화 — 재설계의 설계 입력(2026-07-10).
> HG **현행** 구조를 서술하는 문서가 아니다. 현행 아키텍처는 README를 참조.

> 목적: `engine/decision_engine.py`(10.4k L) 와 `session/*`(집계·결제) 를 **처음부터 다시 짜기** 위해
현재 로직을 한눈에 파악하기 위한 다이어그램 모음.
모든 다이어그램은 **Mermaid** — GitHub / VS Code(Markdown Preview Mermaid) 에서 컴파일 없이 바로 렌더링됩니다.
> 
> 
> 기준 커밋: `d104bca` · 작성 시각 기준 소스 직접 확인 (문서가 아닌 코드가 진실).
> 

---

## 0. 문서 지도

| # | 다이어그램 | 대상 소스 | 재작성 관점 |
| --- | --- | --- | --- |
| 1 | 시스템 컨텍스트 | 전체 서비스 경계 | 외부 계약(무엇이 들어오고 나가는가) |
| 2 | 엔드투엔드 시퀀스 | OPEN→trigger→CLOSE | 세션 생명주기 |
| 3 | 7단계 추론 파이프라인 | `trigger`→`video`→`vision`→`engine` | **추론** 큰 그림 |
| 4 | Trigger 수신 & 큐 | `api/routes/trigger.py`, `service/trigger_service.py` | 입력 정규화·멱등성 |
| 5 | **`judge()` 결정 트리** | `engine/decision_engine.py:215` | **추론 핵심** — 실제 분기 순서 |
| 6 | StrictWeightMatcher | `weight/strict_weight_matcher.py` | 무게 조합 탐색 |
| 7 | Freezer / Segment / Stage 분기 | `decision_engine.py` 내부 | 냉동고 채널 특수 로직 |
| 8 | 세션 집계 & 가격 산정 | `session/door_session_store.py`, `product_aggregator.py` | **결제** 큰 그림 |
| 9 | 반품 복구 3계층 | `product_aggregator` + `door_session_store` | 제거→반납 정합성 |
| 10 | Multi-Zone OPEN/CLOSE 상태기계 | `api/routes/multi_zone.py` | 폴링·최종화 타이밍 |
| 11 | 데이터 계약 & 설정값 | 전역 | 상수·환경변수 |

---

## 1. 시스템 컨텍스트

모델 서비스(8002) 는 **Camera / Node 와 직접**, **IO-Board / Payment 와 간접**으로 통신합니다.
결제(Payment)는 모델이 직접 하지 않고, 모델이 산정한 **품목·수량·금액**을 Node 가 받아 결제로 넘깁니다.

```mermaid
flowchart LR
    CAM["CRK-CAMERA<br/>(AVI 녹화 + 로드셀 수집)"]
    NODE["Edge_Environment / Node.js<br/>(8888)<br/>세션 오케스트레이션"]
    subgraph MODEL["Model Service (FastAPI :8002) — Jetson Orin Nano"]
        TRIG["/trigger<br/>추론 진입"]
        MZ["/api/judge/multi-zone<br/>폴링·결제 결과"]
    end
    IO["CRK-IO-BOARD<br/>(도어락)"]
    PAY["CRK-PAYMENT<br/>(결제)"]

    CAM -- "POST /trigger<br/>videos + loadcells + zone" --> TRIG
    NODE -- "POST /api/judge/multi-zone<br/>OPEN/CLOSE + active_products" --> MZ
    MZ -- "zones[].products / totalPrice" --> NODE
    NODE -. "도어 언락 (직접 X)" .-> IO
    NODE -. "확정 금액 전달 (직접 X)" .-> PAY

    classDef direct fill:#dff,stroke:#0aa;
    classDef indirect fill:#eee,stroke:#999,stroke-dasharray:4 3;
    class CAM,NODE direct;
    class IO,PAY indirect;
```

**핵심 계약** (`docs/agent-guides/architecture.md`)
- `delta_weight < 0` → 자판기에서 **꺼냄(removal)**
- `delta_weight > 0` → 자판기로 **되돌림(return)**
- `active_products` → Node 가 넘기는 **현재 재고 스냅샷** = strict/loadcell 매칭의 유일한 권위 소스
- `stock_qty = 0` → 품절, strict 매칭에서 제외

---

## 2. 엔드투엔드 시퀀스 (한 번의 구매 트랜잭션)

```mermaid
sequenceDiagram
    autonumber
    participant C as Camera
    participant M as Model(:8002)
    participant N as Node(:8888)

    N->>M: POST /multi-zone (session_id=OPEN, active_products)
    M->>M: GlobalDoorSession 시작/유지
    M-->>N: interim 응답 (zones 1..5 진행중)

    loop 문 열려있는 동안 (구매 행동마다)
        C->>M: POST /trigger (videos, loadcells, zone)
        M-->>C: 202 {status: queued}
        Note over M: 큐 워커가 순차 처리<br/>프레임→YOLO→필터→투표→judge()→DoorSession 집계
        N->>M: POST /multi-zone (OPEN, 폴링 ~10s)
        M-->>N: "처리중" interim
    end

    N->>M: POST /multi-zone (session_id=CLOSE)  %% 1차 CLOSE
    M->>M: pending_close 전환 (초기 20s 대기)
    N->>M: POST /multi-zone (CLOSE, 재폴링)      %% 2차+ CLOSE
    M->>M: first_close 이후 trigger 유무 판정<br/>→ 없으면 finalize / 있으면 5s 재대기
    M->>M: 교차존 반품·net-delta 정합성 복구<br/>freezer close 집계
    M-->>N: 최종 응답 zones[].products, totalPrice, productCount
    N->>N: 결제 진행 (Payment)
```

---

## 3. 7단계 추론 파이프라인 (큰 그림)

```mermaid
flowchart TD
    A["POST /trigger<br/>zone + videos{top,side} + loadcells[]"] --> B

    subgraph S1["① Trigger 수신 · api/routes/trigger.py + service/trigger_service.py"]
        B["로드셀 → delta_weight 계산<br/>_detect_stable_regions (슬라이딩 std)<br/>start_avg → end_avg"]
        B --> C{"멱등성 키 중복?<br/>MD5(zone+paths) TTL 5s"}
        C -- 중복 --> C1["드롭"]
        C -- 신규 --> D{"|delta| < 5g ?"}
        D -- yes --> D1["저무게 스킵<br/>무게 전용 판단 경로"]
        D -- no --> E["asyncio.Queue enqueue<br/>(순차 처리 · TensorRT 충돌 방지)"]
    end

    E --> F
    subgraph S2["② 프레임 추출 · video/frame_extractor.py"]
        F["ffprobe 메타(3회 재시도)<br/>FFmpeg NVDEC (-hwaccel cuda)<br/>gamma/contrast 보정 → BGR 480x480"]
    end

    F --> G
    subgraph S3["③ YOLO 추론 · vision/yolo_wrapper.py"]
        G["TensorRT FP16 480x480<br/>conf=0.01, max_det=20<br/>→ List[YOLODetection] (is_hand=cls0)"]
    end

    G --> H
    subgraph S4["④ 필터링 · video/video_processor.py + vision/hand_path_tracker.py"]
        H["1) Motion (BboxTracker, 동적 임계 max(15px,size*0.10))<br/>2) Hand Path (손 궤적 교차)<br/>3) Side ROI (center_x<240)<br/>4) conf<0.4 제거"]
    end

    H --> I
    subgraph S5["⑤ Voting Ensemble · video/voting_ensemble.py"]
        I["Top/Side 투표 축적 → combine()<br/>weighted_conf = top*0.5 + side*0.5 + min*0.2<br/>vote_ratio ≥ 5% OR count ≥ 3"]
    end

    I --> J
    subgraph S6["⑥ 판단 엔진 · engine/decision_engine.py (→ 다이어그램 5)"]
        J["judge(vision_candidates, delta_weight, active_products)<br/>→ JudgmentResult{status, products[], confidence}"]
    end

    J --> K
    subgraph S7["⑦ 세션 통합 & 응답 · session/* (→ 다이어그램 8)"]
        K["DoorSession 축적 → ProductAggregator<br/>→ GlobalSession → totalPrice → Node.js"]
    end

    D1 --> J
```

---

## 4. Trigger 수신 & 큐 워커

```mermaid
flowchart TD
    IN["TriggerInput<br/>zone, videos, loadcells, active_products, timing"] --> DUP{"_register_request<br/>idempotency_key 존재?"}
    DUP -- "5s 이내 재전송" --> DROP["중복 드롭 → 기존 session_id 반환"]
    DUP -- 신규 --> VID{"비디오 파일 존재?"}
    VID -- no --> ERR["에러/무시"]
    VID -- yes --> LW{"_should_skip_low_weight<br/>|delta| < MIN_WEIGHT_CHANGE"}
    LW -- "저무게 & 신뢰가능" --> WO["무게 전용 (vision 생략)"]
    LW -- "일반" --> FV{"_should_force_vision_only?<br/>(로드셀 불안정 등)"}
    FV -- yes --> VOnly["vision_only 경로"]
    FV -- no --> ENQ["QueueItem enqueue<br/>notify_trigger_enqueued(zone)"]

    ENQ --> WK["worker loop (단일 소비자)"]
    WK --> STB["_stabilize_return_delta<br/>반품 delta 안정화 재수집"]
    STB --> PROC["_process: video_processor 실행 → judge()"]
    PROC --> STORE["DoorSessionStore.add_trigger_with_global()<br/>notify_trigger_processed(zone)"]

    classDef race fill:#fee,stroke:#c66;
    class ENQ,WK,STORE race;
```

> **재작성 주목점**: `notify_trigger_enqueued` / `notify_trigger_processed` 는 CLOSE 신호와의
race condition(문 빨리 닫힘) 방지용 카운터. 큐는 **단일 소비자**라 TensorRT 동시 추론이 없음.
> 

---

## 5. `judge()` 결정 트리 — 추론 핵심 (실제 코드 분기 순서)

> `engine/decision_engine.py:215`. 위에서 아래로 **먼저 매칭되는 분기가 즉시 return**.
모든 성공 결과는 마지막에 `_enforce_full_delta_match()` 로 “delta 전량 설명” 검증을 거침.
> 

```mermaid
flowchart TD
    START(["judge()"]) --> VO{"vision_only ?"}
    VO -- yes --> RVO["_judge_vision_only<br/>conf*0.7, count=1"] --> RET(["return + _log_final_branch"])

    VO -- no --> FRZ{"_try_freezer_vision_first<br/>냉동고 vision 정체성 후보 → 무게로 개수 검증"}
    FRZ -- 성공 --> EFDM1["_enforce_full_delta_match"] --> RET

    FRZ -- None --> AUG["_augment_stage_weight_gate_candidates<br/>(stage weight gate 후보 보강)"]
    AUG --> SEG{"_try_segment_weight_matching<br/>분리 가능한 로드셀 제거 '구간'을 개별 매칭"}
    SEG -- 성공 --> EFDM2["_enforce_full_delta_match"] --> RET

    SEG -- None --> HASC{"vision_candidates 있음?"}

    HASC -- "없음" --> NOVIS["Loadcell-only 폴백 체인"]
    subgraph NV["후보 없음 폴백"]
        direction TB
        NOVIS --> SC1{"_try_stage_count_combination_match"}
        SC1 -- 성공 --> RET
        SC1 -- None --> DS1{"_try_detected_single_item_fallback"}
        DS1 -- 성공 --> RET
        DS1 -- None --> VFI{"vision-first identity policy?"}
        VFI -- yes --> SUP["loadcell_identity_suppressed<br/>(정체성 억제 결과)"] --> RET
        VFI -- no --> WBO["judge_by_weight_only"]
        WBO --> WOK{"성공?"}
        WOK -- no --> FF1{"_try_forced_final_fallback"}
        FF1 -- 성공 --> RET
        WOK -- yes --> RET
        FF1 -- None --> RET
    end

    HASC -- "있음" --> MWC{"|delta| < min_weight_change ?"}
    MWC -- yes --> NODET["NO_DETECTION (무게 변화 미미)"] --> RET

    MWC -- no --> SWG{"_try_same_weight_candidate_collision_guard<br/>(동일 무게 후보 충돌 방지)"}
    SWG -- 성공 --> RET

    SWG -- None --> STRICTQ{"strict_mode? (기본 true)"}
    STRICTQ -- yes --> STRICT["_judge_strict → 다이어그램 6"]
    STRICT --> STRES{"결과 != None?"}
    STRES -- yes --> RET

    STRES -- None --> SPC{"_try_same_product_count_match"}
    STRICTQ -- no --> SPC
    SPC -- 성공 --> RET

    SPC -- None --> RELAX["_judge_relaxed<br/>single_product / combination / partial / loadcell_only"]
    RELAX --> RLXOK{"COMPLETE?"}
    RLXOK -- "no & vision-first" --> VFP{"_try_vision_first_identity_partial"}
    VFP -- 성공 --> RET
    RLXOK -- "실패" --> DS2{"_try_detected_single_item_fallback"}
    DS2 -- 성공 --> RET
    DS2 -- None --> FF2{"vision-first? → 억제 / else forced_final"}
    FF2 --> RET
    RLXOK -- yes --> RET

    classDef weight fill:#fff3d6,stroke:#e0a800;
    classDef vision fill:#d6ecff,stroke:#3399ff;
    classDef strict fill:#ffe0e0,stroke:#e06666;
    class FRZ,SEG,AUG,SC1,WBO,SWG,SPC weight;
    class VO,RVO,VFI,VFP,DS1,DS2 vision;
    class STRICT,STRICTQ strict;
```

**분기 요약 (재작성 시 이 순서가 핵심)**

| 순위 | 분기 | 조건/전략 |
| --- | --- | --- |
| 0 | `vision_only` | 로드셀 없음/강제 vision |
| 1 | `freezer_vision_first` | 냉동고: vision 정체성 우선 → 무게로 개수 검증 |
| 2 | `segment_weight_matching` | 로드셀 제거를 시간축 ’구간’으로 분리해 개별 매칭 |
| 3 | (후보 없음) `stage_count_combo` → `detected_single` → `weight_only` → `forced_final` | vision 후보 0일 때 |
| 4 | `min_weight_change` 게이트 | 무게 변화 미미 → NO_DETECTION |
| 5 | `same_weight_candidate_collision_guard` | 동일 무게 후보 모호성 방어 |
| 6 | **`strict`** | 무게 우선 백트래킹 조합 (기본 경로) |
| 7 | `same_product_count_match` | strict 실패 시 동일 품목 개수 조합 |
| 8 | **`relaxed`** | single→combination→partial→loadcell_only |
| 9 | vision-first partial / detected_single / forced_final | 최후 폴백 |

---

## 6. StrictWeightMatcher (무게 우선 조합 탐색)

`weight/strict_weight_matcher.py` · `_judge_strict()` 가 호출.

```mermaid
flowchart TD
    IN["find_valid_combinations<br/>candidates, delta_weight, active_products"] --> T{"target = |delta| < tolerance?"}
    T -- yes --> EMPTY["빈 결과 (target_below_tolerance)"]
    T -- no --> KEY["active_products 를 YOLO class_id 로 인덱싱<br/>stock=0 / vision 미검출 후보 제외"]
    KEY --> BT["_backtrack: 부분집합 합 탐색<br/>Σ(unit_weight × count) ∈ target ± tolerance(±3g)<br/>max_items, max_kinds 제한"]
    BT --> VC{"valid_combos 있음?"}
    VC -- 없음 --> FB["_judge_strict 내부 폴백:<br/>same_product_count → stage_count_combo → rescue_single<br/>→ strict_mode_fallback? relaxed 진입 : NO_DETECTION"]
    VC -- 있음 --> SORT["combination_sort_key 정렬:<br/>-match_score, -adjusted_score, kind_count..."]
    SORT --> PICK["최상위 조합 선택 → JudgmentResult(COMPLETE)"]

    subgraph SCORE["match_score 산식 (조합 평가)"]
        direction LR
        MS["match_score =<br/>weight_score×0.6 + vision_score×0.3 + simplicity_score×0.1"]
        WS["weight_score = max(0, 1 − weight_error/tolerance)"]
        VS2["vision_score = 개수가중 평균 vision conf"]
        SS["simplicity_score = max(0, 1 − (품목종류수−1)×0.2)"]
    end
    SORT -.평가.-> SCORE
```

> **핵심 아이디어**: 로드셀이 ±3g로 정확하다는 가정 → **무게로 가능한 조합을 먼저 뽑고**,
그 중 YOLO가 본 것만 남겨 vision confidence로 최종 선택. 무게로 설명 불가 → NO_DETECTION.
> 

---

## 7. Freezer / Segment / Stage 특수 분기 (심층)

CRK는 냉동고(freezer) **채널(channel)** 구조라 일반 자판기와 다른 경로가 있습니다.

```mermaid
flowchart TD
    subgraph F["freezer_vision_first (judge 1순위)"]
        F1["냉동고 vision 정체성 후보 수집"] --> F2["_select_freezer_channel_target_combination<br/>채널 타깃(위치) 우선 solve"]
        F2 --> F3["_select_freezer_ordered_vision_combination<br/>순서형 vision 조합"]
        F3 --> F4["무게로 개수 검증 → JudgmentResult"]
        F4 --> F5["prior_selected_(position_)product_idxs 로<br/>hand별 경로 중복 방지"]
    end

    subgraph SG["segment_weight_matching (judge 2순위)"]
        G1["로드셀 시계열에서 분리 가능한<br/>제거 '구간(segment)' 검출"] --> G2["_single_segment_selection<br/>_select_segment_match_options"]
        G2 --> G3["구간별 개별 무게 매칭 후 합산<br/>(집계 무게보다 먼저 시도)"]
    end

    subgraph ST["stage weight gate / count combination"]
        H1["_augment_stage_weight_gate_candidates<br/>stage 단위 후보 보강"] --> H2["_try_stage_count_combination_match<br/>stage 개수 조합 매칭"]
    end

    F -. "None이면" .-> SG
    SG -. "None이면" .-> ST
```

> **재작성 주목점**: freezer 경로는 “채널 위치(position) + 순서(order) + hand 경로”까지 추적.
커밋 로그(`Track freezer hand paths per hand`, `best-channel deferral for freezer rescue`)가
이 영역이 가장 최근까지 손댄 복잡 지점임을 보여줌 → 재설계 시 별도 모듈로 분리 권장.
> 

---

## 8. 세션 집계 & 가격 산정 — 결제 큰 그림

`JudgmentResult` → **DoorSession(존 단위)** → **GlobalSession(전체)** → `totalPrice`.

```mermaid
flowchart TD
    JR["JudgmentResult (per trigger)"] --> ADD["DoorSessionStore.add_trigger_with_global()"]
    ADD --> TRG["DoorSession.triggers[] 에 TriggerResult 축적"]
    TRG --> REAGG["_reaggregate_products(session)<br/>전체 trigger 재집계"]

    subgraph AGG["ProductAggregator.aggregate_with_unmatched()"]
        direction TB
        R1{"trigger.delta_weight 부호"}
        R1 -- "< 0 제거" --> RM["_handle_removal<br/>YOLO 품목 추가 · count 증가"]
        R1 -- "> 0 반납" --> RT["_handle_return<br/>무게로 기존 품목 찾아 count 감소"]
        RT --> RTOK{"매칭?"}
        RTOK -- 실패 --> UR["UnmatchedReturn 기록<br/>(→ 다이어그램 9 복구)"]
        RM --> HINT["_handle_return_hints<br/>touch/position 힌트 반영"]
        RT --> HINT
    end
    REAGG --> AGG

    AGG --> PRICE["ProductCount.total_price = unit_price × count<br/>(session/door_session.py:606)"]
    PRICE --> DSP["DoorSession.total_price = Σ product.total_price"]
    DSP --> GS["GlobalDoorSession.total_price = Σ zone.total_price<br/>(global_door_session.py:70)"]
    GS --> YAML["YAML 영속화 (background ThreadPool)"]
    GS --> RESP["_build_zone_result → Node.js 응답<br/>zones[].products{count,price,total}, totalPrice"]

    classDef pay fill:#e0ffe0,stroke:#3a3;
    class PRICE,DSP,GS,RESP pay;
```

**가격 산식은 단순** (재작성 시 그대로 유지 가능):

```
product.total_price = unit_price × count          # door_session.py:606
zone.total_price    = Σ product.total_price        # door_session.py:689
global.total_price  = Σ zone.total_price           # global_door_session.py:70
```

→ 복잡도는 **가격이 아니라 count(수량) 를 확정하는 집계·반품 로직**에 있음.

---

## 9. 반품 복구 3계층 (“꺼냈다가 다시 넣음” 정합성)

`docs/agent-guides/architecture.md` 의 3-pass 구조를 코드로 확인.

```mermaid
flowchart TD
    START["집계 후 net delta 불일치 가능성"] --> L1

    subgraph L1["① 동일 존 즉시 복구 · ProductAggregator._handle_return()"]
        A1["단일 품목 무게 매칭"] --> A2["실패 시 다중 품목 반납 조합"]
    end

    L1 --> L1D["_apply_deferred_return_reconciliation<br/>_apply_same_zone_deferred_return<br/>(close까지 지연된 반납 정산)"]

    L1D --> L2
    subgraph L2["② Net-delta 보정 · DoorSessionStore._validate_net_delta()"]
        B1["세션 net delta = _calculate_effective_net_delta"] --> B2["집계 count 가 net delta 와 어긋나면<br/>제거 전량/일부가 반납됐다고 보고 count 교정"]
    end

    L2 --> L3
    subgraph L3["③ 교차 존 복구 · DoorSessionStore._handle_cross_zone_returns()"]
        C1["미매칭 반납(UnmatchedReturn)"] --> C2["다른 zone 활성 세션의 무게와 매칭"]
        C2 --> C3["매칭 성공 → 해당 zone count 감소 + 기록"]
    end

    L3 --> FRZC["freezer close 집계<br/>FreezerCloseAggregateResolver.apply()<br/>부호있는 net basket 재solve (불안정 close 대비)"]
    FRZC --> DONE["최종 zones 확정"]
```

> **재작성 주목점**: 반품 복구가 3계층 + freezer close 재solve까지 있는 이유는
“빠른 개폐 / 존 착오 반납 / 로드셀 불안정” 실사용 케이스 때문. 로그 reason 코드
(`strict_mismatch`, `no_active_products`, `stock_filtered`, `negative_delta_weight`)를
재설계에서도 보존하면 필드 디버깅이 코드 없이 가능.
> 

---

## 10. Multi-Zone OPEN/CLOSE 상태기계

`api/routes/multi_zone.py` · `judge_multi_zone` (`@router.post("/multi-zone")`).

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Active: session_id=OPEN<br/>_handle_door_open() · GlobalSession 시작
    Active --> Active: OPEN 반복 폴링(~10s)<br/>interim 응답 (처리중)
    Active --> PendingClose: session_id=CLOSE (1차)<br/>handle_close_signal → pending
    PendingClose --> PendingClose: CLOSE 재폴링 &<br/>first_close 이후 trigger 존재<br/>→ 5s 재대기
    PendingClose --> Finalizing: 초기 20s 경과 & trigger 없음<br/>is_ready=true
    Finalizing --> Finalized: 교차존 반품 + net-delta +<br/>freezer close 집계 → _build_decision_summary
    Finalized --> [*]: 최종 zones[], totalPrice 응답 → Node 결제

    note right of PendingClose
        빠른 개폐 대응:
        1차 CLOSE는 즉시 확정 안 함
        큐 대기 trigger 소진까지 대기
    end note
    note right of Active
        OPEN마다 active_products 스냅샷 갱신
        (_maybe_update_active_product_snapshot)
    end note
```

**타이밍 상수**

| 구간 | 값 | 의미 |
| --- | --- | --- |
| 폴링 주기 | 10s | Node → Model |
| pending_close 초기 | 20s | 마지막 trigger 이후 최종화 대기 |
| pending_close 이후 | 5s | 추가 trigger 확인 대기 |
| 세션 TTL | 300s | DoorSession 만료 (`MODEL__BUFFER__TTL_SECONDS`) |

---

## 11. 데이터 계약 & 핵심 설정값

**입력 (`POST /trigger`)**

```json
{
  "zone": 1,
  "videos": {"top": "/path/top.avi", "side": "/path/side.avi"},
  "loadcells": [{"timestamp": 1700000000.0, "values": [100.5, 200.3, 150.2, 180.1]}]
}
```

> 카메라는 존당 **물리 로드셀 2채널**을 보내고, 모델은 이를 **평균이 아니라 합산**해 존 총량으로 씀.
> 

**출력 (`/api/judge/multi-zone` 최종)**

```json
{
  "zones": [{"zone": 1, "products": [{"product_id":"P001","name":"콜라","count":2,"unit_price":1500,"total_price":3000}], "totalPrice": 3000}],
  "totalPrice": 3000, "productCount": 2,
  "globalSessionInfo": {"session_id": "uuid", "status": "complete"}
}
```

**설정값**

| 설정 | 값 | 환경변수 |
| --- | --- | --- |
| 입력 해상도 / 정밀도 | 480×480 / FP16 | - |
| YOLO conf / max_det | 0.01 / 20 | - |
| 최종 필터 conf | 0.4 | - |
| 무게 허용오차 | ±3.0g | `MODEL__WEIGHT__TOLERANCE_GRAMS` |
| 저무게 스킵 | 5g | `MODEL__LOADCELL__*` |
| strict 모드 | true | `MODEL__WEIGHT__STRICT_MODE_*` |
| strict 실패 시 relaxed 진입 | true/false | `MODEL__WEIGHT__STRICT_MODE_FALLBACK` |
| 조합 최대 종류 | - | `MODEL__WEIGHT__MAX_COMBINATION_KINDS` |
| 안정 윈도우 | - | `MODEL__LOADCELL__STABLE_WINDOW_SIZE` |
| 안정 임계 | - | `MODEL__LOADCELL__STABILITY_THRESHOLD_GRAMS` |
| async 스트리밍 | true | `MODEL__ASYNC_STREAMING__ENABLED` |

---

## 부록 A. 파일 → 다이어그램 역참조

| 파일 (`services/model/model_service/`) | 줄수 | 관련 다이어그램 |
| --- | --- | --- |
| `engine/decision_engine.py` | 10,419 | 5, 6, 7 |
| `service/trigger_service.py` | 4,536 | 4 |
| `session/door_session_store.py` | 3,607 | 8, 9, 10 |
| `api/routes/trigger.py` | 1,911 | 3, 4 |
| `api/routes/multi_zone.py` | 1,664 | 2, 10 |
| `session/freezer_close_aggregate.py` | 1,267 | 9 |
| `session/product_aggregator.py` | 1,125 | 8, 9 |
| `session/active_product_store.py` | 1,041 | 5, 11 |
| `weight/strict_weight_matcher.py` | - | 6 |
| `video/voting_ensemble.py` | - | 3 (⑤) |
| `vision/hand_path_tracker.py` | - | 3 (④), 7 |

## 부록 B. 재작성 시 분리 권장 경계 (관찰 기반 제안, 미검증)

1. **추론 파이프라인 (stateless)**: 프레임→YOLO→필터→투표→`judge()`. 입력=한 trigger, 출력=`JudgmentResult`. 순수 함수로 만들면 테스트 용이.
2. **judge() 라우터**: 현재 단일 메서드에 10+ 분기가 순차 매몰. 각 분기를 전략(Strategy)으로 분리하고 우선순위 리스트로 표현하면 다이어그램 5가 곧 코드가 됨.
3. **freezer 채널 로직**: 일반 매칭과 성격이 달라(위치·순서·hand 경로) 별도 모듈 후보.
4. **세션/결제 집계 (stateful)**: DoorSession/GlobalSession + 반품 복구 3계층. 가격은 단순, 복잡도는 count 확정. 이벤트 소싱(trigger 로그 → 재집계) 구조가 이미 `_reaggregate_products`에 있으므로 그 방향 유지 권장.
5. **Multi-Zone 생명주기 (I/O 경계)**: OPEN/CLOSE 상태기계는 결제 타이밍과 직결 → 순수 상태기계로 분리해 타이밍 상수만 주입.