"""세션 트레이 메모리 — 세션 안에서 스스로 학습하는 트레이×상품 증거 맵.

운영단이 유지하는 정적 planogram(배치 사전정보)은 **금지된 전제**다 — 이
모듈은 그 대체물로, 배치를 가정하지 않고 **이 세션에서 실제로 관측·확정된
증거**만 쌓는다. cold-start(세션 첫 이벤트)는 맵이 비어 prior 기여가 0 →
현행 동작과 완전 동일(fail-open). 세션 OPEN마다 리셋되므로 재진열·배치
변경에도 무해하다.

물리 전제: 존마다 좌/우 두 트레이(로드셀 채널 2개)가 있고 트레이마다 별개
상품이 올라갈 수 있다 — 그래서 키는 존이 아니라 **(zone, channel)**이다.
같은 상품이 여러 트레이에 정당하게 존재할 수 있으므로 이 맵은 배타적
배치표가 아니라 **트레이별 증거 카운트**이며, 소비도 항상 소프트(로그 가점/
감점)다 — I-V: 정체성 선택권은 vision, 이 prior는 순위를 미는 증거일 뿐.

등록 게이트 (오판 전파 차단 — cross_zone penalty의 소스 신뢰도 게이트와
같은 원리):
- COMPLETE + 무게 뒷받침(vision_only 아님 — I6를 통과한 COMPLETE는 delta
  전량 설명이 보장됨)
- 채택된 vision 1위가 과금 목록에 포함 (판정이 vision 순위를 뒤집지 않은
  경우만 — `pipeline._vision_top_not_billed is None`)
- PARTIAL·near_gate·refit(무게가 고른 예외 경로)은 등록하지 않는다.

소비 (Phase 1: 무게 우도 shadow의 log_p_tray 항):
- 같은 트레이 증거 있음 → +boost (동일 트레이 반복 취출 일관성)
- 같은 트레이 증거 없음 & 다른 트레이 증거 있음 → −penalty
  (이슈 #17 ses-5: 존5 ch0에서 44×1이 확정된 세션에서 존4 이벤트의 44×3
  후보를 강등 — "저기서 이미 설명된 정체성"이라는 세션 내 증거)
- 둘 다 없음 → 0 (중립)
- 채널 미상(존 합산 이벤트)이면 존 수준으로 완화 매칭 — 모호한 증거로
  등록은 하되 같은 존 전체를 same으로 취급해 과잉 강등을 피한다.

동시성: ModelService의 코스 그레인 RLock 아래에서만 접근된다 (worker 처리와
handle_multi_zone[OPEN 리셋]이 같은 락을 공유) — 자체 락 없음.
"""
from __future__ import annotations

from collections import defaultdict


class SessionTrayMemory:
    def __init__(self, *, boost: float = 0.7, penalty: float = 2.5):
        # penalty 기본 2.5(log ≈ ×12 강등): 이슈 #17 ses-5 실측에서 44×3
        # (score −0.476)과 3×1(−2.91)의 격차 2.43을 뒤집는 최소값 근방 —
        # 아카이브 라벨 실측(conformal 절차)으로 보정할 것. boost는 동일
        # 트레이 반복 취출의 일관성 가점으로 보수적으로 작게 둔다.
        self._boost = boost
        self._penalty = penalty
        # (zone, channel|None) -> class_id -> 확정 횟수
        self._evidence: dict[tuple[int, int | None], dict[int, int]] = defaultdict(
            lambda: defaultdict(int)
        )

    def reset(self) -> None:
        """세션 OPEN마다 호출 — 세션 경계 밖으로 새지 않는다."""
        self._evidence.clear()

    def record(self, zone: int, channel: int | None, class_id: int, count: int = 1) -> None:
        """등록 게이트 통과가 확인된 확정 정체성 1건을 기록한다.

        게이트 판단(COMPLETE·top 일치 등)은 호출측(pipeline) 책임 — 이
        모듈은 채널 키 관리와 prior 계산만 담당한다."""
        if count <= 0:
            return
        self._evidence[(zone, channel)][class_id] += count

    def log_prior(self, zone: int, channel: int | None, class_id: int) -> float:
        """이 (zone, channel) 이벤트에서 class_id 후보의 로그 prior 기여.

        channel이 None이면 같은 존의 모든 트레이를 same으로 취급한다
        (모호할 때는 강등하지 않는 방향 — fail-open)."""
        same = 0
        other = 0
        for (z, ch), by_class in self._evidence.items():
            n = by_class.get(class_id, 0)
            if n <= 0:
                continue
            if z == zone and (channel is None or ch is None or ch == channel):
                same += n
            else:
                other += n
        if same > 0:
            return self._boost
        if other > 0:
            return -self._penalty
        return 0.0

    def priors_for(
        self, zone: int, channel: int | None, class_ids
    ) -> dict[int, float]:
        """후보 class_id들의 0이 아닌 prior만 모아 반환 (shadow 기록용)."""
        out: dict[int, float] = {}
        for cid in class_ids:
            v = self.log_prior(zone, channel, cid)
            if v != 0.0:
                out[cid] = round(v, 3)
        return out

    def snapshot(self) -> dict:
        """아카이브/진단용 — {"zone:channel": {class_id: count}}."""
        return {
            f"{z}:{'-' if ch is None else ch}": dict(by_class)
            for (z, ch), by_class in self._evidence.items()
            if by_class
        }
