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
