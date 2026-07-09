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
