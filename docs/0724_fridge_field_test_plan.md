# 2026-07-24 냉장고 필드 테스트 플랜 — 냉동 검증 모델의 냉장 실기 탑재

> 냉동 실기 전용으로 검증해 온 모델을 냉장고 실기에 올린다. 판정·정산 코어는
> SensorProfile 파라미터 포크(`core/profiles.py`)로 냉장을 1급 지원하며, 탑재 전
> 대청소(잔재 삭제·승격 확정·env 이원화)는 완료됐다 (`docs/fix_logs.md:1283`).
> 냉동판 전례는 `docs/0723_field_test_plan.md`(완료), shadow 승격/폐기 판단 기준은
> `docs/0724_shadow_status_review.md` 참조.

## 0. 사전 준비 (Jetson)

```bash
cd ~/Desktop/Codes/CRK-model-HG   # 실제 경로에 맞게
git pull origin master
source .venv/bin/activate
uv pip install --no-deps -e .     # label-session / analyze-sessions 엔트리포인트
cp .env.example .env              # ★ 냉장고 기본 템플릿 (냉동은 freezer.env.example)
model-service-hg
```

체크리스트:

- **`.env.example`이 냉장 기본 템플릿**이다 — `CABINET_TYPE=refrigerated`(.env.example:25),
  `CAMERA_LAYOUT=dual`(.env.example:31, `dual_top_proxy` 설정 금지 — side x-ROI가 꺼지고
  수직 ROI 전제가 깨진다), `TOP_ROI_ENABLED=1`(.env.example:94)이 기본값.
- YOLO 엔진 경로 확인 — 레포에 없음, 기존 `.engine` 복사 (`MODEL__VISION__YOLO_MODEL_PATH`).
  기동 시 startup probe가 1회 추론으로 검증한다 (`adapters/serve.py:70`) — 실패하면 기동 자체가 실패.
- 세션 아카이브 활성 확인: `MODEL__SESSION__ARCHIVE_DIR=data/sessions`(.env.example:268)
  — 이 플랜의 모든 실측이 아카이브 YAML + 라벨에 걸려 있다.
- 기동 로그 `[CONFIG] cabinet_type=refrigerated default_profile=refrigerator freezer_zones=() camera_layout=dual` 확인 (`model_service.py:108`).
- **상품 DB unit_weight 실측 재등록 확인** — 냉장 판정은 ±5g(REFRIGERATOR
  tolerance, `core/profiles.py:52`)라 DB 공칭무게와 실측 총중량 편차에 냉동(±15g)보다
  훨씬 민감하다. 편차 13~27g으로 정답이 구조적으로 매칭 불가였던 실사고가 있다
  (이슈 #6 ③, `docs/fix_logs.md:195`).
- **냉동과 달리 조기 종료(EARLY_TERMINATION)가 실동작한다** — removal & 비freezer
  한정 코드 경로라 냉동 실기(I15로 항상 비활성)에서는 한 번도 검증된 적 없다
  (.env.example:167-171, `core/profiles.py` early_termination_allowed). §1 P1-④에서 A/B.

## 1. 검증 우선순위

### P0-① top ROI — 진열 오투표 방어 1순위

냉장은 top 공용 카메라 1대가 5존 진열을 넓게 본다 — top ROI 부재가 진열 오투표의
최대 원인이었다 (원본 perf-gap 분석 §5, `ref/present/model-perf-gap-report.md:154`).
원본은 기본 ON이었고 냉장 템플릿도 기본 ON: 트리거 delta ≠ 0일 때 top 카메라의
하단 절반(center_y ≥ `TOP_ROI_Y_SPLIT`)만 유지한다 (.env.example:89-95).

`TOP_ROI_Y_SPLIT=240`은 원본 운영값 — **실기 재측정 대상**. 절차:

1. 기본값(240)으로 한 블록 실험. top ROI 드랍은 `vote_summary.filter_drops_by_stage.vertical_roi`에
   계수된다 (top ROI는 vertical_roi 스테이지에 합산 — `perception/filters.py:98`).
2. **과잉 제거**(정답 상품이 깎임): analyze-sessions의 "정답 상품이 최종 후보에 없던
   트리거" 경고로 잡힌다 → Y_SPLIT을 낮춰 유지 영역을 위로 확대.
3. **진열 오투표 잔존**(하단 진열이 여전히 1위): Y_SPLIT을 올려 취출구 근접 영역만 남긴다.
4. 판정 불능 수준이면 `TOP_ROI_ENABLED=0` 롤백 후 모션 변위 증거에만 의존.

### P0-② strict 무게-우선 판정 정확도 — 기본 시나리오

냉장 존은 무게가 정체성 판별자(±5g, weight_is_discriminative=True)라 strict 중심
체인이 판정을 담당한다 — FreezerVisionFirst·우도 shadow 등 냉동 전용 로직은
precondition에서 스스로 꺼진다 (.env.example:10-12). 단일/복수 취출 기본 시나리오
(§2 표 S1~S3)에서 과금 정오를 먼저 확보한다. 체인 순서는 `judgment/router.py:37-68`
(strict → same_product_count → relaxed 계열 → forced_final).

### P1-③ side ROI 경계 재측정 — center-crop 좌표계

2026-07-24 입력 기하가 left-crop → center-crop으로 전환되어 가로축 크롭 원점이
80px 이동했다 (`docs/fix_logs.md:1194`). 현행 `SIDE_ROI_MAX_CENTER_X=400`은 옛
left-crop 좌표계 값 그대로라 **물리적으로는 옛 경계보다 오른쪽 80px 지점**이다 —
옛 경계와 같은 위치를 원하면 320 (.env.example:81-87).

- 실측: `vote_summary.filter_drops_by_stage.side_roi` — 존 바깥 진열 검출이 안
  잘리면 값을 내리고(→320 방향), 정답 상품이 잘리면(정답 후보 부재 경고) 올린다.
- 함께 감시: `hand_path` 드랍 — 11차 냉동 배치에서 트리거당 551·667로 급증했고
  center-crop 전환과의 상관이 의심 상태다 (`docs/fix_logs.md:1273-1276`).

### P1-④ EARLY_TERMINATION A/B (0 vs 1)

delta가 설명되는 즉시 추론을 중단하므로, **다품목 동시 취출에서 2번째 상품 증거
수집 전 중단할 위험**이 지적돼 있다 (perf-gap §8.5, `ref/present/model-perf-gap-report.md:225-227`).
같은 시나리오 블록(§2 S3)을 `MODEL__VISION__EARLY_TERMINATION=0`(전 프레임 추론)과
`1`로 반복해 비교:

- 2번째 상품의 득표 수(vote_summary.classes)와 과금 정오 diff
- `trace.early_terminated` / reason_codes의 `early_terminated` (`service/pipeline.py:648-650`)
- 1이 0 대비 과금 오답을 만들면 0 고정(처리 시간 증가 감수), 동등하면 1 유지.

### P1-⑤ BOCPD delta 회귀 감시

BOCPD는 냉동 실측(63관측/2 mismatch → 이후 17건 mismatch 0)으로 정식 승격됐고
shadow 관측 장치는 삭제됐다 (.env.example:122-126, `docs/fix_logs.md:1283`). **냉장
로드셀 노이즈 환경은 미검증** — 회귀는 이제 delta 정오로 직접 감시한다:

- delta 0(`insufficient_stable_regions`) 빈발, 과금 오답 세션의 delta-세그먼트 불일치
- 회귀 시 `MODEL__LOADCELL__ANALYZER=plateau` 롤백 (계약 동형이라 코드 무변경).

### P2-⑥ cross-zone penalty 발동 관측

Phase 3 승격(기본 ON)이지만 냉장은 무게 게이트가 좁아(±5g vs ±15g) 타 존 상품의
우연 적합 자체가 드물어 발동 빈도가 낮을 것이다 (.env.example:204-206 — 안전 방향).
발동/오발동만 관측: zones[].notes의 `cross_zone_*` 계열
(`ledger/cross_zone.py:325,334` — source_low_conf/mutual_exempt 진단 포함).

### P2-⑦ ghost shadow 오플래그율

11차 냉동 배치에서 정답 오플래그 3/3 세션 → 에피소드 중복 제거로 주 원인 수정,
재관측 중이다 (`docs/fix_logs.md:1266-1272`). analyze-sessions "고스트 shadow" 섹션의
정답 클래스 오플래그(⚠ 표시)가 0으로 유지되는지 — 냉장은 top 공용 1대가 5존을
봐서 다존 표 노출이 구조적으로 클 수 있어 재평가 가치가 높다.

### P2-⑧ held/tube/recovery shadow 게이트 재평가

냉동 판정: held 정답 오플래그 10차 5건·11차 3건(보류), tube 0:3:2 현행 우세(보류),
recovery는 산탄 증폭의 부정 증거만 (`docs/fix_logs.md:1102,1254`). 냉장 데이터로
재측정 — 항목별 판단 기준과 처분 제안은 `docs/0724_shadow_status_review.md` §4.

## 2. 시나리오 표

| # | 시나리오 | 기대 판정 | 확인할 아카이브 필드 |
|---|---|---|---|
| S1 | 단일 취출 (1존 1품 1개) | COMPLETE, reason `strict` | `judgment.reason`, vote_summary.classes 1위 = 정답 |
| S2 | 동일존 동일 상품 n개 | COMPLETE ×n, reason `strict` 또는 `same_product_count` | delta ≈ n×unit_weight, `judgment.products[].count` |
| S3 | 동시 다품종 (같은 존 2종 동시) | COMPLETE 2종, reason `strict`(조합) | 2번째 상품 득표 존재 여부 — P1-④ A/B 연계, `trace.early_terminated` |
| S4 | 반품 take-return (취출 후 같은 존 반납) | 최종 0원 (settler 1층 동존 무게 매칭, `ledger/settler.py:117`) | zones[].products 비어 있음, notes에 `unmatched_return` 없어야 (`ledger/settler.py:338`) |
| S5 | 교차존 이동 (존A 취출 후 존B 진입·취출) | 존A·존B 각각 정답 과금, 존B에 존A 상품 미과금 | 존B vote_summary에 존A 클래스 표 유입 여부, notes `cross_zone_*` — P2-⑥ |
| S6 | 저무게 상품 (delta < 5g) | 미과금, reason `below_min_weight_change` (`judgment/strategies.py:641`) | 5g 양자화에서 delta가 0으로 뭉개지는지 — 해당 상품 취급 가능성 판단 |
| S7 | 무게 유사 상품 쌍 (±5g 충돌) | vision이 판별 — reason `same_weight_collision_guard`(`judgment/strategies.py:648`) 또는 strict, 모호 시 fail-closed | 두 후보의 votes/conf 격차, 오답이면 `weight_only_ambiguous`류 억제가 동작했는지 |
| S8 | 무취출 (문만 열고 닫음) | 청구 0 (`complete_no_products`) | `label-session --none`으로 라벨 (`docs/fix_logs.md:1120-1122`) — 과금 정오에 반영 |

S1~S3이 P0-②의 본체다. 각 시나리오는 top ROI(P0-①) 기본 ON 상태에서 수행하고,
과잉 제거 의심 시에만 OFF 대조군을 만든다.

## 3. 실험 프로토콜 (세션마다)

1. **배포 직후 시각을 기록**해 둔다 — 리포트는 항상 `--since <그 시각>`으로 돌려
   구 코드 세션의 집계 오염을 막는다 (`docs/fix_logs.md:1062`).
2. 취출 실험 수행 (§2 시나리오).
3. **매 세션 직후 정답 라벨**:
   ```bash
   label-session --latest --zone 2 --take 27x1 --note "S1 단일"
   label-session --latest --none                      # S8 무취출
   ```
4. 블록이 끝나면:
   ```bash
   analyze-sessions --since 2026-07-24T18:00
   analyze-sessions --session <id>       # 오답 세션 상세 덤프 (--full로 원자료)
   ```

### 리포트 섹션 읽는 법 (`adapters/analyze_cli.py` render)

| 섹션 | 보는 것 |
|---|---|
| 과금 정오 | 정답 n/전체 + 오답 세션의 (과금 ← 정답) diff — 최우선 지표 |
| 무게 우도 shadow | **냉동 존 전용** — 순냉장 기기에서는 관측 0이 정상 (.env.example:150-152) |
| tray prior shadow | 위와 동일 (log_p_tray 소비뿐) |
| 트랙릿 T1 | held 강등 관측(정답 플래그 ⚠ = 승격 보류 신호), 튜브 shadow 라벨 정오, 단절 지표 |
| 고스트 shadow | 정답 클래스 오플래그 ⚠ — P2-⑦ |
| conformal 보정 | 정답 상품 votes/ratio/share/conf의 p5 — 채택 임계(MIN_VOTE_*)는 p5 이하로 |
| σ_db 실측 | 냉장에서는 참고만 (LIKELIHOOD_SIGMA_DB는 냉동 존 전용 입력) |

### 승격 게이트 기준

- held/ghost 계열: **정답 클래스 오플래그 0** 배치가 확인될 때까지 shadow 유지
  (analyze_cli.py:747,806-810이 승격 가능 문구를 직접 출력).
- tube/recovery 계열: "shadow만 정답 > 현행만 정답" 우세 지속 시 active, 현행 우세
  지속 시 폐기 (analyze_cli.py:762-765).
- 승격은 env 한 줄, 코드 무변경 (`HELD_TRACK_DEMOTION=active` 등).

### 튜닝 반영 순서

한 번에 한 층만 바꾸고 `--since`로 구획한다:

1. **지각**: 진입 컷(0.50에서 시작, 후보 안 잡히면 0.35까지 — .env.example:45-49),
   ROI 경계(P0-①·P1-③), 모션 변위
2. **채택 임계**: conformal p5 기반 MIN_VOTE_RATIO/COUNT/SHARE
3. **판정·정산 노브**: strict 실패 패턴이 남을 때만
4. **shadow 승격**: 위가 안정된 뒤 (게이트 기준 충족 시)

## 4. 롤백 스위치

| 증상 | 롤백 |
|---|---|
| BOCPD delta 회귀 (P1-⑤) | `MODEL__LOADCELL__ANALYZER=plateau` |
| 진짜 상품이 `no_motion`으로 몰수 | `MODEL__VISION__MOTION_EVIDENCE=0` |
| cross-zone 오강등 | `MODEL__CROSS_ZONE__PENALTY_ENABLED=0` |
| ghost 오동작 (active 승격 이후) | `MODEL__GHOST__MODE=off` |
| close 조합 중재 오조합 | `MODEL__CLOSE__VISION_COMBO=0` |
| top ROI 과잉 제거 (P0-①) | `MODEL__VISION__TOP_ROI_ENABLED=0` |
| 다품목 증거 소실 (P1-④) | `MODEL__VISION__EARLY_TERMINATION=0` |

전부 env 한 줄 재기동 — 코드 롤백이 필요한 항목은 없다.
