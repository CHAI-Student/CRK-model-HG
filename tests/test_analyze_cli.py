"""analyze-sessions — 아카이브 오프라인 리포트 (research §6, 로드맵 단기 ②).

계약: 읽기 전용 — shadow 정오 집계(Phase 2 승격 게이트), conformal 분위수,
σ_db 잔차 실측, tray prior 개입(무-prior 순위 복원), 트랙릿 T1(head_obs
분포·단절). 구 아카이브(class_id/unit_weight/track_detail 미기록)는 조용히 제외.
"""
import json

from crk_model.adapters.analyze_cli import analyze, load_documents, main, render


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

    def test_billing_accuracy_correct_and_wrong(self):
        right = _doc(
            "ses-ok",
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 5}),
            zones=[{"zone": 2, "products": [
                {"product_id": "P27", "class_id": 27, "unit_weight": 155.0,
                 "count": 5},
            ]}],
        )
        wrong = _doc(
            "ses-bad",
            ground_truth=_gt({"zone": 1, "class_id": 46, "count": 1}),
            zones=[{"zone": 1, "products": [
                {"product_id": "P13", "class_id": 13, "unit_weight": 185.0,
                 "count": 1},
            ]}],
        )
        report = analyze([right, wrong])
        bill = report["billing"]
        assert bill["labeled"] == 2 and bill["correct"] == 1
        diff = bill["wrong"][0]
        assert diff["session"] == "ses-bad"
        assert diff["diffs"][0]["ground_truth"] == [(46, 1)]
        assert diff["diffs"][0]["billed"] == [(13, 1)]

    def test_billing_overbilled_unlabeled_zone_counts_wrong(self):
        # GT에 없는 존에 과금이 있으면 오답 (전 존 라벨 전제)
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 1}),
            zones=[
                {"zone": 2, "products": [
                    {"product_id": "P27", "class_id": 27, "unit_weight": 155.0,
                     "count": 1}]},
                {"zone": 3, "products": [
                    {"product_id": "P13", "class_id": 13, "unit_weight": 185.0,
                     "count": 1}]},
            ],
        )
        report = analyze([doc])
        assert report["billing"]["correct"] == 0
        assert report["billing"]["wrong"][0]["diffs"][0]["zone"] == 3

    def test_tray_prior_flip_detected_and_labeled_eval(self):
        # 이슈 #17 ses-5 재현: prior 없으면 44×3이 1위(score −0.476), prior
        # (−2.5)로 3×1이 1위 — ranking의 score − log_p_tray로 무-prior 순위를
        # 복원해 flip을 검출하고, GT(3×1) 대비 prior_helped로 집계.
        doc = _doc(
            ground_truth=_gt({"zone": 4, "class_id": 3, "count": 1}),
            triggers=[_trigger(zone=4, delta=-230.0, trace={
                "likelihood_shadow": [{
                    "scorer": "weight_likelihood",
                    "mismatch": True,
                    "tray_prior": {44: -2.5},
                    "current": {"items": [[44, 3]], "score": -2.976},
                    "top": {"items": [[3, 1]], "score": -2.91},
                    "ranking": [
                        {"items": [[3, 1]], "score": -2.91},
                        {"items": [[44, 3]], "score": -2.976, "log_p_tray": -2.5},
                    ],
                }]
            })],
        )
        report = analyze([doc])
        tp = report["tray_prior"]
        assert tp["observed"] == 1
        (flip,) = tp["flips"]
        assert flip["without_prior"] == [(44, 3)]
        assert flip["with_prior"] == [(3, 1)]
        assert flip["prior_correct"] is True
        assert tp["labeled_eval"] == {
            "prior_helped": 1, "prior_hurt": 0, "both_wrong": 0
        }
        assert "tray prior shadow" in render(report)

    def test_tray_prior_without_rank_change_not_a_flip(self):
        # prior가 실렸지만 1위가 그대로면 개입 계수만 오르고 flip은 아니다
        doc = _doc(triggers=[_trigger(trace={
            "likelihood_shadow": [{
                "mismatch": False,
                "tray_prior": {44: -2.5},
                "current": {"items": [[3, 1]]},
                "top": {"items": [[3, 1]], "score": -0.1},
                "ranking": [
                    {"items": [[3, 1]], "score": -0.1},
                    {"items": [[44, 3]], "score": -4.0, "log_p_tray": -2.5},
                ],
            }]
        })])
        report = analyze([doc])
        assert report["tray_prior"]["observed"] == 1
        assert report["tray_prior"]["flips"] == []

    def test_tracklet_head_split_and_fragmentation(self):
        # T1 (docs/0723_tracklet_cost_benefit.md §8): 정답 클래스 트랙과
        # 비정답(held/배경) 트랙의 head_obs 분리 실측 + 트랙 ≥ 4 단절 의심.
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 30, "count": 1}),
            triggers=[_trigger(zone=2, trace={
                "vote_summary": {"motion_evidence": {"top": {
                    30: {"passed": True, "tracks": 1, "track_detail": [
                        {"first": 140, "last": 200, "obs": 20,
                         "head_obs": 0, "passed": True},
                    ]},
                    27: {"passed": True, "tracks": 5, "track_detail": [
                        {"first": 0, "last": 400, "obs": 200,
                         "head_obs": 28, "passed": True},
                    ]},
                }}},
            })],
        )
        # 구 아카이브 (track_detail 이전) — 조용히 제외
        old = _doc("ses-old", triggers=[_trigger(trace={
            "vote_summary": {"motion_evidence": {"top": {
                13: {"passed": False, "tracks": 2},
            }}},
        })])
        report = analyze([doc, old])
        tk = report["tracklet"]
        assert tk["triggers"] == 1
        assert tk["gt_head_obs"] == [0.0]
        assert tk["non_gt_head_obs"] == [28.0]
        (frag,) = tk["fragmented"]
        assert frag["class_id"] == 27 and frag["tracks"] == 5
        assert tk["quantiles"]["tracks_per_class"]["max"] == 5.0
        out = render(report)
        assert "트랙릿 T1" in out and "단절 의심" in out

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

    def test_session_detail_dump(self, tmp_path, capsys):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        doc = _doc(
            "ses-bad",
            ground_truth=_gt({"zone": 1, "class_id": 46, "count": 1}),
            triggers=[_trigger(
                zone=1, delta=-67.5,
                judgment={"status": "partial",
                          "strategy": "vision_first_identity_partial",
                          "reason": "vision_first_identity_partial",
                          "confidence": 0.4,
                          "products": [{"product_id": "P13", "class_id": 13,
                                        "unit_weight": 185.0, "count": 1}]},
                vision_candidates=[
                    {"class_id": 13, "confidence": 0.8, "vote_count": 60,
                     "vote_ratio": 0.3},
                    {"class_id": 46, "confidence": 1.0, "vote_count": 12,
                     "vote_ratio": 0.06},
                ],
            )],
        )
        (day / "ses-bad.json").write_text(json.dumps(doc), encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--session", "ses-bad"]) == 0
        out = capsys.readouterr().out
        assert "vision_first_identity_partial" in out
        assert "GT: z1:46x1" in out and "c13:60표" in out

    def test_session_detail_not_found(self, tmp_path, capsys):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        (day / "ses-1.json").write_text(json.dumps(_doc()), encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--session", "nope"]) == 1

    def test_since_filters_older_sessions(self, tmp_path, capsys):
        # 코드 버전이 섞인 아카이브에서 배포 이후만 집계 — 세션 id 말미
        # epoch 기준 필터 (구 세션이 최신 코드 평가를 오염시키지 않게)
        day = tmp_path / "2026-07-23"
        day.mkdir()
        old = _doc("ses-1-1784700000", triggers=[_trigger(trace={
            "loadcell_shadow": {"analyzer": "bocpd", "delta": -1.0,
                                "delta_std": 1.0, "primary_delta": 0.0,
                                "primary_reason": "x", "mismatch": True}})])
        new = _doc("ses-2-1784800000", triggers=[_trigger()])
        (day / "ses-1-1784700000.json").write_text(json.dumps(old), encoding="utf-8")
        (day / "ses-2-1784800000.json").write_text(json.dumps(new), encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--since", "1784750000"]) == 0
        out = capsys.readouterr().out
        assert "1/2 세션" in out
        assert "ses-1-1784700000" not in out  # 구 세션 mismatch가 안 섞임

    def test_since_accepts_iso_datetime(self, tmp_path, capsys):
        import datetime

        day = tmp_path / "2026-07-23"
        day.mkdir()
        epoch = datetime.datetime.fromisoformat("2026-07-23T12:00").timestamp()
        doc = _doc(f"ses-1-{int(epoch) + 100}", triggers=[_trigger()])
        (day / "a.json").write_text(json.dumps(doc), encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--since", "2026-07-23T12:00"]) == 0
        assert main(["--dir", str(tmp_path), "--since", "2099-01-01"]) == 1

    def test_load_documents_reports_broken_file(self, tmp_path):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        (day / "bad.json").write_text("{not json", encoding="utf-8")
        docs = load_documents(tmp_path)
        assert docs and "_load_error" in docs[0]
