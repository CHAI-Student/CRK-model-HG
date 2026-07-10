"""CellBeliefStore — 셀 정체성 신념 (설계 v2).

배치는 입력이 아니라 추론 결과: 증거 누적 → 승격, 모순 → 강등(재배치 자기
교정), allowlist 철수 → 무효화, 영속 roundtrip.
"""
from crk_model.ledger.cells import CellBeliefStore


def store(path=None):
    return CellBeliefStore(path)


class TestPromotion:
    def test_unknown_until_enough_evidence(self):
        s = store()
        s.observe(1, 0, "P001", strong=True)
        s.observe(1, 0, "P001", strong=True)
        assert s.identity(1, 0) is None  # promote_score(3.0) 미달

    def test_promotes_after_consistent_strong_evidence(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        assert s.identity(1, 0) == "P001"

    def test_weak_evidence_promotes_slowly(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=False)  # 반품 0.25×
        assert s.identity(1, 0) is None
        for _ in range(9):
            s.observe(1, 0, "P001", strong=False)
        assert s.identity(1, 0) == "P001"  # 12×0.25 = 3.0

    def test_rival_blocks_promotion_until_ratio(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        # 경쟁 증거가 섞이면 (ratio 3배 미달) 승격 불가여야 하므로 처음부터 섞는다
        s2 = store()
        s2.observe(1, 0, "P002", strong=True)
        s2.observe(1, 0, "P002", strong=True)
        for _ in range(3):
            s2.observe(1, 0, "P001", strong=True)
        assert s2.identity(1, 0) is None  # 3.0 vs 2.0 — 3배 비율 미달

    def test_cells_are_independent(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        assert s.identity(1, 1) is None
        assert s.identity(2, 0) is None


class TestDemotion:
    def _confirmed(self):
        s = store()
        for _ in range(5):
            s.observe(1, 0, "P001", strong=True)
        assert s.identity(1, 0) == "P001"
        return s

    def test_single_contradiction_does_not_demote(self):
        s = self._confirmed()
        s.observe(1, 0, "P002", strong=True)  # 1회성 (demote_score 2.0 미달)
        assert s.identity(1, 0) == "P001"

    def test_repeated_strong_contradiction_demotes(self):
        s = self._confirmed()
        s.observe(1, 0, "P002", strong=True)
        s.observe(1, 0, "P002", strong=True)
        assert s.identity(1, 0) is None  # 재배치 자기 교정

    def test_weak_contradiction_never_demotes(self):
        # 오배치 반품(약한 증거)이 신념을 오염시키지 않는다
        s = self._confirmed()
        for _ in range(20):
            s.observe(1, 0, "P002", strong=False)
        assert s.identity(1, 0) == "P001"

    def test_repromotes_to_rival_after_restock(self):
        s = self._confirmed()
        for _ in range(2):
            s.observe(1, 0, "P002", strong=True)  # 강등
        assert s.identity(1, 0) is None
        for _ in range(20):
            s.observe(1, 0, "P002", strong=True)
        assert s.identity(1, 0) == "P002"


class TestInvalidation:
    def test_missing_product_clears_belief(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        s.invalidate_missing({"P002", "P003"})  # P001 진열 철수
        assert s.identity(1, 0) is None

    def test_present_product_survives(self):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        s.invalidate_missing({"P001", "P002"})
        assert s.identity(1, 0) == "P001"


class TestPersistence:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "cells.json"
        s = store(path)
        for _ in range(3):
            s.observe(4, 1, "P178", strong=True)
        reloaded = store(path)
        assert reloaded.identity(4, 1) == "P178"

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = tmp_path / "cells.json"
        path.write_text("{not json", encoding="utf-8")
        s = store(path)  # 로드 실패 → 빈 상태 (fail-closed 안전 방향)
        assert s.identity(1, 0) is None

    def test_memory_only_without_path(self, tmp_path):
        s = store()
        for _ in range(3):
            s.observe(1, 0, "P001", strong=True)
        assert not list(tmp_path.iterdir())  # 파일 부작용 없음
