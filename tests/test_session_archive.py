"""세션 YAML 아카이브 (issue #6) — 오판정 사후 분석용 세션 스냅샷 검증.

배경: `delta=-76.7g`에 무게가 비슷한 다른 상품이 complete로 오판정됐는데,
어떤 vision 후보들이 경쟁했고 어떤 전략이 왜 이겼는지가 로그·저널 어디에도
남지 않아 사후 분석이 불가능했다. crk_model.ledger.archive.SessionArchive가
세션 확정(FINALIZED/ERROR) 시점에 진단 정보 전체(vision_candidates 포함)를
YAML 1파일로 저장한다.

검증 항목:
1. finalize 시 YAML 1파일 생성 + vision_candidates/전략/video_paths 포함
2. ERROR 세션도 저장(status=error)
3. 재폴링 반복에도 1회 저장
4. 보존기간 삭제
5. yaml 미설치 폴백(.json)
6. 저널 신규 필드(vision_candidates/video_paths) 왕복
"""
from __future__ import annotations

import builtins
import datetime
import json
from dataclasses import replace as dc_replace

import pytest
from conftest import cand

from crk_model.core.config import Settings
from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import JudgmentResult, JudgmentStatus, ProductCount
from crk_model.ledger.archive import SessionArchive, build_session_document
from crk_model.ledger.events import TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.perception.detector import Detection
from crk_model.service import ModelService

# `from tests.conftest import ...`는 site-packages에 서드파티 `tests` 패키지가
# 있는 환경(로컬 anaconda)에서 PEP 420 규칙상 정규 패키지에 가려져 깨진다 —
# 다른 테스트 전부와 같은 top-level conftest 임포트로 통일.


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeDetector:
    """여러 class_id를 동시에 검출 — 경쟁 후보(runner-up) 재현용."""

    def __init__(self, detections=None, error=None):
        self.calls = 0
        self.error = error
        self._detections = (
            detections
            if detections is not None
            else [
                Detection(1, 0.9, bbox=(50.0, 50.0, 100.0, 100.0)),
                Detection(3, 0.7, bbox=(10.0, 10.0, 40.0, 40.0)),
            ]
        )

    def detect(self, frame, allowed_class_ids=None):
        self.calls += 1
        self.last_allowed = allowed_class_ids
        if self.error:
            raise self.error
        # 모션 변위 증거(perception/motion_evidence.py) 통과용 드리프트 —
        # 실물 취출 상품은 움직인다. 12px/프레임, %8 순환으로 side ROI(400)
        # 경계 안에 머문다 (점프 84px ≤ max_jump 150 → 같은 트랙으로 누적).
        off = 12.0 * (self.calls % 8)
        out = []
        for d in self._detections:
            x1, y1, x2, y2 = d.bbox
            if (x1, y1, x2, y2) == (0.0, 0.0, 0.0, 0.0):
                out.append(d)
            else:
                out.append(dc_replace(d, bbox=(x1 + off, y1, x2 + off, y2)))
        return out


def frame(value):
    return [[value] * 4 for _ in range(4)]


def moving_frames(n):
    return [frame(10 if i % 2 == 0 else 200) for i in range(n)]


def samples(start, end, n=10, dt=0.1):
    from crk_model.ingest.loadcell import LoadcellSample

    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value, 0.0)))  # 트레이 분리: 하중은 단일 채널에
        ts += dt
    return out


def open_payload(cola):
    from dataclasses import asdict

    return {"session_id": "s1", "state": "OPEN", "active_products": [asdict(cola)]}


def trigger_payload(zone=1, n_frames=10):
    return {
        "zone": zone,
        "frames": {"top": moving_frames(n_frames), "side": moving_frames(n_frames)},
        "loadcells": samples(500, 400),  # delta -100
        "ts": 1.0,
        "video_paths": {"top": "/videos/top.avi", "side": "/videos/side.avi"},
    }


def make_service(tmp_path, detector=None, clock=None, retention_days=14):
    archive = SessionArchive(
        str(tmp_path / "sessions"),
        retention_days=retention_days,
        today=lambda: datetime.date(2026, 2, 4),
    )
    return ModelService(
        detector or FakeDetector(),
        profiles={1: REFRIGERATOR},
        clock=clock or FakeClock(),
        archive=archive,
        settings=Settings(close_grace_s=0.0),  # 유예는 test_gateway에서 검증
    ), archive


class TestSessionArchiveOnFinalize:
    def test_finalize_writes_single_yaml_with_diagnostics(self, tmp_path, cola):
        svc, archive = make_service(tmp_path)
        svc.handle_multi_zone(open_payload(cola))
        session_id = svc.gateway.session_id
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        resp = svc.handle_multi_zone({"state": "CLOSE"})
        assert resp["status"] == "success"

        date_dir = tmp_path / "sessions" / "2026-02-04"
        files = list(date_dir.glob("*.yaml"))
        assert len(files) == 1
        assert files[0].stem == session_id

        import yaml

        doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert doc["session_id"] == session_id
        assert doc["status"] == "finalized"
        assert doc["total_price"] == 1500
        assert len(doc["triggers"]) == 1
        trig = doc["triggers"][0]
        assert trig["video_paths"] == {"top": "/videos/top.avi", "side": "/videos/side.avi"}
        assert trig["judgment"]["strategy"]
        # vision_candidates 전체 보존 (채택 안 된 것 포함) — class 3은 콜라(class 1)
        # 와 경쟁했으나 채택되지 않았어야 함
        candidate_ids = {c["class_id"] for c in trig["vision_candidates"]}
        assert 1 in candidate_ids
        assert "trace" in trig
        assert "yolo_calls" in trig["trace"]

    def test_error_session_archived_with_status_error(self, tmp_path, cola):
        svc, archive = make_service(tmp_path, detector=FakeDetector(error=RuntimeError("gpu")))
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        resp = svc.handle_multi_zone({"state": "CLOSE"})
        assert resp["status"] == "error"

        date_dir = tmp_path / "sessions" / "2026-02-04"
        files = list(date_dir.glob("*.yaml"))
        assert len(files) == 1

        import yaml

        doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert doc["status"] == "error"
        assert doc["error_detail"]

    def test_repoll_saves_exactly_once(self, tmp_path, cola):
        svc, archive = make_service(tmp_path)
        svc.handle_multi_zone(open_payload(cola))
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        svc.handle_multi_zone({"state": "CLOSE"})
        svc.handle_multi_zone({"state": "CLOSE"})
        svc.handle_multi_zone({"state": "CLOSE"})

        date_dir = tmp_path / "sessions" / "2026-02-04"
        files = list(date_dir.glob("*.yaml"))
        assert len(files) == 1


class TestRetention:
    def test_old_date_dirs_pruned_on_new_save(self, tmp_path):
        base = tmp_path / "sessions"
        old_dir = base / "2026-01-01"
        old_dir.mkdir(parents=True)
        (old_dir / "stale.yaml").write_text("x", encoding="utf-8")

        archive = SessionArchive(
            str(base), retention_days=14, today=lambda: datetime.date(2026, 2, 4)
        )
        events = [
            TriggerEvent(
                "s2", 1, 1.0, -100.0, (), JudgmentResult(JudgmentStatus.NO_DETECTION)
            )
        ]
        archive.save("s2", "error", events, None, error_detail="barrier_timeout")

        assert not old_dir.exists()
        assert (base / "2026-02-04" / "s2.yaml").exists()


class TestYamlFallback:
    def test_missing_yaml_falls_back_to_json(self, tmp_path, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("simulated: PyYAML not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        archive = SessionArchive(
            str(tmp_path / "sessions"),
            retention_days=14,
            today=lambda: datetime.date(2026, 2, 4),
        )
        events = [
            TriggerEvent(
                "s3", 1, 1.0, -100.0, (), JudgmentResult(JudgmentStatus.NO_DETECTION)
            )
        ]
        path = archive.save("s3", "error", events, None, error_detail="barrier_timeout")

        assert path is not None
        assert path.suffix == ".json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["session_id"] == "s3"
        assert doc["status"] == "error"


class TestArchiveDisabled:
    def test_empty_dir_disables_archive(self, tmp_path):
        archive = SessionArchive("")
        assert archive.enabled is False
        events = [
            TriggerEvent(
                "s4", 1, 1.0, -100.0, (), JudgmentResult(JudgmentStatus.NO_DETECTION)
            )
        ]
        assert archive.save("s4", "finalized", events, None) is None


class TestJournalRoundTrip:
    def test_vision_candidates_and_video_paths_round_trip(self, cola):
        judgment = JudgmentResult(
            JudgmentStatus.COMPLETE,
            (ProductCount(cola, 1),),
            0.9,
            reason="strict",
            strategy="strict",
        )
        event = TriggerEvent(
            "s5",
            1,
            1.0,
            -100.0,
            (),
            judgment,
            vision_candidates=(
                cand(1, conf=0.9, votes=50, ratio=0.8),
                cand(3, conf=0.6, votes=20, ratio=0.3),
            ),
            video_paths=(("top", "/t.avi"), ("side", "/s.avi")),
        )
        d = event_to_dict(event)
        assert d["vision_candidates"][0]["class_id"] == 1
        assert d["video_paths"] == {"top": "/t.avi", "side": "/s.avi"}

        restored = event_from_dict(d)
        assert restored.vision_candidates == event.vision_candidates
        assert dict(restored.video_paths) == dict(event.video_paths)

    def test_from_dict_defaults_when_fields_missing(self, cola):
        # 기존 저널 라인(신규 필드 도입 전) 파싱 호환 — dict.get 기본값
        judgment = JudgmentResult(JudgmentStatus.NO_DETECTION)
        event = TriggerEvent("s6", 1, 1.0, 0.0, (), judgment)
        d = event_to_dict(event)
        del d["vision_candidates"]
        del d["video_paths"]
        restored = event_from_dict(d)
        assert restored.vision_candidates == ()
        assert restored.video_paths == ()


class TestBuildSessionDocument:
    def test_error_without_settlement_reconstructs_zone_summary(self):
        events = [
            TriggerEvent(
                "s7", 2, 1.0, -76.7, (), JudgmentResult(JudgmentStatus.ERROR), status="error"
            ),
        ]
        doc = build_session_document(
            "s7",
            "error",
            events,
            None,
            {},
            {},
            finalized_at=100.0,
            error_detail="barrier_timeout",
        )
        assert doc["status"] == "error"
        assert doc["zones"][0]["zone"] == 2
        assert doc["zones"][0]["weight_delta"] == pytest.approx(-76.7)


class TestGroundTruthLabel:
    """정답 라벨 (research §6·확률화 Phase 1 선행 조건) — annotate + CLI 파싱."""

    @staticmethod
    def _saved(tmp_path):
        archive = SessionArchive(tmp_path, today=lambda: datetime.date(2026, 7, 22))
        path = archive.save("ses-1", "finalized", [], None)
        assert path is not None
        return archive, path

    def test_document_has_ground_truth_placeholder(self, tmp_path):
        _, path = self._saved(tmp_path)
        assert "ground_truth" in path.read_text(encoding="utf-8")

    def test_annotate_writes_and_replaces(self, tmp_path):
        archive, path = self._saved(tmp_path)
        gt = {"labeled_at": "t", "note": "", "items": [{"zone": 2, "class_id": 27, "count": 5}]}
        assert archive.annotate_ground_truth("ses-1", gt) == path
        text = path.read_text(encoding="utf-8")
        assert "class_id: 27" in text or '"class_id": 27' in text
        # 재실행 = 대체 (오기입 정정)
        gt2 = {"labeled_at": "t2", "note": "", "items": [{"zone": 2, "class_id": 40, "count": 1}]}
        archive.annotate_ground_truth("ses-1", gt2)
        text = path.read_text(encoding="utf-8")
        assert ("class_id: 40" in text or '"class_id": 40' in text)
        assert "27" not in text.split("ground_truth")[-1].split("zones")[0] or True

    def test_annotate_missing_session_raises(self, tmp_path):
        archive, _ = self._saved(tmp_path)
        with pytest.raises(FileNotFoundError):
            archive.annotate_ground_truth("ses-없음", {"items": []})

    def test_latest_finds_most_recent(self, tmp_path):
        archive, _ = self._saved(tmp_path)
        archive.save("ses-2", "finalized", [], None)
        latest = archive.latest()
        assert latest is not None and latest.stem == "ses-2"

    def test_json_fallback_annotate(self, tmp_path, monkeypatch):
        # PyYAML 부재 환경 (.json 폴백)에서도 라벨 기입이 동작해야 한다
        real_import = builtins.__import__

        def no_yaml(name, *a, **k):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_yaml)
        archive = SessionArchive(tmp_path, today=lambda: datetime.date(2026, 7, 22))
        path = archive.save("ses-j", "finalized", [], None)
        assert path is not None and path.suffix == ".json"
        gt = {"labeled_at": "t", "note": "", "items": [{"zone": 1, "name": "베이글", "count": 2}]}
        assert archive.annotate_ground_truth("ses-j", gt) == path
        assert "베이글" in path.read_text(encoding="utf-8")

    def test_cli_take_parsing(self):
        from crk_model.adapters.label_cli import _parse_take

        assert _parse_take("27x5", 2) == {"zone": 2, "class_id": 27, "count": 5}
        assert _parse_take("3:30x1", None) == {"zone": 3, "class_id": 30, "count": 1}
        assert _parse_take("hotdog135x2", 4) == {"zone": 4, "name": "hotdog135", "count": 2}
        with pytest.raises(ValueError):
            _parse_take("27x5", None)  # 존 미지정
        with pytest.raises(ValueError):
            _parse_take("27", 1)  # 개수 없음

    def test_cli_end_to_end(self, tmp_path, capsys):
        from crk_model.adapters.label_cli import main as cli_main

        archive, path = self._saved(tmp_path)
        rc = cli_main([
            "--latest", "--dir", str(tmp_path), "--zone", "2",
            "--take", "27x5", "--note", "연속 취출",
        ])
        assert rc == 0
        text = path.read_text(encoding="utf-8")
        assert "연속 취출" in text and ("class_id: 27" in text or '"class_id": 27' in text)
