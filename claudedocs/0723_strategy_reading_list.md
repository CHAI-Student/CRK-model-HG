# 전략 수립용 독서 목록 — 0722 리서치 이후 확장판 (2026-07-23)

`claudedocs/research_judgment_performance_20260722.md`가 "지금 코드에 바로
적용할 기법"(AIM3S/Grab, BOCPD, ByteTrack, conformal …)이었다면, 이 목록은
**사람이 읽고 판단 프레임을 얻기 위한** 논문·책이다. 선정 기준: 우리가 겪는
사고들(오과금/미과금, 경계 코인플립, 노브 누적, shadow 승격 판단)이 어느
학문 전통의 "이미 풀린 문제"에 대응하는지 — 그 원전과 최신 종설.

각 항목: **무엇** / **왜 우리 상황인가** / **어떻게 읽나**.

---

## 1. 과금 보류(fail-closed)의 이론 — 거부 옵션(reject option)

- **Chow (1970), "On Optimum Recognition Error and Reject Tradeoff"**, IEEE
  Trans. Information Theory.
- **Hendrickx, Perini, Van der Plas, Meert, Davis (2024), "Machine learning
  with a reject option: a survey"**, Machine Learning.
  [ResearchGate](https://www.researchgate.net/publication/379410216_Machine_learning_with_a_reject_option_a_survey) ·
  관련: [uncertainty/reject 종설 (arXiv 2304.04906)](https://arxiv.org/pdf/2304.04906)

**왜**: 우리의 `NO_DETECTION`(weight_only_ambiguous, 모호→불발, PARTIAL 강등)
은 전부 "거부 옵션이 있는 분류"다. Chow는 **오류 비용과 거부 비용이 주어지면
최적 거부 임계가 닫힌 형태로 나온다**는 것을 1970년에 증명했다 — "과청구가
미청구보다 나쁘다(I13/D9)"를 감으로 정한 임계가 아니라 비용 비율에서 역산할
수 있다는 뜻이다. 2024 종설은 모호성 거부(ambiguity, 우리의 "적합 2개=모호")
vs 신규성 거부(novelty, 미매핑 상품)를 구분하는데, 이 구분이 우리 라우터의
불발 사유 분류와 정확히 겹친다.

**어떻게**: 종설의 분류 체계(§2-3)와 비용 정식화만 읽어도 충분. Chow 원문은
짧으니 결론 수식(오류-거부 트레이드오프 곡선)만 확인. 적용 질문: "오과금
1건의 비용 : 미과금 1건의 비용을 몇 대 몇으로 볼 것인가"를 명시하면 지금
흩어져 있는 임계들이 하나의 비용 비율로 정리되는가?

## 2. 프레임 투표·조기 종료의 원전 — 순차 검정

- **Wald (1947), *Sequential Analysis*** (책, 고전). 핵심은 SPRT(순차 확률비
  검정): 증거를 하나씩 보며 로그 우도비가 상/하 경계를 넘으면 즉시 결정,
  아니면 계속 수집.

**왜**: 우리 파이프라인의 프레임별 투표 + EarlyTerminator(L2)는 사실상
수제 SPRT다. Wald의 결과는 **주어진 오판률(α, β)에서 SPRT가 평균 표본 수를
최소화한다**는 것 — "몇 프레임을 보고 멈출 것인가"의 경계를 오판률 목표에서
역산할 수 있다. 조기 종료 임계와 투표 수렴 조건(D7)을 튜닝이 아니라 보장으로
바꾸는 관점.

**어떻게**: 책 전체는 무겁다. 아무 수리통계 교재의 SPRT 장(또는 위키
수준)으로 A/B 경계 공식만 이해한 뒤, "우리 vote 우도비를 어떻게 정의할까"를
생각하며 EarlyTerminator를 다시 보면 된다.

## 3. 베이지안 융합·필터링의 기본기 (책 2권)

- **Thrun, Burgard, Fox, *Probabilistic Robotics*** (MIT Press, 2005; 한국어판
  『확률론적 로보틱스』). 베이즈 필터, 센서 모델, 데이터 연관, 실패 복구.
- **Särkkä, *Bayesian Filtering and Smoothing*** (Cambridge, 무료 PDF 공개).
  칼만/입자 필터의 정갈한 수학.

**왜**: "로드셀은 노이즈 있는 관측, 비전은 우도, 판정은 사후확률"이라는
프레임 자체가 로보틱스의 상태 추정 문법이다. 우리가 0722 설계에서 도입한
우도비 상한(clamp) 같은 장치를 임기응변이 아니라 표준 도구(관측 모델의
outlier-robust화, gating)로 다시 보게 해 준다. 특히 Probabilistic Robotics의
**데이터 연관(어떤 관측이 어떤 개체에서 왔는가)** 장은 멀티트레이 표 분할
문제의 교과서 버전이다.

**어떻게**: Thrun 1-3장(베이즈 필터)과 데이터 연관 장만. Särkkä는 4-7장
(칼만 계열)을 BOCPD 승격 판단 전에 훑으면 "왜 σ_d를 추정치로 넘기는 게
중요한가"가 체감된다.

## 4. 멀티트레이·트랙 귀속 — 다중 표적 추적의 데이터 연관

- **Bar-Shalom & Li, *Estimation with Applications to Tracking and
  Navigation*** (책) — JPDA(결합 확률 데이터 연관).
- **Reid (1979), "An Algorithm for Tracking Multiple Targets"** — MHT(다중
  가설 추적)의 원전.

**왜**: 동시 2트레이 취출에서 "표가 어느 트레이 사건에 속하는가"(이슈 #16),
트랙릿 투표의 진열/취출 인스턴스 구분 — 전부 다중 표적 추적의 **데이터
연관 문제**다. 우리 `_pool_exhaustion_retry`(형제가 소진한 정체성 제거 후
재판정)는 MHT의 가설 가지치기를 1-스텝으로 근사한 것과 같다. 이 문헌은
"연관이 모호할 때 하드 배정 대신 확률 가중(JPDA)" 또는 "복수 가설 유지 후
나중 증거로 해소(MHT)"라는 두 표준 해법을 준다 — 다음 사고가 어느 유형인지
진단하는 어휘로서 가치가 크다.

**어떻게**: JPDA/MHT 개념 장만 (구현 수식은 불필요). 적용 질문: 트레이별
이벤트↔비전 트랙의 배정을 지금처럼 순차 확정하지 않고 작은 배정 문제
(비용 = score)로 한 번에 푸는 게 Phase 3에서 가능한가?

## 5. 변화점 검출의 지형 — BOCPD의 대안까지

- **Truong, Oudre, Vayatis (2020), "Selective review of offline change point
  detection methods"**, Signal Processing 167.
  [arXiv:1801.00718](https://arxiv.org/abs/1801.00718) ·
  [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0165168419303494)
  — 140여 편을 비용함수 × 탐색법 × 변화 수 제약의 3축으로 정리, 파이썬
  패키지 `ruptures` 동봉.

**왜**: 우리는 BOCPD(온라인)를 이식했지만, 트리거 판정은 사실 **사후
일괄(offline) 분석**이다 — 영상·로드셀이 다 도착한 뒤 구간화한다. offline
계열(PELT 등 동적계획법 기반 정확해)이 5샘플 창에서 BOCPD보다 나을 수 있고,
이 종설은 그 선택지를 전부 보여준다. BOCPD 승격 실측에서 애매한 결과가
나오면 여기가 다음 후보 풀이다.

**어떻게**: §2(문제 정식화)와 표 위주로. `ruptures`로 아카이브의 로드셀
시계열을 몇 개 돌려 보는 실험이 반나절 거리.

## 6. 접촉 하중·크리프는 산업 계량의 고전 문제 — 체크웨이어 문헌

- **Halimic & Balachandran, "Kalman filter for dynamic weighing system"**
  ([Semantic Scholar](https://www.semanticscholar.org/paper/Kalman-filter-for-dynamic-weighing-system-Halimic-Balachandran/098a80ca3039d058e6996c2a8948d8235971cdb3))
- **"Adaptive filtering approach to dynamic weighing: a checkweigher case
  study"** ([ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1474667016425395))
- **"Dynamic mass measurement in checkweighers using a discrete time-variant
  low-pass filter"** ([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0888327014000715))
- **"Model-based dynamic compensation of load cell response … environmental
  vibrations"** ([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0888327012002737))

**왜**: 우리의 접촉 하중 오염(눌림 8~32g), 정착 전 판독(creep), 컴프레서
진동은 **체크웨이어(컨베이어 위 동적 계량) 산업이 수십 년 다뤄 온 바로 그
문제**다. 핵심 아이디어 — 로드셀의 스텝 응답을 모델링해 정착 전 신호에서
참값을 역산한다(정착을 기다리지 않는다) — 는 이슈 #14(빠른 취출)의 근본
해법 방향이기도 하다. 5g 양자화·0.8s 캐던스라는 우리 제약에서 어디까지
가능한지 감을 준다.

**어떻게**: 초록+그림 위주로 2-3편. 적용 질문: IO-BOARD가 원신호(양자화
전)를 줄 수 있다면 스텝 응답 모델 기반 delta 추정이 BOCPD보다 몇 배 빠른
확정을 주는가? (하드웨어 협조가 필요하므로 로드맵 항목으로만.)

## 7. 비용 비대칭의 정식화 — 비용 민감 학습

- **Elkan (2001), "The Foundations of Cost-Sensitive Learning"**, IJCAI.

**왜**: 짧고(6쪽) 밀도 높은 고전. **비용 행렬이 주어지면 최적 결정 임계가
어떻게 이동하는지**의 기본 정리를 준다. 우리 시스템의 모든 경계(gate,
share, conf_margin)는 사실 "오과금 비용 > 미과금 비용"이라는 비대칭의
간접 표현인데, Elkan을 읽으면 그 비대칭을 임계 하나하나에 심는 대신 결정
규칙의 마지막 단계 한 곳에 두는 설계가 보인다. §1(거부 옵션)과 세트로.

## 8. shadow → 실측 → 승격을 방법론으로 — 실험 문화와 ML 시스템 부채

- **Kohavi, Tang, Xu, *Trustworthy Online Controlled Experiments*** (Cambridge
  2020; 한국어판 있음). 실험 설계, 가드레일 지표, 점진 롤아웃, 결과 해석의
  함정(다중 비교, 신기함 효과).
- **Sculley et al. (2015), "Hidden Technical Debt in Machine Learning
  Systems"**, NeurIPS. ML 시스템 특유의 부채: 경계/휴리스틱의 누적, 파이프
  라인 정글, 설정 부채, 피드백 루프.

**왜**: 우리 레포의 관행(shadow 병행 → 아카이브 실측 → env 승격)은 이 책이
말하는 통제 실험의 축소판이다 — 책은 그 관행에 **표본 수 판단, 가드레일
지표(우리라면 오과금률), 승격 기준의 사전 등록** 같은 규율을 더해 준다.
Sculley 논문은 우리가 0722 설계 문서에 쓴 문장("경계는 늘어날수록 상호작용이
어려워진다")의 학술 버전 — 지금의 노브 20여 개가 어떤 부채 패턴인지, 언제
갚아야 하는지(확률화 Phase 3 = 부채 상환)의 언어를 준다.

**어떻게**: Kohavi는 1부(기초)와 가드레일/램프업 장. Sculley는 전문 9쪽 전부.

---

## 우선순위 제안 (지금 국면 기준)

| 순서 | 항목 | 이유 |
| --- | --- | --- |
| 1 | §1 거부 옵션 (Chow + 2024 종설) | 오늘의 fail-closed 임계들을 비용 하나로 통일하는 관점 — 즉시 설계에 반영 가능 |
| 2 | §8 Kohavi + Sculley | 내일부터의 shadow 승격 판단에 바로 쓰는 규율 |
| 3 | §5 변화점 종설 | BOCPD 실측이 애매할 때의 대안 지도 |
| 4 | §4 데이터 연관 | 멀티트레이 사고의 다음 세대 해법 어휘 |
| 5 | §3 책 2권 | 장기 기본기 — Phase 3(확률화 통합)의 수학적 토대 |
| 6 | §2, §6, §7 | 각론 — 해당 사고가 다시 터질 때 |
