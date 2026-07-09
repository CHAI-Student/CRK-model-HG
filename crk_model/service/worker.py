"""단일 소비자 직렬 워커 (I7 후반, 제약 C2 — TensorRT 동시 추론 금지).

배리어(I17 ①) 공급 지점: submit()이 enqueued를, 처리 완료가 processed를
카운트한다. enqueue가 항상 먼저이므로 "close가 큐 잔량을 못 보는" race가
구조적으로 불가능하다 (원본 notify_trigger_enqueued/processed의 승격).

장치에서는 이 워커를 전용 스레드/태스크에서 drain()으로 돌린다.
테스트에서는 drain()을 명시 호출한다.

동시성: FastAPI sync 엔드포인트는 anyio threadpool에서 병렬 실행될 수 있고,
별도 워커 스레드가 상시 drain()을 돈다 — 게이트웨이/이벤트로그 등 공유 상태에
락이 없으면 경합이 생긴다. ModelService가 단일 RLock을 주입하며(lock=...),
drain()은 **이벤트 1건 처리 단위**로만 락을 잡는다: 파이프라인 추론
(pipeline.process, 수 초~수십 초)은 락 밖에서 실행하고, 그 결과를 게이트웨이에
기록하는 구간(record_trigger/notify_processed/journal.append)만 락 안에서
수행한다. 큐 전체를 잠그면 추론 동안 multi-zone 폴링이 전부 블록되기 때문.
lock=None(기본값)이면 잠그지 않는다 — 기존 테스트가 lock 없이 워커를 생성하므로
하위호환을 위해 유지.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from collections import deque

from crk_model.gateway.state_machine import MultiZoneGateway
from crk_model.ledger.journal import EventJournal
from crk_model.service.pipeline import TriggerOutcome, TriggerPipeline, TriggerRequest

logger = logging.getLogger(__name__)

_NULL_LOCK = contextlib.nullcontext()


class SerialTriggerWorker:
    def __init__(
        self,
        pipeline: TriggerPipeline,
        gateway: MultiZoneGateway,
        journal: EventJournal | None = None,
        *,
        lock: threading.RLock | None = None,
        outcomes_keep: int = 256,
    ):
        self._pipeline = pipeline
        self._gateway = gateway
        self._journal = journal
        self._lock = lock  # None 허용: 하위호환(기존 테스트는 lock 없이 생성)
        self._queue: deque[tuple[str, TriggerRequest]] = deque()
        # I8: 트레이스 보존 — 무한 성장 방지(24h+ soak)로 상한을 둔다.
        # deque(maxlen=...)도 인덱싱 가능(outcomes[0], outcomes[-1])이라 호환.
        self.outcomes: deque[TriggerOutcome] = deque(maxlen=outcomes_keep)

    def _guard(self):
        """락 보호 구간 컨텍스트 매니저. lock이 None이면 무동작(nullcontext)."""
        return self._lock if self._lock is not None else _NULL_LOCK

    def submit(self, session_id: str, req: TriggerRequest) -> None:
        # I17 ①: enqueue(notify_enqueued)가 append보다 항상 먼저여야 "close가
        # 큐 잔량을 못 보는" race가 없다. deque.append 자체는 GIL로 원자적이지만,
        # "notify_enqueued 후 append 전" 구간에 다른 스레드의 drain()이 끼어들면
        # 배리어 카운트만 오르고 큐엔 아직 없는 순간이 관측될 수 있어 이 복합
        # 구간 전체를 락으로 묶는다.
        with self._guard():
            self._gateway.notify_enqueued(req.zone)
            self._queue.append((session_id, req))

    def drain(self) -> int:
        """큐 소진까지 순차 처리. 처리 건수 반환.

        각 이벤트마다: 락 밖에서 popleft+추론 → 락 안에서 게이트웨이 기록.
        큐 전체를 한 번에 잠그지 않는 이유는 파이프라인 추론이 수 초~수십 초
        걸릴 수 있어, 그 동안 multi-zone 폴링(handle_multi_zone)을 블록하면
        안 되기 때문. popleft는 deque 자체가 GIL로 원자적이라 락 없이도 안전.
        """
        n = 0
        while True:
            with self._guard():
                if not self._queue:
                    break
                session_id, req = self._queue.popleft()

            logger.info(
                "[TRIGGER] processing: session=%s zone=%d loadcells=%d cameras=%s",
                session_id, req.zone, len(req.loadcells), list(req.frames),
            )
            started = time.monotonic()
            outcome = self._pipeline.process(session_id, req)  # 락 밖: 수 초~수십 초 가능
            elapsed_ms = (time.monotonic() - started) * 1000.0
            # 세션 아카이브(issue #6) 진단용 실측 처리시간 — pipeline은 시간을
            # 모르므로 여기서 채운다(frozen dataclass라 replace로 새 객체 생성).
            outcome = dataclasses.replace(outcome, processing_time_ms=elapsed_ms)

            with self._guard():
                accepted = self._gateway.record_trigger(outcome.event)
                if self._journal is not None:
                    self._journal.append(outcome.event)
                self._gateway.notify_processed(req.zone)  # 처리 완료 후에만
                self.outcomes.append(outcome)
            n += 1

            ev, tr = outcome.event, outcome.trace
            log = logger.error if ev.status != "ok" else logger.info
            log(
                "[TRIGGER] done in %.2fs: session=%s zone=%d status=%s judgment=%s "
                "delta=%.1fg products=%s yolo_calls=%d frames=%s early_term=%s "
                "reasons=%s accepted=%s",
                elapsed_ms / 1000.0, session_id, ev.zone, ev.status,
                ev.judgment.status.value, ev.delta_weight,
                [(pc.product.name, pc.count) for pc in ev.judgment.products],
                tr.yolo_calls, dict(tr.processed_frames), tr.early_terminated,
                tr.reason_codes, accepted,
            )
            if not accepted:
                # I11: 확정 후 유입 — 정산에 반영되지 않음 (유실 아님, rejected 기록)
                logger.warning(
                    "[TRIGGER] event rejected (session %s already finalized)", session_id
                )
        return n

    @property
    def pending(self) -> int:
        return len(self._queue)

    def outcomes_for(self, session_id: str) -> list[TriggerOutcome]:
        """세션 아카이브(issue #6) 조회용: 해당 세션의 trace/처리시간을 보존한
        outcome 목록. outcomes_keep(deque maxlen) 상한 밖으로 밀려난 이벤트는
        조회되지 않는다 — 아카이브는 finalize 시점에 곧바로 호출되므로 실전에서
        밀려날 일은 없다(세션 하나가 outcomes_keep건 이상 트리거를 내는 경우는
        상정하지 않음)."""
        return [o for o in self.outcomes if o.event.session_id == session_id]
