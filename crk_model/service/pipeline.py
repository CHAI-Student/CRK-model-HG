"""트리거 파이프라인 — ingest → frames → perception → judgment 연결
(다이어그램 3의 7단계 중 ②~⑥, 원본 trigger_service._process 대응).

경로 분기 (다이어그램 4):
- I2: 빈 allowlist → 추론 차단 (YOLO 호출 0, fail-closed)
- 저무게 스킵: |delta| < 프로파일 게이트 → vision 생략 (QA Q8)
- 로드셀 신뢰 불가 → vision_only 강제 (원본 _should_force_vision_only)
- I1: 처리 예외 → status="error" 이벤트로 전파 (무검출로 조용히 바꾸지 않음)
- L2: 조기 종료 시 추론만 중단 (I15 가드는 EarlyTerminator 내부)

트레이스 (I8): processed_frames 의미 유지 + gate_skipped_frames 신설 +
yolo_calls / early_terminated / reason_codes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import (
    JudgmentResult,
    JudgmentStatus,
    ProductCount,
    VisionCandidate,
)
from crk_model.frames.motion_gate import Frame, HandLatch, MotionGate
from crk_model.ingest.bocpd import BocpdAnalyzer
from crk_model.ingest.loadcell import (
    ChannelWeightEvent,
    LoadcellAnalyzer,
    LoadcellSample,
)
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.likelihood import WeightLikelihoodScorer
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.events import TriggerEvent
from crk_model.perception.detector import HAND_CLASS_ID, Detector
from crk_model.perception.early_termination import EarlyTerminator
from crk_model.perception.filters import DetectionFilterChain
from crk_model.perception.motion_evidence import MotionEvidence
from crk_model.perception.voting import VotingEnsemble
from crk_model.service.snapshot import ActiveProductStore, ProductSnapshot

CAMERAS = ("top", "side")


def _vision_top_not_billed(candidates, judgment) -> str | None:
    """관측성 (이슈 #15): 채택된 비전 1위 후보가 과금 목록에 없으면 사유 코드.

    65표/0.86 1위가 미매핑·게이트 탈락으로 무성 소멸하고 16표 후보가
    과금돼도 아카이브만으로는 알 수 없었다 — 전략과 무관하게 파이프라인이
    "판정이 vision 순위를 뒤집었다"는 사실 자체를 기록한다."""
    if not candidates or not judgment.products:
        return None
    top = max(candidates, key=lambda c: (c.vote_count, c.confidence))
    if any(pc.product.class_id == top.class_id for pc in judgment.products):
        return None
    return f"vision_top_not_billed:class{top.class_id}"


def _with_tubes(tube_summary: dict | None, evidence) -> dict | None:
    """tube_shadow에 튜브 구성 진단(tube_detail)을 동봉 — 의류 산탄의
    "한 궤적, 여러 클래스" 실측 근거. summary가 None(전 모드 off)이면 그대로."""
    if tube_summary is None or evidence is None:
        return tube_summary
    tubes = evidence.tube_detail()
    if tubes:
        tube_summary["tubes"] = tubes
    return tube_summary


@dataclass(frozen=True)
class TriggerRequest:
    zone: int
    frames: Mapping[str, Iterable[Frame]]  # 카메라별 프레임 스트림 (frames/ 산출, 1회 순회)
    loadcells: Sequence[LoadcellSample]
    ts: float
    seq: int | None = None  # D2: 카메라 시퀀스 (선택)
    # 진단 강화 (issue #6): 카메라별 원본 AVI 경로 — 오판정 시 즉시 재생 확인용.
    # model_service.handle_trigger가 payload["video_paths"]를 그대로 실어온다.
    video_paths: Mapping[str, str] = field(default_factory=dict)
    # 0711 교차존 오염: 에피소드 내 change 벽시계 앵커 (카메라 optional 필드).
    change_timestamps: Sequence[float] = ()


@dataclass
class TriggerTrace:
    processed_frames: dict[str, int] = field(default_factory=dict)
    gate_skipped_frames: dict[str, int] = field(default_factory=dict)  # I8 신설
    yolo_calls: int = 0
    early_terminated: bool = False
    reason_codes: list[str] = field(default_factory=list)
    # issue #6 진단(work item 3): vision_candidates=[]인데 yolo_calls는 높은
    # 케이스를 사후에 재구성하기 위한 클래스별/카메라별 요약
    # ({"classes": VotingEnsemble.debug_summary(), "filtered_out_by_camera": {...}}).
    vote_summary: dict = field(default_factory=dict)
    # BOCPD shadow (research §2): 판정에는 미사용 — 기존 분석기와의 delta
    # diff를 아카이브로 실측해 승격 여부를 결정한다 (특히 primary가
    # insufficient_*로 delta=0을 낼 때 BOCPD가 보는 값).
    loadcell_shadow: dict | None = None
    # 무게 우도 score shadow (docs/0722_weight_likelihood_design.md Phase 1):
    # 판정 미사용 — 이벤트(트레이)별 score 순위와 현행 판정의 diff 기록.
    # mismatch=true 세션을 아카이브에서 수집해 승격(Phase 2/3)을 결정한다.
    likelihood_shadow: list[dict] | None = None


@dataclass(frozen=True)
class TriggerOutcome:
    event: TriggerEvent
    trace: TriggerTrace
    # 세션 아카이브(issue #6) 진단용 — pipeline.process() 자체는 0.0으로 두고,
    # worker.drain()이 실측 처리시간으로 채운다(dataclasses.replace, frozen이라).
    processing_time_ms: float = 0.0


class TriggerPipeline:
    def __init__(
        self,
        detector: Detector,
        profiles: Mapping[int, SensorProfile],
        snapshots: ActiveProductStore,
        *,
        router: JudgmentRouter | None = None,
        filters: DetectionFilterChain | None = None,
        early_termination_enabled: bool = True,
        analyzer_factory=None,  # SensorProfile -> LoadcellAnalyzer (테스트/튜닝 주입점)
        default_profile: SensorProfile = REFRIGERATOR,
        # zone이 profiles dict에 없을 때 쓰는 폴백 프로파일. 기본은 기존
        # 동작(REFRIGERATOR)과 동일 — cabinet_type=freezer 기기에서는
        # ModelService가 FREEZER를 주입해 존 미지정 시에도 냉동 프로파일이
        # 기본이 되게 한다 (MODEL__MACHINE__CABINET_TYPE 이식).
        voting_params: Mapping | None = None,
        # VotingEnsemble 생성 인자 (MODEL__VISION__* env → Settings 경유 주입).
        # None이면 라이브러리 기본값 — 기존 테스트/직접 생성 하위호환.
        segment_retry_gap_grams: float = 5.0,
        # 이슈 #10: |delta − sum(segments)|가 이 값을 넘으면 접촉 하중 오염
        # 서명으로 보고, delta 타깃 판정 실패 시 세그먼트 합 타깃으로 1회
        # 재판정한다 (아래 _segment_target_retry). 실측: 오염 트리거는 8~18g,
        # 깨끗한 트리거는 0.
        motion_evidence_enabled: bool = False,
        # 모션 변위 증거 (perception/motion_evidence.py, issue #16 후속):
        # 변위 없는 카메라×클래스의 표를 combine에서 몰수. 라이브러리 기본
        # False(하위호환 — 기존 직접 생성 테스트 보존), 운영값은 Settings가
        # True로 주입 (MODEL__VISION__MOTION_EVIDENCE).
        motion_evidence_floor_px: float | None = None,
        # None = 프로파일 기본 (냉장 10px / 냉동 12px — 원본
        # MOTION_MIN_DISPLACEMENT_PX 동형, 1:1 center-crop 좌표계 전제).
        held_track_min_head: int = 5,
        # T2 held 트랙 판정의 head 임계 (MotionEvidence.held_min_head 주입,
        # MODEL__VISION__HELD_TRACK_MIN_HEAD). 강등 모드 자체는 voting_params
        # 의 held_demotion으로 들어간다 — 판정은 증거층, 몰수는 투표층 소관.
        track_max_gap: int = 0,
        # 갭 1 트랙 소멸 (MotionEvidence.track_max_gap 주입, MODEL__VISION__
        # TRACK_MAX_GAP): 공백 > N 추론프레임 트랙은 사망. 0 = 무소멸(현행).
        bocpd_shadow_enabled: bool = False,
        # BOCPD shadow 분석기 (research §2): 라이브러리 기본 False(하위호환),
        # 운영값은 Settings가 주입 (MODEL__LOADCELL__BOCPD_SHADOW).
        likelihood_shadow_enabled: bool = False,
        # 무게 우도 score shadow (research §1-2 승인분, Phase 1): 라이브러리
        # 기본 False(하위호환), 운영값은 Settings가 주입
        # (MODEL__JUDGMENT__LIKELIHOOD_SHADOW). 판정·정산 무변경 — trace 기록만.
        likelihood_params: Mapping | None = None,
        # WeightLikelihoodScorer 생성 인자 (k/sigma_db 등, MODEL__JUDGMENT__* env).
        tray_memory=None,
        # 세션 트레이 메모리 (ledger/tray_memory.py) — ModelService가 세션
        # 수명(OPEN 리셋)을 관리하며 주입. None이면 기록·prior 모두 비활성
        # (라이브러리 기본, 하위호환). Phase 1: likelihood shadow의
        # log_p_tray 항으로만 소비 — 판정·정산 무변경.
    ):
        self._detector = detector
        self._profiles = dict(profiles)
        self._snapshots = snapshots
        self._router = router or JudgmentRouter()
        self._filters = filters or DetectionFilterChain()
        self._et_enabled = early_termination_enabled
        self._analyzer_factory = analyzer_factory or LoadcellAnalyzer
        self._default_profile = default_profile
        self._voting_params = dict(voting_params) if voting_params else {}
        self._segment_retry_gap = segment_retry_gap_grams
        self._motion_evidence_enabled = motion_evidence_enabled
        self._motion_evidence_floor = motion_evidence_floor_px
        self._held_min_head = held_track_min_head
        self._track_max_gap = track_max_gap
        self._bocpd_shadow = bocpd_shadow_enabled
        self._likelihood: WeightLikelihoodScorer | None = (
            WeightLikelihoodScorer(**dict(likelihood_params or {}))
            if likelihood_shadow_enabled
            else None
        )
        self._tray_memory = tray_memory

    def process(self, session_id: str, req: TriggerRequest) -> TriggerOutcome:
        try:
            return self._process(session_id, req)
        except Exception as exc:
            # I1: 처리 실패는 무검출이 아니라 에러로 전파 (fail-closed)
            judgment = JudgmentResult(
                JudgmentStatus.ERROR, reason=f"processing_error:{type(exc).__name__}"
            )
            event = TriggerEvent(
                session_id,
                req.zone,
                req.ts,
                0.0,
                (),
                judgment,
                req.seq,
                status="error",
                video_paths=tuple(req.video_paths.items()),
                change_timestamps=tuple(req.change_timestamps),
            )
            return TriggerOutcome(event, TriggerTrace(reason_codes=["processing_error"]))

    def _process(self, session_id: str, req: TriggerRequest) -> TriggerOutcome:
        profile = self._profiles.get(req.zone, self._default_profile)
        snapshot = self._snapshots.snapshot()
        trace = TriggerTrace()
        if snapshot.source == "last_valid":
            trace.reason_codes.append("snapshot_source=last_valid")  # I2 폴백 기록

        if not snapshot.inference_allowed:
            # I2: 빈 allowlist → 추론 차단 (YOLO 호출 0)
            trace.reason_codes.append("empty_allowlist_fail_closed")
            judgment = JudgmentResult(
                JudgmentStatus.NO_DETECTION, reason="empty_allowlist_fail_closed"
            )
            return self._outcome(session_id, req, 0.0, (), judgment, trace)

        analyzer = self._analyzer_factory(profile)
        analysis = analyzer.analyze(req.loadcells)
        vision_only = not analysis.stabilized and analysis.reason in (
            "insufficient_samples",
            "insufficient_stable_regions",
        )  # 로드셀 신뢰 불가 → vision 강제
        if vision_only:
            # 관측성 (이슈 #14): vision_only의 원인(insufficient_samples vs
            # insufficient_stable_regions)이 아카이브에 남지 않으면 로드셀
            # 실패 유형을 사후 구분할 수 없다.
            trace.reason_codes.append(f"loadcell_{analysis.reason}")
        if analysis.reason == "needs_return_stabilization":
            # 재수집은 장치측 훅 (QA Q3 ① 순서 계약) — 구간화 보류 사실만 기록
            trace.reason_codes.append("return_stabilization_pending")
        delta = analysis.delta_weight
        if self._bocpd_shadow:
            # shadow는 판정 경로를 절대 깨지 않는다 — 실패는 기록만.
            # primary가 bocpd로 승격된 경우(MODEL__LOADCELL__ANALYZER=bocpd)
            # 자기 비교는 무의미하므로 plateau를 shadow로 돌려 대칭 diff를
            # 유지한다 (승격 후에도 회귀 방향의 mismatch를 관측 가능).
            try:
                if getattr(analyzer, "name", "plateau") == "bocpd":
                    sh_plateau = LoadcellAnalyzer(profile).analyze(req.loadcells)
                    trace.loadcell_shadow = {
                        "analyzer": "plateau",
                        "delta": round(sh_plateau.delta_weight, 2),
                        "reason": sh_plateau.reason,
                        "primary_delta": round(delta, 2),
                        "primary_reason": analysis.reason,
                        "mismatch": abs(sh_plateau.delta_weight - delta) > 5.0,
                    }
                else:
                    sh = BocpdAnalyzer().analyze(req.loadcells)
                    trace.loadcell_shadow = {
                        "analyzer": "bocpd",
                        "delta": round(sh.delta_weight, 2),
                        "delta_std": round(sh.delta_std, 2),
                        "channels": [
                            {
                                "channel": c.channel,
                                "delta": round(c.delta, 2),
                                "levels": [round(s.level, 1) for s in c.segments],
                            }
                            for c in sh.channels
                        ],
                        "primary_delta": round(delta, 2),
                        "primary_reason": analysis.reason,
                        "mismatch": abs(sh.delta_weight - delta) > 5.0,
                    }
            except Exception as exc:  # noqa: BLE001 — shadow 격리
                trace.loadcell_shadow = {"analyzer": "bocpd", "error": type(exc).__name__}

        if not vision_only and abs(delta) < profile.min_weight_change_grams:
            # 저무게 스킵: vision 전체 생략 = YOLO 호출 0 (QA Q8)
            trace.reason_codes.append("low_weight_skip")
            judgment = JudgmentResult(
                JudgmentStatus.NO_DETECTION,
                reason="below_min_weight_change",
                strategy="low_weight_skip",
            )
            return self._outcome(session_id, req, delta, analysis.segments, judgment, trace)

        candidates = self._run_vision(req, profile, snapshot, delta, trace)
        ctx = JudgmentContext(
            zone=req.zone,
            profile=profile,
            delta_weight=delta,
            segments=analysis.segments,
            vision_candidates=candidates,
            active_products=snapshot.products,
            vision_only=vision_only,
        )
        if len(analysis.events) >= 2:
            judgment = self._judge_tray_events(ctx, analysis, trace, session_id)
        else:
            # 단일 이벤트라도 게이트를 넘은 채널이 정확히 하나면 트레이가
            # 특정된다 — 트레이 메모리의 키/prior 해상도로 쓴다.
            channel = (
                analysis.events[0].channel if len(analysis.events) == 1 else None
            )
            judgment = self._router.judge(ctx)
            judgment = self._segment_target_retry(ctx, judgment, analysis, trace)
            self._likelihood_shadow(
                ctx, judgment, trace, channel=channel, session_id=session_id
            )
            self._record_tray_evidence(ctx, judgment, channel, session_id)
        top_code = _vision_top_not_billed(candidates, judgment)
        if top_code:
            trace.reason_codes.append(top_code)
        return self._outcome(
            session_id, req, delta, analysis.segments, judgment, trace, candidates
        )

    def _judge_tray_events(
        self,
        ctx: JudgmentContext,
        analysis,
        trace: TriggerTrace,
        session_id: str | None = None,
    ) -> JudgmentResult:
        """2단계: 트레이별 동시 이벤트를 이벤트당 1회씩 개별 판정 후 병합.

        트레이 분리 구조에서 각 ChannelWeightEvent.delta는 단품(또는 동일
        상품 n개) 무게 그 자체이므로, 존 합산 delta(-324 같은 덩어리)를
        조합 탐색하는 대신 이벤트별 단품 매칭으로 분해한다 — issue #6이
        금지한 자유 조합 탐색과 달리 분해 근거가 물리(트레이)라 안전하다.
        비전 후보 풀은 공유(영상 1개, YOLO 재실행 없음) — 각 이벤트가 자기
        무게로 풀에서 자기 상품을 고른다 (_segment_target_retry와 같은
        zero-GPU 재판정 패턴). 1차 판정 후 형제 이벤트가 소진한 정체성을
        빼고 미확정 이벤트를 1회 재판정한다 (_pool_exhaustion_retry, 이슈 #16).
        """
        trace.reason_codes.append(f"multi_tray_events:{len(analysis.events)}")
        results: list[tuple[ChannelWeightEvent, JudgmentResult]] = []
        for ev in analysis.events:
            ectx = replace(ctx, delta_weight=ev.delta_grams, segments=ev.segments)
            j = self._router.judge(ectx)
            j = self._segment_target_retry(ectx, j, ev, trace)
            results.append((ev, j))
        results = self._pool_exhaustion_retry(ctx, results, trace)
        for ev, j in results:
            self._likelihood_shadow(
                replace(ctx, delta_weight=ev.delta_grams, segments=ev.segments),
                j,
                trace,
                channel=ev.channel,
                session_id=session_id,
            )
        # 등록은 shadow 계산이 전부 끝난 뒤 — 같은 트리거의 형제 이벤트가
        # 서로의 prior에 영향을 주지 않는다 (트리거 시작 시점의 메모리로만
        # shadow 산출, 등록은 트리거 단위 원자적).
        for ev, j in results:
            self._record_tray_evidence(
                replace(ctx, delta_weight=ev.delta_grams), j, ev.channel, session_id
            )

        complete = [
            (ev, j) for ev, j in results
            if j.status is JudgmentStatus.COMPLETE and j.products
        ]
        # 설계 4 (issue #16, docs/0722_issue16_arbitration_design.md): 정산기는
        # 에러가 아닌 모든 판정의 products를 집계하므로(단일 트리거의 near-gate
        # PARTIAL은 과금된다 — #15 정답 경로), 병합만 COMPLETE 한정이면 두
        # 취출이 한 영상에 담겼다는 이유로 덜 과금된다. 고유 정체성 PARTIAL은
        # 과금에 포함한다. 가드 2중: ① 형제 COMPLETE와 정체성이 겹치면 표-그림자
        # 오염 산물이라 제외, ② PARTIAL끼리 겹쳐도 대칭 오염 가능성이라 전부
        # 제외 (과청구가 미청구보다 나쁘다, I13/D9).
        complete_ids = {
            pc.product.class_id for _, j in complete for pc in j.products
        }
        partials = [
            (ev, j) for ev, j in results
            if j.status is JudgmentStatus.PARTIAL and j.products
        ]
        partial_billable = []
        for ev, j in partials:
            ids = {pc.product.class_id for pc in j.products}
            if ids & complete_ids:
                continue  # 가드 ①
            other_ids = {
                pc.product.class_id
                for oev, oj in partials
                if oj is not j
                for pc in oj.products
            }
            if ids & other_ids:
                continue  # 가드 ②
            partial_billable.append((ev, j))
            trace.reason_codes.append(f"partial_billed:ch{ev.channel}")
        billable = complete + partial_billable

        merged: dict[str, ProductCount] = {}
        for _, j in billable:
            for pc in j.products:
                prev = merged.get(pc.product.product_id)
                merged[pc.product.product_id] = ProductCount(
                    pc.product, (prev.count if prev else 0) + pc.count
                )
        reasons = "+".join(
            f"ch{ev.channel}:{j.reason or j.status.value}" for ev, j in results
        )
        if partial_billable:
            reasons += ";partial_billed:" + ",".join(
                f"ch{ev.channel}" for ev, _ in partial_billable
            )
        strategies = ",".join(j.strategy or "-" for _, j in results)
        if len(complete) == len(results):
            status = JudgmentStatus.COMPLETE
            confidence = min(j.confidence for _, j in results)
        elif billable:
            # 일부 트레이만 확정/과금 — 미확정 트레이는 reason에 남긴다
            # (악화 금지, I3 태도)
            status = JudgmentStatus.PARTIAL
            confidence = min(j.confidence for _, j in billable)
        else:
            status = JudgmentStatus.NO_DETECTION
            confidence = 0.0
        return JudgmentResult(
            status,
            tuple(merged.values()),
            confidence,
            reason=f"multi_tray[{reasons}]",
            strategy=f"multi_tray[{strategies}]",
        )

    def _likelihood_shadow(
        self,
        ctx: JudgmentContext,
        judgment: JudgmentResult,
        trace: TriggerTrace,
        channel: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """무게 우도 score shadow (Phase 1) — 판정 경로를 절대 깨지 않는다.

        σ_d는 BOCPD shadow의 delta_std가 있으면 그것을 쓴다 (설계 §2 —
        BOCPD 승격 시 자연 연결). 실패·비적용은 조용히 건너뛰거나 error만
        기록한다 (BOCPD shadow와 동일 격리 패턴)."""
        if self._likelihood is None:
            return
        try:
            sigma_d = None
            sh = trace.loadcell_shadow
            if sh and isinstance(sh.get("delta_std"), (int, float)):
                sigma_d = float(sh["delta_std"])
            tray_prior = None
            if self._tray_memory is not None and ctx.vision_candidates:
                tray_prior = self._tray_memory.priors_for(
                    ctx.zone,
                    channel,
                    [c.class_id for c in ctx.vision_candidates],
                    session_id=session_id,
                )
            entry = self._likelihood.shadow(
                ctx, judgment, sigma_d=sigma_d, tray_prior=tray_prior
            )
        except Exception as exc:  # noqa: BLE001 — shadow 격리
            entry = {"scorer": "weight_likelihood", "error": type(exc).__name__}
        if entry is None:
            return
        if channel is not None:
            entry["channel"] = channel
        if trace.likelihood_shadow is None:
            trace.likelihood_shadow = []
        trace.likelihood_shadow.append(entry)
        if entry.get("mismatch"):
            suffix = f":ch{channel}" if channel is not None else ""
            trace.reason_codes.append(f"likelihood_shadow_mismatch{suffix}")

    def _record_tray_evidence(
        self,
        ctx: JudgmentContext,
        judgment: JudgmentResult,
        channel: int | None,
        session_id: str | None = None,
    ) -> None:
        """세션 트레이 메모리 등록 (ledger/tray_memory.py 등록 게이트).

        오판 전파 차단: COMPLETE + 무게 뒷받침(vision_only 아님 — I6 통과
        COMPLETE는 delta 전량 설명 보장)만 등록한다. PARTIAL·near_gate와
        무게가 정체성을 고른 예외 경로(unique_refit)는 등록하지 않는다.

        vision 1위 일치 조건은 Phase 1에서 제외 (5차 배치 ses-10): 오염이
        심한 존일수록 top 불일치가 흔한데 그게 정확히 prior가 필요한 상황
        — top 일치 게이트는 닭-달걀이다. shadow 전용이라 완화가 안전하고,
        승격 전 라벨 실측으로 재평가한다 (tray_memory.py docstring)."""
        if self._tray_memory is None or ctx.vision_only:
            return
        if judgment.status is not JudgmentStatus.COMPLETE or not judgment.products:
            return
        if "refit" in (judgment.reason or ""):
            return
        for pc in judgment.products:
            if pc.product.class_id > 0:
                self._tray_memory.record(
                    ctx.zone,
                    channel,
                    pc.product.class_id,
                    pc.count,
                    session_id=session_id,
                )

    def _pool_exhaustion_retry(
        self,
        ctx: JudgmentContext,
        results: list[tuple[ChannelWeightEvent, JudgmentResult]],
        trace: TriggerTrace,
    ) -> list[tuple[ChannelWeightEvent, JudgmentResult]]:
        """2-pass 소진 재판정 (이슈 #16): 형제 트레이가 COMPLETE로 소진한
        정체성을 미확정 이벤트의 후보 풀에서 빼고 1회 재판정한다.

        동시 다중 트레이 취출은 영상(투표 풀)이 하나라 트레이별 상품이 표를
        나눠 갖는다 — ch0 상품이 득표 1위면 ch1 판정에서 single_share 게이트가
        진짜 상품(2위권)을 배제하고, near-gate가 1위 정체성을 PARTIAL로 보존한
        채 조기 반환해 ch1 상품이 무성 소멸한다 (실사고 #16: 155g 베이글
        62표가 −135g 이벤트를 near-gate로 가로채 135g 상품 12표/conf 1.0이
        미과금 — CLOSE 냉동 재solve도 다품종 금지라 복구 불가).

        무게로 정체성을 고르는 게 아니다(I-V 유지) — 이미 설명된 정체성을
        제거하고 남은 득표 순위에 다시 맡길 뿐. 채택은 COMPLETE로 개선될
        때만 (악화 금지, I3 태도). ERROR 이벤트는 재판정하지 않는다 (I1).

        한계 (기록): 같은 상품이 두 트레이에 있고 한쪽 delta가 오염된 경우,
        제거 후 남은 후보의 무게 우연 적합이 오과금할 수 있다. 그 경우 기존
        동작(near-gate PARTIAL 미과금)은 매출 누락이었고, 재판정 흔적이
        reason 접미사(+pool_exhaustion)와 trace로 아카이브에 남아 사후 식별
        가능하다 — 미과금 확정보다 관측 가능한 과금 시도를 택한다."""
        consumed = {
            pc.product.class_id
            for _, j in results
            if j.status is JudgmentStatus.COMPLETE
            for pc in j.products
        }
        if not consumed:
            return results
        out: list[tuple[ChannelWeightEvent, JudgmentResult]] = []
        for ev, j in results:
            if j.status not in (JudgmentStatus.PARTIAL, JudgmentStatus.NO_DETECTION):
                out.append((ev, j))
                continue
            remaining = tuple(
                c for c in ctx.vision_candidates if c.class_id not in consumed
            )
            if not remaining or len(remaining) == len(ctx.vision_candidates):
                out.append((ev, j))  # 풀 변화 없음(소진 정체성 미포함) 또는 전멸
                continue
            trace.reason_codes.append(f"multi_tray_pool_exhaustion_retry:ch{ev.channel}")
            rectx = replace(
                ctx,
                delta_weight=ev.delta_grams,
                segments=ev.segments,
                vision_candidates=remaining,
            )
            rj = self._router.judge(rectx)
            rj = self._segment_target_retry(rectx, rj, ev, trace)
            if rj.status is JudgmentStatus.COMPLETE and rj.products:
                rj = replace(
                    rj, reason=(rj.reason or rj.status.value) + "+pool_exhaustion"
                )
                out.append((ev, rj))
            else:
                out.append((ev, j))  # 악화 금지 — 원 판정 유지
        return out

    def _segment_target_retry(
        self, ctx: JudgmentContext, judgment: JudgmentResult, analysis, trace: TriggerTrace
    ) -> JudgmentResult:
        """오염 delta 이중 타깃 재시도 (이슈 #10).

        취출 시 손이 선반을 누르는 접촉 하중(press transient)이 delta 또는
        세그먼트 한쪽을 왜곡한다 — 어느 쪽이 진실에 가까운지는 케이스마다
        다르므로(실측: delta가 맞는 세션과 세그먼트가 맞는 세션이 공존)
        delta 타깃을 우선하되, **실패했고 오염 서명(|delta − sum(segments)|
        > gap)이 있을 때만** 세그먼트 합을 타깃으로 라우터를 1회 재실행한다.

        비용: YOLO 재실행 없음 — 이미 집계된 vision_candidates로 순수 CPU
        재판정(수 ms). 깨끗한 트리거(delta == seg합)는 발동 자체가 없다.
        재시도도 COMPLETE에 못 미치면 원 판정 유지 (악화 금지, I3 태도).
        """
        if judgment.status is JudgmentStatus.COMPLETE or ctx.vision_only:
            return judgment
        if ctx.delta_weight >= 0 or not analysis.segments:
            return judgment
        seg_sum = sum(s.delta_grams for s in analysis.segments)
        if seg_sum >= 0 or abs(ctx.delta_weight - seg_sum) <= self._segment_retry_gap:
            return judgment
        trace.reason_codes.append("segment_target_retry")  # I8: 시도 자체를 기록
        retry = self._router.judge(replace(ctx, delta_weight=seg_sum))
        if retry.status is not JudgmentStatus.COMPLETE or not retry.products:
            return judgment
        return replace(retry, reason=retry.reason + "+segment_target_retry")

    def _run_vision(
        self,
        req: TriggerRequest,
        profile: SensorProfile,
        snapshot: ProductSnapshot,
        delta: float,
        trace: TriggerTrace,
    ) -> tuple[VisionCandidate, ...]:
        voting = VotingEnsemble(**self._voting_params)
        evidence = None
        if self._motion_evidence_enabled:
            floor = (
                self._motion_evidence_floor
                if self._motion_evidence_floor is not None
                else profile.motion_evidence_floor_px
            )
            evidence = MotionEvidence(
                floor_px=floor,
                held_min_head=self._held_min_head,
                track_max_gap=self._track_max_gap,
            )
            voting.attach_motion_evidence(evidence)
        terminator = EarlyTerminator(profile, enabled=self._et_enabled)
        stopped = False
        filtered_out: dict[str, int] = {}  # 진단(work item 3): 카메라별 필터 제거 개수
        # P0-2 (원본 _inference_allowed_class_ids 동형): 판매중 상품의 매핑된
        # class만 추론 허용 — 미매핑 센티널(-1)은 제외. hand는 top에만 포함
        # (원본은 side에서 hand를 추론하지 않는다 — hand-path 추적은 top 소관).
        # 매핑된 상품이 0개면 빈 목록 = fail-closed (검출 0, 어댑터 계약).
        product_ids = sorted({p.class_id for p in snapshot.products if p.class_id >= 0})
        if not product_ids:
            trace.reason_codes.append("no_mapped_class_ids")
        allowed_by_camera: dict[str, tuple[int, ...]] = {
            "top": tuple(dict.fromkeys((*product_ids, HAND_CLASS_ID))),
            "side": tuple(product_ids),
        }
        # 트리거 단위 상태 초기화: 단계별 제거 카운터(issue #6 2차) + 손 궤적·
        # 정지 트랙(이슈 #10 — 이전 영상의 좌표가 다음 영상 필터 기준이 되던 결함)
        self._filters.reset_trigger_state()
        # top ROI 방향 게이트 (P1-5): delta가 0이면 top ROI 미적용 (원본 동형)
        self._filters.set_trigger_delta(delta)
        for camera in CAMERAS:
            frames = req.frames.get(camera)
            if frames is None:
                continue  # 빈 스트림(list)/미제공 모두 아래 for가 0회 순회
            latch = HandLatch()  # 카메라별 래치 (hand-path는 카메라별, L3 계약과 동형)
            gate = MotionGate(profile, latch)
            frame_iter = iter(frames)
            camera_filtered_out = 0
            pos = -1  # held-object A-1 계측: 게이트 스킵 포함 디코드 위치
            try:
                for frame in frame_iter:
                    pos += 1
                    if stopped:
                        break  # L2: 추론만 중단 (프레임 공급은 이미 완료 상태)
                    # FrameBundle이면 게이트는 다운스케일 뷰, 검출기는 풀 프레임
                    decision = gate.evaluate(getattr(frame, "gate_view", frame))
                    if not decision.infer:
                        continue
                    raw = list(
                        self._detector.detect(
                            getattr(frame, "full", frame),
                            allowed_class_ids=allowed_by_camera.get(
                                camera, tuple(product_ids)
                            ),
                        )
                    )
                    detections = self._filters.apply(camera, raw)
                    camera_filtered_out += len(raw) - len(detections)
                    trace.yolo_calls += 1
                    tids = (
                        # pos 전달 = T1 트랙별 위치 계측 (motion_evidence.py)
                        evidence.observe(camera, detections, pos=pos)
                        if evidence is not None
                        else None
                    )
                    voting.add_frame(camera, detections, track_ids=tids, pos=pos)
                    latch.update_after_inference(any(d.is_hand for d in detections))
                    if terminator.should_stop(
                        delta_weight=delta,
                        candidates=voting.combine(),
                        active_products=snapshot.products,
                        frames_since_hand_exit=latch.frames_since_exit,
                    ):
                        stopped = True
            finally:
                # 조기 종료로 스트림을 버릴 때도 cv2/subprocess 리소스가 즉시
                # 해제되도록 제너레이터를 명시적으로 닫는다 (list 등 close 없는
                # 이터레이터는 getattr로 안전 무시).
                closer = getattr(frame_iter, "close", None)
                if closer is not None:
                    closer()
            trace.processed_frames[camera] = gate.processed_frames
            trace.gate_skipped_frames[camera] = gate.gate_skipped_frames
            filtered_out[camera] = camera_filtered_out
        trace.early_terminated = stopped
        if stopped:
            trace.reason_codes.append("early_terminated")
        trace.vote_summary = {
            "classes": voting.debug_summary(),
            "filtered_out_by_camera": filtered_out,
            # issue #6 2차 진단 확장: 필터 단계별(side_roi/hand_path) 제거 수와
            # 투표 진입 컷(entry_conf) 탈락 수 — "후보 0"이 어디서 죽었는지
            # (모델 미검출/필터/진입 컷/결합 임계) 세션 아카이브에서 즉시 구분.
            "filter_drops_by_stage": self._filters.drop_stats,
            # baseline shadow 검증 (이슈 #14 후속): 억제(예정) 대상의 클래스
            # 구성 — 진짜 상품 class가 여기 나타나면 active 승격 보류 신호.
            "baseline_drops_by_class": {
                cam: dict(by_cls)
                for cam, by_cls in self._filters.baseline_drops_by_class.items()
            },
            "entry_dropped_by_camera": dict(voting.entry_dropped),
            # 변위 증거 진단 (issue #16 후속): 카메라×클래스별 통과/최대경로/
            # 임계 — rejected_by: "no_motion"의 근거를 아카이브에서 재구성.
            "motion_evidence": evidence.summary() if evidence is not None else None,
            # T2 held 강등 관측 (shadow/active 공통): 카메라×클래스별
            # [held 표, 전체 표] — 승격 게이트는 analyze-sessions가 라벨과
            # 대조한다 (정답 클래스 held 플래그 = 승격 보류 신호).
            "held_shadow": voting.held_summary(),
            # 트랙릿 갭 4종 shadow (0723 문서 §2 잔여): 클래스별 현행/가상
            # 유효표 병기 + 튜브 구성(tubes) — analyze-sessions tube_eval이
            # 라벨과 대조해 갭별 승격/폐기를 판정한다.
            "tube_shadow": _with_tubes(voting.tube_summary(), evidence),
        }
        return voting.combine()

    @staticmethod
    def _outcome(
        session_id, req, delta, segments, judgment, trace, candidates=()
    ) -> TriggerOutcome:
        event = TriggerEvent(
            session_id,
            req.zone,
            req.ts,
            delta,
            tuple(segments),
            judgment,
            req.seq,
            vision_candidates=tuple(candidates),
            video_paths=tuple(req.video_paths.items()),
            change_timestamps=tuple(req.change_timestamps),
        )
        return TriggerOutcome(event, trace)
