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
from dataclasses import dataclass, field

from crk_model.core.profiles import REFRIGERATOR, SensorProfile
from crk_model.core.types import (
    CellOutcome,
    JudgmentResult,
    JudgmentStatus,
    VisionCandidate,
)
from crk_model.frames.motion_gate import Frame, HandLatch, MotionGate
from crk_model.ingest.loadcell import LoadcellAnalyzer, LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
from crk_model.ledger.cells import CellBeliefStore
from crk_model.ledger.events import TriggerEvent
from crk_model.perception.detector import Detector
from crk_model.perception.early_termination import EarlyTerminator
from crk_model.perception.filters import DetectionFilterChain
from crk_model.perception.voting import VotingEnsemble
from crk_model.service.snapshot import ActiveProductStore, ProductSnapshot

CAMERAS = ("top", "side")


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
        beliefs: CellBeliefStore | None = None,
        # 셀 정체성 신념 (v2) — 미주입 시 메모리 전용 신규 저장소 (독립 사용 하위호환).
        # 운영은 ModelService가 Settings.cells_state_path 기반 저장소를 주입한다.
        default_profile: SensorProfile = REFRIGERATOR,
        # zone이 profiles dict에 없을 때 쓰는 폴백 프로파일. 기본은 기존
        # 동작(REFRIGERATOR)과 동일 — cabinet_type=freezer 기기에서는
        # ModelService가 FREEZER를 주입해 존 미지정 시에도 냉동 프로파일이
        # 기본이 되게 한다 (MODEL__MACHINE__CABINET_TYPE 이식).
        voting_params: Mapping | None = None,
        # VotingEnsemble 생성 인자 (MODEL__VISION__* env → Settings 경유 주입).
        # None이면 라이브러리 기본값 — 기존 테스트/직접 생성 하위호환.
    ):
        self._detector = detector
        self._profiles = dict(profiles)
        self._snapshots = snapshots
        self._router = router or JudgmentRouter()
        self._filters = filters or DetectionFilterChain()
        self._et_enabled = early_termination_enabled
        self._analyzer_factory = analyzer_factory or LoadcellAnalyzer
        self._beliefs = beliefs or CellBeliefStore()
        self._default_profile = default_profile
        self._voting_params = dict(voting_params) if voting_params else {}

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

        # 설계 v2: 채널(=셀)별 독립 분석 — 합산 금지 (README "추론 설계 v2")
        analyses = self._analyzer_factory(profile).analyze_cells(req.loadcells)
        cells = tuple(
            CellOutcome(
                channel=ch,
                delta_weight=a.delta_weight,
                segments=a.segments,
                stabilized=a.stabilized,
                reason=a.reason,
            )
            for ch, a in enumerate(analyses)
        )
        delta = sum(a.delta_weight for a in analyses)  # 존 총량 (로그/이벤트 표기용)
        segments = tuple(s for a in analyses for s in a.segments)
        # 로드셀 신뢰 불가 (전 셀 무신뢰) → vision 강제 (v1 의미론 유지)
        vision_only = not analyses or all(
            not a.stabilized
            and a.reason in ("insufficient_samples", "insufficient_stable_regions")
            for a in analyses
        )
        if any(a.reason == "needs_return_stabilization" for a in analyses):
            # 재수집은 장치측 훅 (순서 계약) — 구간화 보류 사실만 기록
            trace.reason_codes.append("return_stabilization_pending")

        cell_active = any(
            c.stabilized and abs(c.delta_weight) >= profile.min_weight_change_grams
            for c in cells
        )
        cell_unsettled = any(
            not c.stabilized and abs(c.delta_weight) > 0 for c in cells
        )
        if not vision_only and not cell_active and not cell_unsettled:
            # 저무게 스킵: 전 셀 게이트 미달 → vision 생략 = YOLO 호출 0
            trace.reason_codes.append("low_weight_skip")
            judgment = JudgmentResult(
                JudgmentStatus.NO_DETECTION,
                reason="below_min_weight_change",
                strategy="no_signal",
            )
            return self._outcome(
                session_id, req, delta, segments, judgment, trace, cells=cells
            )

        candidates = self._run_vision(req, profile, snapshot, cells, trace)
        identities = self._beliefs.identities_for_zone(
            req.zone, [c.channel for c in cells]
        )
        ctx = JudgmentContext(
            zone=req.zone,
            profile=profile,
            cells=cells,
            vision_candidates=candidates,
            active_products=snapshot.products,
            identities=identities,
            vision_only=vision_only,
        )
        decision = self._router.judge(ctx)
        self._observe_beliefs(req.zone, decision)
        return self._outcome(
            session_id,
            req,
            delta,
            segments,
            decision.result,
            trace,
            candidates,
            cells=decision.cells,
        )

    def _observe_beliefs(self, zone: int, decision) -> None:
        """판정의 독립 증거만 신념에 반영한다 (README "셀 정체성 추정").

        - 미지 셀의 V∩W/무게 단독 채택(vision_weight_match·weight_unique·
          count_pending)은 새 증거 → observe (제거=강, 반품=약).
        - known_identity 판정은 신념 자신을 근거로 하므로 반영하지 않는다
          (자기 강화 루프 방지).
        - contradiction(알려진 셀에서 비전+무게가 함께 다른 상품 지목)은 강한
          모순 증거 — 반복되면 CellBeliefStore가 강등한다 (재배치 자기 교정).
        """
        for c in decision.cells:
            if not c.product_id or c.reason.startswith("known_"):
                continue
            self._beliefs.observe(
                zone, c.channel, c.product_id, strong=c.delta_weight < 0
            )
        for channel, rival in decision.contradictions:
            self._beliefs.observe(zone, channel, rival, strong=True)

    def _run_vision(
        self,
        req: TriggerRequest,
        profile: SensorProfile,
        snapshot: ProductSnapshot,
        cells: tuple[CellOutcome, ...],
        trace: TriggerTrace,
    ) -> tuple[VisionCandidate, ...]:
        voting = VotingEnsemble(**self._voting_params)
        terminator = EarlyTerminator(profile, enabled=self._et_enabled)
        stopped = False
        filtered_out: dict[str, int] = {}  # 진단(work item 3): 카메라별 필터 제거 개수
        self._filters.reset_drop_stats()  # 트리거 단위 단계별 제거 카운터 (issue #6 2차)
        for camera in CAMERAS:
            frames = req.frames.get(camera)
            if frames is None:
                continue  # 빈 스트림(list)/미제공 모두 아래 for가 0회 순회
            latch = HandLatch()  # 카메라별 래치 (hand-path는 카메라별, L3 계약과 동형)
            gate = MotionGate(profile, latch)
            frame_iter = iter(frames)
            camera_filtered_out = 0
            try:
                for frame in frame_iter:
                    if stopped:
                        break  # L2: 추론만 중단 (프레임 공급은 이미 완료 상태)
                    # FrameBundle이면 게이트는 다운스케일 뷰, 검출기는 풀 프레임
                    decision = gate.evaluate(getattr(frame, "gate_view", frame))
                    if not decision.infer:
                        continue
                    raw = list(self._detector.detect(getattr(frame, "full", frame)))
                    detections = self._filters.apply(camera, raw)
                    camera_filtered_out += len(raw) - len(detections)
                    trace.yolo_calls += 1
                    voting.add_frame(camera, detections)
                    latch.update_after_inference(any(d.is_hand for d in detections))
                    if terminator.should_stop(
                        cells=cells,
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
            "entry_dropped_by_camera": dict(voting.entry_dropped),
        }
        return voting.combine()

    @staticmethod
    def _outcome(
        session_id, req, delta, segments, judgment, trace, candidates=(), cells=()
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
            cells=tuple(cells),
        )
        return TriggerOutcome(event, trace)
