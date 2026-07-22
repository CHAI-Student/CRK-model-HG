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


def _env_opt_float(key: str) -> float | None:
    raw = os.environ.get(key)
    return float(raw) if raw not in (None, "") else None


def _env_opt_int(key: str) -> int | None:
    raw = os.environ.get(key)
    return int(raw) if raw not in (None, "") else None


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_zones(key: str) -> tuple[int, ...]:
    raw = os.environ.get(key, "")
    return tuple(int(z) for z in raw.split(",") if z.strip())


_VALID_CABINET_TYPES = ("refrigerated", "freezer")

_VALID_BASELINE_MODES = ("off", "shadow", "active")


def _env_baseline_mode(key: str, default: str) -> str:
    raw = os.environ.get(key)
    if not raw:
        return default
    normalized = raw.strip().lower()
    if normalized not in _VALID_BASELINE_MODES:
        # cabinet_type과 동일한 fail-closed: 오타가 조용히 기본 모드로
        # 폴백되면 shadow 검증 없이 active로 갔다고 오인할 수 있다.
        raise ValueError(f"Invalid baseline suppress mode: {raw}")
    return normalized


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
    # CLOSE 유예 창 (issue #8, 원본 close_initial_wait_seconds 복원): 배리어가
    # 충족돼도 CLOSE·마지막 트리거 도착 후 이 시간 동안 확정을 보류 — 카메라가
    # 아직 쓰고 있는 AVI의 late trigger 유실(0원 확정+rejected) 방지. seq
    # 워터마크(D2) 배포 전까지의 유일한 방어. 0이면 비활성.
    close_grace_s: float = 3.0
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
    # ---- 비전 투표 튜닝 (issue #6 2차: 실기 vote_summary로 conf_floor 전멸 확정) ----
    # 카메라별 투표 진입 임계 — 원본 top/side_confidence_threshold 대응 (코드 기본
    # 0.70, 원본 운영 .env.example은 0.50). 이 값 미만 검출은 투표에 진입하지 못해
    # 노이즈가 평균 conf를 희석하지 않는다 (원본의 노이즈 방어 지점).
    top_confidence_threshold: float = 0.70
    side_confidence_threshold: float = 0.70
    # 후보 채택 임계 — 원본 min_vote_ratio/min_vote_count 대응.
    min_vote_ratio: float = 0.05
    min_vote_count: int = 3
    # 1위 후보 득표 대비 상대 하한 (이슈 #10): 절대 count(3)는 400프레임+
    # 영상에서 노이즈도 통과시켜 저득표 후보가 "무게 filler"로 채택되는
    # 사고(메로나 79g×3)의 원인이 됐다. votes < top×share 후보 제거.
    min_vote_share: float = 0.1
    # 결합 후 weighted_conf 하한 — 원본에는 없는 파라미터 (원본 동형 = 0.0).
    # 진입 컷이 노이즈를 이미 거르므로 기본 0.0. 진입 컷을 0으로 낮춰 저신뢰
    # 투표를 보존하고 싶을 때만 안전판으로 올려 쓴다.
    vote_conf_floor: float = 0.0
    # Side ROI: 존 바깥(오른쪽) 검출 제거 경계 — 카메라 장착에 맞게 조정
    # 가능해야 한다. 기본 400은 left-crop 480×480 좌표계(P0-1)에서의 원본
    # 정합값(side_roi_x_max=400). 구값 240은 squash resize 좌표계 산물로,
    # 실기에서 side 검출 194/195가 제거되던 원인이었다.
    side_roi_max_center_x: float = 400.0
    # 정지 트랙 억제 (이슈 #10 돌출 진열 상품): 같은 class가 IoU ≥ iou로
    # min_frames(추론 프레임 기준) 이상 같은 자리에 머물면 투표에서
    # 제거. min_frames=0이면 비활성.
    static_track_min_frames: int = 24
    static_track_iou: float = 0.85
    # Baseline 억제 (이슈 #14 후속): 손 등장 전(프리롤)에 이미 있던 class를
    # 배경으로 등록하고 같은 자리 재검출을 억제 — static_track의 연속 IoU
    # 0.85 조건을 못 채우는 "깜빡이는 고정 물체" 대응. 모드: off / shadow
    # (드랍 없이 drop_stats["baseline"] 계수만) / active(실제 드랍).
    # shadow 실측으로 진짜 상품 표가 안 깎이는 걸 확인한 뒤 active로 승격.
    baseline_suppress_mode: str = "shadow"
    baseline_suppress_iou: float = 0.5
    # ---- 판정 I-V 노브 (이슈 #15, FreezerVisionFirst 단계별 임계) ----
    # single_share: top 득표 대비 이 비율 이상만 단일 정체성 교체 시도 허용
    # combo_share: 조합 멤버 자격 하한 / refit_share: 유일-적합 구제 자격 하한
    # near_factor: count_gate × 이 배수까지를 "접촉 오염 마진"으로 간주
    judgment_single_share: float = 0.5
    judgment_combo_share: float = 0.3
    judgment_near_factor: float = 2.0
    judgment_refit_share: float = 0.1
    # ---- 무게 중재 재설계 노브 (이슈 #16, docs/0722_issue16_arbitration_design.md) ----
    # count_unit_slack: 개수당 게이트 가산(g) — gate_n(n)=gate+slack×(n−1) (0=flat)
    # conf_override: ① 자격의 conf 문턱 (share 미달 보완, 2.0=비활성)
    # conf_margin: ① 복수 적합 중재에서 conf가 득표 서열을 뒤집는 최소 격차 (2.0=비활성)
    judgment_count_unit_slack: float = 5.0
    judgment_conf_override: float = 0.9
    judgment_conf_margin: float = 0.15
    # ---- 조기 종료 (D7) — removal & 비freezer에서만 유효 ----
    early_termination_enabled: bool = True
    # ---- 모션 게이트 오버라이드 (None = SensorProfile 기본값 유지) ----
    # 프로파일 상수(냉장 0.02/8, 냉동 0.005/4)를 기기 전 존에 대해 덮어쓴다.
    motion_gate_threshold: float | None = None
    motion_gate_keepalive: int | None = None
    # ---- 로드셀 안정 판정 (0.8s 캐던스 기준값, 이슈 #14) ----
    loadcell_stable_window: int = 3
    loadcell_stability_threshold_grams: float = 2.5
    # 오염 delta 이중 타깃 재시도 (이슈 #10): |delta − sum(segments)|가 이
    # 값을 넘으면(접촉 하중 오염 서명) delta 타깃 판정 실패 시 세그먼트 합
    # 타깃으로 1회 재판정. 실측 오염 트리거 8~18g / 깨끗한 트리거 0.
    segment_retry_gap_grams: float = 5.0
    # ---- 교차존 비전 오염 페널티 (docs/cross_zone_penalty.md) ----
    # Phase 3 승격 완료 (2026-07-21): 운영 검증(PENALTY_ENABLED=1)을 거쳐
    # 기본 ON. 비활성화하려면 MODEL__CROSS_ZONE__PENALTY_ENABLED=0.
    cross_zone_penalty_enabled: bool = True
    # shadow 병행 (L6 ②): primary는 페널티 OFF 유지, 페널티 ON 정산기를
    # shadow로 돌려 diff만 기록. PENALTY_ENABLED=1이면 무의미하므로 무시된다.
    cross_zone_shadow: bool = False
    # 카메라 계약 상수 — CRK-CAMERA replay_duration/trigger duration과 단일 소스
    # (trigger duration은 0.8s 로드셀 캐던스 대응으로 3.0 -> 4.0, CRK-CAMERA 7c8395f)
    cross_zone_replay_s: float = 4.0
    cross_zone_trigger_s: float = 4.0
    # IO-BOARD 감지 지연 마진 (ε): 폴링 0.8s(지배 항) + serial/SSE ~0.1s + 여유.
    # 구값 0.3은 0.099s 폴링 + EMA 꼬리 시절 산정. sign-flip relatch(최대 2.4s,
    # 존 무게 0 교차 시)는 의도적으로 미포함 — 과도한 창 확장 방지.
    cross_zone_epsilon_s: float = 1.0
    # soft 페널티 계수 (α) / 페널티 소스 최소 신뢰도 (θ) — Phase 1 계측으로 보정
    cross_zone_alpha: float = 0.5
    cross_zone_source_conf_min: float = 0.35

    @classmethod
    def from_env(cls) -> Settings:
        policy_raw = os.environ.get("MODEL__SESSION__ERROR_POLICY", "block_payment")
        return cls(
            close_timeout_s=_env_float("MODEL__CLOSE__BARRIER_TIMEOUT_S", 10.0),
            close_grace_s=_env_float("MODEL__CLOSE__GRACE_S", 3.0),
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
            top_confidence_threshold=_env_float(
                "MODEL__VISION__TOP_CONFIDENCE_THRESHOLD", 0.70
            ),
            side_confidence_threshold=_env_float(
                "MODEL__VISION__SIDE_CONFIDENCE_THRESHOLD", 0.70
            ),
            min_vote_ratio=_env_float("MODEL__VISION__MIN_VOTE_RATIO", 0.05),
            min_vote_count=_env_int("MODEL__VISION__MIN_VOTE_COUNT", 3),
            min_vote_share=_env_float("MODEL__VISION__MIN_VOTE_SHARE", 0.1),
            vote_conf_floor=_env_float("MODEL__VISION__CONF_FLOOR", 0.0),
            side_roi_max_center_x=_env_float("MODEL__VISION__SIDE_ROI_MAX_CENTER_X", 400.0),
            static_track_min_frames=_env_int(
                "MODEL__VISION__STATIC_TRACK_MIN_FRAMES", 24
            ),
            static_track_iou=_env_float("MODEL__VISION__STATIC_TRACK_IOU", 0.85),
            baseline_suppress_mode=_env_baseline_mode(
                "MODEL__VISION__BASELINE_SUPPRESS_MODE", "shadow"
            ),
            baseline_suppress_iou=_env_float(
                "MODEL__VISION__BASELINE_SUPPRESS_IOU", 0.5
            ),
            segment_retry_gap_grams=_env_float(
                "MODEL__WEIGHT__SEGMENT_RETRY_GAP_GRAMS", 5.0
            ),
            judgment_single_share=_env_float("MODEL__JUDGMENT__SINGLE_SHARE", 0.5),
            judgment_combo_share=_env_float("MODEL__JUDGMENT__COMBO_SHARE", 0.3),
            judgment_near_factor=_env_float("MODEL__JUDGMENT__NEAR_FACTOR", 2.0),
            judgment_refit_share=_env_float("MODEL__JUDGMENT__REFIT_SHARE", 0.1),
            judgment_count_unit_slack=_env_float(
                "MODEL__JUDGMENT__COUNT_UNIT_SLACK", 5.0
            ),
            judgment_conf_override=_env_float("MODEL__JUDGMENT__CONF_OVERRIDE", 0.9),
            judgment_conf_margin=_env_float("MODEL__JUDGMENT__CONF_MARGIN", 0.15),
            early_termination_enabled=_env_bool(
                "MODEL__VISION__EARLY_TERMINATION", True
            ),
            motion_gate_threshold=_env_opt_float("MODEL__VISION__MOTION_GATE_THRESHOLD"),
            motion_gate_keepalive=_env_opt_int("MODEL__VISION__MOTION_GATE_KEEPALIVE"),
            loadcell_stable_window=_env_int("MODEL__WEIGHT__STABLE_WINDOW", 3),
            loadcell_stability_threshold_grams=_env_float(
                "MODEL__WEIGHT__STABILITY_THRESHOLD_GRAMS", 2.5
            ),
            cross_zone_penalty_enabled=_env_bool(
                "MODEL__CROSS_ZONE__PENALTY_ENABLED", True
            ),
            cross_zone_shadow=_env_bool("MODEL__CROSS_ZONE__SHADOW", False),
            cross_zone_replay_s=_env_float("MODEL__CROSS_ZONE__REPLAY_S", 4.0),
            cross_zone_trigger_s=_env_float("MODEL__CROSS_ZONE__TRIGGER_S", 4.0),
            cross_zone_epsilon_s=_env_float("MODEL__CROSS_ZONE__EPSILON_S", 1.0),
            cross_zone_alpha=_env_float("MODEL__CROSS_ZONE__ALPHA", 0.5),
            cross_zone_source_conf_min=_env_float(
                "MODEL__CROSS_ZONE__SOURCE_CONF_MIN", 0.35
            ),
        )
