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

import pytest

from crk_model.core.profiles import REFRIGERATOR
from crk_model.core.types import JudgmentResult, JudgmentStatus, ProductCount
from crk_model.ledger.archive import SessionArchive, build_session_document
from crk_model.ledger.events import TriggerEvent
from crk_model.ledger.journal import event_from_dict, event_to_dict
from crk_model.perception.detector import Detection
from crk_model.service import ModelService
from tests.conftest import cand


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

    def detect(self, frame):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self._detections)


def frame(value):
    return [[value] * 4 for _ in range(4)]


def moving_frames(n):
    return [frame(10 if i % 2 == 0 else 200) for i in range(n)]


def samples(start, end, n=10, dt=0.1):
    from crk_model.ingest.loadcell import LoadcellSample

    out, ts = [], 0.0
    for value in [start] * n + [end] * n:
        out.append(LoadcellSample(ts, (value / 2, value / 2)))
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
    ), archive


class TestSessionArchiveOnFinalize:
    def test_finalize_writes_single_yaml_with_diagnostics(self, tmp_path, cola):
        svc, archive = make_service(tmp_path)
        svc.handle_multi_zone(open_payload(cola))
        session_id = svc.gateway.session_id
        svc.handle_trigger(trigger_payload())
        svc.process_pending()
        resp = svc.handle_multi_zone({"state": "CLOSE"})
        assert resp["status"] == "complete"

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
