"""배치 수집기 — D8: 설계만 확정, 기본 OFF (batch_size=1).

L1+L2 실측 후 목표 미달 시에만 활성화. 활성화 시:
- 고정 배치 + 패딩(부족분 더미, 결과 폐기)이 1안 (dynamic batch의 TRT 프로파일
  재선택·할당자 파편화 리스크 회피).
- 카메라별 분리 수집 — hand-path tracker가 카메라별 프레임 순서에 의존하므로
  인터리빙 혼합 금지 (L3 승인 조건).
"""
from __future__ import annotations

from typing import Any


class FixedBatchCollector:
    def __init__(self, batch_size: int = 1):
        if batch_size < 1:
            raise ValueError("batch_size >= 1")
        self.batch_size = batch_size
        self._buffers: dict[str, list[Any]] = {}

    def add(self, camera: str, frame: Any) -> list[Any] | None:
        """가득 차면 해당 카메라 배치를 반환 (단일 카메라 프레임만 — 인터리빙 금지)."""
        buf = self._buffers.setdefault(camera, [])
        buf.append(frame)
        if len(buf) >= self.batch_size:
            self._buffers[camera] = []
            return buf
        return None

    def flush(self, camera: str) -> tuple[list[Any], int]:
        """잔여 프레임 + 필요한 패딩 수 반환 (패딩 결과는 호출측에서 폐기)."""
        buf = self._buffers.get(camera, [])
        self._buffers[camera] = []
        pad = (self.batch_size - len(buf)) % self.batch_size if buf else 0
        return buf, pad
