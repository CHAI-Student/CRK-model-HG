"""perception — vision 계층: 검출 인터페이스·필터 체인·변위 증거·투표 앙상블."""
from crk_model.perception.detector import Detection, Detector
from crk_model.perception.filters import DetectionFilterChain
from crk_model.perception.motion_evidence import MotionEvidence
from crk_model.perception.voting import VotingEnsemble

__all__ = [
    "Detection",
    "DetectionFilterChain",
    "Detector",
    "MotionEvidence",
    "VotingEnsemble",
]
