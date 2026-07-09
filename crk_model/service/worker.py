"""단일 소비자 직렬 워커 (I7 후반, 제약 C2 — TensorRT 동시 추론 금지).

배리어(I17 ①) 공급 지점: submit()이 enqueued를, 처리 완료가 processed를
카운트한다. enqueue가 항상 먼저이므로 "close가 큐 잔량을 못 보는" race가
구조적으로 불가능하다 (원본 notify_trigger_enqueued/processed의 승격).

장치에서는 이 워커를 전용 스레드/태스크에서 drain()으로 돌린다.
테스트에서는 drain()을 명시 호출한다.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from crk_model.gateway.state_machine import MultiZoneGateway
from crk_model.ledger.journal import EventJournal
from crk_model.service.pipeline import TriggerOutcome, TriggerPipeline, TriggerRequest

logger = logging.getLogger(__name__)


class SerialTriggerWorker:
    def __init__(
        self,
        pipeline: TriggerPipeline,
        gateway: MultiZoneGateway,
        journal: EventJournal | None = None,
    ):
        self._pipeline = pipeline
        self._gateway = gateway
        self._journal = journal
        self._queue: deque[tuple[str, TriggerRequest]] = deque()
        self.outcomes: list[TriggerOutcome] = []  # I8: 트레이스 보존

    def submit(self, session_id: str, req: TriggerRequest) -> None:
        self._gateway.notify_enqueued(req.zone)  # I17 ①: enqueue 먼저
        self._queue.append((session_id, req))

    def drain(self) -> int:
        """큐 소진까지 순차 처리. 처리 건수 반환."""
        n = 0
        while self._queue:
            session_id, req = self._queue.popleft()
            logger.info(
                "[TRIGGER] processing: session=%s zone=%d loadcells=%d cameras=%s",
                session_id, req.zone, len(req.loadcells), list(req.frames),
            )
            started = time.monotonic()
            outcome = self._pipeline.process(session_id, req)
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
                time.monotonic() - started, session_id, ev.zone, ev.status,
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
