# CRK-model-HG — Model Service (Greenfield Redesign)

Last reviewed: 2026-07-09

AI 스마트 자판기 모델 서비스의 백지 재설계 구현. 레거시/참조 서비스인
[CRK-model](https://github.com/CHAI-Student/CRK-model)(FastAPI + TensorRT)의
외부 계약을 유지하면서, 설계 문서 3종의 결론을 코드로 옮겼다:

- `CRK-model/docs/GREENFIELD_DESIGN_GUIDE.md` — 결정 **D1~D10 전부 권장안** 채택
- `CRK-model/docs/REDESIGN_RATIONALE_QA.md` — 불변식 **I1~I17**을 타입·인터페이스·탐색 공간 제약으로 표현
- `CRK-model/docs/OPTIMIZED_ARCHITECTURE.md` — 레버 L1(모션 게이트)·L2(조기 종료)·L5(전략 라우터)·L6(단일 정산기) 반영, L3(배치)는 설계만·기본 OFF

순수 파이썬, 런타임 의존성 0. YOLO TensorRT·NVDEC 등 장치 결합 요소는
프로토콜(`perception.Detector`) 뒤의 어댑터로 주입한다. Jetson 실기 검증(G4)
전까지 이 레포의 통과 상태는 설계·계약 수준의 증명이다.

## Current Status

- 로컬 검증 게이트 (2026-07-09 기준, 다른 트랙에서 계속 늘어나는 중): `pytest tests -q` → **140+ passed** (numpy/ffmpeg/fastapi 미설치 환경은 해당 테스트 skip)
- G0(정적/단위) 수준 커버: 불변식 I1~I17 전건, E2E(OPEN→trigger→CLOSE→결제 페이로드), HTTP 어댑터 E2E, G2.5 훅(저널 replay 등가성)
- 장치 어댑터(`crk_model/adapters/`) 포함: TensorRT Detector, AVI lazy 디코드(스트리밍·NVDEC ffmpeg 폴백 포함), FastAPI 바인딩, `model-service-hg` 진입점 — **Jetson 실기 검증(G4)은 미수행**. TensorRT 로딩·NVDEC·CUDA 가시성은 Jetson 트리거 런으로만 증명된다.
- 최근 완성 항목: 스트리밍 AVI 디코드(OOM 방지), NVDEC ffmpeg 디코더 자동 폴백(`MODEL__VIDEO__DECODER`), 상태 변이 구간 동시성 락, `[OPS][CLOSE]` 존별 확정 요약 로그, L5 판정 전략 확장(원본 다이어그램 5의 후보-없음 폴백 분기 포함), 세션 YAML 아카이브(오판정 사후 분석용, 진행 중), CI(ruff + pytest) — 상세는 `docs/fix_logs.md` 참조
- 미커버 (착수 전 확보물 P1~P5 대기): 게이트/조기종료 임계값 실측(P1 코퍼스), 세션 아카이브 replay(P2), interim 의미론·에러 정책 Node 합의(P3·P4), 카메라 seq 펌웨어(P5)

## Jetson Quick Start

Jetson Orin Nano(JetPack, Ubuntu 22.04)에서 1회 준비 후 실행:

```bash
git clone https://github.com/CHAI-Student/CRK-model-HG.git
cd CRK-model-HG

chmod +x scripts/setup_jetson.sh
chmod +x scripts/install_jetson_torch.sh
chmod +x scripts/jetson_env.sh
./scripts/setup_jetson.sh
       # system-site venv + 어댑터 의존성

source .venv/bin/activate
MODEL__VISION__YOLO_MODEL_PATH=models/set9_doorfas_0323_imbal.engine model-service-hg
```
기존 CRK-model을 가동 중이라면 중단 후 model-service-hg 실행. 

`.engine` 파일은 이 레포에 없다 — CRK-model에서 쓰던 엔진 파일을 `models/`에
복사하거나 절대경로로 지정한다. 기동 시 startup probe가 엔진을 1회 실행하므로
**로드 실패·CUDA 불가면 서비스가 즉시 죽는다** (무증상 기동 금지, 이관 리뷰 #1).

코드 업데이트 후. 
```bash
deactivate 2>/dev/null
git pull origin master

source .venv/bin/activate
MODEL__VISION__YOLO_MODEL_PATH=models/set9_doorfas_0323_imbal.engine model-service-hg

```


헬스 체크:

```bash
curl http://localhost:8002/api/health
# {"status":"ok","door_state":"idle","queue_pending":0,"barrier_satisfied":true,...}
```

CRK-model의 CUDA/TensorRT 경로 부트스트랩(`scripts/jetson_env.sh`)이 필요한
환경이면 먼저 그것을 source한 뒤 실행한다. 기존 CRK-model `.venv`를 재사용하는
방법도 있다: 그 venv를 활성화한 채 `uv pip install --no-deps -e /path/to/CRK-model-HG`
`uv pip install fastapi "uvicorn[standard]"` 후 `model-service-hg`.

## Operations & Diagnostics

운영 중 상태 확인·사후 분석용 로그와 아카이브. 정상 동작의 일부이며 별도 설정
없이도 남는다(아카이브·저널 경로만 env로 조정 가능).

### 운영 로그

- `[OPS][CLOSE]` — 세션 확정(finalize) 시 1회, 존별 분해를 포함한 확정 요약
  (`session_id`, 존별 `weight_delta`/`products`/`triggers`, 세션 전체
  `total_weight_delta`/`total_products`/`total_price`).
- `[OPS][CLOSE_ERROR]` — 에러 세션으로 확정될 때(I13, `blocked=true` 등) 사유와
  함께 기록.
- `[OPS][SESSION_ARCHIVE]` — 세션 아카이브 파일 기록 성공/실패 시 기록.

### 세션 아카이브 (오판정 사후 분석용)

세션이 확정(finalize)될 때마다 트리거별 vision 후보·판정 전략·신뢰도·
`video_paths`까지 포함한 세션 전체 기록을 파일로 남긴다. "왜 이렇게
과금됐는가"를 로그만으로 재구성하기 위한 것으로, 정산 로직 자체에는
영향을 주지 않는다.

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `MODEL__SESSION__ARCHIVE_DIR` | `data/sessions` | 아카이브 루트 디렉터리. 빈 문자열(`""`)이면 아카이브 비활성화 |
| `MODEL__SESSION__ARCHIVE_RETENTION_DAYS` | 14 | 일자별 디렉터리 보존 기간(일) |

파일 경로: `data/sessions/YYYY-MM-DD/<session_id>.yaml` (PyYAML이 없으면
`.json`으로 자동 폴백).

### 이벤트 저널

`TriggerEvent` 시퀀스를 JSONL로 append하는 저널. G2.5(정산 등가성) replay와
장애 후 재구성에 쓰인다.

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `MODEL__LEDGER__JOURNAL_PATH` | `logs/events.jsonl` | 저널 파일 경로. 일자별로 로테이션 |
| `MODEL__LEDGER__JOURNAL_RETENTION_DAYS` | 14 | 로테이션된 저널 파일 보존 기간(일) |

### 비디오 디코더

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `MODEL__VIDEO__DECODER` | `auto` | `auto`\|`ffmpeg`\|`opencv`. `auto`는 NVDEC(hwaccel cuda) 가용 + numpy 존재 시 ffmpeg 스트리밍 파이프를 쓰고, 아니면 cv2(CPU 디코드)로 폴백 |

## Manual Setup -> 꼭 해야한다면... 

```bash
uv venv --system-site-packages --python python3.10 .venv
source .venv/bin/activate
uv pip install --no-deps -e .
uv pip install "fastapi>=0.100.0" "uvicorn[standard]>=0.23.0"
# ultralytics가 system-site에 없을 때만 (CPU torch 오염 방지를 위해 --no-deps):
uv pip install --no-deps "ultralytics>=8.0.0,<9.0.0" "ultralytics-thop>=2.0.18"

cp ../CRK-model/.env.example .env 2>/dev/null || touch .env
echo "MODEL__VISION__YOLO_MODEL_PATH=models/siyeon_best.engine" >> .env
```

원칙은 CRK-model과 동일: venv는 반드시 `--system-site-packages`(JetPack의
CUDA/TensorRT/torch/OpenCV/numpy<2 사용), ultralytics는 `--no-deps`로만 설치,
일상 실행에 plain `uv run`/`uv sync` 금지 (환경 재동기화로 CUDA torch가
CPU wheel로 덮일 수 있음).

## Live Engine Preview

카메라 입력과 TensorRT `.engine` 추론 출력을 실시간 bbox/라벨로 육안 검증하는
독립 유틸(`scripts/live_engine_preview.py`) — FastAPI 서비스(`model-service-hg`)와
완전 분리되어 있고 `crk_model` 패키지에도 의존하지 않는다:

```bash
python scripts/live_engine_preview.py --model models/set9_doorfas_0323_imbal.engine --source 0 --display-backend ffplay
```

자주 쓰는 옵션:

```bash
python scripts/live_engine_preview.py \
  --model models/set9_doorfas_0323_imbal.engine \
  --source 0 \
  --width 640 \
  --height 480 \
  --imgsz 480 \
  --conf 0.25 \
  --display-backend ffplay
```

- `--backend {auto,v4l2,gstreamer,ffmpeg}` — 캡처 백엔드 선택.
- `--display-backend auto`는 OpenCV HighGUI가 가능하면 그것을, `GUI: NONE`으로
  빌드된 헤드리스 OpenCV라면 `ffplay`(rawvideo 파이프)로 자동 폴백한다.
- `--classes 0,2,5` 같은 콤마 목록으로 특정 YOLO 클래스만 필터링 가능.
- Jetson CUDA/TensorRT 런타임 경로가 필요하면 실행 전에
  `source scripts/jetson_env.sh`로 준비한다 (스크립트 자체는 이제 이 부트스트랩을
  자동으로 재실행하지 않는다 — `model_service.core.runtime_env` 같은 서비스
  전용 모듈에 의존하지 않는 완전 독립 스크립트이기 때문).
- 이 스크립트는 Jetson 전용 육안 검증 도구다 — 개발 PC 실행으로 TensorRT/CUDA
  준비 상태를 판단하지 않는다. cv2/ultralytics를 직접 import하므로(코어의
  "런타임 의존성 0" 원칙의 명시적 예외), 개발 PC에는 두 패키지가 없어도
  `--help`는 정상 동작한다.

## Quick Start (개발 PC — 도메인 코어)

```bash
git clone https://github.com/CHAI-Student/CRK-model-HG.git
cd CRK-model-HG
pytest tests -q        # 코어는 런타임 의존성 0 (fastapi 있으면 HTTP E2E도 실행)
```

서비스 사용은 파사드 직접 호출 (HTTP 어댑터는 이 파사드를 감싸기만 한다):

```python
from crk_model.service import ModelService
from crk_model.core.config import Settings

svc = ModelService(detector=MyTensorRTDetector(),        # Detector 프로토콜 구현
                   settings=Settings.from_env(),
                   startup_probe_frame=probe)            # 로드 실패 = 기동 실패 (fail-fast)

svc.handle_multi_zone({"session_id": s, "state": "OPEN", "active_products": [...]})
svc.handle_trigger({"zone": 1, "frames": {...}, "loadcells": [...], "video_paths": {...}})
svc.process_pending()                                    # 전용 스레드에서 주기 호출
svc.handle_multi_zone({"session_id": s, "state": "CLOSE"})   # 배리어 충족 시 결제 페이로드
```

## Architecture

### 1. 시스템 컨텍스트 — 외부 계약 (C4/C5)

```mermaid
flowchart LR
    CAM["CRK-CAMERA<br/>(AVI 녹화 + 로드셀)"]
    NODE["Node.js (8888)<br/>세션 오케스트레이션"]
    subgraph HG["CRK-model-HG"]
        direction TB
        ADPT["adapters/ HTTP 바인딩 (무로직)<br/>FastAPI + AVI 디코드 + TensorRT Detector"]
        FAC["ModelService 파사드<br/>handle_trigger / handle_multi_zone"]
        ADPT --> FAC
    end
    PAY["CRK-PAYMENT"]

    CAM -- "POST /trigger<br/>frames + loadcells + zone (+seq)" --> ADPT
    NODE -- "POST /multi-zone<br/>OPEN/CLOSE + active_products" --> ADPT
    FAC -- "FinalizedSettlement만<br/>(I10: interim은 TypeError)" --> NODE
    NODE -. "확정 금액" .-> PAY
```

### 2. 트리거 파이프라인 — 데이터 평면 (unpaced · event-driven)

```mermaid
flowchart TD
    T["handle_trigger"] --> IDEM{"멱등성 I7<br/>MD5(zone+paths) TTL 5s"}
    IDEM -- 중복 --> DROP["드롭"]
    IDEM -- 신규 --> ENQ["SerialTriggerWorker.submit<br/>barrier.notify_enqueued ★I17①"]
    ENQ --> Q["단일 소비자 큐 (I7 · C2)"]

    Q --> SNAP{"ActiveProductStore<br/>allowlist?"}
    SNAP -- "empty" --> FC["추론 차단 (I2 fail-closed)<br/>YOLO 호출 0"]
    SNAP -- "current / last_valid" --> LC["LoadcellAnalyzer (D4)<br/>stabilize 후 구간화 → WeightSegment[]"]

    LC --> LW{"|delta| < 프로파일 게이트?"}
    LW -- yes --> SKIP["저무게 스킵 (QA Q8)<br/>YOLO 호출 0"]
    LW -- no --> MG["MotionGate (D6/L1)<br/>absdiff + 손 래치(I16) + keepalive"]
    MG -- 통과 프레임만 --> DET["Detector (어댑터)"]
    DET --> FILT["FilterChain: Side ROI + Hand Path<br/>(conf 하한 없음 — I4)"]
    FILT --> VOTE["VotingEnsemble<br/>분모 = 게이트 통과 프레임"]
    VOTE --> ET{"EarlyTerminator (D7/L2)<br/>removal & 비freezer 한정 (I15)"}
    ET -- 수렴 --> STOP["추론만 중단"]
    ET -- 미수렴 --> MG

    VOTE --> RT["JudgmentRouter (D3/L5)<br/>선언적 우선순위 · Stage/Strategy 분리<br/>SensorProfile 주입 · 全결과 I6 강제"]
    FC --> EV
    SKIP --> EV
    RT --> EV["TriggerEvent (불변, I1: 예외→error)<br/>EventLog + EventJournal(JSONL)"]
    EV --> PROC["barrier.notify_processed ★I17①"]
```

### 3. 세션 확정 — 제어 평면 (time-paced → causal barrier로 승격, I17)

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Active: OPEN<br/>스냅샷 갱신 + 새 배리어
    Active --> Active: OPEN 재폴링 → interim<br/>(InterimSummary 타입 — 결제 불가 I10)
    Active --> PendingClose: CLOSE
    PendingClose --> PendingClose: 배리어 미충족<br/>(queue_pending / loadcell_unstable / seq_gap)
    PendingClose --> Finalized: ★배리어 충족 (I17)<br/>enqueued==processed ∧ 로드셀 안정 ∧ seq 도착<br/>→ CloseSettler (즉시, debounce 없음)
    PendingClose --> Error: 상한 타임아웃 만료<br/>= 에러 세션 (D9 fail-closed)<br/>부분 확정·유실 확정 금지
    Finalized --> Finalized: 재폴링 → 동일 정산 객체 (I11)
    Error --> [*]
    Finalized --> [*]: build_payment_payload<br/>(FinalizedSettlement만 통과)

    note right of PendingClose
        현행 CRK-model: 3s/1s 고정 debounce
        HG: 인과 배리어 + 상한 타임아웃
        큐가 비면 대기 0초 확정
    end note
```

### 4. close-time 단일 정산기 (D5/L6) — 반품 복구 4층의 통합

```mermaid
flowchart TD
    EVS["세션 전체 TriggerEvent[]"] --> ERR{"에러 이벤트 존재?"}
    ERR -- "yes (I13)" --> POL{"ErrorSessionPolicy (D9)"}
    POL -- "BLOCK_PAYMENT (기본)" --> BLK["blocked=true → 결제 불가"]
    POL -- "FINALIZE_ERROR_FREE_ZONES<br/>(Node 합의 시)" --> EXC["에러 존 제외 + 기록"]
    ERR -- no --> P1
    EXC --> P1

    P1["① 동존 즉시: removal 축적<br/>return 무게 매칭 차감"] --> P2
    P2["② net-delta 교정<br/>과잉 청구 감산 (미매칭 반품 소거)"] --> P3
    P3["③ 교차존: 미매칭 반품을<br/>타 존 장바구니와 매칭"] --> P4
    P4["④ freezer 재solve: 부호있는 net<br/>개수 게이트 ±15g (I3) 실패 시 증분 유지"] --> OUT

    OUT["FinalizedSettlement<br/>count≥0 (I14) · notes[] (I8)<br/>멱등 캐시 (I11)"]
    OUT -.-> SHDW["ShadowSettlerRunner<br/>구/신 병행 diff 로깅 (L6 조건)"]
```

## Module Map

모듈 경계 = 테스트 경계 (D10). 화살표 방향으로만 의존한다.

| 모듈 | 책임 | 상태성 | 원본 대응 | 테스트 |
|------|------|--------|-----------|--------|
| `core/` | 타입(I10 분리), SensorProfile(D3), 에러 정책(D9), env 설정 | 무상태 | core/config.py | (전역) |
| `ingest/` | 멱등성(I7), 로드셀 분석 → WeightSegment[] (D4) | 무상태 | trigger.py 일부 | test_ingest |
| `frames/` | 모션 게이트 + 손 래치(D6/I16), 배치 수집(D8·기본 OFF) | 트리거 내 | frame_extractor | test_frames |
| `perception/` | Detector 프로토콜, 필터 체인, 투표, 조기 종료(D7/I15) | 트리거 내 | yolo_wrapper, video_processor | test_perception |
| `judgment/` | Stage/Strategy 라우터(D3), strict 매처(I5·I6·I12) | 무상태 (순수) | decision_engine (10.4k줄 해체) | test_judgment |
| `ledger/` | 이벤트 소싱, 인과 배리어(D1/I17), 단일 정산기(D5), 저널, shadow | 영속 | session/* 통합 | test_ledger |
| `gateway/` | OPEN/CLOSE 상태기계, 결제 페이로드(I10 타입 강제) | 상태기계 | multi_zone.py | test_gateway |
| `service/` | 파이프라인 오케스트레이션, 직렬 워커, 스냅샷(I2), 파사드 | 조립 | trigger_service, api/routes | test_service |
| `adapters/` | 장치 결합: TensorRT Detector, AVI lazy 디코드, FastAPI, 진입점 (전부 lazy import) | I/O 경계 | yolo_wrapper, frame_extractor, main.py | test_adapters |

## Design Decision Map (D1~D10 → 구현)

| 결정 | 권장안 | 구현 위치 |
|------|--------|-----------|
| D1 확정 모델 | 인과 배리어(I17), debounce → 상한 타임아웃 강등, 만료 시 에러 세션 | `ledger/barrier.py`, `gateway/state_machine.py` |
| D2 공통 시간축 | 카메라 seq watermark (선택 — 없어도 동작) | `barrier.set_close_watermark`, `TriggerEvent.seq` |
| D3 판정 구조 | Stage/Strategy 분리 + 선언적 순서(다이어그램5 보존) + SensorProfile + 텔레메트리 | `judgment/` |
| D4 구간화 위치 | ingest 소속, stabilize 후 순서 고정, plateau 평균 드리프트 흡수 | `ingest/loadcell.py` |
| D5 정산 구조 | 이벤트 소싱 + close 단일 정산기 + shadow 병행 | `ledger/settler.py`, `shadow.py`, `journal.py` |
| D6 프레임 공급 | 모션 게이트 + 손 래치 + keepalive + freezer 별도 임계 | `frames/motion_gate.py` |
| D7 조기 종료 | removal·비freezer 한정, judge()와 tolerance 단일 소스 | `perception/early_termination.py` |
| D8 배치 | 설계만, 기본 OFF, 고정 배치+패딩, 카메라 분리 | `frames/batch.py` |
| D9 에러 세션 | 계약 enum, 기본 fail-closed | `core/policy.py` |
| D10 모듈 경계 | 경계 = 테스트 경계 | 패키지 구조 |

## Invariant Coverage (I1~I17)

전부 실제 사고(오과금·매출 누락)의 재발 방지책이며, 예외 처리가 아니라 구조로 표현했다:

- **I1** 처리 실패 → `status="error"` 이벤트 (`pipeline.process` except 절) · **I2** 빈 allowlist fail-closed + last_valid (`service/snapshot.py`)
- **I3** freezer ±15g 게이트 — 판정·정산 양쪽 · **I4** conf 하한은 투표 결합 후에만 · **I5/I12** 매처 탐색 공간에서 원천 배제
- **I6** `enforce_full_delta_match` 라우터 전건 적용 · **I7** 멱등 TTL + 단일 소비자 큐
- **I8** reason/notes/pending/trace 사유 코드 · **I9** 시나리오 계약은 G1에서 인수 (P1·P2 후)
- **I10** Interim/Finalized 타입 분리 — 결제 빌더가 TypeError로 거부 · **I11** 정산 멱등 캐시 + 확정 후 이벤트 거부
- **I13** 에러 세션 무성 확정 금지 (D9) · **I14** `_Basket.remove_one`이 음수 차단
- **I15** 조기 종료 removal·비freezer 한정 · **I16** 손 래치 활성 중 스킵 금지 · **I17** 인과 배리어 확정

## Configuration

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `MODEL__CLOSE__BARRIER_TIMEOUT_S` | 10.0 | I17 상한 타임아웃 (정상 경로 아님) |
| `MODEL__VISION__BATCH_SIZE` | 1 | D8 배치 (1 = OFF) |
| `MODEL__ZONES__FREEZER` | (없음) | freezer 프로파일 존 목록 (예: `9,10`) |
| `MODEL__SESSION__ERROR_POLICY` | `block_payment` | D9 (변경은 Node 합의 P4 필요) |
| `MODEL__TRIGGER__IDEMPOTENCY_TTL_S` | 5.0 | I7 멱등 TTL |
| `MODEL__VIDEO__DECODER` | `auto` | 비디오 디코더 `auto`\|`ffmpeg`\|`opencv` (상세: Operations & Diagnostics) |
| `MODEL__SESSION__ARCHIVE_DIR` | `data/sessions` | 세션 확정 YAML/JSON 아카이브 루트, 빈 문자열이면 비활성 (상세: Operations & Diagnostics) |
| `MODEL__SESSION__ARCHIVE_RETENTION_DAYS` | 14 | 세션 아카이브 보존 기간(일) |
| `MODEL__LEDGER__JOURNAL_PATH` | `logs/events.jsonl` | 이벤트 저널 경로, 일자 로테이션 |
| `MODEL__LEDGER__JOURNAL_RETENTION_DAYS` | 14 | 이벤트 저널 보존 기간(일) |

게이트·tolerance·구간화 임계는 env가 아니라 `SensorProfile`(코드) 소속 —
존 타입별 물리 특성이므로 배포 설정으로 흔들리지 않게 한다 (C3).

## Verification Gates

| 게이트 | 상태 | 내용 |
| ------ | ---- | ---- |
| G0 정적/단위 | ✅ 140+ passed | 불변식 전건 + E2E + 필터/게이트/정산 + CI(ruff+pytest) |
| G1 판정 등가성 | ⏳ P1·P2 대기 | 924 시나리오 계약 인수 |
| G2 게이팅 검증 | ⏳ P1 대기 | 현장 AVI 코퍼스 전체 파이프라인 재실행 diff |
| G2.5 정산 등가성 | 훅 완성 (`EventJournal.replay`) | 세션 아카이브(P2) replay |
| G3 프로토콜 계약 | 파사드 계약 고정 | interim 의미론 Node 합의(P3) 별도 |
| G4 장치 검증 | ⏳ Jetson 반입 | 파워모드·스로틀링·OOM·24h soak |
