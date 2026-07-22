"""perception — vision 계층: 검출 인터페이스·필터 체인·투표 앙상블·조기 종료(D7)."""
from crk_model.perception.detector import Detection, Detector
from crk_model.perception.early_termination import EarlyTerminationConfig, EarlyTerminator
from crk_model.perception.filters import DetectionFilterChain
from crk_model.perception.motion_evidence import MotionEvidence
from crk_model.perception.voting import VotingEnsemble

__all__ = [
    "Detection",
    "DetectionFilterChain",
    "Detector",
    "EarlyTerminationConfig",
    "EarlyTerminator",
    "MotionEvidence",
    "VotingEnsemble",
]
