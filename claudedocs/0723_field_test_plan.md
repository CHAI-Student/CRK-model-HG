# 2026-07-23 현장 테스트 플랜 — 0722~0723 변경분 검증

이번 밤 사이 master에 올라간 4개 커밋의 실기 검증 절차. 전부 **기본값이
기존 동작 보존**이라 `git pull`만으로 위험이 늘지 않는다 — 새 동작은 env로
명시적으로 켠다.

## 0. 배포 (Jetson)

```bash
cd ~/Desktop/Codes/CRK-model-HG   # 실제 경로에 맞게
deactivate 2>/dev/null; git pull origin master
source .venv/bin/activate
uv pip install --no-deps -e .     # ★ 필수: analyze-sessions 엔트리포인트 추가됨
MODEL__VISION__YOLO_MODEL_PATH=models/<engine>.engine model-service-hg
```

기동 로그에서 `[CONFIG] ... camera_layout=...` 한 줄 확인.

## 1. 변경분 요약 (커밋 순)

| 커밋 | 내용 | 기본 상태 |
| --- | --- | --- |
| 8031641 | 무게 우도 score shadow (Phase 1) — 냉동 이벤트별 score 순위 vs 현행 판정 diff를 아카이브에 기록 | **자동 ON** (판정 무변경, 기록만) |
| 3554b91 | 수직 ROI(P1-5) + 손 conf 하한(P1-7) 이식 | ROI는 **OFF** (env로 켬) / hand floor는 **0.30 ON** |
| 7ea7cd9 | `analyze-sessions` 오프라인 리포트 CLI + 아카이브에 class_id/unit_weight 기록 | 도구 — 서비스 무영향 |
| f5f3536 | BOCPD primary 승격 스위치 (`MODEL__LOADCELL__ANALYZER`) | **plateau** (현행 유지) |

## 2. 실험 프로토콜 (세션마다)

1. 취출 실험 수행 (아래 시나리오).
2. **매 세션 직후 정답 라벨** — 이번 변경분의 실측 판정이 전부 이 라벨에
   걸려 있다:
   ```bash
   label-session --latest --zone 2 --take 27x5 --note "5연속 취출"
   ```
3. 실험 블록이 끝나면:
   ```bash
   analyze-sessions          # shadow 정오·conformal·σ_db 리포트
   ```

### 권장 시나리오 (이슈 재현 우선순위)

- **#14 빠른 취출**: 트리거 직후 1~2초 내 꺼내기 — analyze-sessions의
  BOCPD mismatch에 "primary 0.0 vs shadow 실delta"가 잡히는지. 잡히고
  shadow가 맞으면 → `MODEL__LOADCELL__ANALYZER=bocpd`로 승격 (코드 무변경).
- **#16 4형 재현**: 동시 2트레이 / 순차 2트레이(4초 내) / 동일 상품 5연속 /
  단품(진열 오염 존) — 과금 정확도 + `likelihood_shadow` mismatch의 score
  정오가 리포트에 집계된다 (Phase 2 승격 게이트 데이터).
- **진열 오투표 존**: 진열 상품이 화면 하단에 크게 잡히는 존에서 취출 —
  아래 3번 수직 ROI 검증과 연계.

## 3. 수직 ROI 켜기 (별도 블록으로)

기본 OFF다. 한 실험 블록을 기존 설정으로 돌린 뒤, `.env`에 추가하고 재기동:

```bash
MODEL__VISION__CAMERA_LAYOUT=dual_top_proxy
# (선택) MODEL__VISION__FREEZER_ROI_VERTICAL_REGION=upper  # 기본값
# (선택) MODEL__VISION__FREEZER_ROI_Y_SPLIT=240.0          # 기본값
```

확인 지점 (세션 아카이브 `vote_summary`):
- `filter_drops_by_stage.vertical_roi`에 진열 클래스가 잡히는가
- 진짜 취출 상품이 득표 1위로 복원되는가 (이슈 #16 D형 "진열 만두 63표")
- **과잉 제거 경고**: 정답 상품이 vertical_roi로 깎이면(상단 진열 구조 등)
  `FREEZER_ROI_Y_SPLIT`을 올리거나 레이아웃을 되돌린다 — analyze-sessions의
  "정답 상품이 최종 후보에 없던 트리거" 경고로도 잡힌다.

손 conf 하한(0.30)은 이미 켜져 있다 — 래치/hand_path 이상 시
`MODEL__VISION__HAND_CONFIDENCE_THRESHOLD=0`으로 롤백.

## 4. 승격 판단 기준 (리포트 읽는 법)

- **BOCPD**: mismatch 목록에서 primary가 틀리고 shadow가 맞는 비율 우세 →
  `MODEL__LOADCELL__ANALYZER=bocpd`. 승격 후에도 shadow가 plateau로 뒤집혀
  회귀 방향 mismatch를 계속 관측한다.
- **무게 우도 (Phase 2)**: "score만 정답 > 현행만 정답"일 때만 중재(3b)를
  score 비교로 교체하는 Phase 2 진행. 리포트가 이 집계를 직접 출력한다.
- **conformal**: 정답 상품 통계의 p5가 현행 임계(MIN_VOTE_RATIO 0.02,
  SHARE 0.1 등)보다 위면 여유 있음, 아래면 임계를 p5 이하로 완화 검토.
- **σ_db**: `suggested_sigma_db`가 5와 크게 다르면
  `MODEL__JUDGMENT__LIKELIHOOD_SIGMA_DB` 갱신 (gate_n slack 5g/개의 근거
  실측이기도 하다).

## 5. 롤백 요약

| 증상 | 롤백 |
| --- | --- |
| 수직 ROI 과잉 제거 | `MODEL__VISION__CAMERA_LAYOUT=dual` (또는 줄 삭제) |
| 손 래치 이상 | `MODEL__VISION__HAND_CONFIDENCE_THRESHOLD=0` |
| BOCPD 승격 후 회귀 | `MODEL__LOADCELL__ANALYZER=plateau` |
| 우도 shadow 오버헤드 의심 | `MODEL__JUDGMENT__LIKELIHOOD_SHADOW=0` (순수 CPU 수 ms라 사실상 불필요) |
