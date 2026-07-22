"""analyze-sessions — 아카이브 오프라인 리포트 (research §6, 로드맵 단기 ②).

계약: 읽기 전용 — shadow 정오 집계(Phase 2 승격 게이트), conformal 분위수,
σ_db 잔차 실측. 구 아카이브(class_id/unit_weight 미기록)는 조용히 제외.
"""
import json

from crk_model.adapters.analyze_cli import analyze, load_documents, main


def _doc(session_id="ses-1", **over):
    doc = {
        "session_id": session_id,
        "status": "finalized",
        "ground_truth": None,
        "zones": [],
        "triggers": [],
    }
    doc.update(over)
    return doc


def _trigger(zone=2, delta=-155.0, **over):
    trig = {
        "zone": zone,
        "delta_weight": delta,
        "judgment": {"status": "complete", "products": []},
        "vision_candidates": [],
        "trace": {},
    }
    trig.update(over)
    return trig


def _gt(*items, note=""):
    return {"labeled_at": "2026-07-23T00:00:00", "note": note, "items": list(items)}


class TestAnalyze:
    def test_bocpd_mismatch_collected(self):
        doc = _doc(triggers=[_trigger(trace={
            "loadcell_shadow": {
                "analyzer": "bocpd", "delta": -170.0, "delta_std": 4.9,
                "primary_delta": 0.0, "primary_reason": "insufficient_stable_regions",
                "mismatch": True,
            }
        })])
        report = analyze([doc])
        assert report["bocpd"]["observed"] == 1
        m = report["bocpd"]["mismatches"][0]
        assert m["shadow_delta"] == -170.0 and m["primary_delta"] == 0.0

    def test_likelihood_labeled_eval_score_correct(self):
        # 현행 판정 27×1, score 1위 40×1, GT 40×1 → score만 정답
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 40, "count": 1}),
            triggers=[_trigger(trace={
                "likelihood_shadow": [{
                    "scorer": "weight_likelihood",
                    "mismatch": True,
                    "current": {"items": [[27, 1]], "score": -1.0},
                    "top": {"items": [[40, 1]], "score": -0.2},
                }]
            })],
        )
        report = analyze([doc])
        ev = report["likelihood"]["labeled_eval"]
        assert ev == {"score_correct": 1, "current_correct": 0, "both_wrong": 0}
        rec = report["likelihood"]["mismatches"][0]
        assert rec["score_correct"] is True and rec["current_correct"] is False

    def test_likelihood_multi_channel_entries_aggregate(self):
        # 멀티트레이: 채널별 entry 합이 존 GT와 비교 단위
        doc = _doc(
            ground_truth=_gt(
                {"zone": 2, "class_id": 27, "count": 1},
                {"zone": 2, "class_id": 40, "count": 1},
            ),
            triggers=[_trigger(trace={
                "likelihood_shadow": [
                    {"mismatch": False,
                     "current": {"items": [[27, 1]]}, "top": {"items": [[27, 1]]}},
                    {"mismatch": True, "channel": 1,
                     "current": {"items": []}, "top": {"items": [[40, 1]]}},
                ]
            })],
        )
        report = analyze([doc])
        rec = report["likelihood"]["mismatches"][0]
        assert rec["score_top"] == [(27, 1), (40, 1)]
        assert rec["score_correct"] is True

    def test_calibration_quantiles_and_missing(self):
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 1}),
            triggers=[
                _trigger(vision_candidates=[
                    {"class_id": 27, "confidence": 0.9, "vote_count": 30,
                     "vote_ratio": 0.3},
                    {"class_id": 13, "confidence": 0.5, "vote_count": 60,
                     "vote_ratio": 0.6},
                ]),
                _trigger(vision_candidates=[]),  # 정답이 후보에 없던 트리거
            ],
        )
        report = analyze([doc])
        q = report["calibration"]["quantiles"]
        assert q["votes"]["n"] == 1 and q["votes"]["min"] == 30.0
        assert abs(q["share"]["min"] - 0.5) < 1e-9  # 30/60
        assert len(report["calibration"]["missing_from_candidates"]) == 1

    def test_sigma_db_unit_residuals(self):
        # GT 베이글×5, delta −743, unit_weight 155 → 개당 잔차 (743−775)/5 = −6.4
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 5}),
            triggers=[_trigger(delta=-743.0, judgment={
                "status": "complete",
                "products": [{"product_id": "P27", "class_id": 27,
                              "unit_weight": 155.0, "count": 5}],
            })],
        )
        report = analyze([doc])
        assert report["sigma_db"]["unit_residuals"] == [-6.4]
        assert report["sigma_db"]["suggested_sigma_db"] == 6.4

    def test_old_archive_without_class_id_skipped_quietly(self):
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 1}),
            triggers=[_trigger(judgment={
                "status": "complete",
                "products": [{"product_id": "P27", "count": 1}],  # 구 스키마
            })],
        )
        report = analyze([doc])  # 예외 없이 완료, σ_db 표본 없음
        assert report["sigma_db"]["unit_residuals"] == []


class TestCli:
    def test_end_to_end_json_archive(self, tmp_path, capsys):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        doc = _doc(triggers=[_trigger(trace={
            "loadcell_shadow": {"analyzer": "bocpd", "delta": -100.0,
                                "delta_std": 3.0, "primary_delta": 0.0,
                                "primary_reason": "x", "mismatch": True},
        })])
        (day / "ses-1.json").write_text(json.dumps(doc), encoding="utf-8")
        assert main(["--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "BOCPD shadow" in out and "ses-1" in out

    def test_empty_dir_returns_error(self, tmp_path, capsys):
        assert main(["--dir", str(tmp_path)]) == 1

    def test_load_documents_reports_broken_file(self, tmp_path):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        (day / "bad.json").write_text("{not json", encoding="utf-8")
        docs = load_documents(tmp_path)
        assert docs and "_load_error" in docs[0]
