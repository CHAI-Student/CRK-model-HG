"""이벤트 저널 — 불변 트리거 이벤트의 append-only JSONL 영속화 (D5).

원본의 YAML 세션 영속(data/sessions/)에 대응하되, 이벤트 소싱 구조에 맞게
"집계 상태"가 아니라 "이벤트"를 기록한다. replay()가 G2.5(세션 정산 등가성
게이트)의 훅: 회수한 저널을 재생해 구/신 정산기 diff를 검증한다.

무한 성장 방지 (24h+ soak): 단일 파일이 아니라 일자별 로테이션
(<stem>_YYYYMMDD<suffix>)으로 쓴다. 생성자에 준 path는 "베이스 경로"로
재해석되고(예: logs/events.jsonl → logs/events_20260709.jsonl), append 시
날짜가 바뀌면 새 파일을 연다. 보존기간(retention_days) 초과분은 append가
날짜 롤오버를 감지한 시점에 삭제한다. replay()는 존재하는 모든 로테이션
파일을 날짜순으로 이어 읽는다 (G2.5 등가성 보존 — 동작은 예전과 동일하게
"이 베이스 경로 아래 기록된 전체 이벤트"를 반환).
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from crk_model.core.types import (
    ActiveProduct,
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
    WeightSegment,
)
from crk_model.ledger.events import TriggerEvent


def event_to_dict(e: TriggerEvent) -> dict:
    return {
        "session_id": e.session_id,
        "zone": e.zone,
        "ts": e.ts,
        "delta_weight": e.delta_weight,
        "segments": [asdict(s) for s in e.segments],
        "judgment": {
            "status": e.judgment.status.value,
            "confidence": e.judgment.confidence,
            "reason": e.judgment.reason,
            "strategy": e.judgment.strategy,
            "products": [
                {"count": pc.count, "product": asdict(pc.product)}
                for pc in e.judgment.products
            ],
        },
        "seq": e.seq,
        "status": e.status,
        # 진단 강화 (issue #6): G2 코퍼스 재생의 입력이 되므로 저널에도 남긴다.
        "vision_candidates": [asdict(c) for c in e.vision_candidates],
        "video_paths": {k: v for k, v in e.video_paths},
    }


def event_from_dict(d: dict) -> TriggerEvent:
    j = d["judgment"]
    judgment = JudgmentResult(
        status=JudgmentStatus(j["status"]),
        products=tuple(
            ProductCount(ActiveProduct(**pc["product"]), pc["count"])
            for pc in j["products"]
        ),
        confidence=j["confidence"],
        reason=j["reason"],
        strategy=j["strategy"],
    )
    return TriggerEvent(
        session_id=d["session_id"],
        zone=d["zone"],
        ts=d["ts"],
        delta_weight=d["delta_weight"],
        segments=tuple(WeightSegment(**s) for s in d["segments"]),
        judgment=judgment,
        seq=d["seq"],
        status=d["status"],
        # dict.get 기본값 — 기존 저널 라인(신규 필드 도입 전) 파싱 호환.
        vision_candidates=tuple(
            VisionCandidate(**c) for c in d.get("vision_candidates", ())
        ),
        video_paths=tuple(d.get("video_paths", {}).items()),
    )


_DATE_FMT = "%Y%m%d"


class EventJournal:
    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 14,
        today: Callable[[], datetime.date] | None = None,
    ):
        """path는 "베이스 경로"로 취급된다 — 실제로는
        <parent>/<stem>_<YYYYMMDD><suffix>에 기록한다.

        today: 테스트에서 날짜를 주입하기 위한 훅(기본은 실제 오늘 날짜).
        """
        base = Path(path)
        self._dir = base.parent
        self._stem = base.stem
        self._suffix = base.suffix or ".jsonl"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._retention_days = retention_days
        self._today = today or (lambda: datetime.date.today())
        self._current_date: datetime.date | None = None

    # -- 파일명 헬퍼 --
    def _path_for(self, date: datetime.date) -> Path:
        return self._dir / f"{self._stem}_{date.strftime(_DATE_FMT)}{self._suffix}"

    def _dated_files(self) -> list[tuple[datetime.date, Path]]:
        """로테이션 규칙에 맞는 파일을 (날짜, 경로)로 수집 (정렬 없음)."""
        prefix = f"{self._stem}_"
        out = []
        for p in self._dir.glob(f"{prefix}*{self._suffix}"):
            date_part = p.stem[len(prefix):]
            try:
                d = datetime.datetime.strptime(date_part, _DATE_FMT).date()
            except ValueError:
                continue  # 로테이션 규칙과 무관한 파일은 무시
            out.append((d, p))
        return out

    def _rotation_files(self) -> list[Path]:
        """존재하는 모든 로테이션 파일을 날짜순으로 반환 (G2.5: replay 순서 보존)."""
        return [p for _, p in sorted(self._dated_files(), key=lambda t: t[0])]

    def _prune_old(self, current: datetime.date) -> None:
        """보존기간 초과 로테이션 파일을 삭제한다 (날짜 롤오버 시점에만 수행)."""
        cutoff = current - datetime.timedelta(days=self._retention_days)
        for d, p in self._dated_files():
            if d < cutoff:
                p.unlink(missing_ok=True)

    def append(self, event: TriggerEvent) -> None:
        today = self._today()
        if today != self._current_date:
            # 날짜 롤오버(또는 최초 호출) — 새 파일로 전환 + 보존기간 정리
            self._current_date = today
            self._prune_old(today)
        path = self._path_for(today)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")

    def replay(self, session_id: str | None = None) -> list[TriggerEvent]:
        """G2.5 훅: 저널 재생 → 정산기 등가성 검증 입력.

        존재하는 모든 로테이션 파일을 날짜순으로 이어 읽는다 (동작은 단일
        파일이던 시절과 동일 — "이 베이스 경로 아래 기록된 전체 이벤트")."""
        events: list[TriggerEvent] = []
        for path in self._rotation_files():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    e = event_from_dict(json.loads(line))
                    if session_id is None or e.session_id == session_id:
                        events.append(e)
        return events
