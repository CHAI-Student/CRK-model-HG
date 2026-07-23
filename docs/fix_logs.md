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

---

## 2026-07-09 추론 성공했으나 Node "결제할 내역이 없습니다" — 결제 페이로드 wire 형식 불일치 (issue #6 4차)

- 증상: 튜닝 후 추론·정산 전부 정상 (메로나 800원 freezer_vision_first conf 0.76,
  만두 3,700원 + 교차존 net_delta 보정) — 그러나 Node/키오스크가
  "결제할 내역이 없습니다"를 표시하며 결제 미진행.

- 원인: 우리 확정 응답이 `{"status": "complete", zones: [{products:
  [{product_id, unit_price, total_price}]}], ...}` 형식인데, 원본 finalize
  응답(multi_zone.py 1108-1128행)은 `success: true` + `status: "success"` +
  **평탄화된 `products` 배열**("Node.js 하위 호환" 주석 명시) + 상품 항목 키
  `productIdx`(IF11 문자열)/`productId`(YOLO class)/`name`/`count`/`price`다.
  Node는 이 계약으로 결제 항목을 읽으므로 우리 응답에서 결제 내역을 찾지 못함.

- 해결방안: `build_payment_payload`를 원본 finalize wire 형식으로 재작성 —
  success/status("success"|"complete_no_products")/has_products/
  global_session_id/평탄화 products/zones(productNames·productCounts·
  weightDelta 포함)/totalPrice/totalProductCount/productCount. 에러 응답에도
  `success: false` 추가. confidence는 정산 결과에 per-product 값이 없어 0.0
  고정(표시용). I10(확정 타입만 통과)·I13(blocked 차단)은 그대로.

- 관련 파일: `crk_model/gateway/state_machine.py`(build_payment_payload),
  `crk_model/service/model_service.py`(_to_response), tests 5개 파일의
  status 기대값 "complete"→"success" (상품 0개 확정은 "complete_no_products").

- 테스트: `python -m pytest -q` → 182 passed. 스모크로 원본 동형 응답 확인.
  실기 재검증 대기 (결제 연동).

---

## 2026-07-09 CLOSE가 카메라 업로드보다 빨라 0원 확정 + late trigger rejected (GitHub issue #8)

- 증상: 만두 2개 취출 후 문 닫음 — CLOSE 도착 시점(18:32:24.358)에 트리거가
  아직 없어(queue_pending=0) 배리어가 자명하게 충족 → 0원(complete_no_products)
  즉시 확정. 0.66초 뒤 /trigger 도착, 추론은 성공(만두×2, -434.4g)했으나
  `event rejected (session already finalized)` — 7,400원 매출 누락.

- 원인: 인과 배리어(I17)는 "도착한" 트리거만 셀 수 있다 — 문 닫힘 시점에
  카메라가 아직 AVI를 기록/업로드 중이면 그 트리거는 배리어에 보이지 않는다.
  원본은 정확히 이 레이스를 `close_initial_wait_seconds=3.0`(첫 CLOSE 후 시간
  대기)으로 방어했는데, 우리 재설계는 고정 대기를 카메라 seq 워터마크(D2,
  I17 ③)로 대체하는 전제로 제거했다. seq는 펌웨어 미배포(P5) 상태라 방어가
  없는 채로 "큐가 비면 즉시 확정"이 실기에서 그대로 발동한 것.

- 해결방안: CLOSE 유예 창 복원 — 배리어가 충족돼도
  `max(close_ts, 마지막 트리거 도착 시각) + close_grace_s`(기본 3.0,
  `MODEL__CLOSE__GRACE_S`)까지 확정을 보류하고 `close_grace_pending`으로 응답.
  유예 내 도착한 late trigger는 배리어를 다시 열어 정상 수용된다.
  seq_watermark가 온 CLOSE는 인과 신호가 완결이므로 유예 생략(즉시 확정).
  Node 폴링이 ~10s 주기이므로 체감 지연은 다음 폴링 1회 수준(원본과 동일).

- 관련 파일: `crk_model/gateway/state_machine.py`(close_grace_s, _last_enqueue_ts,
  _watermark_set), `crk_model/core/config.py`, `crk_model/service/model_service.py`,
  `.env.example`, `README.md`, `tests/test_gateway.py`(TestCloseGrace 3건 —
  실측 0.66s 타이밍 재현 포함), 기존 테스트 헬퍼들은 close_grace_s=0으로 고정
  (유예는 전용 테스트에서만 검증).

- 테스트: `python -m pytest -q` → 185 passed. 스모크: CLOSE 선도착 → 유예 →
  late trigger 수용 → 3,700원 정상 확정. 실기 재검증 대기.

---

## 2026-07-09 (issue #8 후속) 엣지 워터마크 — 카메라 펌웨어 없이 인과 배리어 완결

- 배경: issue #8의 CLOSE 유예(3s)는 시간 휴리스틱 — 근본 해결은 "close 시점에
  트리거가 몇 개 생겼는지"라는 인과 정보인데, 원설계(D2 카메라 seq)는 펌웨어
  변경(P5)이 필요해 보류 상태였다. 관찰: 녹화 디렉토리
  (`Edge_Environment/<세션>/inference/zone_N/…`)의 소유자가 엣지(Node)이므로,
  Node가 close 시점에 존별 녹화 수를 세어 CLOSE payload에 실을 수 있다.

- 구현: CLOSE payload에 `expected_triggers: {"존": 수}` (선택 필드) 수신 —
  CausalBarrier에 개수 기반 워터마크(I17 ③')를 추가해 존별 도착 수가 기대에
  못 미치면 `awaiting_triggers`로 확정 보류, 전부 도착하면 시간 유예 없이
  즉시 확정. 기대 트리거 미도착은 close_timeout에서 ERROR (D9 fail-closed).
  워터마크 부재 시 기존 유예 3초 폴백 (Node 무변경 하위호환). 어댑터에서
  JSON 문자열 키 정규화, 파싱 불가 값은 무시(부가 신호 원칙).

- 관련 파일: `crk_model/ledger/barrier.py`(set_expected_counts),
  `crk_model/gateway/state_machine.py`(handle_close expected_triggers),
  `crk_model/adapters/http_app.py`(_int_key_counts 정규화),
  `crk_model/service/model_service.py`, `README.md`(Node 구현 가이드),
  `tests/test_gateway.py`(TestEdgeWatermark 3건).

- 테스트: `python -m pytest -q` → 188 passed. E2E 스모크: 워터마크 CLOSE →
  awaiting_triggers 보류 → 0.66s 후 트리거 도착 → 유예 없이 즉시 3,700원 확정.
  Node 측 구현은 Edge_Environment 팀 몫 (README 가이드 참조).

## 2026-07-22 원본 정합 웨이브 1 — left-crop·classes·max-conf (perf-gap 보고서 P0-1·P0-2·P1-4)

**배경**: 원본 CRK-model(d104bca)과의 전수 비교(ref/present/model-perf-gap-report.md)에서
같은 .engine을 쓰는데도 HG 성능이 낮은 구조적 원인 7건이 확정됐다. 이 중
env 튜닝으로 복구 불가능한 상위 3건을 이식했다.

**P0-1 — 입력 기하: squash resize → left-crop** (`adapters/avi_frames.py`)
- 구현: opencv 경로는 `img[:size, :size]` 슬라이스 후 부족분만 리사이즈,
  ffmpeg 경로는 `-s 480x480` 대신 `-vf crop=min(iw\,480):min(ih\,480):0:0,scale=480:480`.
  운영 640×480 소스에서는 무손실 좌크롭만 발생(스케일은 1:1 통과), 소형
  테스트 픽스처만 리사이즈 보정.
- 근거: 원본은 yolo_wrapper `_preprocess_image`의 crop_policy="left"로 오른쪽
  160px(존 바깥)를 버리고 비율을 보존한다 — 실운영 트레이스
  `preprocess.crop_box {x1:0, x2:480}` 확인. 엔진이 이 기하에서 운영돼 왔으므로
  squash(가로 25% 압축)는 conf 하락 + bbox 좌표계 왜곡의 원천이었다.
- 파생 정렬: `SIDE_ROI_MAX_CENTER_X` 기본 240 → **400** (config·filters·
  .env.example·README). 240은 squash 좌표계 산물로 side 검출 194/195 제거
  사건의 원인, 400이 crop 좌표계의 원본 정합값(side_roi_x_max=400).
  ※ **배포 Jetson의 실제 .env에 SIDE_ROI=480(구 임시 해제값)이 남아 있으면
  400으로 갱신할 것.**

**P0-2 — predict classes 허용목록** (`perception/detector.py`, `adapters/yolo_detector.py`,
`service/pipeline.py`)
- Detector 프로토콜에 `allowed_class_ids` 추가 (None=무제한, 빈 목록=fail-closed
  즉시 [] — predict 호출 없음). 어댑터는 `predict(classes=...)`로 전달.
- 파이프라인이 카메라별 목록 구성 (원본 `_inference_allowed_class_ids` 동형):
  top = 매핑된 판매중 상품 + hand(0), side = 상품만 (원본은 side에서 hand를
  추론하지 않는다 — side 래치는 absdiff 모션+keepalive로 동작). 미매핑
  센티널(-1)은 제외, 매핑 0개면 `no_mapped_class_ids` reason code.
- 근거: conf=0.01 + max_det=20에서 전 클래스 추론 시 노이즈 클래스가 20슬롯을
  잠식해 저신뢰 실상품(냉동 김서림 0.2~0.4대)을 밀어냈다. 원본은 실운영에서
  allowed_class_ids 11종으로 제한(트레이스 확인). 부수 효과: 비판매 클래스가
  min_vote_share의 1위 기준을 오염시키는 경로도 차단.
- `HAND_CLASS_ID = 0` 상수를 detector.py로 단일화 (시스템 계약 — 상품 매핑이
  -1 센티널을 쓰는 이유).

**P1-4 — conf 결합: 진입컷 통과 표 평균 → 카메라별 max** (`perception/voting.py`)
- `_weighted_confidence` 입력을 `mean` → `max`로 변경. 산식(0.6/0.4+bonus 0.2,
  top_only/side_only)은 기존과 동일 — 입력만 원본
  (top/side_max_confidence)과 동형이 됐다.
- 근거: 평균 결합은 같은 장면에서 최종 conf가 원본보다 항상 낮게 나온다
  (0.72 1회+0.45 20회 → 원본 0.72 vs 평균 0.46). conf_floor=0.0 기본에서
  후보 생존에는 영향 없지만, 후단 판정의 모든 신뢰도 비교(vision_only ×0.7,
  동일 무게대 최고 conf 채택, freezer 전략 tie-break, 아카이브 기록)가
  원본 대비 열세였다. issue #6의 "평균 희석 전멸"과 같은 뿌리의 잔재.

**검증**: `.venv/bin/python -m pytest tests/ -q` → **239 passed, 3 failed**
(실패 3건은 기존과 동일한 macOS ffmpeg 바이너리 파손 — dyld x265 dylib 누락,
코드 무관). `ruff check .` clean. 신규 테스트: 카메라별 allowlist 전달 2건
(test_service.TestAllowedClassIds), max 결합 1건
(test_perception.test_weighted_conf_uses_camera_max_not_mean).
실기 검증 항목: ① left-crop 후 side ROI 400 실측 재확인(vote_summary
filter_drops_by_stage.side_roi), ② classes 제한 후 후보 분포 변화
(entry_dropped/debug_summary), ③ Jetson 배포 .env의 SIDE_ROI 구값(480/240) 정리.

**미이식 잔여 (보고서 P0-3, P1-5~7)**: 감마/콘트라스트 전처리, top ROI·냉동
수직 ROI, rescue 경로(threshold/roi/no-motion), freezer 전용 vote 하한,
hand conf floor — perf-gap 보고서 §10 참조.

## 2026-07-22 동시 다중 트레이 취출에서 한쪽 트레이 상품 미과금 (GitHub issue #16)

- **증상**: 냉동 zone 2에서 loadcell[2](155g 베이글)·loadcell[3](135g 상품)
  동시 각 1개 취출 → 베이글 1개만 과금(2,800원), 135g 상품 무성 소멸.
  세션은 `freezer_close_gate_failed:keep_incremental`로 확정.
- **원인 체인** (P0 배포본 트레이스로 확정 — 로드셀 분해는 정상,
  `multi_tray_events:2`, 세그먼트 −155/−135):
  ① 동시 취출은 영상(투표 풀)이 하나 — 베이글 62표 vs 135g 상품 12표
  (conf 1.0)로 표가 나뉨. ② ch1(−135) 판정에서 `FreezerVisionFirstStrategy`
  ①단계의 single_share(0.5) 게이트가 12표(19%) 후보를 배제, 득표 1위
  베이글(잔차 20g)은 게이트(15g) 실패. ③ ②단계 near-gate(잔차≤30g)가
  "오염 가정, top 정체성 보존 PARTIAL"로 **조기 반환** — ④ unique-refit
  (12표 ≥ refit 하한 6.2표, 잔차 0으로 적합)에 도달 불가. ④ `_judge_tray_events`
  병합은 COMPLETE만 합산 → PARTIAL인 ch1 상품 탈락. ⑤ CLOSE 냉동 재solve는
  다품종 net 재solve 금지(178g 원칙)라 복구 불가.
  근본: single_share/near-gate는 단일 상품 트리거 기준 보정값인데, 동시
  다중 트레이에서는 표 분할로 전제가 깨진다.
- **수정**: `pipeline._pool_exhaustion_retry` 신설 (2-pass 소진 재판정).
  1차 판정 후 형제 이벤트가 COMPLETE로 소진한 정체성을 PARTIAL/NO_DETECTION
  이벤트의 후보 풀에서 제거하고 라우터를 1회 재실행 — ch1은 top이 진짜
  상품이 되어 ①단계 COMPLETE로 복구된다. 채택은 COMPLETE로 개선될 때만
  (악화 금지), ERROR 이벤트 제외(I1), YOLO 재실행 없음(zero-GPU).
  I-V 유지: 무게로 정체성을 고르는 게 아니라 이미 설명된 정체성을 제거하고
  남은 득표 순위에 다시 맡긴다. 관측성: reason 접미사 `+pool_exhaustion` +
  trace `multi_tray_pool_exhaustion_retry:ch{N}`.
- **한계 (기록)**: 같은 상품이 두 트레이에 있고 한쪽 delta가 오염된 경우
  제거 후 무게 우연 적합이 오과금할 수 있음 — 기존 동작은 그 경우에도
  미과금(매출 누락)이었고, 재판정 흔적이 아카이브에 남아 사후 식별 가능.
- **부수 경고**: 이 트레이스의 `baseline_drops_by_class`(shadow)는 side
  class 40의 표 12/12 전부를 "드랍했을" 대상으로 계수 — baseline을 active로
  승격하면 이 이슈가 악화된다. 승격 보류 신호.
- **검증**: 회귀 테스트
  `test_service.py::TestMultiTrayEvents::test_issue16_vote_dominated_second_tray_recovered`
  (득표 20:4 불균형 + −155/−135 동시 이벤트 → 두 상품 모두 ×1 COMPLETE).
  전체 `pytest -q` → 240 passed, 3 failed(기존 macOS ffmpeg 환경 문제).
  `ruff check .` clean.

## 2026-07-22 (issue #16 2차) 재판정 발동에도 미채택 — unique-refit 모호성 판정 정교화

- **증상**: 1차 수정 배포 후 같은 시나리오 재현(이슈 코멘트 YAML) — trace에
  `multi_tray_pool_exhaustion_retry:ch1`은 찍혔으나 여전히 베이글 1개만 과금.
- **원인**: 이번 영상엔 배경 후보가 더 있었다 — 재판정 풀 {13:25표(168g),
  40:8표(135g), 24:8표(115g)}에서 새 top(13)은 잔차 33으로 결정적 반증(>near 30)
  → ④ unique-refit로 진행했으나, −135g 타깃에 40(잔차 0)과 24(잔차 20)
  **둘 다 near(30g) 안**이라 "적합 2개=모호"로 불발 → 재판정 결과가
  COMPLETE가 아니어서 채택 안 됨(악화 금지 정상 동작).
- **수정** (`strategies.py` ④): 적합을 2계층으로 분리 — **하드 게이트(±gate)
  내 유일 적합이면 near 밴드(gate<r≤near) 적합과 무관하게 채택**, 하드
  게이트 적합이 없을 때만 near 밴드 유일 적합 사용. near 밴드는 top의 접촉
  오염 가정(②)을 위한 창이지 대안 정체성의 적합 창이 아니다. 하드 게이트
  안에 2개 이상이면 종전대로 모호·불발 (I-V의 "±15g 창은 우연이 겹칠 만큼
  넓다" 원칙 유지 — 기존 모호성 테스트(370g: 잔차 0 vs 10, 둘 다 게이트 내)
  는 여전히 불발).
- **검증**: 회귀 테스트
  `test_issue16_retry_with_near_band_distractor_recovered`(코멘트 YAML 동형:
  27×20표/13×10표/40×4표/24×4표, −155/−135) → 27×1 + 40×1 COMPLETE,
  reason `freezer_vision_first_unique_refit+pool_exhaustion`.
  전체 241 passed / 3 env-failed, ruff clean. 기존 refit 테스트 2건
  (`test_unique_refit_rescues...`, `test_ambiguous_refit_refused...`) 무변경 통과.

## 2026-07-22 (issue #16 3차) 무게 중재 재설계 + 멀티트레이 PARTIAL 과금 — 설계 3·4 구현

설계 문서: `docs/0722_issue16_arbitration_design.md` (사고 4건 분석·원칙·시뮬레이션).
원칙: **무게는 거부권만, 복수 적합의 선택은 vision(득표+conf)이 한다** — DB
unit_weight는 정책상 고정이고 실측과 10~30g 편차가 있으므로(라벨 168g/DB 185g 등)
무게 산술에 확정권을 주면 우연 적합 오과금이 구조적으로 반복된다.

- **3a n-스케일 게이트** (`strategies.py`, `settler.py`): `gate_n(n) = count_gate
  + COUNT_UNIT_SLACK×(n−1)` (기본 5g/개). ①·④·냉동 close 재solve·I6(라우터,
  freezer 한정)에 적용. **③ 조합은 flat 유지** — 우연 적합 공간이 조합적으로
  크고 #10 filler가 조합형 (구현 중 3종 정답 케이스의 k=2 오적합 회귀로 확정).
- **3b 선착 폐지 + 중재** (`strategies.py` ①): 자격 후보 전원의 적합 수집 후
  결정. 복수 적합이면 득표·conf 일치 → 그 후보, conf가 CONF_MARGIN(0.15) 이상
  우세 → conf 승(reason `…single_arbitrated`), 전역 득표 1위 적합 → 서열 유지,
  그 외 → 모호로 ② near 폴스루. 실사고 C(베이글 5개 → 만두 4개 오과금:
  잔차 3g짜리 우연 적합이 잔차 32g짜리 정답을 선착으로 이김) 교정.
- **3c conf 자격 확장** (`strategies.py` ①): single_share(50%) 미달이어도
  `conf ≥ CONF_OVERRIDE(0.9) ∧ votes ≥ refit_share(10%)`면 적합 자격.
  진열 오염이 득표 순위를 왜곡해도 max-conf는 독립 신호 (실사고 D: conf 1.0
  진짜 상품 19표 vs 진열 만두 63표). conf 0.9는 양 카메라 동시 검출에서만
  도달 가능한 수준 — 단일 카메라 상한은 0.6.
- **4 멀티트레이 병합에 고유 정체성 PARTIAL 포함** (`pipeline.py`): 정산기는
  원래 PARTIAL 상품도 과금하므로(#15 정답 경로) 병합만 COMPLETE 한정인 것은
  비대칭 — 4초 안에 두 번 집으면 덜 과금됐다(실사고 B). 가드 2중: 형제
  COMPLETE와 정체성 겹침 제외(표-그림자), PARTIAL 상호 겹침 전부 제외
  (I13/D9). reason `partial_billed:chN` + trace 코드로 관측.
- env 3종 신설(`MODEL__JUDGMENT__COUNT_UNIT_SLACK=5.0 / CONF_OVERRIDE=0.9 /
  CONF_MARGIN=0.15`) — 비활성값(0/2.0/2.0)으로 구 동작 복원 가능 (롤백 스토리,
  롤백 동형성 테스트 포함).
- **의도된 동작 변경 1건**: 이슈 #15의 −370g 케이스(176×2, 잔차 18)가
  near-gate PARTIAL → ① COMPLETE로 격상 (gate_n(2)=20 ≥ 18, 과금 동일).
  같은 픽스처의 함정(만두 185×2=370 잔차 0)은 자격 양문(share 50%/conf 0.9)
  으로 여전히 차단 — 테스트 기대값 갱신으로 문서화.
- 검증: 신규 테스트 7건(중재 C/conf자격 D/모호 폴스루/롤백 동형성/병합 2건/
  정산 gate_n) + 기대값 갱신 1건(−370 격상) + 전체 `pytest -q` →
  **248 passed, 3 failed**(기존 macOS ffmpeg 환경 문제), `ruff check .` clean.

## 2026-07-22 (issue #16 4차) 모션 변위 증거 이식 — 진열/배경 오투표의 일반해

- **배경**: static_track(연속 IoU 정지)과 baseline(손 등장 전 존재)은 "집어간
  상품은 움직이고 진열 상품은 안 움직인다"는 물리의 **대리 신호**였고 각자
  구멍이 있었다. baseline은 실기 4건에서 top 무력(프리롤에 이미 손 → 등록창
  0) / side 폭주(hand 미추론 → 등록 무한, side 353~1,735드랍 vs top 0)로
  판정됐다. 원본의 모션 변위 필터(BboxTracker 사후 일괄, perf-gap 원인 #6)가
  이 물리를 직접 재는 일반해다 — P0-1(left-crop 정합)로 좌표계가 원본과
  같아져 픽셀 임계(10/12px)를 그대로 이식할 수 있게 됐다.
- **구현**: `perception/motion_evidence.py` 신설 — 카메라×클래스별 최근접
  중심 매칭(점프 상한 150px, IoU 앵커 아님: 빠른 이동은 IoU가 무너진다)
  트랙에 누적 경로·최대 변위를 쌓고, `max(floor, bbox×0.10)` 미달 클래스의
  표를 `VotingEnsemble.combine()`에서 카메라 단위로 몰수. min_vote_share의
  1위 기준도 몰수 반영 후 계산(배경 1위가 상대 하한을 오염시키지 않게).
  bbox 없는 검출은 면제(fail-open, 증거 보존). floor는 프로파일 소속
  (냉장 10px/냉동 12px — 원본 MOTION_MIN_DISPLACEMENT_PX 동형).
- **배선**: pipeline이 트리거마다 MotionEvidence를 만들어 필터 통과 검출을
  관찰시키고 ensemble에 attach. env `MODEL__VISION__MOTION_EVIDENCE`(기본 1),
  `MODEL__VISION__MOTION_EVIDENCE_FLOOR_PX`(비우면 프로파일). 라이브러리
  직접 생성(TriggerPipeline ctor)은 기본 OFF (하위호환 — entry cut과 동일
  패턴). 관측성: vote_summary에 `rejected_by: "no_motion"` + `motion_evidence`
  블록(카메라×클래스 통과/최대경로/임계).
- **baseline 퇴역**: .env.example 기본 off로 전환 + 퇴역 사유 주석. 코드
  (shadow/active)는 유지 — active 재도전은 side hand 재포함 + hand-release
  선행 필요. static_track은 유지(위치 단위 억제 — 같은 클래스 진열+취출
  동시 케이스의 표 개수 오염을 마저 잡는 상보 계층).
- **테스트 픽스처 계약 변경**: ModelService 경유 페이크 검출은 이제 움직여야
  한다(실물 계약) — 4개 테스트 파일의 FakeDetector가 12px/프레임(%8 순환)
  드리프트를 갖도록 갱신.
- **검증**: 신규 테스트 6건(정지 몰수/깜빡임 몰수/zero-bbox 면제/카메라 독립/
  share 기준 오염 방지/파이프라인 진열 억제) + 전체 `pytest -q` →
  **254 passed, 3 failed**(기존 macOS ffmpeg 환경 문제), `ruff check .` clean.
- 실기 검증 항목: 로그 4형 시나리오 재현 시 진열 클래스가 `no_motion`으로
  몰수되고 진짜 상품이 득표 1위가 되는지 (vote_summary.motion_evidence).

## 2026-07-22 (issue #16 5차) 트랙릿 투표 — 표의 트랙 귀속 (research §3 적용 결정)

- **결정** (claudedocs/research_judgment_performance_20260722.md 검토 후):
  planogram prior·임베딩 인식은 채택 안 함, BOCPD·무게 확률화는 승인(후속),
  **트랙릿 투표는 즉시 적용**.
- **구현**: 4차의 변위 증거를 트랙 단위로 승격 — `MotionEvidence.observe()`가
  검출별 트랙 id를 반환하고, `VotingEnsemble`이 표를 `(conf, track_id)`로
  저장한 뒤 combine에서 **트랙 단위** 변위 검증(`track_qualifies`). 표 단위
  (프레임 검출)는 유지되므로 기존 게이트 캘리브레이션(ratio/count/share·
  judgment share 계열)은 그대로 유효하다.
- 클래스 단위(4차) 대비 이득: 같은 클래스가 진열+취출로 동시에 있을 때
  진열 인스턴스 트랙의 표가 몰수된다 — 클래스 단위로는 "한 트랙이라도
  움직이면 클래스 전체 표 유효"라 진열 표 인플레이션이 남았다.
  "오래 보이는 것 = 표 많은 것" 편향의 실질 종결 (ByteTrack-lite,
  칼만/IoU 없이 최근접 중심 매칭 — 순수 파이썬, 런타임 의존성 0 유지).
- 폴백: 트랙 귀속 없는 표(tid None — zero-bbox 면제, 직접 생성 사용처)는
  클래스 단위 판정 유지 (하위호환).
- **검증**: 신규 테스트 1건(진열+취출 동일 클래스 — 클래스 단위면 20표,
  트랙 단위라 10표) + 전체 `pytest -q` → **255 passed, 3 failed**(기존
  macOS ffmpeg 환경 문제), `ruff check .` clean.
- 미채택 결정 기록: planogram(Edge payload 협조 불가), 임베딩 개방형
  인식(참조 사진 확보 곤란). 승인 대기열: BOCPD shadow 분석기(§2),
  무게 이벤트 확률화(§1-2 — I-V와의 정합 설계 필요, 우도비 상한 방식).

## 2026-07-22 (research §2 적용) BOCPD 로드셀 shadow 분석기

- **구현**: `ingest/bocpd.py` — Adams & MacKay 2007 run-length 사후분포 재귀의
  축소 구현 (고정 노이즈 가우시안 σ=2.5g + run별 평균 모호 켤레 사전 κ₀=0.01,
  hazard 0.1, 순수 파이썬). MAP run length → 구간 재구성(경계 부기: cp 메시지는
  점프 샘플 도착 전 생성되므로 흡수 구간은 t−r+1..t) → 2σ 이내 인접 구간 병합
  → 채널별 delta = 마지막 레벨 − 첫 레벨, delta_std = σ√(1/n_f+1/n_l).
- **shadow 전용**: 판정·정산 무변경. pipeline이 분석 직후 try/except로 계산해
  `trace.loadcell_shadow`(delta/std/채널 레벨/primary와의 mismatch)로만 기록 —
  세션 아카이브에 직렬화된다. env `MODEL__LOADCELL__BOCPD_SHADOW`(기본 1),
  라이브러리 ctor 기본 OFF(하위호환).
- **표적 실패 모드**: ① #14 무음 0원(stable_window=3이 5샘플 창에서 실패 →
  primary delta=0인데 shadow가 실delta 제시), ② issue #16 로그 3(1.6s 간격
  연속 취출 → 2샘플 플래토가 stable_window 미달 → 존 delta 뭉개짐). 테스트로
  ②를 고정: 2샘플 플래토 계단(820→200)에서 BOCPD delta −620±5 판독.
- **검증**: 신규 테스트 5건(클린 스텝/2샘플 플래토 계단/2채널 독립 변화/
  insufficient/파이프라인 trace 배선) + 전체 → **260 passed, 3 failed**(기존
  ffmpeg 환경), ruff clean.
- **승격 판단 기준**: 아카이브에서 mismatch=true 세션 수집 → primary가 틀리고
  shadow가 맞는 비율 실측 → LoadcellAnalyzer 대체 여부 결정 (reason 계약은
  "사후확률 < 임계"로 대응 가능).
- **후속 설계 문서**: 무게 이벤트 확률화(우도비 상한 방식, research §1-2 승인)
  → `docs/0722_weight_likelihood_design.md` — BOCPD의 delta_std가 σ_d 입력으로
  연결되는 3단계 이행안. 선행 조건: 아카이브 정답 라벨 필드 (research §6 공유).

## 2026-07-22 세션 아카이브 정답 라벨 필드 + label-session CLI (research §6 선행 조건)

- **배경**: conformal 임계 보정(§6)과 무게 확률화 Phase 1(§1-2, docs/
  0722_weight_likelihood_design.md)의 공통 전제 = "실제로 무엇을 몇 개
  취출했는가"의 구조화 기록. 지금까지는 GitHub 이슈 코멘트에 수기로 적었다.
- **구현**: ① 아카이브 문서에 `ground_truth` 필드(기본 null — 스키마 가시성),
  스키마 `{labeled_at, note, items: [{zone, class_id|name, count}]}`.
  ② `SessionArchive.annotate_ground_truth()`/`find()`/`latest()` — save()와
  달리 실패를 삼키지 않는다(라벨링은 사람이 실행 중인 작업). ③ `label-session`
  콘솔 엔트리포인트(adapters/label_cli.py): `--latest`(실험 직후) 또는 세션 id,
  `--take [존:]<class_id|이름>x<개수>` 반복, `--note`. 재실행 = 라벨 대체.
- **검증**: 신규 테스트 7건(placeholder/기입·대체/미존재 에러/latest/json 폴백/
  CLI 파싱/CLI e2e) — 전체 **267 passed, 3 failed**(기존 ffmpeg 환경), ruff clean.
- 사용: 실험 직후 Jetson에서 `label-session --latest --zone 2 --take 27x5`.
  주의: pyproject 엔트리포인트 추가라 배포 시 `uv pip install --no-deps -e .`
  재실행 필요.

## 2026-07-23 (research §1-2 Phase 1) 무게 우도 score shadow — 판정 확률화의 관측 단계

- **배경**: 냉동 판정의 무게 규칙은 전부 이산 경계(gate_n·near 밴드·share·
  conf_override·margin)라 경계 바로 안팎에서 판정이 뒤집힌다 — #15(3g),
  #16 로그 3(near 밴드 2g 초과) 모두 경계 사고. 설계
  `docs/0722_weight_likelihood_design.md`는 이를 단일 score
  `log P_vision + clamp(log L_weight, ±log k)`로 연속화하되, clamp가 I-V
  ("무게는 거부권만")의 연속판이 되도록 상한을 둔다. Phase 1은 판정 무변경
  shadow — 승격은 아카이브 실측(정답 라벨 대비 score 정오)이 게이트.
- **구현**: ① `judgment/likelihood.py` 신설 — `WeightLikelihoodScorer`.
  적용 조건은 FreezerVisionFirst와 동형(freezer removal + 후보 존재).
  배정 후보군 = 단일 정체성 n개(identity_pool 6, n=round(target/w) stock
  클램프) + 현행 판정 결과. σ_eff² = σ_d² + Σn·σ_db²(개당 편차의 개수 비례
  누적 — gate_n 슬랙의 연속판), σ_d는 BOCPD shadow의 delta_std 연결(부재 시
  3.5g). ② `service/pipeline.py` — 단일 이벤트·멀티트레이(이벤트별,
  channel 표기) 판정 직후 `trace.likelihood_shadow`에 기록, 1위 불일치면
  reason_codes에 `likelihood_shadow_mismatch[:chN]`. try/except 격리(BOCPD
  패턴). ③ 아카이브 trace 직렬화에 포함. ④ env 3종:
  `MODEL__JUDGMENT__LIKELIHOOD_SHADOW`(기본 1) / `LIKELIHOOD_K`(20 — 1이면
  무게 무력, 롤백 스토리) / `LIKELIHOOD_SIGMA_DB`(5g). 라이브러리 ctor 기본
  OFF (하위호환, 기존 shadow들과 동일 배선).
- **관측 계약**: 엔트리 = {k, sigma_d, sigma_db, current(items/score),
  top, mismatch, ranking[≤5]} — ranking 항목마다 log_p_vision/log_l_weight/
  clamped/residual 분해 기록 (연속 점수의 감사성 방어, 설계 §5 리스크 대응).
  mismatch에는 "현행 무과금인데 score 1위 존재"(NO_DETECTION diff)도 포함 —
  매출 누락 방향의 diff도 셀 수 있다.
- **검증**: 신규 `tests/test_likelihood.py` 14건 — 적용 조건 3, 스코어링 7
  (실사고 C diff 기록, k=1 무력화 롤백, clamp 경계, σ_eff 개수 스케일,
  BOCPD σ_d 주입, NO_DETECTION diff), 배선 4(기록/기본 OFF/판정 무변경
  동형성/멀티트레이 채널/아카이브 직렬화). 전체 `pytest -q` →
  **280 passed, 4 skipped**, `ruff check .` clean.
- 실기 검증 항목: 냉동 취출 세션 아카이브에서 `likelihood_shadow` 존재 확인
  → `label-session`으로 정답 기입 → mismatch 세션의 score 정오 비율 실측
  (Phase 2 승격 게이트).

## 2026-07-23 원본 정합 웨이브 2 — 수직 ROI(P1-5) + 손 conf 하한(P1-7) 이식

- **배경**: perf-gap 미이식 잔여 중 진열 오투표에 직결되는 2건. 원본 냉동
  실기는 dual-top(공용 스트림 2개가 모두 top 뷰)에서
  `FREEZER_ROI_VERTICAL_REGION=upper`로 **하단 절반(진열 선반) 검출을 물리적
  으로 차단**해 왔다 — HG는 side x-ROI만 이식돼 있어 진열 오투표(이슈 #16
  D형 "진열 만두 63표")의 원본측 방어선이 빠져 있었다. 손 conf 하한
  (원본 hand_confidence_threshold, 운영 0.30)도 미이식이라 저신뢰 유령 손이
  모션 게이트 래치(I16)와 hand_path 궤적 기준을 오염시킬 수 있었다.
- **구현**: ① `perception/filters.py` — `vertical_roi_region`("off"|"upper"|
  "lower", 기본 off)·`vertical_roi_y_split`(240): dual-top이면 **두 카메라
  모두** center_y 기준 해당 절반만 유지하고 side x-ROI는 생략(원본
  `_uses_freezer_dual_top_profile` 동형). `top_roi_enabled`/`top_roi_y_split`:
  냉장(dual) 레이아웃 top 카메라 전용 — 트리거 delta가 0이 아닐 때 하단
  절반(center_y >= split) 유지 (원본 `_top_roi_accepts` 동형, delta는
  pipeline이 `set_trigger_delta`로 주입). `hand_conf_floor`: 하한 미만 hand
  검출을 래치·궤적 입력에서 제외. drop_stats에 `vertical_roi`/`hand_conf`
  단계 신설 — vote_summary로 제거량 관측 가능. ② `core/config.py` —
  `MODEL__VISION__CAMERA_LAYOUT`("dual"|"dual_top_proxy", 오타 fail-closed
  ValueError) + FREEZER_ROI_VERTICAL_REGION/Y_SPLIT + TOP_ROI_ENABLED/Y_SPLIT
  + HAND_CONFIDENCE_THRESHOLD(기본 0.30). ③ `service/model_service.py` —
  cabinet_type=freezer ∧ camera_layout=dual_top_proxy일 때만 수직 ROI 활성
  (냉장 기기는 layout이 dual_top_proxy여도 off — 원본 조건 동형), 기동
  [CONFIG] 로그에 camera_layout 표기.
- **활성화 계약 (실기)**: 기본값은 전부 기존 동작 보존 — 수직 ROI는 Jetson
  .env에 `MODEL__VISION__CAMERA_LAYOUT=dual_top_proxy` 추가로만 켜진다.
  손 conf 하한은 기본 0.30(원본 운영값)으로 즉시 적용,
  `MODEL__VISION__HAND_CONFIDENCE_THRESHOLD=0`으로 롤백 가능.
- **검증**: 신규 테스트 7건 — 필터 단위 5(upper 양 카메라+side x-ROI 생략/
  lower/오타 거부/top ROI delta 게이트/유령 손 궤적 미등록) + ModelService
  배선 E2E 2(freezer+dual_top_proxy 하단 억제, dual 기본 하단 보존).
  기존 스테이지 목록 테스트 1건 갱신. 전체 `pytest -q` → **287 passed,
  4 skipped**, `ruff check .` clean.
- 실기 검증 항목: dual_top_proxy 활성 후 ① vote_summary
  filter_drops_by_stage.vertical_roi에 진열 클래스가 잡히는지, ② 진짜 취출
  상품(상단 통과 후) 득표 1위 복원 여부, ③ hand_conf 드랍 수와 래치 동작.

## 2026-07-23 analyze-sessions — 아카이브 오프라인 실측 리포트 (research §6, 로드맵 단기 ②)

- **배경**: shadow 3종(BOCPD·무게 우도·baseline)과 conformal 보정의 공통
  병목은 "아카이브 실측"이 수작업이라는 것 — mismatch 세션을 눈으로 뒤지고
  정오를 수기로 셌다. research §6이 권고한 오프라인 스크립트(아카이브 →
  분위수 → env 제안)를 label-session과 같은 어댑터 CLI 패턴으로 구현.
- **구현**: ① `adapters/analyze_cli.py` + 콘솔 엔트리 `analyze-sessions`
  (읽기 전용, stdlib+PyYAML). 리포트 3부: BOCPD mismatch 목록(primary vs
  shadow delta), 무게 우도 mismatch 목록 + **라벨 대비 정오 집계**(score만
  정답/현행만 정답/둘 다 오답 — Phase 2 승격 게이트의 실측치, 멀티트레이는
  채널 entry 합산으로 존 GT와 비교), conformal 분위수(정답 상품의
  votes/ratio/share/conf p5 → MIN_VOTE_* 제안) + 정답이 후보에 없던 트리거
  경고, σ_db 개당 잔차((|Δ|−n·w)/n) 분포 → LIKELIHOOD_SIGMA_DB 제안.
  ② `ledger/archive.py` — 판정·존 products 직렬화에 `class_id`/`unit_weight`
  추가 (정답 라벨은 class_id 기반인데 아카이브 products는 가격뿐이라
  자동 대조가 불가능했다). 구 아카이브(키 부재)는 조용히 집계 제외.
- **검증**: 신규 `tests/test_analyze_cli.py` 9건 (BOCPD 수집/score 정오/
  멀티채널 합산/분위수·미후보 경고/σ_db 잔차/구 스키마 무시/CLI e2e/빈
  디렉토리/손상 파일). 전체 `pytest -q` → **296 passed, 4 skipped**,
  `ruff check .` clean.
- 사용 (실기): 실험 → `label-session --latest --zone N --take 27x5` →
  `analyze-sessions`. 주의: 엔트리포인트 추가라 배포 시
  `uv pip install --no-deps -e .` 재실행 필요 (label-session과 동일).

## 2026-07-23 BOCPD primary 승격 스위치 — MODEL__LOADCELL__ANALYZER (이슈 #14 대비)

- **배경**: 이슈 #14(빠른 취출 → plateau 실패 → delta=0 → 무음 0원)의 근본
  대응인 BOCPD는 shadow로 관측 중이고, 승격은 아카이브 실측이 게이트다.
  그런데 승격 시점에 코드 배포가 필요하면 실측→적용 사이클이 길어진다 —
  실측에서 우세가 확인되는 즉시 env 하나로 전환할 수 있게 스위치를 미리
  배선한다 (기본값은 현행 plateau 그대로).
- **구현**: ① `ingest/bocpd.py` — `BocpdLoadcellAnalyzer(profile)` 어댑터:
  LoadcellAnalysis 계약 동형 (insufficient_* reason, min_weight_change 채널
  게이트, segment_step 임계, 반품 안정화 대기 QA Q3 ①, 멀티트레이
  ChannelWeightEvent). 바뀌는 것은 "안정 구간"의 정의(3연속 std 창 →
  run-length 사후분포)뿐. 전 채널 평탄 시의 vision_only 강제
  (insufficient_stable_regions)도 보수적으로 유지. ② `core/config.py` —
  `MODEL__LOADCELL__ANALYZER`("plateau"|"bocpd", 오타 fail-closed).
  ③ `service/model_service.py` — analyzer_factory 분기.
  ④ `service/pipeline.py` — primary가 bocpd면 shadow를 plateau로 뒤집어
  대칭 diff 유지 (승격 후에도 회귀 방향 mismatch 관측 가능).
- **검증**: 신규 테스트 6건 — 어댑터 4(#14 creep에서 plateau 실패/BOCPD
  성공 대비, 반품 보류 계약, 평탄 계약, 2채널 이벤트) + 서비스 스위치
  E2E(judgment 동일 + shadow=plateau) + env 오타 거부. 전체 `pytest -q` →
  **302 passed, 4 skipped**, `ruff check .` clean.
- 승격 절차 (실기): 실험 → label-session → analyze-sessions의 BOCPD
  mismatch 정오 확인 → 우세하면 Jetson .env에
  `MODEL__LOADCELL__ANALYZER=bocpd` — 코드 무변경.

## 2026-07-23 CI 34연속 실패 — ffmpeg `-hwaccel cuda`가 드라이버 없는 호스트에서 EPERM (근본 수정)

- **증상**: GitHub Actions CI가 도입(2026-07-09) 이래 34/35회 실패.
  실패는 항상 같은 3건 — `test_frames_streaming.py::TestDecodeAviStreaming`의
  ffmpeg 디코드 테스트가 `OSError: ffmpeg decode failed (rc=255) ...
  Error opening output files: Operation not permitted`. (macOS 로컬의
  "ffmpeg dylib 파손"으로 기록돼 있었으나 별개 — 우분투 표준 ffmpeg에서도
  재현되는 코드 문제였다.)
- **원인**: `_ffmpeg_hwaccel_available()`이 `ffmpeg -hwaccels` 출력에
  "cuda"가 있는지만 봤다. 이 목록은 **빌드에 컴파일된 hwaccel 목록**이라
  NVIDIA 드라이버가 없는 호스트(GitHub 러너, 일반 PC)에서도 cuda가 나온다.
  그 판정으로 `-hwaccel cuda`를 넘기면 CUDA 디바이스 생성이
  AVERROR(EPERM)로 실패하고, `-v error` 때문에 상세 로그 없이 마지막 줄
  ("Error opening output files: Operation not permitted")만 남은 채 디코드
  전체가 죽는다 — CPU 폴백 없이. 같은 결함이 실기에서도 위험했다: Jetson의
  CUDA 상태가 깨지면(드라이버/JetPack 문제) 디코드가 통째로 실패해 트리거가
  전부 error 이벤트가 된다 (원본 frame_extractor는 "HWACCEL: CPU" 폴백 보유).
- **해결방안**: ① 프로브를 실사용 검사로 교체 —
  `ffmpeg -init_hw_device cuda -f lavfi -i color … -f null -`의 rc==0으로
  판정 (컴파일 목록이 아니라 디바이스 초기화 성공 여부. Jetson에서만 True).
  ② 런타임 CPU 폴백 — `_decode_avi_ffmpeg`를 hwaccel 시도 → **0프레임 실패
  시에만** CPU 재시도로 구조화 (`_decode_avi_ffmpeg_cmd(hwaccel=...)` 분리).
  프레임을 이미 방출한 뒤의 실패는 폴백하지 않는다 (중복 방출 방지, I1 전파).
- **관련 파일**: `crk_model/adapters/avi_frames.py`,
  `tests/test_frames_streaming.py` (`TestHwaccelProbeAndFallback` 4건 —
  프로브 실초기화 판정 2, 0프레임 CPU 폴백, 중간 실패 전파).
- **테스트**: 전체 `pytest -q` → **306 passed, 4 skipped**, `ruff check .`
  clean. CI green 여부는 push 후 확인.

## 2026-07-23 analyze-sessions에 과금 정오 총괄 추가 (실기 1차 실측 후속)

- **배경**: 첫 실기 실측(라벨 20세션)에서 리포트가 shadow mismatch만 세고
  "현행 판정이 결국 몇 세션을 맞게 과금했는가"는 안 보였다 — shadow와
  현행이 **일치하면서 둘 다 틀린** 세션은 어디에도 안 잡히는 사각.
- **구현**: 라벨된 세션의 최종 확정(zones products, class_id 멀티셋)을 존별
  GT와 비교 — 정답 n/전체, 오답 세션의 존별 (과금 ← 정답) diff 목록.
  GT에 없는 존의 과금(초과 청구)도 오답으로 계수(전 존 라벨 전제).
  구 스키마(class_id 없음)는 판정 불가로 별도 계수.
- **검증**: 신규 테스트 2건 (정오 분리, 비라벨 존 초과 청구). 전체
  `pytest -q` → **308 passed, 4 skipped**, ruff clean.

## 2026-07-23 analyze-sessions --session 상세 덤프 (오답 세션 사후 분석용)

- 실기 1차 실측이 오답 3세션을 특정 — 원인 확정에는 판정 전략·득표·탈락
  사유가 필요한데 YAML을 직접 뒤지는 것은 원격 협업에서 느리다.
  `analyze-sessions --session <id>`가 GT/존 확정/트리거별 judgment·
  candidates·vote_summary(classes, 단계별 드랍, 진입 탈락)·shadow 2종을
  한 화면으로 덤프한다 (--json 병용 시 원문 문서).
- 테스트 2건 추가, 전체 **310 passed, 4 skipped**, ruff clean.

## 2026-07-23 실기 2차 실측 (22/29) 대응 — held-object A-1 계측 + partial 청구 conf 하한

- **실측 요약** (analyze-sessions, 라벨 29세션): 정답 22/29. 오답 7건의
  세부 덤프(--session)로 원인 3계열 확정:
  ① **held-object 표 오염** (ses-9 zone4: zone5에서 꺼낸 44를 들고 zone4
  접근 → zone4 두 트리거 모두 44가 득표 1위 131~153표. Δ−230에서 44×3=237이
  gate_n(3) 우연 적합 + 중재의 "전역 득표 1위 적합 존중"이 채택 → 정답
  3×1(잔차 6, conf 1.0) 패배. ses-13/16/2도 동일 계열 정황) —
  docs/0713_held_object_demotion.md가 예견한 바로 그 실패 모드.
  ② **빠른 취출 → 진짜 상품 표 소멸** (ses-7: GT 44+35 동시 취출인데 44는
  1표(no_motion 몰수)·35는 0표, entry 탈락 top 474 — 오염 후보 13만 잔존.
  PARTIAL 상호 겹침 가드가 13 과금은 막았지만 0원 매출 누락. ses-3-1784788285
  도 동일: GT 30이 0표).
  ③ **저증거 identity partial 과금** (ses-3-1784788285: 5표/청구 conf
  0.157짜리 24를 잔차 65g인데도 과금).
- **이번 수정 2건**:
  1. **held-object A-1 계측** (0713 §3·§6 1단계, 판정 영향 0):
     `VisionCandidate`에 `head_votes/span_ratio/first_pos_ratio` 진단 필드
     (기본값 하위호환), `VotingEnsemble.add_frame(pos=...)`가 디코드 위치
     (게이트 스킵 포함)로 (camera,class)별 first/last/head를 집계,
     pipeline이 pos를 배선, 아카이브 vision_candidates에 직렬화.
     다음 실기에서 carried-in(head↑·span≈1) vs 진짜 취출 분포를 실측한 뒤
     A-2(soft 강등, HELD_* env)를 켠다.
  2. **partial 청구 conf 하한** (`MODEL__JUDGMENT__PARTIAL_MIN_CONFIDENCE`
     기본 0.18 — 원본 multi_kind_min_confidence 동형): vision_first_identity_
     partial·relaxed_partial의 무게 미검증 count=1 청구는 청구 conf(원 conf
     ×0.5)가 하한 이상일 때만. 미달 시 하위 후보로 넘어가지 않고 불발
     (후보 쇼핑 금지, I-V 태도). COMPLETE(무게 검증) 경로는 무관.
     ses-3-1784788285형 오과금이 NO_DETECTION으로 바뀐다 (I13: 과청구 <
     미청구).
- **검증**: 신규 테스트 6건 (voting 위치 신호 2 — carried-in/진짜 분리,
  pos 미제공 하위호환; judgment 하한 4 — 실기 재현 차단/하한 0 롤백/COMPLETE
  무관/기존 partial 유지). 전체 `pytest -q` → **315 passed, 4 skipped**,
  `ruff check .` clean.
- **미해결 (다음 단계)**: ①의 근본 해결은 A-2 강등 (A-1 분포 확인 후) +
  세션 소진 정체성 감쇄 검토. ②는 냉동판 rescue 설계 필요 (원본
  threshold_rescue는 freezer 비활성이라 이식 불가 — 무게 적합 + 저conf
  후보 구제의 freezer-safe 형태). BOCPD·우도 Phase 2는 표본 계속 수집.

## 2026-07-23 실기 3차 실측 (25/36) 대응 — ④ refit conf 중재 + 덤프에 A-1 신호 표시

- **실측 요약** (신규 8세션: 정답 4·오답 4, 누적 25/36): held-object 대조
  실험(ses-2: 44 들고 zone4 취출)은 **정답** — 오염 득표가 약하면(17표 vs
  19표) 3b 중재·무게 거부권이 정확히 동작함을 실증. 오답 4건의 원인:
  ① ses-8 zone2 — held 27이 94표로 **min_vote_share의 분모(top votes)를
  인플레이션**시켜 정답 30(9표)을 후보에서 제거(share 기준 9.4에 0.4표
  미달), held 27이 identity partial로 과금. A-2 강등의 share 분모 반영이
  근본 해법 (A-1 신호로 확정 예정).
  ② ses-3-1784790444 ch0 — 정답 40(5표/conf0.82, 잔차 2.3)의 유일-적합을
  35×2(4표/conf0.35)가 "적합 2개=모호"로 차단 → 오염 top 24가 identity
  partial 과금. ③ ses-4 ch1·ses-3 ch1 — 동시 취출 표 분할로 정답 23이
  1~2표뿐(후보 진입 실패/자격 미달) → 오염 top 13의 near-gate/중재 과금.
  ④ ses-6 — 양 채널 모두 오염 top 13의 partial → 상호 겹침 가드가 전부
  제외(오과금 방지는 성공) → 0원.
- **수정 1 — ④ refit conf 중재**: 하드 게이트 복수 적합 시, 최고 conf
  적합이 **다른 모든 적합보다 conf_margin(0.15) 이상 우세**할 때만 채택
  (reason `freezer_vision_first_refit_arbitrated`). refit 풀은 구조상 전부
  저득표라 득표 차 1~2표는 증거 자격이 없음 — conf만 결정 기준. 기존
  모호성 계약(conf 동률·근접 → 불발) 유지: ses-6 ch0(0.80 vs 0.87)·기존
  픽스처(0.5 vs 0.5)는 계속 불발.
- **수정 2 — --session 덤프에 A-1 신호 표시**: candidates 라인에
  `/head{n}/span{r}` 표기 (span_ratio 기록된 아카이브 한정) — held 후보
  (head↑·span≈1)를 덤프만으로 식별, A-2 임계 확정의 실측 소스.
- **검증**: 신규 테스트 2건 (ses-3 ch0 재현 중재 채택 / ses-6 ch0 재현
  모호 유지) + 기존 모호성·refit 테스트 무변경 통과. 전체 `pytest -q` →
  **317 passed, 4 skipped**, ruff clean.
- **다음**: A-2 held 강등 (A-1 분포 확인 후 — share 분모 반영 포함),
  동시 취출 표 소멸(진짜 상품 1~5표)의 구제 설계.

## 2026-07-23 실기 4차 실측 (30/43) — refit 중재 conf 하한 + A-1 임계 실측 확정

- **실측 요약** (신규 7세션: 정답 5·오답 2, 누적 30/43): 정답 중 주목할 것 —
  ① ses-2(27 들고 30 취출, ses-8 재현)가 이번엔 **정답**: 30이 13표로 share
  하한(9.2)을 넘어 생존, ④ near 유일적합 + I6 강등 PARTIAL로 정확 과금.
  ② ses-6(3 들고 44 취출): zone4 오판(13 partial)을 **cross-zone penalty가
  CLOSE에서 3×1로 교정** — 0711 페널티의 첫 실기 성공 관측. ③ ses-3(들고-
  반납 후 취출)·ses-4(같은 존 들고 취출)·ses-7(동시 같은존) 전부 정답.
- **A-1 신호 실측 분리 확인** (docs/0713 §10에 표 기록): held 후보
  head_votes 27~33 vs 진짜 취출 0~2 — HEAD 조건(≥5) 실증. 단 설계
  SPAN_MIN 0.8은 실측(0.13~0.54)과 불일치 — A-2에서 하향/제거 필요.
  한계 실측: 든 상품이 프리롤에 화면 밖이면(ses-5) head=0 — 세션 소진
  정체성 계열이 보완해야 하며, ses-5는 zone4 COMPLETE(44×3) 확정이라
  cross-zone penalty도 미개입(ses-6과의 비대칭).
- **오답 2건**: ses-5(위 44×3 — held+무게 우연 적합, A-2/소진 감쇄 대상),
  ses-1(동시 취출 — 정답 23이 후보에 아예 없음 + **어제 넣은 refit 중재가
  conf 0.69 유령 13을 채택해 과청구 방향으로 악화**. margin 우세만으로는
  "덜 흐린 유령"이 이긴다).
- **수정**: ④ refit 중재에 절대 conf 하한 `MODEL__JUDGMENT__REFIT_ARB_
  CONF_FLOOR`(기본 0.8) — 정당 케이스(ses-3 ch0, conf 0.82)는 통과, ses-1
  유령(0.69)은 차단되어 기존 보수 경로(identity partial → 병합 가드 억제)
  로 복귀. 회귀 테스트 1건(ses-1 ch1 재현) 추가.
- **우도 Phase 2 판단 갱신**: mismatch 정오 3:4:4로 score 열세 반전 —
  score의 오답 다수가 "동일 상품 n개 우연 적합 선호"(40×2, 46×3, 30×3,
  13×2)로, 배정 후보군에 다품종 조합이 없어 log_p_vision이 count에 무감한
  구조적 한계. **Phase 2 승격 부결** (shadow 유지), 개선하려면 조합 배정
  열거 + count 페널티가 선행돼야 함을 기록.
- **검증**: 전체 `pytest -q` → **318 passed, 4 skipped**, ruff clean.

## 2026-07-23 analyze-sessions --since — 코드 버전 혼합 아카이브의 집계 오염 방지

- **증상 (사용자 지적)**: 모델/판정 코드를 계속 바꾸는 실험 중에도 리포트가
  아카이브 전체를 집계 — 구 코드 출처 세션의 오답·mismatch가 최신 코드
  평가에 계속 섞인다 (과금 정오·Phase 2 정오 집계 모두).
- **해결**: `--since <epoch|ISO일시>` 필터. 기준은 세션 id 말미 epoch
  (ses-N-<epoch>), 없으면 파일 mtime — 아카이브의 finalized_at은 monotonic
  clock이라 벽시계 비교 불가. 사용: 배포 직후 시각을 적어 두고
  `analyze-sessions --since 2026-07-23T21:00`. 완전 리셋은 디렉토리 이동
  (`mv data/sessions data/sessions.pre-<태그>`)으로도 가능 — 서비스가
  다음 확정 때 디렉토리를 재생성한다.
- 테스트 2건 추가, 전체 **320 passed, 4 skipped**, ruff clean.

## 2026-07-23 트랙릿 잔여 갭 4종 shadow 구현 (0723 문서 §9, 사용자 승인)

- **배경**: 의류/사람 오탐(8차 산탄)이 변위 몰수를 통과하는 구조적 이유 —
  사람이 움직이므로 궤적 자체는 "취출 물리"를 만족한다. 실무 문헌 조사
  (Seq-NMS 튜브 재점수화, ByteTrack 2단계 연관, ECCV18 트랙 기반 하드
  네거티브 수확) 대조로 잔여 갭 4개 확정, 사용자 지시로 갭 4→2→1→3 순
  구현. 0723 문서의 위험 분석(G1 역전·fragmentation)을 존중해 **전 갭
  shadow-first** — 판정 무변경, env로만 active 승격.
- **구현**:
  - 갭 4 `TUBE_IDENTITY=shadow`: 클래스 무관 튜브 층(_Tube) 병행 연관 +
    다수결 결정적 소수(30% 문턱) 몰수(active 시). 표 이전이 아니라 몰수.
  - 갭 2 `VOTE_RECOVERY=shadow`(+`_FLOOR=0.35`): 변위 통과 트랙 + 같은
    (클래스,트랙) 진입 표 앵커 조건의 저신뢰 표 회수 — 5차 "23이 1표"
    표 기아 대응. 앵커 없는 순저신뢰 궤적(의류 바닥)은 회수 불가.
  - 갭 1 `TRACK_MIN_HITS`/`TRACK_MAX_GAP`(기본 0=off): probation·트랙
    소멸 — fail-closed 방향이라 계측(short probe 3) 실측 후에만 켠다.
  - 갭 3: 자격 트랙별 평균 conf 최대(tube_conf) 계측 상시 (판정 미개입).
- **관측/승격 게이트**: `vote_summary.tube_shadow`(클래스별 현행/가상
  유효표 + 튜브 히스토그램) → analyze-sessions "튜브 shadow" 섹션이 1위
  변경 트리거의 라벨 정오(shadow만 정답/현행만 정답/둘 다 오답)를 집계.
- **관련 파일**: perception/motion_evidence.py(_Tube·tube_minority·
  track_obs·tube_detail·track_max_gap), perception/voting.py(모드 3종·
  회수 풀·tube_summary), core/config.py, service/model_service.py,
  service/pipeline.py(_with_tubes), adapters/analyze_cli.py(tube_eval).
- **테스트**: 신규 14건(재현 테스트 test_tube_summary_top_flip_for_
  promotion_eval 포함), 전체 **366 passed, 4 skipped**, ruff clean.

## 2026-07-23 10차 배치(트랙릿 shadow 첫 실기) — held 오플래그 정정 + 라벨/덤프 계측 보강

- **10차 결과**: 과금 8/12 (실질 9/12 — ses-12는 라벨 형식 문제). 오답 3건
  전부 "옷≈13" 계열: ses-3(23→13 대체), ses-11(교차존 채택이 13 선택),
  ses-8(held 44 우연 적합 44×4). BOCPD primary 17건 mismatch 0 (승격 안정).
- **held 판정 결함 발견 (shadow 게이트가 잡음)**: 정답 클래스 held 플래그
  5건, 결정타는 ses-6 z1 c40 **60/61표** — 진열 상품도 프리롤 0프레임부터
  관측되므로 "진열→취출 전환" 트랙이 head_obs 기준으로는 carried-in과
  구분 불가. active였다면 진짜 취출 표 60개를 몰수할 뻔했다.
  → track_held에 **head 구간 내 이동 ≥ floor_px** 요건 추가 (carried-in은
  손에 들려 head에도 움직이고, 진열은 head에 정지). 실패 방향은
  fail-open(정지 hold 놓침 = 증거 보존). track_detail에 head_path 노출.
- **튜브 shadow 첫 판정: 승격 보류 (0:3:2, 현행 우세)** — 1위 변경 5건 중
  3건이 shadow를 c13(옷 클래스) 쪽으로 밀었다. 가설: 회수(갭 2)가 13의
  저신뢰 산탄을 증폭(13은 자기 튜브의 다수라 소수 몰수 비적용). 단
  --session 덤프에 tube_shadow 분해가 없어 갭별 원인 확정 불가였다
  → render_session에 tube_shadow(클래스별 소수/단명/회수/tconf 분해 +
  튜브 히스토그램) 추가. 다음 배치에서 원인 확정 후 회수 상한/앵커 조정.
- **라벨 도구**: label-session `--none`(무취출 GT — 청구 0이어야 정답) 추가,
  analyze는 ground_truth 존재 여부로 라벨 판정(빈 items 허용) + 구 0x1
  우회 라벨은 class 0 필터로 동일 취급 (ses-12 소급 정정).
- **재확인**: 단절(실질 트랙/클래스 median 4) 심각 — TRACK_MIN_HITS/
  MAX_GAP은 계속 off 유지. σ_db 제안 40.16은 오염 표본으로 불신(6.3 유지).
- 테스트 3건 추가, 전체 **369 passed, 4 skipped**, ruff clean.

## 2026-07-23 --session 덤프 압축 (11차 준비) — 승격·은퇴 필드 정리

- **증상 (사용자 지적)**: 덤프 필드가 누적돼 사후 분석 시 노이즈가 신호를
  덮음 — motion_evidence 전체 트랙 덤프가 지배적, BOCPD 일치·은퇴 스테이지
  0 드랍·candidates와 중복인 생존 클래스가 매 트리거 반복.
- **정리 원칙**: "승격/은퇴로 일치가 기본값이 된 필드는 예외만 출력".
  ① loadcell_shadow: mismatch일 때만 (BOCPD primary 승격 후 일치가 정상)
  ② filter_drops: 드랍 0 스테이지 숨김 (baseline·static_track 은퇴 잔재)
  ③ classes → rejected 줄로 축약 (생존자는 candidates 줄과 중복)
  ④ motion_evidence → "몰수 클래스 + held 트랙"만 (전체 분포는 집계
  리포트 트랙릿 T1 섹션이 소비)
  ⑤ likelihood_shadow: 일치 1줄, mismatch만 ranking 상위 3.
  원자료 전체는 `--full` 플래그로 보존 (스키마·아카이브는 무변경 — 표시만).
- 테스트 1건 추가, 전체 **370 passed, 4 skipped**, ruff clean.
