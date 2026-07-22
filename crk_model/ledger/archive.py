"""세션 YAML 아카이브 (issue #6) — 오판정 사후 분석용 세션 스냅샷 영속화.

배경: `delta=-76.7g`에 무게가 비슷한 다른 상품이 complete로 오판정됐는데,
어떤 vision 후보들이 경쟁했고 어떤 전략이 왜 이겼는지가 로그·저널 어디에도
남지 않아 사후 분석이 불가능했다. 원본(reference/CRK-model)은 세션마다 YAML
(data/sessions/completed/{날짜}/{door_session_id}.yaml)을 남겨 진단에 썼다
(session/yaml_persistence.py, session/door_session.py 대응).

이 모듈은 원본 YAML 포맷을 이벤트 소싱 도메인(TriggerEvent/FinalizedSettlement)
으로 재해석해 같은 목적(오판정 즉시 원인 추적)을 달성한다:
- 트리거별로 vision_candidates 전체(채택 안 된 후보 포함)와 video_paths를 남겨
  오판정 즉시 "어떤 후보가 경쟁했는지" + "어떤 AVI를 봐야 하는지"를 알 수 있게 한다.
- trace(yolo_calls/frames/gate_skipped/early_terminated/reason_codes)도 함께
  남겨 "왜 그 후보들만 봤는지"(모션 게이트·조기 종료)까지 재구성 가능하게 한다.

설계 원칙:
- 세션당 정확히 1회 저장 — 호출측(MultiZoneGateway._notify_finalize)이 FINALIZED/
  ERROR로 "최초" 전이하는 시점에만 부르므로 자연히 충족된다 (I11과 동형).
- 저장 실패는 절대 서비스 경로를 죽이지 않는다 (부가 기능) — try/except 후 warning.
- 런타임 의존성 0 원칙 유지: yaml은 모듈 최상단이 아니라 저장 시점에 lazy import.
  Jetson은 ultralytics 의존성으로 PyYAML이 사실상 항상 있지만, 없으면(ImportError)
  같은 내용을 .json으로 저장해 진단 데이터 유실을 막는다.
- 보존기간(retention_days) 초과 날짜 디렉토리는 새 아카이브 저장 시점에 정리한다
  (crk_model.ledger.journal.EventJournal의 로테이션/prune 패턴과 동일).
"""
from __future__ import annotations

import datetime
import logging
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from crk_model.core.types import FinalizedSettlement, ZoneBasket
from crk_model.ledger.events import TriggerEvent

if TYPE_CHECKING:
    # 지연 임포트 (순환 회피): crk_model.service.__init__ -> model_service ->
    # ledger.archive(본 모듈) 경로가 있어 모듈 최상단에서 service.pipeline을
    # 끌어오면 부분 초기화 상태의 순환 임포트가 된다. 타입 힌트 용도뿐이므로
    # TYPE_CHECKING 블록으로 미룬다 (런타임에는 아예 import되지 않음).
    from crk_model.service.pipeline import TriggerTrace

logger = logging.getLogger(__name__)

_DATE_FMT = "%Y-%m-%d"


def _load_document(path: Path) -> dict:
    if path.suffix == ".json":
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trace_to_dict(trace: TriggerTrace | None) -> dict:
    if trace is None:
        return {}
    return {
        "yolo_calls": trace.yolo_calls,
        "processed_frames": dict(trace.processed_frames),
        "gate_skipped_frames": dict(trace.gate_skipped_frames),
        "early_terminated": trace.early_terminated,
        "reason_codes": list(trace.reason_codes),
        "vote_summary": dict(trace.vote_summary) if trace.vote_summary else {},
        "loadcell_shadow": trace.loadcell_shadow,
        "likelihood_shadow": trace.likelihood_shadow,
    }


def _event_to_dict(
    event: TriggerEvent, trace: TriggerTrace | None, processing_time_ms: float
) -> dict:
    j = event.judgment
    return {
        "ts": event.ts,
        "zone": event.zone,
        "delta_weight": event.delta_weight,
        "segments": [
            {"start_ts": s.start_ts, "end_ts": s.end_ts, "delta_grams": s.delta_grams}
            for s in event.segments
        ],
        "status": event.status,
        "judgment": {
            "status": j.status.value,
            "strategy": j.strategy,
            "reason": j.reason,
            "confidence": j.confidence,
            "products": [
                {
                    "product_id": pc.product.product_id,
                    "name": pc.product.name,
                    # class_id/unit_weight: 정답 라벨(ground_truth, class_id
                    # 기반)·σ_db 잔차 실측(analyze-sessions)이 아카이브만으로
                    # 대조 가능하도록 기록 (가격만으로는 불가능했다).
                    "class_id": pc.product.class_id,
                    "unit_weight": pc.product.unit_weight,
                    "count": pc.count,
                    "unit_price": pc.product.unit_price,
                    "total_price": pc.total_price,
                }
                for pc in j.products
            ],
        },
        # 진단 핵심: 채택 안 된 후보 포함 전체 — 오판정 시 "무엇과 경쟁했는지"
        "vision_candidates": [
            {
                "class_id": c.class_id,
                "confidence": c.confidence,
                "vote_count": c.vote_count,
                "vote_ratio": c.vote_ratio,
            }
            for c in event.vision_candidates
        ],
        "video_paths": dict(event.video_paths),
        # 0711 교차존 오염 (Phase 1 계측): 에피소드 내 서브이벤트 앵커
        "change_timestamps": list(event.change_timestamps),
        "trace": _trace_to_dict(trace),
        "processing_time_ms": processing_time_ms,
    }


def _zone_to_dict(zb: ZoneBasket) -> dict:
    return {
        "zone": zb.zone,
        "weight_delta": zb.weight_delta,
        "trigger_count": zb.trigger_count,
        "notes": list(zb.notes),
        "products": [
            {
                "product_id": pc.product.product_id,
                "name": pc.product.name,
                "class_id": pc.product.class_id,
                "unit_weight": pc.product.unit_weight,
                "count": pc.count,
                "unit_price": pc.product.unit_price,
                "total_price": pc.total_price,
            }
            for pc in zb.products
        ],
    }


def build_session_document(
    session_id: str,
    status: str,  # "finalized" | "error"
    events: Sequence[TriggerEvent],
    settlement: FinalizedSettlement | None,
    traces: Mapping[TriggerEvent, TriggerTrace],
    processing_times_ms: Mapping[TriggerEvent, float],
    finalized_at: float,
    error_detail: str = "",
) -> dict:
    """세션 전체를 아카이브 문서(dict)로 조립 — YAML/JSON 공용 직렬화 입력.

    원본 YAML 포맷(door_session_id/zone/status/triggers/aggregated_products/
    summary)을 우리 도메인(세션+존별+트리거별)으로 재해석했다."""
    ordered_events = sorted(events, key=lambda e: e.ts)
    triggers = [
        _event_to_dict(e, traces.get(e), processing_times_ms.get(e, 0.0))
        for e in ordered_events
    ]

    if settlement is not None:
        total_price = settlement.total_price
        product_count = settlement.product_count
        zones = [_zone_to_dict(zb) for zb in settlement.zones]
        notes = list(settlement.notes)
    else:
        # barrier_timeout 등으로 settlement 자체가 없는 에러 세션 — 트리거
        # 이벤트만으로도 존별 근사 요약을 재구성해 진단 가치를 유지한다.
        total_price = 0
        product_count = 0
        by_zone: dict[int, list[TriggerEvent]] = {}
        for e in ordered_events:
            by_zone.setdefault(e.zone, []).append(e)
        zones = [
            {
                "zone": zone,
                "weight_delta": sum(e.delta_weight for e in zone_events),
                "trigger_count": len(zone_events),
                "notes": [],
                "products": [],
            }
            for zone, zone_events in sorted(by_zone.items())
        ]
        notes = []

    return {
        "session_id": session_id,
        "status": status,
        "finalized_at": finalized_at,
        "total_price": total_price,
        "product_count": product_count,
        "notes": notes,
        "error_detail": error_detail,
        # 정답 라벨 (research §6·확률화 Phase 1의 선행 조건): 실험자가 실제
        # 취출 품목/수량을 사후 기입한다 — `label-session` CLI 또는
        # SessionArchive.annotate_ground_truth(). None = 미라벨.
        # 스키마: {labeled_at, note, items: [{zone, class_id|name, count}]}
        "ground_truth": None,
        "zones": zones,
        "triggers": triggers,
    }


class SessionArchive:
    """세션 확정(FINALIZED/ERROR) 시점의 1회성 YAML(또는 JSON 폴백) 저장.

    경로: {archive_dir}/{YYYY-MM-DD}/{session_id}.yaml — 날짜는 finalize 시각
    기준(clock/today 주입 가능, 테스트 결정성).

    archive_dir이 빈 문자열이면 비활성(save가 즉시 무동작) — MODEL__SESSION__
    ARCHIVE_DIR=""로 운영 중 끌 수 있게 한다."""

    def __init__(
        self,
        archive_dir: str | Path,
        *,
        retention_days: int = 14,
        today: Callable[[], datetime.date] | None = None,
    ):
        self._dir = Path(archive_dir) if archive_dir else None
        self._retention_days = retention_days
        self._today = today or (lambda: datetime.date.today())

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def save(
        self,
        session_id: str,
        status: str,
        events: Sequence[TriggerEvent],
        settlement: FinalizedSettlement | None,
        traces: Mapping[TriggerEvent, TriggerTrace] | None = None,
        processing_times_ms: Mapping[TriggerEvent, float] | None = None,
        error_detail: str = "",
        finalized_at: float | None = None,
    ) -> Path | None:
        """세션 문서를 조립해 디스크에 쓴다. 실패해도 예외를 전파하지 않는다
        (아카이브는 부가 기능 — 서비스 경로를 절대 죽이지 않는다).

        반환값: 저장된 파일 경로(성공) 또는 None(비활성이거나 실패)."""
        if self._dir is None:
            return None
        try:
            return self._save(
                session_id,
                status,
                events,
                settlement,
                traces or {},
                processing_times_ms or {},
                error_detail,
                finalized_at,
            )
        except Exception:
            logger.warning(
                "[SESSION_ARCHIVE] failed to save session=%s (non-fatal)",
                session_id, exc_info=True,
            )
            return None

    def _save(
        self,
        session_id: str,
        status: str,
        events: Sequence[TriggerEvent],
        settlement: FinalizedSettlement | None,
        traces: Mapping[TriggerEvent, TriggerTrace],
        processing_times_ms: Mapping[TriggerEvent, float],
        error_detail: str,
        finalized_at: float | None,
    ) -> Path:
        assert self._dir is not None
        today = self._today()
        self._prune_old(today)

        doc = build_session_document(
            session_id,
            status,
            events,
            settlement,
            traces,
            processing_times_ms,
            finalized_at if finalized_at is not None else 0.0,
            error_detail,
        )

        date_dir = self._dir / today.strftime(_DATE_FMT)
        date_dir.mkdir(parents=True, exist_ok=True)

        path = self._write(date_dir, session_id, doc)
        logger.info("[SESSION_ARCHIVE] path=%s", path)
        return path

    def _write(self, date_dir: Path, session_id: str, doc: dict) -> Path:
        # 런타임 의존성 0 원칙: yaml은 저장 시점에만 import한다 (모듈 최상단 금지).
        try:
            import yaml  # type: ignore
        except ImportError:
            path = date_dir / f"{session_id}.json"
            import json

            path.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return path

        path = date_dir / f"{session_id}.yaml"
        path.write_text(
            yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def find(self, session_id: str) -> Path | None:
        """날짜 디렉토리 전체에서 세션 파일 탐색 (최신 날짜 우선)."""
        if self._dir is None or not self._dir.exists():
            return None
        for date_dir in sorted(
            (c for c in self._dir.iterdir() if c.is_dir()), reverse=True
        ):
            for suffix in (".yaml", ".json"):
                path = date_dir / f"{session_id}{suffix}"
                if path.exists():
                    return path
        return None

    def latest(self) -> Path | None:
        """가장 최근 아카이브 파일 — 실험 직후 `label-session --latest`용."""
        if self._dir is None or not self._dir.exists():
            return None
        candidates = [
            p
            for date_dir in self._dir.iterdir()
            if date_dir.is_dir()
            for p in date_dir.iterdir()
            if p.suffix in (".yaml", ".json")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def annotate_ground_truth(self, session_id: str, ground_truth: dict) -> Path:
        """저장된 세션 문서에 정답 라벨을 기입한다 (기존 라벨은 대체).

        conformal 보정(research §6)·무게 확률화 Phase 1 diff 정오 판정의
        데이터 소스 — 실험 시 실제 취출 품목/수량의 구조화 (이슈 코멘트
        수기 기록의 대체). save()와 달리 실패를 삼키지 않는다: 라벨링은
        사람이 지금 실행 중인 작업이라 조용한 실패가 더 해롭다."""
        path = self.find(session_id)
        if path is None:
            raise FileNotFoundError(
                f"session archive not found: {session_id} (dir={self._dir})"
            )
        doc = _load_document(path)
        doc["ground_truth"] = ground_truth
        self._rewrite(path, doc)
        return path

    @staticmethod
    def _rewrite(path: Path, doc: dict) -> None:
        if path.suffix == ".json":
            import json

            path.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return
        import yaml  # yaml 파일이 존재한다 = PyYAML로 저장됐다 (폴백 규칙)

        path.write_text(
            yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _prune_old(self, current: datetime.date) -> None:
        """보존기간(MODEL__SESSION__ARCHIVE_RETENTION_DAYS) 초과 날짜 디렉토리
        삭제 — journal.py의 로테이션 prune과 동일 패턴(새 아카이브 저장 시점에
        수행)."""
        assert self._dir is not None
        if not self._dir.exists():
            return
        cutoff = current - datetime.timedelta(days=self._retention_days)
        for child in self._dir.iterdir():
            if not child.is_dir():
                continue
            try:
                d = datetime.datetime.strptime(child.name, _DATE_FMT).date()
            except ValueError:
                continue  # 아카이브 로테이션 규칙과 무관한 디렉토리는 무시
            if d < cutoff:
                shutil.rmtree(child, ignore_errors=True)
