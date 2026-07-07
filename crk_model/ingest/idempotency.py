"""트리거 멱등성 (I7) — MD5(zone+video paths), TTL 5s."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class RegisterResult:
    duplicate: bool
    session_id: str  # 중복이면 기존 세션 ID 반환 (드롭 응답에 사용)


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
