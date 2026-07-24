# 원본 CRK-model 대비 개선 사항

> 2026-07-24 작성. 비교 기준: 원본 CRK-model d104bca(2026-07-03) vs HG master
> (냉장 탑재 대청소 시점, `docs/fix_logs.md:1283`). 원본→HG 방향의 성능 격차
> 분석은 `ref/present/model-perf-gap-report.md`(2026-07-22) — 이 문서는 반대
> 방향(HG가 원본보다 나은 지점, 보고서 부록 B의 확장)과, 그 보고서가 지적한
> 격차 중 **이후 해소된 항목의 이력**(§5)을 정리한다. 각 항목에 실사고/실측
> 근거 1줄씩 붙인다.

## 1. 구조적 개선 (아키텍처)

| 항목 | 원본 | HG | 실사고/실측 근거 |
|---|---|---|---|
| CLOSE 확정 — 인과 배리어 (I17) | 3s/1s 시간 debounce | 트리거 도착 인과 배리어 + edge watermark(`expected_triggers`) + 유예 3s — perf-gap 부록 A가 "HG 우위"로 명시 | issue #8: CLOSE가 업로드보다 0.66s 빨라 0원 확정·7,400원 누락 → 유예+워터마크로 재발 차단 (`docs/fix_logs.md:479,511`) |
| Interim/Finalized 타입 분리 (I10) | 없음 | 확정 타입만 결제 페이로드로 통과 — 잠정 집계가 과금에 섞일 수 없는 타입 강제 | 결제 wire 재작성 때도 I10이 불변식으로 유지됨 (`docs/fix_logs.md:450`) |
| 멱등 정산 캐시 (I11) | wire 반복 전달 의존 | settler 세션 키 멱등 캐시가 이중 과금 불가를 보장 (`test_settlement_idempotent_at_settler_layer`) | issue #5 계열 3차 정정에서 wire와 분리해 테스트로 고정 (`docs/fix_logs.md:379-411`) |
| fail-closed 관행 | 부분적 | cabinet_type 오타 → ValueError 기동 거부, 에러 세션 `block_payment`(D9/I13), weight_only 모호 → NO_DETECTION | 냉동 기기가 냉장 프로파일로 조용히 동작한 실사고(이슈 #6)의 재발 방지 (`docs/fix_logs.md:235,195`) |
| 스트리밍 디코드 (OOM 방지) | 전체 프레임 리스트 상주 | 제너레이터 스트리밍(프레임 1장 상주) + hwaccel 실사용 프로브·CPU 폴백 | 카메라당 ~276MB 상주는 4GB Jetson OOM 위험 (`docs/fix_logs.md:98-108`); hwaccel 오판정으로 디코드 전체가 죽던 결함 근본 수정 (`docs/fix_logs.md:914`) |
| fail-fast startup probe | 없음 | 기동 시 detector 1회 추론 — 엔진 문제를 첫 트리거가 아니라 기동에서 잡음 (`adapters/serve.py:70`, `service/model_service.py:10`) | 이관 리뷰 #1 반영 |
| class_id=-1 센티널 | camelCase alias·이름 매핑 | 매핑 실패 시 0(hand)이 아닌 -1 — hand 오청구 구조적 차단 | 이슈 #6: 전 상품 class_id=0 붕괴로 vision 매칭 전멸·weight_only 오과금 (`docs/fix_logs.md:195`) |
| SensorProfile 파라미터 포크 | freezer 분기 코드 산재 | tolerance/게이트/조기 종료/모션 임계 전부 프로파일 소속 — 냉장·냉동 겸용 단일 코드 (`core/profiles.py`) | 이번 냉장 탑재가 코드 수정 0, env 전환만으로 성립하는 근거 (`docs/fix_logs.md:1283`) |

## 2. 판정 개선

| 항목 | 내용 | 실사고/실측 근거 |
|---|---|---|
| min_vote_share (HG 고유, 이슈 #10) | 1위 득표 대비 상대 하한 — 절대 COUNT는 400프레임+ 영상에서 노이즈도 통과시킨다 | 8표(1위의 4%) filler 후보가 무게만 맞아 채택된 사고(비비고 → 메로나 79g×3) 차단 (.env.example:59-62) |
| 무게 중재 재설계 (이슈 #16, `docs/0722_issue16_arbitration_design.md`) | gate_n(n)=gate+slack×(n−1), 선착 폐지 + 득표·conf 중재, conf_override 자격 | 실사고 C: 잔차 3g 우연 적합이 잔차 32g 정답을 선착으로 이겨 베이글 5개 → 만두 4개 오과금 (`docs/fix_logs.md:655`) |
| I-V 원칙 (무게=거부권) | 무게는 개수 검증·반증만, 정체성 선택 금지 — freezer weight_only 억제 내장 (`docs/baseline_and_judgment_iv.md`) | 원본 다이어그램 대조에서 freezer loadcell-only 정체성 판정 누락 발견·차단 (`docs/fix_logs.md:143`) |
| partial 청구 최소 conf (0.18) | 무게 미검증 count=1 청구의 conf 하한 — 원본 multi_kind_min_confidence 동형 | 실기: 5표/청구 conf 0.157짜리 상품을 잔차 65g인데 과금 → NO_DETECTION으로 교정 (`docs/fix_logs.md:964`) |
| refit 중재 conf floor (0.8) | 복수 적합 refit 중재의 절대 하한 — margin 우세만으로는 "덜 흐린 유령"이 이긴다 | 4차 ses-1: conf 0.69 유령 채택 오과금 재현 차단 (`docs/fix_logs.md:1033`) |

## 3. 세션-스코프 신규 능력 (원본에 없음)

트리거 단위 판정만 있는 원본과 달리, HG는 세션(문 열림~닫힘) 스코프의 오염원을
CLOSE 2차 패스에서 다룬다 — 이슈 #17 오답 34건 전수 분석의 결론("잔존 실패는
트리거 내 표 정제가 아니라 세션 스코프 오염원과 융합 중재 규칙에 있다",
`docs/fix_logs.md:1142`)이 이 계층의 존재 이유다.

| 항목 | 내용 | 실사고/실측 근거 |
|---|---|---|
| cross-zone 오염 페널티 | 타 존 취출 장면의 AVI 오염을 soft 페널티로 보정 — Phase 3 승격, 기본 ON (`docs/cross_zone_penalty.md`) | 4차 ses-6: zone4 오판(13 partial)을 CLOSE에서 3×1로 교정 — 첫 실기 성공 (`docs/fix_logs.md:1033`) |
| 세션 고스트 원장 (shadow) | 다존 자격 표 + 세션 내 무게 뒷받침 0인 클래스 강등 (`ledger/ghost_ledger.py`) | 옷 프린트 유령표(c13/c24) 15건 — 트리거 안에서는 진짜와 구분 불가, 세션 스코프 신호 (`docs/fix_logs.md:1142`) |
| close 비전 조합 중재 | 단일 종 ×N 스냅·게이트 실패 시 자격 표 2종 조합 우선 | 3+44 → 44×4 무게 정수배 스냅 7회 재발 대응, 11차 가드 정정 후 2종 인식 성공 (`docs/fix_logs.md:1142,1254`) |
| 세션 트레이 메모리 | (zone, channel) 키 소프트 prior — 세션 OPEN마다 리셋 | 정적 planogram(운영 입력, **금지 제약**)과 달리 운영 입력 0인 세션-스코프 대안 (.env.example:157-162) |
| BOCPD 변화점 분석기 | run-length 사후분포로 "안정 구간" 재정의 — plateau의 3연속 std 창이 놓치는 빠른 취출 해소 | #14 무음 0원: plateau delta 0일 때 BOCPD는 −297.5±2.6 채널 분해까지 정확. 63관측/2 mismatch → 17건 0으로 정식 승격 (freezer.env.example:127-134, `docs/fix_logs.md:1106`) |

## 4. 관측성 · 운영

| 항목 | 내용 | 실사고/실측 근거 |
|---|---|---|
| 세션 YAML 아카이브 + vote_summary | 클래스별 votes/탈락 사유(rejected_by)/단계별 필터 드랍/진입 탈락을 세션마다 기록 | 이슈 #6 "yolo_calls 300+인데 후보 0"의 원인(conf_floor 평균 희석)을 수기 재현 없이 확정 (`docs/fix_logs.md:414`) |
| label-session / analyze-sessions | 정답 라벨 기입 → 과금 정오·shadow 라벨 대조·conformal 분위수·σ_db를 오프라인 리포트로 (`adapters/analyze_cli.py`) | 수작업이던 mismatch 정오 집계를 자동화 — 실기 배치마다 정답 n/전체가 즉시 나옴 (`docs/fix_logs.md:776,865,943`) |
| shadow-first 승격 문화 | 신규 판정 기제는 관측 전용으로 배포 → 라벨 실측 게이트 통과 후 env로 승격 | 게이트가 잘못된 승격을 실제로 막았다: held 오플래그 5건(10차), ghost 오플래그 3/3(11차) — active였다면 진짜 취출 표 60개를 몰수할 뻔 (`docs/fix_logs.md:1102,1254`) |
| --since / --session 도구 | 코드 버전 혼합 아카이브의 집계 오염 방지, 오답 세션 원클릭 덤프 | 구 코드 세션의 오답이 최신 코드 평가에 섞이던 문제 (`docs/fix_logs.md:1062,955`) |

## 5. perf-gap 격차 해소 이력 (2026-07-22 보고서 § 대비)

보고서 시점의 격차 중 상당수가 이후 이식·전환으로 해소됐다 — 아래는 "격차"가
아니라 **해소 이력**이다. 파라미터 전수 대조는 보고서 부록 A 참조 (부록 A의
HG 열은 7/22 기준이므로 아래 표가 현행 정본).

| 보고서 § | 격차 (7/22 시점) | 현재 상태 | 근거 |
|---|---|---|---|
| §1 입력 기하 | squash resize (가로 25% 왜곡) | **해소** — left-crop 이식(웨이브 1) 후 center-crop 전환 확정 (80ed346). squash는 제거됨. 잔여: side ROI 경계 실측 재조정 (필드테스트 플랜 P1-③) | `docs/fix_logs.md:536,1194` |
| §2 classes 허용목록 | 전 클래스 추론 | **해소** — 카메라별 allowlist 전달 (top=상품+hand, side=상품만, 빈 목록 fail-closed) | `docs/fix_logs.md:557-570` |
| §3 conf 결합 | 진입컷 통과 mean | **해소** — 카메라별 max 결합 (원본 동형), 가중 5종 env 노출 | `docs/fix_logs.md:572-580,1181` |
| §4 rescue 체계 | threshold/roi/no-motion rescue 부재 | **미이식 (의도적 보류)** — 원본 threshold_rescue는 freezer 비활성이라 냉동엔 이식 불가 기록; 냉장용은 fail-closed 철학과의 정합 설계 필요 | `docs/fix_logs.md:999` |
| §5 ROI 체계 | top ROI·냉동 수직 ROI 부재 | **해소** — 웨이브 2에서 이식 (top ROI: delta 연동 하단 유지, 수직 ROI: dual-top 상단 유지). 냉장 템플릿은 top ROI 기본 ON | `docs/fix_logs.md:828`, .env.example:94 |
| §6 모션 증거 | 변위 요구 없음 | **해소+진보** — 원본 변위 필터(클래스 단위) 이식 후 트랙 단위로 승격: 같은 클래스 진열+취출 공존까지 분리 (원본은 불가) | `docs/fix_logs.md:692,727` |
| §7 냉동 판정단 | 원본 6/19~7/3 진화 미반영 | **독자 노선으로 대체** — pool exhaustion retry(이슈 #16), I-V 중재 재설계, close 재solve+조합 중재, 세션-스코프 계층(§3) | `docs/fix_logs.md:595,655,1142` |
| §8-1 감마/콘트라스트 | 부재 | 미이식 (잔여) | 보고서 §10 P0-3 |
| §8-2 freezer 전용 vote 하한 | 부재 | **대체 해소** — env 이원화로 freezer 템플릿의 전역 노브가 겸함 | freezer.env.example:38-44 |
| §8-3 hand conf floor | 부재 | **해소** — 0.30 기본 적용 (원본 운영값 동형) | `docs/fix_logs.md:828-836` |
| §8-4 top_k 캡 | 부재 | 미이식 — min_vote_share가 부분 방어 (보고서도 동일 평가) | 보고서 §8 |
| §8-5 조기 종료 기본 ON | HG만 ON | 유지 — 냉장 실기에서 A/B 실측 예정 (필드테스트 플랜 P1-④) | `docs/0724_fridge_field_test_plan.md` |

## 6. 회귀 방지 목록 (보고서 부록 B 계승)

원본 정합 작업·튜닝 시 깨뜨리면 안 되는 HG 우위 항목: 인과 배리어 확정(I17),
Interim/Finalized 분리(I10), 멱등 정산 캐시(I11), class_id=-1 센티널,
weight_only 유일 매칭 제한, min_vote_share, cross-zone 페널티, 세션 아카이브
vote_summary, 스트리밍 디코드, fail-fast startup probe
(`ref/present/model-perf-gap-report.md:314-320`) — 여기에 본 문서 §3(세션-스코프
계층)과 §4(라벨 실측 도구·shadow-first 게이트)를 추가한다.
