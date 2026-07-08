"""env 기반 설정 — 원본 `MODEL__*` 관행 보존 (레버별 독립 플래그 + 즉시 롤백 env).

.env 파싱은 호스트 어댑터 소관. 여기서는 os.environ만 읽는다 (의존성 0).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from crk_model.core.policy import ErrorSessionPolicy


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw else default


def _env_zones(key: str) -> tuple[int, ...]:
    raw = os.environ.get(key, "")
    return tuple(int(z) for z in raw.split(",") if z.strip())


@dataclass(frozen=True)
class Settings:
    # I17: 인과 배리어 상한 타임아웃 (정상 경로 아님 — debounce 3s보다 길게)
    close_timeout_s: float = 10.0
    # D8: 기본 OFF
    batch_size: int = 1
    # freezer 프로파일을 적용할 존 목록 (예: "9,10")
    freezer_zones: tuple[int, ...] = field(default_factory=tuple)
    # D9: Node 합의(P4) 전 기본값은 fail-closed
    error_policy: ErrorSessionPolicy = ErrorSessionPolicy.BLOCK_PAYMENT
    # I7: 트리거 멱등성 TTL
    idempotency_ttl_s: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        policy_raw = os.environ.get("MODEL__SESSION__ERROR_POLICY", "block_payment")
        return cls(
            close_timeout_s=_env_float("MODEL__CLOSE__BARRIER_TIMEOUT_S", 10.0),
            batch_size=_env_int("MODEL__VISION__BATCH_SIZE", 1),
            freezer_zones=_env_zones("MODEL__ZONES__FREEZER"),
            error_policy=ErrorSessionPolicy(policy_raw),
            idempotency_ttl_s=_env_float("MODEL__TRIGGER__IDEMPOTENCY_TTL_S", 5.0),
        )
