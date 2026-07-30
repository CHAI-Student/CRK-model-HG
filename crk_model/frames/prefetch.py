"""프레임 프리페처 — 디코드 ‖ 추론 파이프라이닝 (T2-3,
docs/devdoc/research/0728_freezer_latency_research.md).

배경: 트리거 처리에서 디코드(ffmpeg 서브프로세스 파이프 읽기 + numpy 변환)는
추론과 직렬로 실행돼 비YOLO 비용(총 처리의 12~21%)이 그대로 지연에 더해진다.
ffmpeg는 별도 프로세스이고 TensorRT 파이썬 바인딩·numpy·파이프 read는 GIL을
해제하므로, 백그라운드 스레드가 큐 깊이만큼 선행 디코드하면 디코드 비용이
추론 시간에 은닉된다. 카메라별 프리페처를 트리거 시작 시점에 전부 만들면
top 추론 중 side 디코드도 함께 진행된다 (2캠 동시 디코드).

메모리 상한: depth × 프레임 691KB (480×480×3) — depth 4 기준 카메라당
~2.8MB로 fix_logs.md:104의 OOM 경고(400장 상주 ~276MB)와 무관하다.

계약:
- 순서 보존: 소스 이터레이터의 방출 순서 그대로.
- 예외 전파 (I1): 소스가 던진 예외는 큐에 남은 프레임을 모두 소진한 뒤
  소비자에게 재던져진다 — 비프리페치 대비 "예외가 드러나는 프레임 위치"만
  뒤로 밀릴 수 있고(선행 디코드분), 무검출로 삼켜지지는 않는다.
- close(): 소비자가 중도 포기(조기 종료 등)하면 생산자 스레드를 멈추고
  소스의 close()(ffmpeg kill 등)까지 전파한다. 멱등.
- 런타임 의존성 0: threading/queue 표준 라이브러리만 사용.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from typing import Any

_SENTINEL = object()


class PrefetchFrames:
    """소스 이터레이터를 백그라운드 스레드에서 depth장 선행 소비하는 래퍼."""

    def __init__(self, source: Iterator[Any], depth: int = 2):
        if depth < 1:
            raise ValueError("depth >= 1")
        self._source = source
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._pump, name="frame-prefetch", daemon=True
        )
        self._thread.start()

    def _put(self, item) -> bool:
        """stop 신호를 살피며 put — close()로 소비자가 떠나도 생산자가
        가득 찬 큐에 영원히 블록되지 않는다."""
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _pump(self) -> None:
        try:
            for item in self._source:
                if not self._put(item):
                    return  # close됨 — finally가 소스를 닫는다
        except BaseException as exc:  # noqa: BLE001 — I1: 소비자로 재전파
            self._error = exc
        finally:
            closer = getattr(self._source, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 — 정리 실패는 전파 대상 아님
                    pass
            self._put(_SENTINEL)

    def __iter__(self) -> PrefetchFrames:
        return self

    def __next__(self):
        if self._stop.is_set():
            raise StopIteration
        item = self._queue.get()
        if item is _SENTINEL:
            self._stop.set()  # 재호출 시 즉시 StopIteration (제너레이터 동형)
            if self._error is not None:
                error, self._error = self._error, None
                raise error
            raise StopIteration
        return item

    def close(self) -> None:
        """생산자 중단 + 소스 close 전파. 멱등 — 파이프라인의 finally 계약
        (pipeline._run_vision: 조기 종료 시 스트림 즉시 해제)과 동형."""
        self._stop.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._thread.join(timeout=5.0)
