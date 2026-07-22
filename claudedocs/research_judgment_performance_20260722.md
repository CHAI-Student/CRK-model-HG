# CRK-model-HG 추론·판정 성능 향상 — 문헌 조사와 적용 방향

> 2026-07-22 작성. /sc:research 결과물.
> 질문: "CRK-model-HG의 추론·판정 성능을 끌어올릴 수 있는 공신력 있는 논문들과 적용 방향"
> 기준 코드: 프레임 투표 앙상블(`perception/voting.py`), plateau 기반 로드셀
> 분석(`ingest/loadcell.py`), 규칙 기반 판정 라우터(I-V, `judgment/`),
> baseline/static/hand_path 필터(`perception/filters.py`), Jetson 엣지 추론.

---

## 요약 (Executive Summary)

CRK-model-HG가 부딪혀 온 사고들(#8 #10 #14 #15)은 문헌상 **잘 알려진 문제
계열**이고, 각각 대응하는 검증된 연구가 있다. 임팩트×난이도 기준 우선순위:

| 순위 | 제안 | 근거 논문 | 직접 해결하는 문제 | 난이도 |
|---|---|---|---|---|
| 1 | 로드셀 plateau → **BOCPD** (베이지안 온라인 변화점) | Adams & MacKay 2007 | #14 delta=0 (5샘플 창에서 안정 판정 실패) | 낮음 |
| 2 | 프레임 투표 → **트랙릿 단위 투표** (ByteTrack) | Zhang et al. ECCV 2022 | 배경 vote 인플레이션, static/baseline 필터의 근본 원인 | 중간 |
| 3 | 손 접촉 상태 검출 (**100DOH**) | Shan et al. CVPR 2020 | hand_path 근사의 한계 — "들고 있는 물체"를 직접 식별 | 중간 |
| 4 | 투표 임계 **conformal 보정** | Angelopoulos & Bates 2021 | 손튜닝 노브(MIN_VOTE_*, CONF_FLOOR)의 통계적 근거 | 낮음 |
| 5 | 판정 융합의 **확률화** (Grab/AIM3S/DS 이론) | Liu et al. 2020, Falcão et al. 2019 | 규칙 라우터의 경계 불연속(15g 경계 코인플립) | 높음 |
| 6 | **임베딩 기반 개방형 상품 인식** | RP2K, RetailKLIP | 신규 SKU 재학습, #15류 class 매핑 드리프트 | 높음 |

공통 관찰: 현재 시스템의 필터 3종(static/baseline/hand_path)과 vote share
계열 노브는 **"프레임 독립 투표"라는 표현의 한계를 보정하는 장치들**이다.
문헌의 방향(트래킹 + 접촉 상태 + 확률 융합)으로 가면 이 보정 장치들의
상당수가 구조적으로 불필요해진다.

---

## 1. 비전+무게 융합 자율 결제 시스템 — 직접 선행연구

### 논문

- **AIM3S** — [Falcão et al., "Autonomous Inventory Monitoring through
  Multi-Modal Sensing for Cashier-Less Convenience Stores", ACM BuildSys
  2019](https://dl.acm.org/doi/10.1145/3360322.3361018): 카메라 + 선반
  무게 센서 + **상품 배치 사전 지식(planogram prior)** 3원 융합. 융합
  정확도 93.2%로 vision-only 대비 우위 실증.
- **FAIM** — [Falcão et al., Frontiers in Built Environment
  2020](https://www.frontiersin.org/journals/built-environment/articles/10.3389/fbuil.2020.568372/full):
  AIM3S의 확장 — 무게 이벤트를 확률 분포로, 비전 인식을 우도로 취급하는
  적응형 융합.
- **Grab** — [Liu et al., "Grab: Fast and Accurate Sensor Processing for
  Cashier-Free Shopping", arXiv:2001.01033](https://arxiv.org/abs/2001.01033):
  포즈 추적 + 팔 동선 연관 + 카메라·무게·RFID **확률적 융합 프레임워크**.
  교란 행동 40% 조건에서도 정밀도/재현율 90%+.
- **ISACS** — [In-Store Autonomous Checkout System for Retail,
  2021](https://www.researchgate.net/publication/354594501_ISACS_In-Store_Autonomous_Checkout_System_for_Retail)

### CRK-model-HG 적용 방향

1. **Planogram prior의 도입** (AIM3S의 세 번째 모달리티). 현재 판정은
   allowlist(재고 유무)만 쓰고 **"이 트레이에 어떤 상품이 배치돼 있는가"**
   를 안 쓴다. 트레이 분리 구조(채널별 이벤트, `loadcell.py` 2단계)와
   결합하면: 채널 4에서 무게가 빠졌는데 후보가 채널 4 배치 상품이 아니면
   페널티. #15에서 만두(다른 트레이 배치)가 존1 취출로 과금되는 것을
   구조적으로 차단할 수 있다. **難: OPEN payload에 배치 정보 추가 필요
   (Edge_Environment 협조), 판정층 변경은 소규모.**
2. **무게 이벤트의 확률화** (FAIM/Grab). 현재 count 게이트는 |잔차|≤15g
   경첩 하나로 이분한다 — #15에서 3g 차이로 정체성이 뒤집힌 원인. 잔차를
   가우시안 우도 exp(−r²/2σ²)로, vote를 정체성 사전확률로 놓고 곱하면
   경계 불연속이 사라진다. near_gate(②단계)는 이 방향의 이산 근사였다.
   **難: I-V 원칙과의 정합 설계 필요 — "무게가 정체성을 선택 못 한다"를
   우도비 상한(예: 무게 우도비가 vote 사전비를 최대 k배까지만 뒤집게)으로
   연속화하는 형태를 권장.**

신뢰도: 높음 (동일 도메인, 실환경 실증 논문 다수)

---

## 2. 로드셀 신호 — plateau 휴리스틱을 변화점 검출로

### 논문

- **BOCPD** — [Adams & MacKay, "Bayesian Online Changepoint Detection",
  arXiv:0710.3742 (2007)](https://arxiv.org/abs/0710.3742): run-length
  사후분포를 온라인 메시지 패싱으로 유지 — "마지막 변화 이후 몇 샘플째인가"
  자체가 확률변수. 참조 구현 다수
  ([해설](https://gregorygundersen.com/blog/2019/08/13/bocd/)).

### 적용 방향

현행 `_stable_plateaus`는 **3연속 샘플 std ≤ 2.5g**라는 경성 창을 요구한다.
0.8s 캐던스 × post-roll 4s = 5샘플에서 이 조건은 마진이 1샘플뿐이고, #14의
`insufficient_stable_regions` → delta=0 → 무음 0원이 그 결과였다.

BOCPD로 바꾸면: 5g 양자화 노이즈를 관측 모델(가우시안, σ≈2.5g)에 넣고
**"현재 run이 새 레벨일 확률"과 레벨 추정치**를 동시에 얻는다 —
- 정착이 느린 creep(5g 스텝)도 run-length 분포가 부드럽게 처리 (경성
  3연속 조건 없음)
- delta = (마지막 run의 레벨 추정) − (변화 전 run의 레벨 추정), **불확실성
  포함** — settler가 "delta=0"이 아니라 "delta=−170±12"를 받게 됨
- 구현이 작다 (수십 줄, 순수 CPU). `LoadcellAnalyzer._analyze_series`를
  대체하는 두 번째 분석기로 넣고 **shadow 비교**(기존 결과와 병기 로깅)
  후 승격하는 경로를 권장. 기존 reason 계약(insufficient_*)은 "사후확률
  < 임계"로 자연 대응.

신뢰도: 높음 (고전·검증된 방법, 시나리오 적합성 명확)

---

## 3. 프레임 투표 → 트랙릿 투표

### 논문

- **ByteTrack** — [Zhang et al., "ByteTrack: Multi-Object Tracking by
  Associating Every Detection Box", ECCV
  2022](https://dl.acm.org/doi/10.1007/978-3-031-20047-2_1): 저신뢰
  검출까지 IoU 연관에 활용하는 단순·강력한 트래커. 칼만 + IoU만으로
  동작해 **추가 GPU 비용 0**.
- 초저전력 임베디드 응용: [Multi-resolution Rescored ByteTrack for Video
  Object Detection on Ultra-low-power Embedded
  Systems](https://arxiv.org/pdf/2404.11488)

### 적용 방향

현행 투표는 프레임 독립이라 **"오래 보이는 것 = 표 많은 것"**이 된다.
배경 상품이 표를 쓸어가고(#10: 배경 27이 171표, 진품 3표), 이를 막으려
static_track·baseline·min_vote_share가 겹겹이 붙었다. 트랙릿 단위로 바꾸면:

- 검출 → ByteTrack 연관 → **트랙(수명 있는 개체)** → 후보 = 트랙,
  vote_count = 트랙 수명이 아니라 **트랙의 이동량/손 근접도로 가중**
- "정지 트랙 = 배경, 움직인 트랙 = 사건 관여"가 필터가 아니라 **표현
  수준에서** 구분됨 — static_track(IoU 연속 조건의 취약함)과 baseline
  (손 등장 전 경계의 사각)이 하는 일이 트랙 속성 하나로 통합
- I-V ①의 밴드 비교도 "트랙 vs 트랙"이 되어 프레임 수 편향이 사라짐

**難: `_run_vision` 루프에 트래커 상태 추가(중간), voting 계약 변경(중간).
기존 필터를 제거하지 말고 트랙 가중 투표를 shadow로 병행 → vote_summary
비교 후 전환.**

신뢰도: 높음 (방법 자체는 표준), 적용 효과는 중간 확신 — 실측 필요

---

## 4. 손-물체 접촉 상태 검출

### 논문

- **100DOH** — [Shan et al., "Understanding Human Hands in Contact at
  Internet Scale", CVPR 2020
  (Oral)](https://openaccess.thecvf.com/content_CVPR_2020/html/Shan_Understanding_Human_Hands_in_Contact_at_Internet_Scale_CVPR_2020_paper.html):
  손 위치 + 좌/우 + **접촉 상태(portable object 접촉 여부)** + **접촉 중인
  물체 bbox**를 한 번에 출력. [공개 모델](https://github.com/ddshan/hand_object_detector)
  (Faster-RCNN 기반).
- 후속 평가: [Improving and Evaluating Hand-Object Interaction
  Detection](https://arxiv.org/html/2606.17384v1)

### 적용 방향

hand_path 필터는 "손 궤적 40px 근방 교차"라는 기하 근사다. 접촉 상태
모델을 쓰면 **"지금 손에 들려 있는 물체의 bbox"를 직접** 얻는다:

- held-object bbox와 IoU가 높은 검출만 투표 가중 ↑ — 프리롤 배경은
  자연 배제 (baseline의 목적을 사건 정의로 달성)
- HandLatch(I16)의 신뢰도 향상: "손이 ROI 안" → "손이 물체를 쥔 상태"
- 판정 confidence에 "접촉 증거 유무" 항 추가 가능

**難: Jetson에 두 번째 모델 — YOLO 검출과 접촉 헤드를 공유 백본으로
통합하거나(재학습 필요, 높음), 접촉 판정을 트리거당 키프레임 몇 장에만
돌리는 절충(중간)이 현실적. 지연 예산(현재 ~13s/trigger)이 빠듯하므로
2번(트랙릿)로 먼저 이득을 본 뒤 검토 권장.**

신뢰도: 중간 (효과는 확실하나 엣지 비용이 관건)

---

## 5. 판정 융합의 이론적 기반 — 규칙에서 확률/증거로

### 논문

- Grab의 확률적 연관 프레임워크 (§1)
- **Dempster-Shafer 증거 이론** 계열: [An adaptive and late multifusion
  framework based on evidential deep learning and Dempster–Shafer theory,
  KAIS 2024](https://link.springer.com/article/10.1007/s10115-024-02150-2),
  [A Weighted Combination Method for Conflicting Evidence in Multi-Sensor
  Data Fusion, Sensors 2018](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5982568/)

### 적용 방향

I-V 재설계가 도입한 개념들 — "무게는 반증만", "모호하면 불발", "PARTIAL로
강등" — 은 DS 이론의 언어와 정확히 일치한다: vote는 정체성에 대한 belief
mass, 무게 잔차는 반증 증거, **"적합 2개 = 모호"는 conflict mass**,
PARTIAL은 uncertainty를 남긴 채 최소 주장만 하는 것. 장기적으로 4단계
if-체인을 DS 결합 규칙 하나로 대체하면:

- 단계 경계(50%/30%/10%, 15/30g)가 연속 함수로 흡수 → 경계 코인플립 소멸
- freezer/냉장 차이가 "무게 증거의 mass 배분 차이"로 통일 — 전략 배제
  리스트(precondition 가드 6개)가 불필요해짐

**難: 높음. 규칙 라우터는 감사가능성(어느 전략이 왜)이 장점이므로,
전환하더라도 reason 계약과 사고 재현 테스트(#8 #10 #15)를 게이트로 유지할
것. 단기에는 I-V 유지가 옳다.**

신뢰도: 중간 (이론 적합성 높음, 이행 비용·감사성 트레이드오프)

---

## 6. 임계값의 통계적 보정 — conformal prediction

### 논문

- [Angelopoulos & Bates, "A Gentle Introduction to Conformal Prediction
  and Distribution-Free Uncertainty Quantification",
  arXiv:2107.07511](https://arxiv.org/pdf/2107.07511v1):
  보정셋 분위수로 임계를 정하면 **P(정답 ∈ 예측집합) ≥ 1−α**가
  분포 무관·유한표본으로 보장.

### 적용 방향

현재 `MIN_VOTE_RATIO=0.02, CONF_FLOOR=0.4` 등은 사고 사후 대응으로 정해진
값이다. 축적 중인 세션 아카이브(정답 라벨: 실험 시 실제 취출 기록)를
보정셋으로 쓰면:

- "후보 채택 임계"를 **목표 재현율(예: 취출 상품이 후보에 남을 확률
  ≥ 95%)**에서 역산 — 노브 튜닝이 "감"에서 "보장"으로
- vision_only 과금 정책(#14 후속 결정)에도 근거 제공: conf가 보정된
  집합 크기 1일 때만 과금 등
- **구현 비용이 가장 낮다**: 오프라인 스크립트(아카이브 yaml → 분위수
  계산 → .env 값 제안) 하나면 시작 가능. `tools/`에 두는 것을 권장.

전제: 아카이브에 **정답 라벨**이 있어야 한다 — 실험 프로토콜에 "실제
취출 품목/수량" 기록 필드를 추가할 것 (이슈 본문에 수기로 적던 것의
구조화).

신뢰도: 높음 (방법 보장이 수학적, 필요한 건 라벨 데이터뿐)

---

## 7. 개방형 상품 인식 — class_id 고정의 탈피

### 논문

- **RPC** — [Wei et al., "RPC: A Large-Scale Retail Product Checkout
  Dataset", arXiv:1901.07249](https://arxiv.org/abs/1901.07249): 200 SKU,
  30K 체크아웃 장면. 단품 이미지로만 학습하는 cross-domain 설정 —
  CRK의 "진열 학습 → 손에 든 인식" 도메인 갭과 동형.
- **RP2K** — [Peng et al., arXiv:2006.12634](https://arxiv.org/pdf/2006.12634):
  2,388 SKU 실매장 이미지 50만 장 — 세밀 분류 사전학습 소스.
- **RetailKLIP** — [Finetuning OpenCLIP for zero-shot retail product
  classification, arXiv:2312.10282](https://arxiv.org/html/2312.10282v1):
  임베딩 매칭으로 **재학습 없이 신규 SKU** 인식.

### 적용 방향

#15의 근본 취약점 하나는 **YOLO class_id ↔ 상품 DB 매핑이 정적**이라는
것이다(미매핑 → 후보 무성 소멸, `vision_top_not_billed`로 관측만 가능).
2단 구조로 바꾸면: ① 검출기는 "상품임"만 검출(class-agnostic) → ② crop
임베딩을 상품 DB의 참조 임베딩과 매칭해 정체성 부여.

- 신상품 입고 = 참조 사진 몇 장 등록 (엔진 재변환·재학습 불필요 —
  현재 NumPy2/half 변환 이슈 계열 부담도 소멸)
- 매핑 드리프트가 구조적으로 사라짐 (매칭 스코어가 곧 매핑)
- RP2K/RPC 사전학습 + 현장 fine-tune으로 시작 가능

**難: 높음 (파이프라인 대수술 + Jetson 임베딩 추론 비용). 분기적 결정이
필요한 로드맵 항목 — 단기 과제들과 독립적으로 검토.**

신뢰도: 중간~높음 (업계 표준 방향이나 엣지 비용 실측 필요)

---

## 8. 엣지 추론 최적화 (보조)

- [Review of large YOLOv8 and RT-DETR energy efficiency on edge devices,
  Scientific Reports 2026](https://www.nature.com/articles/s41598-026-46453-6):
  Jetson Orin NX에서 TensorRT가 최적 런타임; **RT-DETR은 INT8에서 정확도
  급락 경향, YOLO 계열이 양자화 안정적**.
- [YOLO26 아키텍처·벤치마크, arXiv:2509.25164](https://arxiv.org/html/2509.25164v5)

적용: 현재 half(FP16) 변환 기조 유지가 맞다. 처리시간(~13s/trigger)이
문제면 모델 교체보다 **INT8 보정(양자화 인식 캘리브레이션) + 트랙릿
투표로 프레임당 작업 감소**가 순서. RT-DETR 이행은 근거 부족.

---

## 로드맵 제안

```
단기 (수 주, 낮은 리스크)
  ① BOCPD 분석기 shadow 병행 (§2) — #14 재발 계열 근본 대응
  ② conformal 보정 스크립트 + 아카이브 정답 라벨 필드 (§6)
  ③ planogram prior 페널티 (§1-1) — Edge 협조 필요
중기 (1~2개월)
  ④ ByteTrack 트랙릿 투표 shadow → 전환 (§3)
  ⑤ (④ 이후) 접촉 상태 키프레임 검증 (§4)
장기 (분기)
  ⑥ 임베딩 기반 개방형 인식 파일럿 (§7)
  ⑦ DS/확률 융합 판정기 검토 (§5) — I-V 테스트 스위트를 게이트로
```

모든 항목은 이 저장소의 확립된 패턴 — **shadow 병행 → 아카이브 실측 비교
→ 승격** — 을 따를 것을 전제로 한다.

## 한계

- 검색은 영문 문헌 위주 (2026-07 시점). 각 논문의 세부 수치는 원문 확인 필요.
- §3·§4의 효과 추정은 코드 구조 분석 기반이며 실측 검증 전 단계.
