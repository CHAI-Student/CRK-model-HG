"""gateway — 문 세션 상태기계(D1): OPEN/CLOSE 수명주기·결제 페이로드(I10)."""
from crk_model.core.policy import ErrorSessionPolicy
from crk_model.gateway.state_machine import (
    DoorState,
    GatewayResponse,
    MultiZoneGateway,
    build_payment_payload,
)

__all__ = [
    "DoorState",
    "ErrorSessionPolicy",
    "GatewayResponse",
    "MultiZoneGateway",
    "build_payment_payload",
]
