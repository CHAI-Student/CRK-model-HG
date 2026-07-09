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
from crk_model.core.types import JudgmentResult, JudgmentStatus, VisionCandidate
from crk_model.frames.motion_gate import Frame, HandLatch, MotionGate
from crk_model.ingest.loadcell import LoadcellAnalyzer, LoadcellSample
from crk_model.judgment.interfaces import JudgmentContext
from crk_model.judgment.router import JudgmentRouter
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


@dataclass
class TriggerTrace:
    processed_frames: dict[str, int] = field(default_factory=dict)
    gate_skipped_frames: dict[str, int] = field(default_factory=dict)  # I8 신설
    yolo_calls: int = 0
    early_terminated: bool = False
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TriggerOutcome:
    event: TriggerEvent
    trace: TriggerTrace


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
    ):
        self._detector = detector
        self._profiles = dict(profiles)
        self._snapshots = snapshots
        self._router = router or JudgmentRouter()
        self._filters = filters or DetectionFilterChain()
        self._et_enabled = early_termination_enabled
        self._analyzer_factory = analyzer_factory or LoadcellAnalyzer

    def process(self, session_id: str, req: TriggerRequest) -> TriggerOutcome:
        try:
            return self._process(session_id, req)
        except Exception as exc:
            # I1: 처리 실패는 무검출이 아니라 에러로 전파 (fail-closed)
            judgment = JudgmentResult(
                JudgmentStatus.ERROR, reason=f"processing_error:{type(exc).__name__}"
            )
            event = TriggerEvent(
                session_id, req.zone, req.ts, 0.0, (), judgment, req.seq, status="error"
            )
            return TriggerOutcome(event, TriggerTrace(reason_codes=["processing_error"]))

    def _process(self, session_id: str, req: TriggerRequest) -> TriggerOutcome:
        profile = self._profiles.get(req.zone, REFRIGERATOR)
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

        analysis = self._analyzer_factory(profile).analyze(req.loadcells)
        vision_only = not analysis.stabilized and analysis.reason in (
            "insufficient_samples",
            "insufficient_stable_regions",
        )  # 로드셀 신뢰 불가 → vision 강제
        if analysis.reason == "needs_return_stabilization":
            # 재수집은 장치측 훅 (QA Q3 ① 순서 계약) — 구간화 보류 사실만 기록
            trace.reason_codes.append("return_stabilization_pending")
        delta = analysis.delta_weight

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
        judgment = self._router.judge(ctx)
        return self._outcome(session_id, req, delta, analysis.segments, judgment, trace)

    def _run_vision(
        self,
        req: TriggerRequest,
        profile: SensorProfile,
        snapshot: ProductSnapshot,
        delta: float,
        trace: TriggerTrace,
    ) -> tuple[VisionCandidate, ...]:
        voting = VotingEnsemble()
        terminator = EarlyTerminator(profile, enabled=self._et_enabled)
        stopped = False
        for camera in CAMERAS:
            frames = req.frames.get(camera)
            if frames is None:
                continue  # 빈 스트림(list)/미제공 모두 아래 for가 0회 순회
            latch = HandLatch()  # 카메라별 래치 (hand-path는 카메라별, L3 계약과 동형)
            gate = MotionGate(profile, latch)
            frame_iter = iter(frames)
            try:
                for frame in frame_iter:
                    if stopped:
                        break  # L2: 추론만 중단 (프레임 공급은 이미 완료 상태)
                    # FrameBundle이면 게이트는 다운스케일 뷰, 검출기는 풀 프레임
                    decision = gate.evaluate(getattr(frame, "gate_view", frame))
                    if not decision.infer:
                        continue
                    detections = self._filters.apply(
                        camera, list(self._detector.detect(getattr(frame, "full", frame)))
                    )
                    trace.yolo_calls += 1
                    voting.add_frame(camera, detections)
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
        trace.early_terminated = stopped
        if stopped:
            trace.reason_codes.append("early_terminated")
        return voting.combine()

    @staticmethod
    def _outcome(session_id, req, delta, segments, judgment, trace) -> TriggerOutcome:
        event = TriggerEvent(
            session_id, req.zone, req.ts, delta, tuple(segments), judgment, req.seq
        )
        return TriggerOutcome(event, trace)
