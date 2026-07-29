# 냉동 트리거 처리시간 단축 리서치 (2026-07-28)

3방향 병렬 조사(코드 감사 / 이슈·문서 실측치 스윕 / Jetson·TensorRT 외부 리서치)의
종합. 목표: **냉동 경로의 트리거당 영상 디코드 + YOLO 처리시간을 냉장 수준
이하로** — 정확도(과금 정오) 회귀 없이.

---

## 1. 확정된 비용 모델 (34개 실기 트레이스 전수 회귀)

```
processing_time_ms ≈ 40.1ms × yolo_calls   (평균 절대오차 4.4%)
yolo_calls = Σ processed_frames − Σ gate_skipped_frames   (34건 전건 항등)
```

| | 냉동 (n=30) | 냉장 (#18) |
|---|---|---|
| 트리거당 평균 | **13.7s** (5.7~26.1) | **5.7~6.8s** (ET 발동 시 2.0~2.7) |
| 게이트 스킵률 | **25%** (2~46%) | **56~65%** |
| 디코드 프레임 | 459 (357~740, 병합 연장 시 ↑) | ~420 |

- 프레임 예산 소스: 프리롤 4s + 포스트롤 4s × 30fps × 2캠 = 480프레임/트리거
  (CRK-CAMERA 계약). 트리거 병합 연장 시 740까지 (#16 C2, 26.1s 최악).
- `close_timeout 10s < 트리거당 13.7s` — 구조적으로 추론이 배리어보다 길다
  (이슈 #3/#4의 queue_pending ERROR 근원).
- 비 YOLO 비용(디코드+게이트+집계)은 총 처리의 12~21% — 회귀에서 분리조차 안 됨.

**냉동이 냉장보다 느린 이유는 정확히 두 가지다:**
1. **게이트 스킵률 25% vs 60%** — 김서림/성에/AE 스윙 때문에 임계를 0.005로
   낮춰 놓았기 때문 (`profiles.py` 주석이 자백: "스킵 이득이 0에 수렴").
   keepalive 4는 최소 추론 비율 25%를 강제 (냉장 8 = 12.5%).
2. **조기 종료 영구 비활성 (I15)** — top·side 전 프레임 완주. 냉장은 top에서
   합의 시 side를 통째로 생략.

## 2. 40ms의 정체 — CPU가 ~72%, GPU는 논다

외부 실측(Orin Nano Super, YOLO11n INT8, ultralytics#23467): preprocess
5.6ms(46%) + **inference 3.4ms(28%)** + postprocess 3.2ms(26%). 우리 40ms도
같은 구조 — ultralytics `predict()`의 파이썬 letterbox/NMS/동기화가 지배하고
TensorRT 커널 자체는 소수다. **"모델이 느리다"가 아니라 "모델 주변이 느리다".**

추가 확정 사항:
- **NVDEC은 MJPEG를 디코드하지 못한다** (Orin NVDEC 코덱 목록에 JPEG 없음,
  JPEG는 별도 NVJPG 엔진, ffmpeg HW MJPEG는 Thor 전용). 즉
  `avi_frames.py`의 hwaccel 경로는 **한 번도 성공한 적 없고 디코드는 항상
  100% CPU였다**. AVI MJPEG는 4:2:2라 NVJPG 경로도 사실상 차단
  (`ffprobe -show_streams | grep pix_fmt`로 확인 — `yuvj422p`면 확정).
- **Orin Nano 4GB의 기본 전력 모드는 10W (GPU 624.75MHz)** — 단 실기는
  `nvpmodel -q` = **25W 확인** (2026-07-28, Super 구성 플래시됨). 전력 상한은
  이미 최적 근처이므로 남는 것은 **DVFS 램프업 제거**뿐: 버스트 워크로드는
  램프업(GPU ~5ms + EMC ~8ms × 다단계)을 트리거마다 지불하며,
  `jetson_clocks` 실측에서 정상 상태 −7.4% vs wall-clock **−22.6%**
  (그 격차가 램프업분). 기대치는 원안 15~25%에서 **~5-10%로 하향**.

## 3. 실행안 (우선순위·티어)

### Tier 0 — 측정·설정 (코드 0~수 줄, 판정 무영향)

| # | 작업 | 기대 | 근거 |
|---|---|---|---|
| 0-1 | ~~전력 모드 확인~~ **완료: 25W 확인** (2026-07-28). 잔여: `jetson_clocks` 영구화(systemd)로 DVFS 램프업 제거 + `tegrastats`로 발열 스로틀 확인 | **~5-10%** (하향) | 전력 상한은 이미 최적 근처 — 램프업분만 남음 |
| 0-2 | `ffprobe`로 AVI pix_fmt 확인 (yuvj422p 예상) | HW 디코드 방향 조사 종결 | 4:2:2 = NVJPG 차단 |
| 0-3 | 죽은 NVDEC hwaccel 경로 제거 (`avi_frames.py` `_ffmpeg_hwaccel_available` 프로브·cuda 인자) | 실패하는 init 비용 제거, 코드 단순화 | MJPEG는 NVDEC 대상이 아님 |
| 0-4 | 단계별 ms 계측 추가 (디코드/게이트/pre/infer/post 분해, trace에 동봉) + 웜업 후 측정 | 이후 모든 티어의 실측 기반 | 40ms 내부 분해가 기기에서 미확인 |

### Tier 1 — 무위험 코드 수정 (판정 비트 동일)

| # | 작업 | 기대 절감 | 근거 |
|---|---|---|---|
| 1-1 | **`voting.combine()` 지연 평가** — 냉동에서 `should_stop`은 무조건 False인데 인자 평가로 combine이 매 추론 프레임 실행(O(표²)), 결과 100% 폐기. candidates를 지연 콜러블로 (**적용 완료 2026-07-28** — 냉장에서도 손 미퇴장·반품 프레임의 combine이 사라짐) | **0.2~2s/트리거** (프레임 많을수록 큼) | `pipeline.py:660-665`, `early_termination.py:49-50`, 0723 비용 문서도 동일 지적 |
| 1-2 | **gate_view 생성 순서 교정** — 480² 풀프레임 float64 평균 후 다운샘플 → 다운샘플 후 평균 (연산 16배↓, nearest 인덱스와 채널 평균은 교환 가능 = **비트 동일**, 동일성 회귀 테스트 포함. **적용 완료 2026-07-28**) | **0.5~1.5s/트리거** | `avi_frames.py` `_gate_view` |
| 1-3 | 조기 종료 시(냉장) side 스트림 open 자체 생략 | 냉장 한정 50~200ms | `decode_avi`가 첫 프레임 즉시 평가 |

### Tier 2 — 구조 개선 (최대 레버, 판정 동일성 검증 필요)

| # | 작업 | 기대 | 핵심 근거·함정 |
|---|---|---|---|
| 2-1 | **전처리 완료 GPU 텐서(BCHW)를 predict에 직접 투입** — letterbox·BGR→RGB·CHW·/255를 GPU에서 배치로 만들어 넘기면 ultralytics preprocess가 ~0.004ms로 소멸 | **프레임당 CPU ~46% 제거** | 박스가 letterbox 좌표계로 반환 → `scale_boxes()` 필수 (누락 시 조용한 좌표 왜곡) |
| 2-2 | **게이트 통과 프레임 배치 4 (고정배치+패딩, 2캠 스택)** — D8/L3 재개 | 커널 런치 상각, Orin NX 실측 이미지당 −42%, Orin Nano 1.52× | 과거 보류 사유 ①(predict 오버헤드로 1.5× 미달)은 2-1이 해소. 4GB에서 batch 4 상한. **I16 래치 지연** (배치 B → 래치 갱신 B프레임 지연): keepalive 4가 상한이라 최악 +3프레임(0.1s) 창 — G2 재생으로 판정 diff 검증 |
| 2-3 | **디코드 ‖ 추론 파이프라이닝** — 2캠 ffmpeg 동시 실행 + 프리페치 큐(maxsize 2~4) | 디코드분 은닉 **1~2s** | TRT 파이썬 바인딩은 GIL 해제(소스 확인). **주의**: 단일 YOLO 객체는 predictor `_lock`으로 스레드 직렬화 + `ops.Profile`이 배치당 6회 device-wide sync — 이걸 우회 안 하면 이득 0. CUDA multi-stream은 Orin 실측 역효과라 금지 |

Tier 0+1+2 합산 기대: **40ms/call → 15~20ms/call, 냉동 13.7s → 5~7s** (가설
— 0-4 계측과 G2 재생으로 확정).

**T2 구현 상태 (2026-07-28)**: 브랜치 `perf/t2-batch-pipeline`에 2-1/2-2/2-3
전부 구현 — 기본값(BATCH_SIZE=1, PREFETCH=0)이면 기존 경로와 동일, 페이크
기반 판정 동등성 테스트(배치4 vs 비배치, ET 중간 발동, 프리페치) 포함.
기기 활성화 절차:
```bash
BATCH=4 PT_FILE=<모델>.pt bash scripts/convert_engine.sh
#   → models/<모델>_batch4.engine (batch 접미사 — 배치 다른 엔진의 덮어쓰기 방지)
# .env: MODEL__VISION__YOLO_MODEL_PATH=models/<모델>_batch4.engine
#       MODEL__VISION__BATCH_SIZE=4, MODEL__VIDEO__PREFETCH=4
# 기동 프로브가 detect_batch를 1회 실행 — 엔진/텐서 불일치는 기동 실패로 드러남
```
기기 검증 체크리스트: ① 기동 프로브 통과 ② 동일 AVI 재생에서 비배치와
과금 diff 0 (SAVE_DETECTIONS+render로 bbox 좌표 육안 대조 — 텐서 입력은
letterbox 생략이라 좌표계 등식이 어댑터 가정) ③ processing_time_ms 전후 비교.

**효과 분리 측정 매트릭스** (`MODEL__VISION__TENSOR_INPUT` — 2026-07-29 추가):
| 단계 | 구성 | 측정 대상 |
|---|---|---|
| A | 기본값 (batch-1 엔진) | 베이스라인 |
| B | A + `TENSOR_INPUT=1` (엔진 재수출 불필요) | T2-1 GPU 전처리 소멸 단독 |
| C | `_batch4.engine` + `BATCH_SIZE=4` | T2-2 배치 상각 추가분 |
| D | + `PREFETCH=4` (어느 단이든 독립) | T2-3 디코드 은닉 |

### Tier 3 — 냉동 게이트 정상화 (추론 수 자체 감축, 판정 변경 가능 → G2 필수)

| # | 작업 | 기대 | 리스크 |
|---|---|---|---|
| 3-1 | **AE/김서림 내성 게이트** — absdiff 전 전역 밝기 오프셋(median) 제거(+가벼운 스무딩). 냉동 임계를 냉장 수준(0.02/8)으로 정상화하는 유일한 근본 해법 | 스킵률 25%→50~70% = **추가 30~50%** | 실패 방향은 fail-safe(스킵 실패=무손실)지만 **투표 분모(게이트 통과 수) 축소로 MIN_VOTE_RATIO 컷이 헐거워짐** — 재생 검증에서 ratio/share 게이트 동시 확인 |
| 3-2 | env 완화 병행: `MOTION_GATE_KEEPALIVE=8` (최소 추론 비율 25%→12.5%) | 케이스별 | I16 래치가 취출 순간 보호. 손 최초 등장 프레임은 미보호 — 3-1 없이 단독 적용은 김서림 환경에서 실효 제한 |
| 3-3 | 1/8 그레이 디코드 게이트(사전 게이트) + JPEG 바이트 크기 델타 zero-decode 게이트 | 디코드분 1.4~2.7× (VGA에선 4~8× 아님 — 실측) | MJPEG all-intra라 부분집합 디코드 정합성 위험 0 |

**검증 인프라가 이미 준비됨**: SAVE_DETECTIONS + 세션 아카이브 + AVI 보존 +
render-session → 현장 코퍼스로 "게이트 변경 → 전체 파이프라인 재생 → 최종
과금 diff" (G2, `OPTIMIZED_ARCHITECTURE.md:265-280`)를 이제 실행할 수 있다.
**주의**: 재생 시 `MODEL__VIDEO__DECODER=ffmpeg` 고정 — opencv 경로는 그레이
변환·리사이즈 방식이 달라 게이트 결정이 조용히 달라진다 (개발 PC 스킵률
과대평가).

### Tier 4 — 모델 측 (이득 소, 별도 트랙)

- **predict conf 0.01 → 상향 검토**: NMS 후보 ~8400개의 postprocess 비용
  (~1.7ms+)이 직접 감소. 단 **I4·트랙 연결이 저신뢰 검출을 소비**한다
  (vote_recovery floor 0.35, hand floor 0.30, ByteTrack식 트랙 잇기) —
  0.1 수준까지만, tube_shadow로 트랙 단절 감시하며.
- **INT8**: nano급 이득 1.18~1.27× (~0.7-1.0ms). 캘리브레이션 함정 다수
  (`data=` 생략 시 coco8 8장 폴백 → mAP −48% 전례 / 자기 도메인 ≥500장 /
  배포 기기에서 / batch=8 / conf 재선정). 07-22 결정("프레임당 작업 감소가
  우선")과 일치 — Tier 2·3 이후에.
- **imgsz 480→416/384**: sublinear(~0.6-1.2ms) + **좌표계 상수 전면 재보정
  연쇄** (side ROI, y_split, hand_margin, motion floor, max_jump, crop 계약)
  — 비용 대비 비권장.

## 4. 하지 말 것 (조사로 배제 확정)

| 방향 | 사유 |
|---|---|
| 냉동 조기 종료 허용 (I15 변경) | 3중 독립 근거: 후반 프레임 증거 / weight_is_discriminative=False + ±15g count_gate의 오조합 성립률 / 다중 트레이·pool_exhaustion이 완전 투표 풀 전제. 절감 대비 과금 오류 비대칭 |
| DLA | **Orin Nano에 DLA 없음** (NVIDIA 공식. 검색 상위 proventusnova 글은 거짓 서술) |
| HW MJPEG 디코드 (NVDEC/NVJPG/nvjpegdec) | NVDEC 미지원 코덱, 4:2:2 차단, VGA에서 HW가 CPU에 일관 패배 (25 vs 4.2fps), JP6.0 다중 파일 버그 |
| CUDA multi-stream / 다중 context | Orin 실측 역효과 (10스트림 18ms vs 1회 2ms), NVIDIA 내부 버그 미해결 |
| 멀티프로세싱 / MPS | CUDA context 300~600MB/프로세스 — 4GB 불가 |
| Pruning/KD/2:4 sparsity | FLOPs −65% → latency −25%. TOPS 6~10%만 쓰는 기기에서 무의미. 수 주 소요 |
| DeepStream | 스트림 지향 — 트리거 버스트에 형태 불일치, 인식 로직 전면 재작성 |
| Detect-then-track (추론 프레임 대체) | 프레임 갭 10에서 IDF1 −13pt. 손 취출 = IoU-칼만 최약 영역. (현행 트랙릿 투표 보조는 유지) |
| 프리롤/포스트롤 단축 | 프리롤 첫 30프레임 = head_votes/held 계측 기반 (자르면 이중과금 신호 소멸). 포스트롤 4s = 로드셀 안정화 2.4s 제약 |

## 5. 안전 한계 (프레임을 덜 볼 때 깨지는 것)

- **I16**: 손 래치 활성 중 스킵 금지 — 게이트/배치 어떤 변경도 이 불변식 유지.
- **head 계측은 pos(디코드 위치) 기준** — 게이트·ET로 추론 수를 줄이는 건
  안전(스킵 프레임도 pos는 증가), **디코드 프레임 자체를 줄이는 건 위험**.
  단 `track_held`의 head_obs는 관측 수 기준이라 게이트 스킵 증가에 민감 —
  tube_shadow/held_shadow로 감시.
- **투표 분모 = 게이트 통과 수** — 스킵률을 올리면 ratio 게이트가 자동
  완화됨. 게이트 변경 배치에는 MIN_VOTE_RATIO/SHARE 재점검 동봉.
- 속도 변경과 판정 변경을 **같은 배치에 섞지 않는다** — 현재 진행 중인
  냉동 오분류(#17) 디버깅과 교락 방지. Tier 0/1은 판정 불변이라 즉시 가능.

## 6. 권장 순서 (진행 상태)

1. ~~Tier 0-1 전력 모드 확인~~ **완료 — 25W** (잔여: jetson_clocks 영구화)
2. ~~Tier 1 (1-1, 1-2)~~ **적용 완료 (2026-07-28)** — 실기 다음 세션에서
   processing_time_ms 전후 비교로 절감 실측
3. **Tier 0-4 계측** 추가 후 실기 몇 세션으로 40ms 내부 분해 확정
4. **Tier 2 (2-1 → 2-3 → 2-2)** — G2 재생 하네스(아카이브 AVI + 과금 diff)와 함께
5. **Tier 3 (3-1 게이트 정상화)** — 별도 배치, 재생 검증 후 env 승격
6. Tier 4는 위 결과 실측 후 재평가

목표 궤적: **13.7s → (T1) ~11-13s 예상 → (T2) 5~7s → (T3) 3~5s** —
close_timeout 10s 안으로 들어오는 지점이 T2다.
