# 테스트 가이드

이 디렉터리는 CRK-model-HG의 판정·정산 불변식, 장치 연동 계약, 운영 진단 도구를
검증한다. 2026-08-20 기준 `master@be4e372`에서 **444건**이 통과한다.

## 삭제 가능성 점검 결과

현재 통째로 삭제해도 되는 테스트 파일은 없다. 각 파일은 현행 코드 경로, 외부
계약, 운영 데이터 호환성 또는 배포 스위치 중 적어도 하나를 독립적으로 보호한다.

- 2026-07-30 폐기된 무게 우도와 세션 tray memory의 전용 테스트
  `test_likelihood.py`, `test_tray_memory.py`는 구현 파일과 함께 이미 삭제됐다.
- `test_analyze_cli.py`의 폐기 shadow 필드 입력은 폐기 기능을 테스트하는 것이
  아니다. 과거 세션 아카이브를 새 분석기가 예외 없이 읽는 하위 호환 계약이다.
- `test_perception.py`의 tube 테스트는 폐기된 tube 다수결 몰수가 아니라 현재 남은
  진단 계측(`tube_diag`)이 판정에 개입하지 않는지 확인한다.
- `test_ghost_ledger.py`의 ghost 및 held 관련 테스트는 아직 `shadow`로 운용 중인
  기능을 보호한다. 기본 비활성 또는 shadow라는 이유만으로 삭제할 수 없다.
- `test_t2_batch.py`는 기본 OFF인 성능 레버가 켜져도 판정이 변하지 않는다는
  등가성 계약을 검증한다.

테스트를 삭제하려면 대응 생산 코드·환경변수·문서가 함께 폐기됐거나, 같은 실패
모드를 더 직접적인 테스트가 완전히 대체한다는 근거가 있어야 한다. 폐기 결정과
실측 근거는 `docs/07-rejected-and-retired.md`에 먼저 남긴다.

## 실행 방법

```bash
# 현재 저장소 venv 기준 전체 테스트
.venv/bin/python -m pytest tests -q

# 파일 또는 단일 케이스
.venv/bin/python -m pytest tests/test_gateway.py -q
.venv/bin/python -m pytest \
  tests/test_gateway.py::TestEdgeWatermark::test_missing_expected_trigger_times_out_to_error -q

# 수집 목록만 확인
.venv/bin/python -m pytest tests --collect-only -q

# 정적 검사
.venv/bin/ruff check .
```

CI는 FastAPI, NumPy, ffmpeg까지 설치해 환경 의존 테스트가 빠지지 않게 실행한다.
로컬 환경에 해당 의존성이 없으면 관련 테스트만 `skip`될 수 있다.

## 공용 픽스처

`conftest.py`는 테스트용 상품 `cola`, `water`, `bar170`, `bar178`과
`VisionCandidate` 생성 헬퍼 `cand()`를 제공한다. 테스트가 특정 상품명보다 무게,
재고, class ID와 같은 계약을 드러내도록 고정된 값으로 구성돼 있다.

## 파일별 목적과 검증 범위

| 파일 | 건수 | 만들어진 이유 | 주요 검증 내용과 유지 근거 |
|---|---:|---|---|
| `test_adapters.py` | 4 | 도메인 서비스와 FastAPI/프레임 어댑터의 결합 경계를 검증 | 게이트용 축소 뷰와 검출용 원본 프레임 분리, OPEN→trigger→CLOSE HTTP 흐름, health 배리어, 중복 트리거 응답. FastAPI 연동 회귀를 막으므로 유지한다. |
| `test_analyze_cli.py` | 22 | 운영 아카이브를 배포 후 분석하는 `analyze-sessions`의 정본 동작을 고정 | 청구 정오, 분위수, 개당 잔차, 트랙·held·ghost 계측, 단건/기간 조회, 손상 파일 오류와 구 스키마 관용 파싱. 과거 운영 데이터 호환 때문에 유지한다. |
| `test_cross_zone.py` | 24 | 다른 존 영상이 섞여 발생한 오과금을 CLOSE 2차 패스에서 교정 | 시간 앵커, 오염 창, 상호 강등·self-fit·무게 가드, PARTIAL 재판정, 저널 왕복과 무겹침 진단. 승격된 기본 ON 기제이므로 핵심 회귀 테스트다. |
| `test_frames.py` | 7 | 추론량을 줄이는 모션 게이트가 손 존재 시 증거를 버리지 않도록 검증 | 첫 프레임, 정지/움직임, 손 래치, exit 확인, keepalive, 직전 통과 프레임 기준 비교. 프레임 누락이 판정 누락으로 직결돼 유지한다. |
| `test_frames_streaming.py` | 18 | 긴 AVI의 메모리 폭증 방지와 ffmpeg 스트리밍 최적화를 검증 | hwaccel 프로브와 CPU 폴백, lazy iterator와 자원 해제, NumPy/순수 Python 차분 등가성, generator 1회 소비, 게이트 뷰 순서. `ffmpeg`/`numpy` 환경 의존이다. |
| `test_gateway.py` | 16 | OPEN/CLOSE 상태기계와 결제 확정 시점을 보호 | 인과 배리어, 큐·로드셀·seq 조건, close grace/timeout, expected trigger 워터마크, 확정 1회 전달, 새 세션 리셋, 결제 타입·confidence. 외부 결제 경계라 중복처럼 보여도 삭제하지 않는다. |
| `test_ghost_ledger.py` | 15 | 여러 존에 나타난 무게 미지지 클래스의 ghost shadow를 검증 | ghost 성립 조건, 영상/시각 기반 에피소드 병합, 표 하한, off/shadow/active 동작과 정산기 결합. 승격 대기 중인 현행 계측이므로 유지한다. |
| `test_ingest.py` | 18 | 카메라 트리거 멱등성과 로드셀 시계열 해석 계약을 검증 | TTL 중복 제거, plateau 분석, 채널별 변화·안정화, 현재 primary인 BOCPD의 급변·creep·반품·평탄 시계열. 장치 입력의 첫 도메인 경계다. |
| `test_judgment.py` | 79 | 판정 전략 우선순위와 냉장/냉동 안전 불변식을 세밀하게 고정 | strict/relaxed 탐색, 재고·품절, 전량 설명, freezer vision-first, refit, segment/count 조합, PARTIAL 거부권, 실기 사고 재현 및 롤백 노브. 가장 큰 순수 도메인 회귀 스위트다. |
| `test_ledger.py` | 28 | 트리거별 판정을 세션 단위 청구로 바꾸는 정산기를 검증 | 배리어, 멱등 확정, 반품·교차존·net delta, error policy, 냉동 재해석, 비전 조합 가드, 잠정 집계의 결제 차단. 금액 확정 안전성 때문에 유지한다. |
| `test_lifecycle.py` | 33 | 장시간 운영에서 상태·파일·메모리가 무한히 자라지 않도록 검증 | 결과 deque 상한, 세션 prune, 저널 일자 회전·보존·replay, 동시 poll/drain, cabinet/profile 및 env 설정 배선. 24h soak 전 자동 방어선이다. |
| `test_ops_logging.py` | 4 | 현장 장애 분석에 필요한 CLOSE 구조화 로그를 보장 | 정상·오류 세션 로그, 재폴링 중복 억제, 존별 무게·트리거 수·notes 포함. 기능 결과가 같아도 운영 관측성이 사라지는 회귀를 막는다. |
| `test_perception.py` | 60 | 검출 결과를 판정 후보로 축약하는 지각 계층을 검증 | 투표 분모·가중 confidence, 조기 종료 안전 조건, 모션 변위·held shadow, tube 진단 무개입, side hand, hand-window 분모, 측정 불가 클래스 정책. 실기 오판의 주요 원인층이다. |
| `test_product_mapping.py` | 17 | Node 상품 필드를 YOLO class ID로 안정적으로 변환 | camel/snake 별칭, 영문명 대소문자 폴백, 미매핑 `-1`, OPEN 로그와 HTTP E2E. 매핑 실패로 allowlist가 비는 사고를 방지한다. |
| `test_render_cli.py` | 12 | 저장된 bbox를 당시 영상 기하에 맞게 재현하는 `render-session`을 검증 | mp4/JPG 생성, 장치 경로 remap, 누락 자료 안내, crop 원점, 그리기 primitive, 구 `kept=false` 레코드 제외. `ffmpeg`/`numpy` 환경 의존이다. |
| `test_service.py` | 44 | 7단계 파이프라인과 서비스 파사드의 통합 동작을 검증 | 멀티트레이, 전체 세션 E2E, snapshot fail-closed, 중복·저무게·예외, 허용 클래스, 필터·재시도, 저널 replay, frame detection 저장, env 배선. 계층 간 연결 회귀를 잡는다. |
| `test_session_archive.py` | 18 | 오판정 사후 재구성용 세션 아카이브를 검증 | FINALIZED/ERROR 1회 저장, 보존기간, YAML→JSON 폴백, 저널 신규·누락 필드 호환, 정답 라벨 CLI, frame detection 포함. 운영 증거 보존 계약이다. |
| `test_t2_batch.py` | 19 | Jetson 지연 개선용 마이크로배치·프리페치가 판정을 바꾸지 않도록 검증 | 배치/비배치 결과 등가성, 잔여 배치, 예외·close 전파, 정적 batch 엔진, stream open 시점, tensor input 및 env 배선. 기본 OFF여도 재활성화 가능한 현행 코드라 유지한다. |
| `test_wire_contract.py` | 6 | Node·카메라가 의존하는 HTTP 필드 계약을 별도 고정 | `/trigger` 성공·중복, `change_timestamps`, 비디오 파일 사전 검증, `/api/health` 필수 필드. 내부 E2E와 목적이 달라 별도 유지한다. |

## 테스트가 겹쳐 보일 때의 구분

- `test_judgment.py`는 순수 판정 함수, `test_service.py`는 파이프라인 조립,
  `test_adapters.py`와 `test_wire_contract.py`는 HTTP 변환을 각각 검증한다.
- `test_ledger.py`는 정산 규칙, `test_gateway.py`는 확정 시점과 응답 수명주기를
  검증한다.
- `test_frames.py`는 모션 게이트 규칙, `test_frames_streaming.py`는 프레임 공급과
  자원 수명주기를 검증한다.

같은 시나리오가 여러 층에 나타나더라도 실패 위치와 보호 계약이 다르므로 단순
중복으로 보지 않는다.
