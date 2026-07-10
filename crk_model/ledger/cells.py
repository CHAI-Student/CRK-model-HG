"""셀 정체성 신념 저장소 (설계 v2).

배치는 입력이 아니라 추론 결과다 — 사람이 배치표를 등록해야 한다면 비전·로드셀
추론이 무의미하다 (README "셀 정체성 추정"). 전제 3(한 로드셀에 한 상품 종류)이
보장하는 것은 "셀마다 추정할 정답이 정확히 하나 존재한다"는 것뿐이고, 그 정답은
확정 트리거의 비전×무게 교차 증거로 이 저장소에 누적된다.

증거 규칙:
- 제거(−delta) 관측은 강한 증거(셀에 있던 것만 꺼낼 수 있음), 반품(+delta)은
  약한 증거(고객이 엉뚱한 셀에 되돌려놓을 수 있음).
- 승격: 모순 없는 증거가 promote_score 이상 + 경쟁 상품 대비 promote_ratio배
  이상이면 "알려진 셀" — 이후 비전이 실패해도 무게만으로 판정된다.
- 강등: 알려진 셀에서 다른 상품의 강한 모순 증거(비전 지목 + delta 설명)가
  demote_score 이상 쌓이면 미지로 복귀 — 재고 보충으로 배치가 바뀌어도 자기
  교정된다. 승격 시점에 경쟁 점수를 리셋하므로 "확신 이후의" 모순만 센다.
- 무효화: OPEN allowlist에서 사라진 상품을 정체성으로 갖던 셀은 미지로 복귀.

영속: JSON 파일 (path=None이면 메모리 전용 — 테스트/임시 인스턴스가 실제
저장소에 부작용을 남기지 않게 하는 SessionArchive와 동일 원칙, 운영 진입점이
Settings.cells_state_path로 명시 주입). 로드 실패는 빈 상태로 시작(안전 방향),
저장 실패는 경고 후 계속(판정 자체는 메모리 상태로 동작).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def _key(zone: int, channel: int) -> str:
    return f"z{zone}c{channel}"


class CellBeliefStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        promote_score: float = 3.0,
        promote_ratio: float = 3.0,
        demote_score: float = 2.0,
        strong_weight: float = 1.0,
        weak_weight: float = 0.25,
        cold_weight: float = 0.5,
    ):
        self._path = Path(path) if path else None
        self._promote_score = promote_score
        self._promote_ratio = promote_ratio
        self._demote_score = demote_score
        self._strong = strong_weight
        self._weak = weak_weight
        self._cold = cold_weight
        self._lock = threading.Lock()
        self._scores: dict[str, dict[str, float]] = {}
        self._confirmed: dict[str, str] = {}
        self._load()

    # ---- 조회 ----
    def identity(self, zone: int, channel: int) -> str | None:
        """확신에 도달한 셀의 상품 ID. 미지면 None."""
        with self._lock:
            return self._confirmed.get(_key(zone, channel))

    def identities_for_zone(self, zone: int, channels: Iterable[int]) -> dict[int, str]:
        with self._lock:
            out = {}
            for ch in channels:
                pid = self._confirmed.get(_key(zone, ch))
                if pid is not None:
                    out[ch] = pid
            return out

    # ---- 증거 ----
    def observe(
        self, zone: int, channel: int, product_id: str, *, strong: bool, cold: bool = False
    ) -> None:
        """확정 트리거의 (셀 → 상품) 관측 1건.

        확신 상태에서 다른 상품의 관측이 들어오면 모순 증거로 누적되고,
        demote_score를 넘으면 강등된다 (약한 증거만으로는 강등 불가 —
        오배치 반품이 신념을 오염시키지 않게 한다).

        cold=True는 cold start 순위 채택(이슈 #9) — 근거가 비전 순위뿐이므로
        저가중(cold_weight)으로 쌓아 잘못된 리더가 신념으로 굳는 속도를 늦춘다.
        """
        if not product_id:
            return
        weight = self._cold if cold else (self._strong if strong else self._weak)
        key = _key(zone, channel)
        with self._lock:
            scores = self._scores.setdefault(key, {})
            scores[product_id] = scores.get(product_id, 0.0) + weight
            confirmed = self._confirmed.get(key)
            if confirmed is None:
                self._maybe_promote(key, scores)
            elif product_id != confirmed and strong:
                if scores[product_id] >= self._demote_score:
                    del self._confirmed[key]
                    logger.warning(
                        "[CELLS] demoted zone=%s ch=%s was=%s rival=%s (contradiction)",
                        zone, channel, confirmed, product_id,
                    )
                    self._maybe_promote(key, scores)
            self._save()

    def _maybe_promote(self, key: str, scores: dict[str, float]) -> None:
        best_pid, best = max(scores.items(), key=lambda kv: kv[1])
        rival = max((v for pid, v in scores.items() if pid != best_pid), default=0.0)
        if best >= self._promote_score and best >= self._promote_ratio * max(rival, 1e-9):
            self._confirmed[key] = best_pid
            # 승격 시점에 경쟁 점수 리셋 — 이후 모순 증거만 demote_score와 비교
            self._scores[key] = {best_pid: best}

    # ---- 무효화 ----
    def invalidate_missing(self, active_product_ids: set[str]) -> None:
        """OPEN allowlist에 없는 상품의 신념/점수 제거 (진열 철수 대응)."""
        with self._lock:
            changed = False
            for key in list(self._confirmed):
                if self._confirmed[key] not in active_product_ids:
                    del self._confirmed[key]
                    changed = True
            for scores in self._scores.values():
                for pid in [p for p in scores if p not in active_product_ids]:
                    del scores[pid]
                    changed = True
            if changed:
                self._save()

    # ---- 영속 ----
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._confirmed = dict(data.get("confirmed", {}))
            self._scores = {
                k: {pid: float(v) for pid, v in s.items()}
                for k, s in data.get("scores", {}).items()
            }
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # 로드 실패 = 빈 상태 시작 (미지 셀은 fail-closed라 안전 방향)
            logger.warning("[CELLS] state load failed (%s) — starting empty", exc)
            self._confirmed = {}
            self._scores = {}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"version": 1, "confirmed": self._confirmed, "scores": self._scores},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("[CELLS] state save failed (%s) — continuing in-memory", exc)
