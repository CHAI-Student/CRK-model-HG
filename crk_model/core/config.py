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


_VALID_CABINET_TYPES = ("refrigerated", "freezer")


def _env_cabinet_type(key: str, default: str) -> str:
    raw = os.environ.get(key)
    if not raw:
        return default
    normalized = raw.strip().lower()
    if normalized not in _VALID_CABINET_TYPES:
        # 원본 MachineModel.validate_cabinet_type 대응 — 오타/잘못된 값이 조용히
        # "refrigerated"로 폴백되는 것을 막는다 (fail-closed: 냉동 기기가 냉장
        # 프로파일로 동작하는 사고 재발 방지).
        raise ValueError(f"Invalid cabinet type: {raw}")
    return normalized


@dataclass(frozen=True)
class Settings:
    # I17: 인과 배리어 상한 타임아웃 (정상 경로 아님 — debounce 3s보다 길게)
    close_timeout_s: float = 10.0
    # queue_pending(워커 처리 중)은 유실이 아니라 진행 중 — Jetson 디코드+TRT
    # 추론이 close_timeout보다 길 수 있어 별도의 넉넉한 stall 상한을 적용한다.
    # 이 상한 초과 = 워커 사망/행 (I17 fail-closed 유지)
    worker_stall_timeout_s: float = 120.0
    # D8: 기본 OFF
    batch_size: int = 1
    # freezer 프로파일을 적용할 존 목록 (예: "9,10") — cabinet_type이 정하는
    # 기본 프로파일에 대한 존 단위 오버라이드로만 쓰인다 (freezer 기기에서
    # 특정 존만 냉장인 경우 등은 현재 스코프 밖).
    freezer_zones: tuple[int, ...] = field(default_factory=tuple)
    # 기기 단위 정적 설정 (원본 MachineModel.cabinet_type 대응, config.py
    # 60-75행). "refrigerated"|"freezer" — 실기가 냉동이면 반드시 명시해야
    # 한다. 미설정 시 기본값(refrigerated)이 전 존에 ±3g 프로파일을 적용해
    # 이슈 #6의 공동 원인이 됐다.
    cabinet_type: str = "refrigerated"
    # D9: Node 합의(P4) 전 기본값은 fail-closed
    error_policy: ErrorSessionPolicy = ErrorSessionPolicy.BLOCK_PAYMENT
    # I7: 트리거 멱등성 TTL
    idempotency_ttl_s: float = 5.0
    # 무한 성장 방지: worker.outcomes 트레이스 보존 개수 상한 (I8, 24h+ soak 대비)
    outcomes_keep: int = 256
    # 무한 성장 방지: EventLog/settler 멱등 캐시에서 보존할 최근 세션 개수
    # (I11: 현재+직전 세션은 항상 보존 — CLOSE 재폴링이 새 OPEN 직후 섞여 들어올 수 있음)
    keep_sessions: int = 4
    # 무한 성장 방지: EventJournal 일자별 로테이션 파일 보존기간(일)
    journal_retention_days: int = 14
    # 세션 YAML 아카이브 (issue #6: 오판정 사후 분석용) — 빈 문자열이면 비활성.
    session_archive_dir: str = "data/sessions"
    session_archive_retention_days: int = 14

    @classmethod
    def from_env(cls) -> Settings:
        policy_raw = os.environ.get("MODEL__SESSION__ERROR_POLICY", "block_payment")
        return cls(
            close_timeout_s=_env_float("MODEL__CLOSE__BARRIER_TIMEOUT_S", 10.0),
            worker_stall_timeout_s=_env_float("MODEL__CLOSE__WORKER_STALL_TIMEOUT_S", 120.0),
            batch_size=_env_int("MODEL__VISION__BATCH_SIZE", 1),
            freezer_zones=_env_zones("MODEL__ZONES__FREEZER"),
            cabinet_type=_env_cabinet_type("MODEL__MACHINE__CABINET_TYPE", "refrigerated"),
            error_policy=ErrorSessionPolicy(policy_raw),
            idempotency_ttl_s=_env_float("MODEL__TRIGGER__IDEMPOTENCY_TTL_S", 5.0),
            outcomes_keep=_env_int("MODEL__TRIGGER__OUTCOMES_KEEP", 256),
            keep_sessions=_env_int("MODEL__LEDGER__KEEP_SESSIONS", 4),
            journal_retention_days=_env_int("MODEL__LEDGER__JOURNAL_RETENTION_DAYS", 14),
            session_archive_dir=os.environ.get("MODEL__SESSION__ARCHIVE_DIR", "data/sessions"),
            session_archive_retention_days=_env_int(
                "MODEL__SESSION__ARCHIVE_RETENTION_DAYS", 14
            ),
        )
