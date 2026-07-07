# CRK-model-HG — 그린필드 재설계

AI 스마트 자판기 모델 서비스의 백지 재설계.
`../docs/GREENFIELD_DESIGN_GUIDE.md`의 **결정 D1~D10을 전부 권장안으로** 구현했다.
불변식 I1~I17(`../docs/REDESIGN_RATIONALE_QA.md`)은 예외 처리가 아니라
**타입·인터페이스·탐색 공간 제약**으로 표현했다 (함정 #5).

순수 파이썬 · 외부 런타임 의존성 0. YOLO TensorRT는 `perception.Detector`
프로토콜 뒤의 장치 측 어댑터로 주입한다 (제약 C1).

## 결정 → 구현 맵

| 결정 | 권장안 | 구현 위치 |
|------|--------|-----------|
| **D1** 확정 시점 모델 | 인과 배리어 (I17) — 큐 정합 ∧ 로드셀 안정 ∧ seq 도착. 고정 debounce는 상한 타임아웃으로 강등, 만료 시 에러 세션 | `ledger/barrier.py`, `gateway/state_machine.py` |
| **D2** 공통 시간축 | 카메라 seq watermark 지원 (선택적 — 없어도 ①③으로 동작) | `ledger/barrier.py` (`set_close_watermark`), `TriggerEvent.seq` |
| **D3** 판정 계층 구조 | Stage(입력 변환)/Strategy(결정자) 인터페이스 분리 + 선언적 우선순위 리스트(다이어그램 5 순서 보존) + SensorProfile 주입 + 히트 텔레메트리 | `judgment/interfaces.py`, `judgment/router.py`, `core/profiles.py` |
| **D4** 무게 구간화 위치 | ingest에서 `WeightSegment[]` 정규화. stabilization 완료 후에만 구간화(QA Q3 ①), plateau 평균 기반 드리프트 흡수(Q3 ②), 구간 임계는 프로파일 소속 | `ingest/loadcell.py` |
| **D5** 정산 구조 | 이벤트 소싱 + close-time 단일 글로벌 정산기 (동존>net-delta>교차존>freezer 재solve 내부 우선순위) + shadow 병행 | `ledger/events.py`, `ledger/settler.py`, `ledger/shadow.py` |
| **D6** 프레임 공급 | 모션 게이트 + 손 래치(I16) + keepalive + freezer 별도 임계 + `gate_skipped_frames` 트레이스 필드 | `frames/motion_gate.py` |
| **D7** 조기 종료 | removal & 비freezer 한정(I15), judge()와 tolerance 단일 소스 공유 | `perception/early_termination.py` |
| **D8** 배치 추론 | 설계만 확정, 기본 OFF(batch_size=1). 고정 배치+패딩, 카메라별 분리 수집 | `frames/batch.py` |
| **D9** 에러 세션 정책 | 계약을 enum으로 명시, 기본 fail-closed(BLOCK_PAYMENT). Node 합의(P4) 전 변경 금지 | `core/policy.py`, `ledger/settler.py` |
| **D10** 모듈 경계 | ingest → frames → perception → judgment(순수함수) → ledger(영속) → gateway(상태기계). 모듈 경계 = 테스트 경계 | 패키지 구조 + `tests/` |

## 불변식 구현 방식 (발췌)

- **I10** interim ≠ finalized: `InterimSummary`/`FinalizedSettlement`를 다른 타입으로 —
  `build_payment_payload()`가 잠정치를 `TypeError`로 거부 (런타임이 아니라 계약 수준 차단).
- **I17** 인과 배리어: `CausalBarrier.status()`가 미충족 사유를 기계 판독 가능 코드로 반환.
  타임아웃 만료 + 미충족 = `DoorState.ERROR` (부분 확정·유실 확정 금지).
- **I5/I12** 품절 제외·stock 상한: `StrictWeightMatcher`의 탐색 공간에서 원천 배제.
- **I6** 전량 설명 강제: 라우터가 모든 성공 결과에 `enforce_full_delta_match()` 적용.
- **I3** freezer 개수 게이트: `freezer_vision_first`와 close 재solve 양쪽에서 ±15g 통과 필수,
  실패 시 확정 포기(증분 유지) — 178g 다중청구 사건 재발 방지.
- **I11** finalize 멱등: 정산기 세션 캐시 + 확정 후 이벤트 거부(`EventLog.rejected`).
- **I14** count ≥ 0: `_Basket.remove_one()`이 구조적으로 음수 차단.
- **I8** 사유 코드: 판정 `reason`, 정산 `notes`, 배리어 `pending`, 라우터 `miss_log`.

## 실행

```bash
pytest tests -q
```

## 이 레포가 다루지 않는 것 (착수 전 확보물 P1~P5 대기)

- YOLO TensorRT 엔진·NVDEC 디코드 (장치 어댑터, G4)
- 모션 게이트·조기 종료 임계값 튜닝 — 현장 AVI 코퍼스(P1) 실측 전까지 기본값은 가설
- 세션 YAML replay 게이트 G2.5 — 아카이브(P2) 회수 후
- interim 의미론·에러 정책의 Node 합의(P3·P4), 카메라 seq 펌웨어(P5)
