# 2026-07-24 shadow 기능 현황 + 검토 권고

> 냉장 탑재 대청소(`docs/fix_logs.md:1283`)의 판단 근거 정본. shadow-first로
> 배포된 판정 기제들의 승격/은퇴/대기 현황을 냉동 실기 실측(4~11차 배치,
> `docs/fix_logs.md`)에서 인용해 고정하고, 대기군 각각의 냉장 재평가 가치와
> 처분 기준을 명시한다. 냉장 실측 절차는 `docs/0724_fridge_field_test_plan.md`.

## 1. 승격 확정 (기본 ON — shadow 관측 장치 삭제)

| 기능 | env (현행) | 승격 근거 (냉동 실측) | 롤백 |
|---|---|---|---|
| BOCPD 로드셀 분석기 | `MODEL__LOADCELL__ANALYZER=bocpd` | 63관측/2 mismatch → 10차 17건 mismatch 0 (`docs/fix_logs.md:1106`); #14 무음 0원에서 plateau delta 0 vs BOCPD −297.5±2.6 채널 분해 (freezer.env.example:127-134). BOCPD_SHADOW 장치는 삭제 | `=plateau` (계약 동형, 코드 무변경) |
| cross-zone penalty | `MODEL__CROSS_ZONE__PENALTY_ENABLED=1` | Phase 3 승격(2026-07-21). 4차 ses-6 첫 실기 교정 성공 (`docs/fix_logs.md:1038-1040`), 11차 self-fit 가드 포함 정상 동작 (`docs/fix_logs.md:1259`). SHADOW 장치(ledger/shadow.py)는 dead code로 삭제 | `=0` |
| MOTION_EVIDENCE (변위 몰수) | `MODEL__VISION__MOTION_EVIDENCE=1` | 이슈 #16 4차 이식부터 기본 ON — static_track/baseline이 대리 신호로 쫓던 물리("집어간 것만 움직인다")의 직접 검사 (`docs/fix_logs.md:692`) | `=0` |
| close 비전 조합 중재 | `MODEL__CLOSE__VISION_COMBO=1` | 3+44→44×4 스냅 7회 재발 대응(`docs/fix_logs.md:1142`); 11차 ses-1 "count > 증분" 가드 헛다리 정정 후 유지, ses-6에서 2종 인식 성공 (`docs/fix_logs.md:1260-1265`) | `=0` |

## 2. 은퇴 · 삭제 (2026-07-24)

| 기능 | 은퇴 사유 | 근거 |
|---|---|---|
| static_track (정지 트랙 억제) | T3 은퇴 — 변위 몰수(트랙 단위)가 같은 물리를 직접 재면서 흡수. 코드 기본값 24로 살아 있던 미동기화까지 이번에 해소 | `docs/0723_tracklet_cost_benefit.md` §8 T3, `docs/fix_logs.md:1292` |
| baseline (프리롤 배경 억제) | 실기 4건에서 top 무력(프리롤에 이미 손 → 등록창 0) / side 폭주(353~1,735드랍 vs top 0) 판정 — issue #16 퇴역 권고 확정 | `docs/fix_logs.md:695-698`, `docs/baseline_and_judgment_iv.md` 상태 배지 |
| BOCPD_SHADOW 장치 | primary 승격 확정으로 대칭 diff 기록·아카이브 필드·리포트 섹션 삭제. plateau 분석기 자체는 killswitch로 유지 | `docs/fix_logs.md:1307-1315` |
| CROSS_ZONE__SHADOW + ShadowSettlerRunner | Phase 3 승격(기본 ON) 이후 배선 조건이 항상 False인 dead code — `ledger/shadow.py` 파일째 삭제 | `docs/fix_logs.md:1316-1317` |

## 3. 승격 대기 shadow (유지)

| 기능 | env (모드) | 냉동 실측 판정 | 상태 |
|---|---|---|---|
| held T2 (carried-in 트랙 강등) | `HELD_TRACK_DEMOTION=shadow` | 정답 클래스 오플래그 10차 5건(결정타: 진열→취출 전환 트랙 60/61표) → head 이동 요건 추가 후에도 11차 3건 (`docs/fix_logs.md:1107-1111,1279`) | 보류 |
| tube_identity (튜브 다수결 몰수) | `TUBE_IDENTITY=shadow` | 10차 라벨 정오 0:3:2 현행 우세 — 1위 변경 5건 중 3건이 shadow를 c13(옷) 쪽으로 (`docs/fix_logs.md:1114-1119`) | 보류 |
| vote_recovery (저신뢰 표 회수) | `VOTE_RECOVERY=shadow` (FLOOR 0.35) | 13 저신뢰 산탄 증폭 가설 — tube 열세 3건의 유력 원인 (`docs/fix_logs.md:1117`). 긍정 증거 없음 | 보류 (폐기 후보) |
| likelihood + tray_prior (무게 우도 score) | `LIKELIHOOD_SHADOW=1`, `TRAY_PRIOR=1` | 4차 mismatch 정오 3:4:4로 Phase 2 승격 부결 (동일 상품 n개 우연 적합 선호의 구조적 한계, `docs/fix_logs.md:1055-1059`) → 11차 2/1/1 보류 (`docs/fix_logs.md:1280`) | Phase 2 부결·보류 |
| ghost (세션 고스트 원장) | `GHOST__MODE=shadow` | 11차 정답 오플래그 3/3 세션 → 주 원인(에피소드 공유) 수정: detect_ghosts에 에피소드 ≥2 요건. 재관측 중 (`docs/fix_logs.md:1266-1272`) | 재관측 중 |
| track_min_hits / max_gap (probation·소멸) | 기본 0=off | 발동 이력 0 — 단절(실질 트랙/클래스 median 4)이 심각해 fail-closed 스위치를 켤 수 없었다 (`docs/fix_logs.md:1123-1124`) | off 유지 |

## 4. 검토 권고 (대기군 각각)

### held T2

- (a) 냉동에서 못 오른 이유: 진열 상품도 프리롤 0프레임부터 관측되므로
  "진열→취출 전환" 트랙이 head_obs 기준으로 carried-in과 구분되지 않았다
  (10차 ses-6 c40 60/61표 — active였다면 진짜 취출 표 60개 몰수). head 이동
  요건 추가 후에도 take-return 표 홍수 케이스와 오플래그가 공존 (11차 ses-9 z3).
- (b) 냉장 재평가 가치: hold 시나리오(들고 타 존 접근, #17 hold 잔상 5건 계열)는
  캐비닛 무관 — 유효. 단 냉장은 ±5g 무게 판별력이 held 우연 적합을 대부분
  거부하므로 냉동보다 절박성은 낮다.
- (c) 처분: **유지**. 관측 비용이 낮고 실패 모드가 실존한다. 정답 오플래그 0
  배치 확인 전 active 금지 (analyze-sessions ⚠ 신호).

### tube_identity

- (a) 냉동에서 못 오른 이유: shadow 1위 변경이 오히려 c13(옷) 쪽으로 기울었다
  (0:3:2) — vote_recovery와의 합성 효과가 유력하나 갭별 분해 계측이 10차에야
  추가되어 원인 확정이 늦었다.
- (b) 냉장 재평가 가치: 옷 산탄("한 궤적, 깜빡이는 클래스")은 사람이 오가는 한
  캐비닛 무관 — 유효 가설. 냉장은 존 전용 side 카메라(근접 촬영)라 의류 노출
  기하가 냉동(공용 광각)과 달라 재실측 가치가 있다.
- (c) 처분: 유지하되, 냉장에서도 현행 우세가 지속되면(예: 2배치 연속 tube_eval
  열세) 다수결 몰수는 폐기하고 tube_conf 계측만 남기는 축소를 검토.

### vote_recovery

- (a) 냉동에서 못 오른 이유: 표 기아 구제(5차 "정답 23이 1표")가 목적이었으나
  실측은 역방향 — 13 저신뢰 산탄을 증폭한다는 가설이 tube 열세의 원인으로
  지목됐다. 앵커 조건이 의류 바닥 회수를 막는다는 설계 가정이 실기에서
  확인되지 않았다.
- (b) 냉장 재평가 가치: 빠른 취출 표 기아는 냉장에서도 발생 가능(가설 유효).
  단 **긍정 증거가 한 번도 없고 부정 증거만 있다.**
- (c) 처분: **폐기 후보 — 명시 제안**: 냉장 1~2배치 내 tube_eval에서 "shadow만
  정답" 기여가 0이면 삭제 권고 (회수 로직 + `VOTE_RECOVERY*` env 제거,
  static_track 전례와 동일하게 코드째). 산탄 증폭이 재확인되면 배치를 기다리지
  않고 즉시 삭제.

### likelihood + tray_prior

- (a) 냉동에서 못 오른 이유: 배정 후보군에 다품종 조합이 없어 log_p_vision이
  count에 무감 → "동일 상품 n개 우연 적합"을 선호하는 구조적 한계로 4차
  3:4:4 부결. 개선에는 조합 배정 열거 + count 페널티가 선행돼야 함이 기록됨.
- (b) 냉장 재평가 가치: **없음(무관)** — applicable 조건이 freezer removal 한정
  (weight_is_discriminative=False)이라 냉장 존에서는 아무것도 기록하지 않는다
  (.env.example:150-152). 순냉장 기기에서는 휴면이며 관측 0이 정상.
- (c) 처분: 유지 (냉장 기기에 무해·무비용). 승격/폐기 판단은 냉동 기기 데이터로만
  진행 — 혼합 기기(`MODEL__ZONES__FREEZER`) 대비 보존 가치.

### ghost

- (a) 냉동에서 못 오른 이유: 11차 오플래그 3/3의 주 원인은 검출 로직이 아니라
  입력 결함 — 동시·연쇄 취출의 존 트리거들이 연장 병합된 같은 에피소드 영상을
  공유해 모든 클래스가 공짜로 "2존 등장"했다. 에피소드 ≥2 요건으로 수정 완료.
  잔존 한계: side 광학 공유, 오과금이 진짜의 뒷받침을 가로채는 순환.
- (b) 냉장 재평가 가치: **높음** — 옷 유령은 캐비닛 무관이고, 냉장은 top 공용
  1대가 5존 진열을 봐서 다존 표 노출이 구조적으로 더 흔할 수 있다
  (top ROI가 1차 방어, ghost가 세션 스코프 2차 방어).
- (c) 처분: 유지 — 수정 후 재관측 1순위. 냉장 배치에서 정답 오플래그 0 확인 시
  active 승격 후보.

### track_min_hits / max_gap

- (a) 냉동에서 못 오른 이유: 켠 적이 없다(기본 0=off, 발동 0회) — 단절이
  median 4로 심각해, 단명 트랙 몰수(fail-closed)가 진짜 상품 표를 죽일 위험이
  실측으로 확인된 상태였다.
- (b) 냉장 재평가 가치: 냉장은 김서림·성에가 없어 검출 연속성(단절률) 개선이
  기대되나, 켜기 전 short probe 계측 실측이 필요한 조건은 동일하다.
- (c) 처분: off 유지 (유지 비용 0). 냉장 2배치 내 단절 지표가 개선되지 않으면
  env 2종은 삭제하고 계측(트랙 수·short probe)만 남기는 정리를 검토.

## 5. 승격 / 폐기 절차 (재명시)

1. 배포 직후 시각 기록 → 실험 → `label-session` (무취출은 `--none`).
2. `analyze-sessions --since <배포시각>` — 코드 버전 혼합 아카이브의 집계 오염
   방지 (`docs/fix_logs.md:1062`). 오답·플래그 세션은 `--session <id>`로 덤프.
3. 게이트 기준 (리포트가 판정 문구를 직접 출력, `adapters/analyze_cli.py:747,762-765,806-810`):
   - **강등형** (held, ghost): 정답 클래스 오플래그(⚠) **0** 배치 확인 → active.
   - **재순위형** (tube, recovery): 라벨 정오에서 "shadow만 정답 > 현행만 정답"
     우세 지속 → active. **현행 우세 지속 → 폐기.**
   - **fail-closed형** (track_min_hits/max_gap): 계측(short probe·단절률) 실측으로
     안전 확인 후에만 env로.
4. 승격은 env 한 줄(코드 무변경): `HELD_TRACK_DEMOTION=active`,
   `TUBE_IDENTITY=active`, `MODEL__GHOST__MODE=active` 등.
5. **폐기는 코드 삭제로** — ".env에서 0으로 꺼두기"는 코드 기본값과의 미동기화로
   부활 경로를 남긴다 (static_track이 .env 0인데 코드 기본 24로 살아 있던 전례,
   `docs/fix_logs.md:1292-1297`). 삭제 시 과거 아카이브 호환(제네릭 파싱 + 0행
   숨김)은 이번 대청소 방식(`docs/fix_logs.md:1304-1305`)을 따른다.
