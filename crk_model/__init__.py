"""CRK-model-HG — 스마트 자판기 모델 서비스 그린필드 재설계.

GREENFIELD_DESIGN_GUIDE.md의 결정 D1~D10을 전부 권장안으로 구현.
모듈 경계 = 테스트 경계 (D10): ingest → frames → perception → judgment(순수)
→ ledger(영속) → gateway(상태기계).
"""

__version__ = "0.1.0"
