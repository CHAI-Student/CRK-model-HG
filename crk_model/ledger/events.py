"""이벤트 소싱 (D5): 트리거 = 불변 이벤트 축적. 확정 후 이벤트는 거부+기록 (I11 지원)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from crk_model.core.types import JudgmentResult, VisionCandidate, WeightSegment


@dataclass(frozen=True)
class TriggerEvent:
    session_id: str
    zone: int
    ts: float
    delta_weight: float
    segments: tuple[WeightSegment, ...]
    judgment: JudgmentResult
    seq: int | None = None  # D2: 카메라 시퀀스 (선택 — 없어도 동작)
    status: str = "ok"  # "ok" | "error" (I1: 처리 실패는 에러로 전파)
    # 진단 강화 (issue #6): 투표 앙상블 최종 후보 전체(채택 안 된 것 포함).
    # 오판정 사후 분석의 핵심 — 어떤 후보들이 경쟁했는지 남긴다.
    vision_candidates: tuple[VisionCandidate, ...] = ()
    # 원본 YAML의 video_paths 대응 — 오판정 시 해당 AVI를 즉시 찾는 용도.
    # frozen dataclass라 dict 대신 hashable한 (camera, path) 튜플로 보관한다.
    video_paths: tuple[tuple[str, str], ...] = ()
    # 0711 교차존 오염 (docs/cross_zone_penalty.md): 카메라가 보내는 에피소드 내
    # 서브이벤트(change) 벽시계 앵커 — 연장 병합된 에피소드의 t0/t2를 CLOSE
    # 2차 패스가 재구성하는 근거. IO-BOARD 단일 클럭(F7)이라 존 간 비교 가능.
    # 구버전 카메라는 빈 튜플 — segments/ts 폴백 (cross_zone.sub_event_anchors).
    change_timestamps: tuple[float, ...] = ()


@dataclass
class EventLog:
    _events: dict[str, list[TriggerEvent]] = field(default_factory=lambda: defaultdict(list))
    _finalized: set[str] = field(default_factory=set)
    rejected: list[TriggerEvent] = field(default_factory=list)

    def append(self, event: TriggerEvent) -> bool:
        if event.session_id in self._finalized:
            # I11: 확정 후 유입 이벤트는 정산에 반영 불가 — 유실 대신 기록
            self.rejected.append(event)
            return False
        self._events[event.session_id].append(event)
        return True

    def events_for(self, session_id: str) -> tuple[TriggerEvent, ...]:
        return tuple(self._events.get(session_id, ()))

    def mark_finalized(self, session_id: str) -> None:
        self._finalized.add(session_id)

    def prune(self, keep_session_ids: set[str]) -> None:
        """무한 성장 방지 (24h+ soak): keep_session_ids 밖의 세션 이벤트·확정
        마커를 제거한다. 호출측(ModelService)이 최근 K개 세션(I11: 현재+직전
        보존)만 keep_session_ids로 넘겨야 한다 — 여기서는 순수하게 교집합만
        수행한다."""
        for sid in [s for s in self._events if s not in keep_session_ids]:
            del self._events[sid]
        self._finalized &= keep_session_ids
