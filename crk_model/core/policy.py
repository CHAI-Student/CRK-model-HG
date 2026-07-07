"""에러 세션 결제 정책 — D9 (기술 결정이 아니라 사업 결정).

I13: 에러 trigger를 안은 세션의 무성(silent) 확정 금지.
"에러 포함 세션의 결제 확정 가능 여부" 자체가 Node 팀·운영과의 계약 항목(P4)이며,
합의 전 기본값은 fail-closed(BLOCK_PAYMENT)다.
"""
from __future__ import annotations

from enum import Enum


class ErrorSessionPolicy(str, Enum):
    # 기본값: 에러 trigger가 하나라도 있으면 세션 전체 결제 차단 (fail-closed)
    BLOCK_PAYMENT = "block_payment"
    # 합의 시 선택지: 에러 없는 존만 확정, 에러 존은 제외 + 기록
    FINALIZE_ERROR_FREE_ZONES = "finalize_error_free_zones"
