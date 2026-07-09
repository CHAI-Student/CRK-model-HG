# Fix Logs

버그 수정 시마다 원인/해결방안을 아래 형식으로 기록한다.

```text
## YYYY-MM-DD 제목 (관련 이슈)
- 증상:
- 원인:
- 해결방안:
- 관련 파일:
- 테스트:
```

---

## 2026-07-09 multi-zone CLOSE 폴링이 FINALIZED에서 영구 고착 (GitHub issue #5)

- 증상: 문 세션이 정상적으로 finalize(`[MULTI-ZONE CLOSE] session=... -> finalized`, `status=complete`)된 이후,
  에지 디바이스가 새 `OPEN` 신호 대신 계속 `CLOSE`로 폴링하면 서버가 동일한 finalized 결제 결과를
  10초 간격으로 무한 반복 응답. 다음 세션으로 전혀 진행되지 않고 "stuck" 상태로 남음.

- 원인: `crk_model/gateway/state_machine.py`의 `MultiZoneGateway`에서 `DoorState.FINALIZED`
  상태를 벗어나는 유일한 경로가 새 `OPEN` 신호(`handle_open`)뿐이었음. `poll()`은 `FINALIZED`일 때
  항상 `self._settle()`의 멱등 캐시 결과를 그대로 반환하도록만 되어 있어(I11 의도는 맞음),
  "결과를 한 번 전달한 뒤에는 세션을 정리한다"는 원본(`CRK-model`, 리팩토링 전) 저장소의 동작이
  누락되어 있었다. 원본은 `door_session_store.py`의 `finalize_global_session()`에서 finalize
  직후 `self._global_session = None`으로 즉시 세션을 비워, 이후 CLOSE 폴링이 오면 "활성 세션
  없음"으로 자연스럽게 빠지도록 되어 있었음 — 리팩토링 과정에서 이 초기화 로직 없이 멱등성만
  이식되어 회귀가 발생함.

- 해결방안: `MultiZoneGateway`에 `finalized_hold_s`(기본 15초) 유예시간을 추가. `FINALIZED` 진입
  시각(`_finalized_ts`)을 기록해두고, 유예시간 이내 재폴링에는 기존과 동일하게 멱등 결과를 반환
  (I11 유지, 네트워크 재전송 등으로 인한 정상 재시도 보호)하되, 유예시간을 넘기면 새 `OPEN` 없이도
  자동으로 `DoorState.IDLE`로 복귀시켜 세션을 정리(`session_id=None`)하도록 수정. 이후 CLOSE 폴링은
  더 이상 finalized 결과를 반복하지 않고 "활성 세션 없음"에 준하는 응답(`status=processing`,
  `detail=session_expired:<session_id>`)으로 자연스럽게 빠진다.

- 관련 파일:
  - `crk_model/gateway/state_machine.py` — `MultiZoneGateway.__init__` (`finalized_hold_s`,
    `_finalized_ts` 추가), `poll()`의 FINALIZED 분기에 유예시간 기반 자동 리셋 추가,
    `handle_open()`에서 `_finalized_ts` 초기화.
  - `tests/test_gateway.py` — `test_finalized_auto_resets_to_idle_after_hold_without_new_open`
    회귀 테스트 추가.

- 테스트: `python -m pytest -q` (85 passed, 3 skipped).

---

## 2026-07-09 (후속 정정) 위 fix의 `finalized_hold_s` 자동 리셋을 되돌리고 로그 중복 억제로 대체

- 증상: 위 fix 배포 후 재현 테스트에서 `POST /api/judge/multi-zone`이 `finalized->idle`
  전환 이후에도 계속 들어오고, `[MULTI-ZONE] state=CLOSE products=11 -> status=processing`
  로그가 반복해서 찍힘. 즉 로그 스팸 자체는 해결되지 않았고 반복되는 문구만
  `finalized`에서 `processing`으로 바뀜.

- 원인(오진단 정정): CLOSE 신호는 원-샷(one-shot) 이벤트가 아니라, 문이 물리적으로
  닫혀있는 동안 에지 장치가 계속 보내는 level-triggered 폴링이다(OPEN이 문이 열려있는
  동안 반복 전송되는 것과 대칭). 실제 이슈 #5 로그도 finalize 이후 CLOSE가 10초
  간격으로 수 분간 계속 들어온 것으로, 이는 정상적인 클라이언트 동작이다. 반면 새
  `OPEN`이 오면 `handle_multi_zone`의 OPEN 분기(`model_service.py`)가
  `DoorState.FINALIZED`에서도 무조건 새 세션을 시작하므로, 다음 세션 진행이 막히는
  일은 원래 없었다. 즉 "FINALIZED가 다음 세션을 막는다"는 최초 진단이 틀렸음.
  직전 fix의 `finalized_hold_s` 타임아웃은 문이 계속 닫혀있는 상황에서 정산 완료
  정보(payment payload, I11 멱등 응답)를 `status=processing`(빈 payload)으로
  덮어써 버려, 실제로는 없던 기능적 회귀를 새로 만들었다. 진짜 문제는 상태 전이가
  아니라 로깅이었다 — `http_app.py`의 `[MULTI-ZONE] ...` 로그와
  `model_service.py`의 `[MULTI-ZONE CLOSE] ... -> finalized` 로그가 매 요청마다
  무조건 찍혀서, 동일 결과가 반복되는 정상 상황이 "멈춘 것처럼" 보였을 뿐임.

- 해결방안:
  1. `crk_model/gateway/state_machine.py`의 `finalized_hold_s`/`_finalized_ts`
     자동 리셋 로직을 되돌려 FINALIZED가 원래대로(I11) 새 OPEN 전까지 멱등하게
     유지되도록 복원.
  2. 로그 중복만 억제: `http_app.py`의 `/api/judge/multi-zone` 핸들러와
     `model_service.py`의 CLOSE 분기 각각에 마지막으로 로깅한 결과 키
     (`state/status/detail`)를 기억해두고, 이전 호출과 동일하면 로그를 생략.
     실제 HTTP 응답(정산 payload 포함)은 매 호출 그대로 반환 — 프로토콜/상태
     동작은 변경하지 않고 로그 볼륨만 줄임.

- 관련 파일:
  - `crk_model/gateway/state_machine.py` — `finalized_hold_s`/`_finalized_ts` 제거,
    FINALIZED 분기를 원래의 무조건 멱등 응답으로 복원.
  - `crk_model/adapters/http_app.py` — `/api/judge/multi-zone`에서 `(state, status,
    detail)`이 이전과 동일하면 `[MULTI-ZONE] ...` 로그 생략.
  - `crk_model/service/model_service.py` — `ModelService._last_close_log_key` 추가,
    CLOSE 분기에서 `(session_id, resp.state, resp.detail)`이 이전과 동일하면
    `[MULTI-ZONE CLOSE] ...` 로그 생략.
  - `tests/test_gateway.py` — 자동 리셋 테스트를
    `test_repoll_after_finalize_stays_finalized_without_new_open`으로 교체
    (긴 시간 경과 후에도 새 OPEN 전까지 FINALIZED가 유지되는지 검증).
  - `tests/test_service.py` — `test_repoll_after_complete_does_not_spam_log` 추가
    (동일 결과 반복 시 `[MULTI-ZONE CLOSE] ... -> finalized` 로그가 1회만 찍히는지 검증).

- 테스트: `python -m pytest -q` (86 passed, 3 skipped).

---

## 2026-07-09 환경 완성 웨이브 — Jetson 4GB 실환경 결함 4종 일괄 수정

깊은 판정 로직 착수 전 인프라 계층 완성 작업(3개 병렬 트랙)에서 수정된 결함들.
기능 추가(NVDEC 디코더, wire 계약 필드, ruff/CI)는 결함이 아니므로 여기선 생략.

- 증상/원인 ① **OOM 위험**: `decode_avi`가 영상 전체를 `list[FrameBundle]`로 메모리에
  상주 — 480×480×3 × ~400프레임 ≈ 카메라당 ~276MB, 두 카메라 동시 처리 시 4GB Jetson에서
  OOM 위험. `LazyAviFrames`도 전체 리스트를 캐시.
- 해결 ①: 제너레이터 스트리밍으로 전환(프레임 1장씩만 상주), 조기 종료 시
  `finally`/`close()`로 cv2/ffmpeg 리소스 즉시 해제. 0프레임/열기 실패는 첫 `next()`에서
  IOError 판정(I1 유지).

- 증상/원인 ② **동시성 무방비**: FastAPI sync 엔드포인트는 anyio threadpool에서 병렬
  실행되고 별도 워커 스레드가 상시 drain을 도는데, 게이트웨이 상태·배리어 카운터·
  EventLog·스냅샷에 락이 전혀 없었음 (원본 DoorSessionStore는 `self._lock` 사용).
- 해결 ②: `ModelService` 소유 단일 RLock — handle_trigger/handle_multi_zone의 상태 변이
  구간 보호, 워커는 이벤트 1건 단위로만 락(파이프라인 추론은 락 밖 → 수 초짜리 추론이
  폴링을 블록하지 않음).

- 증상/원인 ③ **무한 성장(24h+ soak 불가)**: `worker.outcomes` list 무한 append,
  EventLog/settler 멱등 캐시 세션 무한 누적, 저널 JSONL 단일 파일 무한 성장.
- 해결 ③: outcomes `deque(maxlen)`, 새 세션 OPEN 시 최근 K세션만 남기고 prune
  (I11: 현재+직전 세션 캐시는 보존 — CLOSE 재폴링 멱등성 유지 검증 테스트 포함),
  저널 일자별 로테이션 + 보존기간 삭제. 알려진 잔여: `EventLog.rejected`는 아직 무상한
  (거부 이벤트는 드물어 후속 트랙으로 이관).

- 증상/원인 ④ **모션 게이트 파이썬 병목**: `_diff_ratio`가 120×120=14,400픽셀을 순수
  파이썬 이중 루프로 순회 — 트리거당 ~700프레임이면 루프 1천만 회로, L1 설계가 가정한
  프레임당 1~2ms를 크게 초과해 게이트 이득을 잠식.
- 해결 ④: numpy ndarray 입력이면 벡터화 fast path(int16 승격으로 uint8 오버플로 방지),
  numpy 부재/리스트 입력은 기존 순수 파이썬 폴백 — 판정 등가성 테스트 포함.

- 관련 파일: `crk_model/adapters/avi_frames.py`(전면 재작성),
  `crk_model/frames/motion_gate.py`, `crk_model/service/pipeline.py`(frames 계약
  Iterable화), `crk_model/service/{model_service,worker}.py`(락·상한),
  `crk_model/ledger/{events,settler,journal}.py`(prune·로테이션),
  `crk_model/core/config.py`(신규 설정), `crk_model/adapters/http_app.py`(wire 계약),
  `tests/test_frames_streaming.py`·`tests/test_lifecycle.py`·`tests/test_wire_contract.py`(신규).

- 테스트: `python -m pytest -q` → 122 passed (numpy/ffmpeg/fastapi 설치 환경 기준.
  미설치 환경은 해당 테스트 skip으로 114 passed). Jetson 실기(G4: NVDEC 경로,
  24h soak)는 미검증으로 남음.

---

## 2026-07-09 freezer에서 vision 후보 없을 때 loadcell-only 정체성 판정이 억제되지 않던 결함

- 증상: L5 판정 확장 중 원본 다이어그램 5와의 대조에서 발견 (실기 오과금 보고는 아직
  없음 — 잠재 결함). freezer 존에서 vision 후보가 0개일 때 `NoCandidateFallbackStrategy`가
  프로파일 구분 없이 weight_only(무게만으로 품목 식별)를 수행 — 로드셀 오차 5~15g인
  냉동고에서 무게로 "무엇인지"를 판정하면 오식별 과금 위험 (178g 사건과 동일 원리).

- 원인: 원본 judge()의 "후보 없음 폴백" 체인에 있던 vision-first identity policy 분기
  (freezer → `loadcell_identity_suppressed` 억제)가 라우터 이식 시 누락. 골격 이식 단계에서
  weight_only 경로만 옮겨지고 프로파일 조건 분기가 빠졌음.

- 해결방안: `NoCandidateFallbackStrategy.solve()`에서
  `not profile.weight_is_discriminative`(freezer)이면 weight_only를 건너뛰고
  `NO_DETECTION, reason="loadcell_identity_suppressed"` 반환 (QA Q1의 센서 물리 원칙을
  후보-없음 경로에도 적용). 냉장고(±3g)는 기존 weight_only 유지. 같은 원리로 신규
  `RelaxedLoadcellOnlyStrategy`도 freezer 억제를 내장.

- 관련 파일: `crk_model/judgment/strategies.py`, `crk_model/judgment/router.py`
  (누락 전략 4종 추가와 함께 수정), `tests/test_judgment.py`
  (`TestNoCandidateFreezerSuppression` 등 신규 28건).

- 테스트: `python -m pytest -q` → 136 passed.

---

## 2026-07-09 live_engine_preview 카메라 열기 실패 시 진단 부재 (GitHub issue #7)

- 증상: Jetson 실기에서 `--source 0`, `--source 2` 모두
  `VIDEOIO(V4L2:/dev/videoN): can't open camera by index` 후
  `ERROR camera/video source could not be opened` 한 줄로 종료 — 어떤 장치가
  존재하는지, 누가 점유 중인지, CSI인지 알 수 없어 현장에서 진행 불가.

- 원인: ① 실행 환경 요인(코드 결함 아님) — 자판기에서는 CRK-CAMERA/Edge_Environment
  캡처 서비스가 카메라를 상시 점유(V4L2는 배타 오픈)하거나 CSI 카메라라 V4L2
  인덱스로 열 수 없음. ② 스크립트 결함 — 실패 시 원인 판별에 필요한 진단
  (장치 목록·점유 프로세스·CSI 안내)을 전혀 출력하지 않았고, CSI/GStreamer
  소스를 지정할 방법도 없었음. 참고: 이슈 로그의 실행 경로는 원본 레포
  (~/Codes/CRK-model)였으나 캡처 로직이 동일해 어느 쪽이든 같은 증상.

- 해결방안: `--list-devices` 진단(모델 로드 없이 /dev/video* 열거,
  `v4l2-ctl --list-devices`, fuser/lsof 점유 프로세스 표시) + 열기 실패 시 동일
  진단 자동 실행 + 발견 장치 기반 재시도 커맨드 예시 출력. 소스 형식 확장:
  `/dev/videoN` 경로, `csi:N`(nvarguscamerasrc 파이프라인 자동 조립),
  `gst:<pipeline>`. README 트러블슈팅 소절 추가.

- 관련 파일: `scripts/live_engine_preview.py`, `README.md`.

- 테스트: `python -m pytest -q` → 145 passed, cv2 없는 macOS에서
  `--help`/`--list-devices` 동작 확인. Jetson 실기 재검증 대기.

---

## 2026-07-09 실기 오판정 — 상품 class 매핑 전멸 + weight_only 다품목 조합 과금 (GitHub issue #6)

- 증상: 실기에서 두 존 모두 오과금 — zone4 만두 1개(정답)를 베이글+라라스윗으로,
  zone5 베이글 1개(정답)를 요맘때+라라스윗으로 과금. 세션 아카이브 YAML로 확인:
  두 트리거 모두 `vision_candidates: []`(yolo_calls 300+에도 최종 후보 0) →
  `no_candidate_fallback/weight_only`(conf 0.3)가 무게 조합으로 판정.

- 원인 (3중):
  1. **상품→YOLO class 매핑 전멸 (확정)**: 아카이브에 상품 `class_id: 0`(hand
     클래스!) 기록. 원본의 camelCase alias(`trainingIdx`/`yoloClassId`)와
     엔진 class_names 기반 **이름 매핑**(manager.py yolo_name_to_id →
     ActiveProductStore)이 어댑터 이식에서 누락 — 숫자 필드 3종만 보고 기본값 0.
     전 상품이 class_id=0으로 붕괴해 vision 계열 전략이 구조적으로 매칭 불가,
     모든 트리거가 weight_only로 추락.
  2. **weight_only 다품목 조합 과금**: 원본 `judge_by_weight_only`는
     nearest-single(단일 품목)이었는데 우리는 StrictWeightMatcher 조합 탐색을
     그대로 써서, 우연히 합이 맞는 2품목 조합(140+87≈227 등)을 complete로 과금.
  3. **무게 DB 불일치 (데이터, 코드 밖)**: 정답 상품의 공칭 무게와 실측 delta가
     13~27g(10~15%) 차이 — 라벨 무게 vs 포장 포함 총중량. ±3g 톨러런스로는
     정답이 매칭될 수 없는 상태에서 우연 조합만 통과. → 상품 DB unit_weight를
     실측 기준으로 재등록 필요 (운영 이관).

- 해결방안: ① 숫자 alias 전체 복원 + 이름 매핑(product_eng_name→name, 대소문자
  무시) + unmapped는 0이 아닌 **-1**(hand 충돌 방지) + `_product_by_class`에서
  `class_id <= 0` 제외 + OPEN 시 `mapped=n/total unmapped=[...]` 경고 로그.
  ② weight_only를 단일 품목·유일 매칭으로 제한, 톨러런스 창에 2상품 이상이면
  `weight_only_ambiguous`로 NO_DETECTION (오과금 < 매출 누락, D9 fail-closed).
  ③ vision 후보 0의 원인(모델 미검출/필터 제거/투표 임계 미달) 규명용
  `vote_summary`(클래스별 votes/ratio/conf/탈락 사유)를 trace→세션 아카이브에 기록.

- 관련 파일: `crk_model/adapters/{http_app,serve,yolo_detector}.py`,
  `crk_model/judgment/strategies.py`, `crk_model/perception/voting.py`,
  `crk_model/service/{model_service,pipeline}.py`, `crk_model/ledger/archive.py`,
  `tests/test_product_mapping.py`(신규), `tests/test_judgment.py`.

- 테스트: `python -m pytest -q` → 165 passed. vision 후보 0 원인은 다음 실기
  재현의 vote_summary로 확정 예정.

---

## 2026-07-09 냉동 기기가 냉장 프로파일로 동작 (cabinet_type 미이식) + weight_only 동일 상품 다수 개수 미지원

- 증상: ① 실기 기기는 냉동(freezer)인데 이 레포는 존 목록 오버라이드
  `MODEL__ZONES__FREEZER`만 지원해, 이를 설정하지 않으면 전 존이 냉장
  (REFRIGERATOR ±3g) 프로파일로 판정됨 — 이슈 #6 오판정의 공동 원인. ②
  직전 수정(위 항목, issue #6 대응)에서 weight_only를 "단일 품목·count=1
  유일 매칭"으로 과도 제한해, 동일 상품 n개 제거(delta = n × unit_weight)가
  no_detection으로 빠지는 회귀가 발생.

- 원인: ① 원본(`reference/CRK-model/services/model/model_service/core/config.py`
  60-75행)은 기기 단위 정적 설정 `MachineModel.cabinet_type`
  (`MODEL__MACHINE__CABINET_TYPE`, "refrigerated"|"freezer", 기본
  refrigerated)을 두고 `_is_freezer_mode()`가 이를 참조했는데, 이 설정과
  기본 프로파일 결정 로직이 이관 과정에서 누락되어 존 단위 오버라이드
  (`freezer_zones`)만 남았음. ② 직전 결함 수정이 issue #6(2품목 우연 조합
  오청구)을 막으려고 weight_only의 count를 아예 1로 고정해버려, "동일 상품
  n개"라는 정상 케이스까지 함께 차단했음(다품목 조합 금지와 동일 상품 개수
  허용을 구분하지 않은 과도 일반화).

- 해결방안:
  1. `crk_model/core/config.py`: `Settings.cabinet_type: str = "refrigerated"`
     추가, `from_env()`가 `MODEL__MACHINE__CABINET_TYPE`을 읽어 원본처럼
     `strip().lower()` 정규화 후 `"refrigerated"|"freezer"` 외 값이면
     `ValueError`(원본 `validate_cabinet_type` 대응, 오타로 조용히 냉장이
     되는 사고 방지).
  2. `crk_model/service/model_service.py`: `_default_profile_from_settings()`
     신설(cabinet_type=="freezer"면 FREEZER, 아니면 REFRIGERATOR) —
     `ModelService.__init__`이 이를 계산해 `self._default_profile`로 보관하고
     기동 로그(`[CONFIG] cabinet_type=... default_profile=... freezer_zones=...`)
     1줄을 남긴다. `MODEL__ZONES__FREEZER`(freezer_zones)는 여전히 기본
     프로파일에 대한 존 단위 오버라이드로만 동작(의미 변경 없음).
  3. `crk_model/service/pipeline.py`: `TriggerPipeline`에 `default_profile`
     파라미터(기본값 `REFRIGERATOR`, 기존 동작과 100% 호환) 추가,
     `_process`의 하드코딩된 `self._profiles.get(req.zone, REFRIGERATOR)`
     폴백을 `self._profiles.get(req.zone, self._default_profile)`로 교체.
     `ModelService`가 생성 시 `default_profile=self._default_profile`을
     주입해 존 미지정 시에도 냉동 기기는 기본이 FREEZER가 되게 한다(다른
     로직은 변경 없음, 최소 diff).
  4. `crk_model/ledger/settler.py`·`crk_model/gateway/state_machine.py`:
     CLOSE 정산/잠정 집계의 존 프로파일 폴백도 REFRIGERATOR 하드코딩이어서,
     cabinet_type=freezer일 때 판정(pipeline)은 FREEZER인데 정산의
     tolerance·count gate는 냉장 ±3g로 계산되는 불일치가 남아 있었음
     (판정·정산 tolerance 단일 소스 원칙 위반, 결제 금액에 직접 영향 —
     예: removal -100g 후 return +90g이 freezer ±15g면 반품 매칭으로 0원,
     냉장 폴백이면 미매칭으로 1개 과금). `_profile()`/`pass_same_zone()`/
     `interim_summary()`에 `default`(기본값 REFRIGERATOR, 하위호환) 파라미터,
     `CloseSettler`·`MultiZoneGateway`에 `default_profile` 생성자 파라미터를
     추가하고, `ModelService`가 세 경로(pipeline·settler·gateway interim)
     모두에 같은 `self._default_profile`을 주입.
  5. `crk_model/judgment/strategies.py`의 `NoCandidateFallbackStrategy`
     weight_only 로직 확장: 각 상품 p에 대해 n ∈ 1..min(stock,
     `StrictWeightMatcher.max_items`=6)에서 `|target − n×unit_weight| ≤
     tolerance`인 (p, n) 쌍을 전수 수집. 결과가 정확히 1쌍이면 채택
     (reason="weight_only", count=n, I12: n ≤ stock), 2쌍 이상(서로 다른
     상품이거나 같은 상품에 서로 다른 n이 동시에 그럴듯한 경우 포함)이면
     `weight_only_ambiguous`로 NO_DETECTION(기존 fail-closed 유지), 0쌍이면
     `no_candidates_forced_final`. 서로 다른 상품을 섞는 다품목 조합 탐색은
     여전히 금지(issue #6 재발 방지 유지) — 이번 확장은 "동일 상품 n개"만
     구제한다. freezer 억제(`loadcell_identity_suppressed`)는 그대로 유지.

- 관련 파일: `crk_model/core/config.py`, `crk_model/service/model_service.py`,
  `crk_model/service/pipeline.py`(default_profile 파라미터 추가, 최소 diff),
  `crk_model/ledger/settler.py`·`crk_model/gateway/state_machine.py`
  (정산/잠정 집계 폴백 프로파일 주입), `crk_model/judgment/strategies.py`,
  `tests/test_judgment.py`(`TestWeightOnlySameProductCount` 신규 3건 + 다품목
  조합 금지 회귀 테스트를 실제 다품목 케이스로 정정), `tests/test_lifecycle.py`
  (`TestCabinetTypeDefaultProfile` 신규 4건 — freezer 기본 프로파일 판정 E2E,
  refrigerated 회귀 방지, CLOSE 정산 freezer tolerance E2E, 잘못된
  cabinet_type 값 거부), `README.md`(Configuration 표에
  `MODEL__MACHINE__CABINET_TYPE` 행 추가).

- 테스트: `python -m pytest -q` → 172 passed (기존 165 + 신규 7). `ruff check .`
  → All checks passed.

---

## 2026-07-09 투표 앙상블이 원본보다 과보수적으로 이식되어 vision 후보 전멸 (이슈 #6 유력 원인)

- 증상: 실기에서 yolo_calls 300+에도 `vision_candidates: []`가 계속 발생 —
  전 트리거가 weight_only 판정으로 강등되어 손실 방지형(fail-closed) 로직이
  발동, 실제로는 정상 촬영된 케이스까지 저신뢰 처리·오과금 위험.

- 원인: `crk_model/perception/voting.py`의 `combine()`이 단일 카메라 검출도
  양쪽 카메라 검출과 동일한 공용 가중치(top 0.5 / side 0.5)를 썼다. 한쪽
  카메라만 검출되면 다른 쪽이 0이 되어 conf가 사실상 반토막(top conf=0.7
  단일 검출 → weighted=0.35)나, 결합 후 conf_floor(0.4) 문턱을 넘지 못하고
  전멸했다. 원본
  `reference/CRK-model/services/model/model_service/video/voting_ensemble.py`
  (327-458행)을 정독한 결과, 원본은 처음부터 단일 카메라 전용
  `top_only_weight`(0.60)/`side_only_weight`(0.40) 가중치를 별도로 두고
  있었다(`config.py` 244-264행, 기본 top_weight=0.60/side_weight=0.40도
  우리 구버전의 0.5/0.5와 다름). 우리 이식본은 이 분기를 통째로 누락했다.

  덧붙여 "conf 0.01→0.4 2단계"(`REDESIGN_RATIONALE_QA.md` Q4, I4)라는
  전제도 원본 코드로 재확인한 결과 부정확했다. 원본에서 conf=0.01은
  YOLO 엔진 내부 NMS 파라미터(`yolo_wrapper.py`
  `yolo_internal_conf_threshold`)일 뿐이고, 실제 conf 컷은
  `video_processor.py`의 프레임 루프에서 **투표 등록 이전**에
  `det.conf < _threshold_for_camera(camera_type)`(기본 top/side 각 0.70,
  실배포 `jetson-stride2.env` 0.70 · `.env.example` 0.50)로 걸린다.
  결합(combine) 이후 필터는 `vote_ratio >= min_vote_ratio OR
  vote_count >= min_vote_count`뿐이며(`video_processor.py` 3079-3087행),
  원본 `combine()`에는 결합 후 conf 하한이 아예 없다. "0.4"라는 수치는
  원본 어디에도 없다(`multi_kind_min_confidence` 기본값은 0.18로 별개
  용도, `HAND_CONFIDENCE_THRESHOLD=0.40`은 손 검출 전용이라 이것과 혼동된
  것으로 보임).

- 해결방안:
  1. `crk_model/perception/voting.py`: `VotingEnsemble.__init__`에
     `top_weight`(기본 0.60)·`side_weight`(기본 0.40, 구버전 0.5/0.5에서
     변경)·`top_only_weight`(0.60)·`side_only_weight`(0.40)·
     `common_class_bonus`(0.2, 기존 유지) 파라미터를 추가.
     `_weighted_confidence()` 신설: 양쪽 카메라 검출 시
     `top*top_weight + side*side_weight + min(top,side)*common_class_bonus`
     (원본 dynamic_bonus 산식과 동형), 단일 카메라 검출 시
     `conf * top_only_weight` 또는 `conf * side_only_weight` 전용
     가중치를 사용하도록 `combine()`·`debug_summary()`를 정렬.
     상한 clamp(`min(weighted, 1.0)`)도 원본과 동일하게 추가.
  2. conf_floor(결합 후 하한)는 원본에 대응 개념이 없지만 **의도적으로
     유지**한다 — 우리 아키텍처는 `filters.py`에서 프레임 단계 conf 컷을
     하지 않기로 이미 설계돼 있고(I4 주석, "conf 필터는 여기서 하지
     않는다"), 그 설계를 지키는 한 결합 후 안전판을 완전히 없애면 노이즈
     검출이 그대로 후보가 되는 회귀가 생긴다. top_only/side_only 가중치
     정렬만으로 이슈 #6이 지목한 회귀(고신뢰 단일 카메라 검출의 conf
     반토막)는 해소되므로, conf_floor 값(0.4)·필터 위치는 손대지 않았다.
  3. `crk_model/service/pipeline.py`: 변경 없음 (`VotingEnsemble()`이
     새 파라미터 기본값만 사용하므로 diff 불필요, 확인만 수행).
  4. vote_ratio 분모("게이트 통과 프레임 수")는 원본과 다르게 유지되는
     `OPTIMIZED_ARCHITECTURE.md` L1 승인 조건의 의도된 재설계이며, 이번
     조사·수정 대상에서 제외했다(원본으로 되돌리지 않음).

- 관련 파일: `crk_model/perception/voting.py`(가중치 산식 정렬),
  `tests/test_perception.py`(`test_weighted_conf_formula` 기대값을 원본
  가중치 0.60/0.40로 갱신 + 신규 4건:
  `test_single_camera_high_conf_survives_as_candidate`(이슈 #6 회귀
  재현·해소 확인), `test_side_only_uses_side_only_weight`,
  `test_common_class_bonus_both_cameras_detected`,
  `test_top_only_weight_exceeds_side_only_weight_for_equal_confidence`).

- 테스트: `python -m pytest -q` → 176 passed (기존 172 + 신규 4). `ruff check .`
  → All checks passed.

---

## 2026-07-09 확정 후 CLOSE 재폴링에 complete 반복 → 에지 device busy 영구 유지 (issue #5 계열 3차 — 최종 정정)

- 증상: finalize 이후에도 에지(Edge_Environment)가 `POST /api/judge/multi-zone`을
  계속 보내며 device busy 상태가 해제되지 않음. 우리 서버는 CLOSE 재폴링마다
  동일한 `status=complete` + 결제 페이로드를 멱등 반복 응답 중이었음.

- 원인 (2차 정정의 오류 정정): issue #5의 2차 수정에서 "CLOSE는 level-triggered
  이므로 결제 확정 정보를 매번 그대로 돌려줘야 한다"고 전제했는데, 이는 원본
  코드와 대조하지 않은 추정이었다. 원본은 `finalize_global_session()`이 확정
  직후 `_global_session = None`으로 세션을 즉시 비워 **확정 결과를 정확히 1회만
  전달**하고, 이후 CLOSE 폴링에는 `"No active door session to close"`
  (success=True, status="success", 빈 zones)를 응답한다 — 에지는 바로 이 응답을
  받아야 device busy를 해제한다. complete를 반복 주면 에지 상태기계가 트랜잭션
  종료를 인지하지 못한다 (실기 확인).

- 해결방안: ① `MultiZoneGateway.poll()`의 확정 분기에서 결제 페이로드를 실은
  FINALIZED 응답을 반환한 직후 `state = IDLE`로 즉시 복귀 (원본 동형, session_id는
  late trigger 귀속·사후 추적용으로 유지 — 다음 OPEN이 새 ID 발급). FINALIZED는
  더 이상 지속 상태가 아니므로 재폴링 멱등 분기 삭제. ② `handle_multi_zone`
  CLOSE 분기에서 게이트웨이가 IDLE이면 원본 wire 계약대로 "No active door
  session to close" 응답. ③ I11(이중 과금 불가)은 wire 반복 전달이 아니라
  settler의 세션 키 멱등 캐시가 보장함을 테스트로 명시
  (`test_settlement_idempotent_at_settler_layer`). [OPS][CLOSE]·세션 아카이브는
  확정 "그 호출"에서 1회 실행되므로 영향 없음.

- 관련 파일: `crk_model/gateway/state_machine.py`,
  `crk_model/service/model_service.py`, `tests/test_gateway.py`
  (`test_finalize_delivers_once_then_returns_to_idle`), `tests/test_service.py`
  (`test_close_after_delivery_reports_no_active_session`).

- 테스트: `python -m pytest -q` → 176 passed. 스모크: OPEN→trigger→CLOSE(complete
  1회)→CLOSE×3(전부 "No active door session", door_state=idle)→새 OPEN 정상.

---

## 2026-07-09 vote_summary로 확정: conf_floor 평균 희석 전멸 → 원본식 투표 진입 컷 이식 + 비전 env 튜닝 개방 (issue #6 3차)

- 증상: cabinet_type=freezer·매핑 mapped=11/11 정상 적용 후에도 여전히
  `vision_candidates: []` → no_candidate_fallback (freezer 억제 덕에 오과금은
  없어졌으나 0원 no_detection = 매출 누락). 이번엔 vote_summary가 남아 원인
  확정: 실제 상품이 94~96표(360프레임 중 26%)를 받고도 **전부
  `rejected_by: conf_floor`** — 저신뢰(0.01~) 투표까지 평균에 섞여 클래스별
  weighted_conf가 0.10~0.16에 머묾. 모델은 정상적으로 보고 있었음.
  부수 단서: side 카메라 검출 195프레임 중 194개가 필터 제거(단계 미상).

- 원인: 원본의 노이즈 방어 지점은 **투표 진입 전 카메라별 conf 임계**
  (top/side_confidence_threshold, 코드 기본 0.70·운영 .env.example 0.50)이고
  결합 후 하한은 존재하지 않는다. 우리는 진입 컷 없이 conf 0.01부터 전부
  투표시켜 평균을 희석시킨 뒤 결합 후 하한(0.4)을 걸었다 — 다수의 중간 conf
  검출이 구조적으로 전멸하는 조합. 또한 이 임계들이 전부 하드코딩이라 현장
  튜닝이 불가능했다.

- 해결방안: ① VotingEnsemble에 카메라별 진입 컷(entry_conf_top/side) 이식 +
  진입 탈락 카운터. ② MODEL__VISION__TOP/SIDE_CONFIDENCE_THRESHOLD(기본 0.70),
  MIN_VOTE_RATIO(0.05)/MIN_VOTE_COUNT(3), CONF_FLOOR(기본 0.0 — 원본 동형),
  SIDE_ROI_MAX_CENTER_X(240)를 Settings→pipeline으로 배선해 env 튜닝 개방.
  ③ .env.example 신설 — 전 env 문서화 + vote_summary 읽는 법·냉동 기기 권장
  시작값(진입 컷 0.50, 안 잡히면 0.35) 가이드. ④ side 필터 미스터리 규명용
  filter_drops_by_stage(side_roi/hand_path 단계별)·entry_dropped_by_camera를
  vote_summary에 추가 — 다음 재현에서 어느 단계가 지웠는지 즉시 판별.

- 관련 파일: `crk_model/perception/voting.py`, `crk_model/perception/filters.py`,
  `crk_model/service/pipeline.py`, `crk_model/service/model_service.py`,
  `crk_model/core/config.py`, `.env.example`(신규), `README.md`,
  `tests/test_perception.py`, `tests/test_lifecycle.py`.

- 테스트: `python -m pytest -q` → 182 passed. 실기 재현 대기 — .env에
  진입 컷 0.50부터 시작, vote_summary의 entry_dropped/rejected_by로 조정.
