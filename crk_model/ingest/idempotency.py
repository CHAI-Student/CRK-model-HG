"""트리거 멱등성 (I7) — MD5(zone+video paths), TTL 5s."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterResult:
    duplicate: bool
    # 최초 등록 시 발급된 식별자를 그대로 되돌려준다 — 호출측
    # (model_service.handle_trigger)은 trigger_id를 넣고 중복 응답의
    # trigger_id로 회신한다. 필드명은 초기 계약의 잔재.
    session_id: str


class IdempotencyRegistry:
    def __init__(self, ttl_seconds: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, str]] = {}

    @staticmethod
    def key_for(zone: int, video_paths: Mapping[str, str]) -> str:
        raw = f"{zone}|" + "|".join(f"{k}:{v}" for k, v in sorted(video_paths.items()))
        return hashlib.md5(raw.encode()).hexdigest()

    def register(self, key: str, session_id: str) -> RegisterResult:
        now = self._clock()
        for k in [k for k, (ts, _) in self._entries.items() if now - ts > self._ttl]:
            del self._entries[k]
        if key in self._entries:
            _, existing = self._entries[key]
            return RegisterResult(duplicate=True, session_id=existing)
        self._entries[key] = (now, session_id)
        return RegisterResult(duplicate=False, session_id=session_id)
