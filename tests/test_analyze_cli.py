"""analyze-sessions — 세션 아카이브 오프라인 분석과 CLI 계약.

청구 정오, 보정 분위수, 개당 무게 잔차, held/ghost shadow, 트랙 단절과 세션
상세 출력을 검증한다. 폐기된 shadow 필드나 class_id 등 일부 필드가 없는 구
아카이브도 예외 없이 읽는 관용 파싱은 운영 데이터 호환성 계약으로 유지한다.
"""
import json
from copy import deepcopy

from crk_model.adapters.analyze_cli import analyze, load_documents, main, render, render_session
from crk_model.perception.detector import Detection
from crk_model.perception.motion_direction import (
    MotionDirectionConfig,
    YDirectionConfig,
    summarize_direction,
)
from crk_model.perception.motion_evidence import MotionEvidence


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
    def test_old_archive_retired_shadow_fields_ignored(self):
        # 폐기된 shadow 기제(BOCPD 2026-07-24, 무게 우도·tray prior·튜브 다수결
        # 2026-07-30)의 구 아카이브 필드는 예외 없이 조용히 무시된다 —
        # 아카이브는 코드 버전이 섞이므로 관용 파싱이 계약이다.
        doc = _doc(triggers=[_trigger(trace={
            "loadcell_shadow": {
                "analyzer": "bocpd", "delta": -170.0, "delta_std": 4.9,
                "primary_delta": 0.0, "primary_reason": "insufficient_stable_regions",
                "mismatch": True,
            },
            "likelihood_shadow": [{
                "scorer": "weight_likelihood", "mismatch": True,
                "current": {"items": [[27, 1]], "score": -5.6},
                "top": {"items": [[13, 1]], "score": -0.6},
                "tray_prior": {13: -2.5},
            }],
            "vote_summary": {"tube_shadow": {
                "by_class": {"13": {"votes": 8, "shadow": 0, "minority": 8}},
                "top_current": 13, "top_shadow": None, "changed": True,
            }},
        })])
        report = analyze([doc])
        assert report["sessions"] == 1
        assert "likelihood" not in report and "tray_prior" not in report
        assert "tube_eval" not in report["tracklet"]

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

    def test_unit_residual_samples(self):
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
        assert report["unit_residual"]["samples"] == [-6.4]
        assert report["unit_residual"]["suggested_slack"] == 6.4

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

    def test_tracklet_head_split_and_fragmentation(self):
        # T1 (docs/devdoc/design/0723_tracklet_cost_benefit.md §8) — 7차 실측 보정 반영:
        # head_obs는 이동(passed)·실질(obs≥3) 트랙만(정답 클래스의 진열
        # 인스턴스와 플리커 잔트랙 배제), 단절 의심은 실질 트랙 ≥ 4,
        # 에피소드 병합으로 영상을 공유한 형제 존 트리거는 1회만 계수.
        detail_30 = [
            {"first": 140, "last": 200, "obs": 20, "head_obs": 0, "passed": True},
            # 진열 인스턴스 (정지) — head 모집단에서 제외돼야 함
            {"first": 0, "last": 300, "obs": 90, "head_obs": 25, "passed": False},
        ]
        detail_27 = [
            {"first": 0, "last": 400, "obs": 200, "head_obs": 28, "passed": True},
            {"first": 50, "last": 90, "obs": 12, "head_obs": 0, "passed": False},
            {"first": 100, "last": 130, "obs": 8, "head_obs": 0, "passed": False},
            {"first": 200, "last": 230, "obs": 5, "head_obs": 0, "passed": False},
            # 플리커 잔트랙 (obs<3) — 실질 트랙이 아니다
            {"first": 300, "last": 301, "obs": 1, "head_obs": 0, "passed": False},
        ]
        me = {"top": {
            30: {"passed": True, "tracks": 2, "track_detail": detail_30},
            27: {"passed": True, "tracks": 5, "track_detail": detail_27},
        }}
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 30, "count": 1}),
            triggers=[
                _trigger(zone=2, trace={"vote_summary": {"motion_evidence": me}}),
                # 공유 영상의 형제 존 트리거 — detail 동일 → 중복 미계수
                _trigger(zone=3, trace={"vote_summary": {"motion_evidence": me}}),
            ],
        )
        # 구 아카이브 (track_detail 이전) — 조용히 제외
        old = _doc("ses-old", triggers=[_trigger(trace={
            "vote_summary": {"motion_evidence": {"top": {
                13: {"passed": False, "tracks": 2},
            }}},
        })])
        report = analyze([doc, old])
        tk = report["tracklet"]
        assert tk["triggers"] == 2  # detail 관측 트리거 수 (중복 제거는 지표만)
        assert tk["gt_head_obs"] == [0.0]  # 이동 트랙만 — 진열(정지) 제외
        assert tk["non_gt_head_obs"] == [28.0]  # held 패턴 트랙
        (frag,) = tk["fragmented"]
        assert frag["class_id"] == 27 and frag["tracks"] == 4  # 실질 트랙만
        assert tk["quantiles"]["tracks_per_class"] == {
            "n": 2, "min": 2.0, "p5": 2.0, "p25": 2.0, "median": 4.0, "max": 4.0,
        }
        out = render(report)
        assert "트랙릿 T1" in out and "단절 의심" in out

    def test_tracklet_held_shadow_gt_flag_and_non_gt(self):
        # T2 승격 게이트 입력: 정답 클래스에 held 플래그가 서면 active 보류
        # 신호(진짜 취출 표를 깎을 뻔한 사례), 비정답 건수는 기대 효과.
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 30, "count": 1}),
            triggers=[_trigger(zone=2, trace={"vote_summary": {"held_shadow": {
                "top": {27: [28, 60], 30: [6, 20]},
            }}})],
        )
        report = analyze([doc])
        tk = report["tracklet"]
        assert tk["held_non_gt"] == 1
        (flag,) = tk["held_gt_flags"]
        assert flag["class_id"] == 30 and flag["held_votes"] == 6
        out = render(report)
        assert "held 강등 관측" in out and "active 승격 보류" in out

    def test_ghost_shadow_eval_and_gt_flag(self):
        # 세션 고스트 원장 shadow (ghost_ledger): 정산 notes에서 검출 세션·
        # 재판정 시뮬 라벨 정오·정답 오플래그(승격 보류 신호)를 집계한다.
        helped = _doc(
            "ses-ghost",
            ground_truth=_gt({"zone": 1, "class_id": 40, "count": 2}),
            zones=[{"zone": 1, "products": [
                {"product_id": "P24", "class_id": 24, "unit_weight": 166.0,
                 "count": 1},
            ]}],
            notes=[
                "ghost_classes:class24@z1/4",
                "zone1:ghost_shadow:billed=class24:would=class40x2",
            ],
        )
        flagged = _doc(
            "ses-flag",
            ground_truth=_gt({"zone": 2, "class_id": 13, "count": 1}),
            zones=[{"zone": 2, "products": [
                {"product_id": "P13", "class_id": 13, "unit_weight": 189.0,
                 "count": 1},
            ]}],
            notes=["ghost_classes:class13@z2/3"],
        )
        report = analyze([helped, flagged])
        gh = report["ghost"]
        assert gh["observed"] == 2
        assert gh["labeled_eval"] == {
            "shadow_correct": 1, "current_correct": 0, "both_wrong": 0,
        }
        assert gh["gt_flagged"] == [{"session": "ses-flag", "classes": [13]}]
        out = render(report)
        assert "고스트 shadow" in out and "ghost 오플래그" in out

    def test_none_take_label_counts_as_labeled(self):
        # label-session --none (10차 ses-12): 무취출 GT — 청구 0이면 정답,
        # 청구가 있으면 오답. 구 0x1 우회 라벨도 class 0 필터로 동일 취급.
        clean = _doc("ses-none", ground_truth=_gt(note="gesture only"))
        legacy = _doc(
            "ses-zero",
            ground_truth=_gt({"zone": 2, "class_id": 0, "count": 1}),
        )
        overbilled = _doc(
            "ses-ghost",
            ground_truth=_gt(note="gesture only"),
            zones=[{"zone": 2, "products": [
                {"product_id": "P13", "class_id": 13, "unit_weight": 185.0,
                 "count": 1}]}],
        )
        report = analyze([clean, legacy, overbilled])
        bill = report["billing"]
        assert report["labeled"] == 3
        assert bill["labeled"] == 3 and bill["correct"] == 2
        assert bill["wrong"][0]["session"] == "ses-ghost"

    def test_session_dump_compact_vs_full(self):
        # 11차 정리: 승격(BOCPD 일치)·은퇴(0 드랍)·중복(생존 클래스) 필드는
        # 압축 덤프에서 접힌다 — 예외(mismatch·탈락·held)만 남는다.
        from crk_model.adapters.analyze_cli import render_session

        doc = _doc(triggers=[_trigger(
            vision_candidates=[
                {"class_id": 27, "confidence": 0.9, "vote_count": 30,
                 "vote_ratio": 0.3},
            ],
            trace={
                "vote_summary": {
                    "classes": {
                        27: {"votes": 30, "ratio": 0.3, "weighted_conf": 0.9,
                             "rejected_by": None},
                        3: {"votes": 4, "ratio": 0.01, "weighted_conf": 0.2,
                            "rejected_by": "share"},
                    },
                    "filter_drops_by_stage": {
                        "baseline": {"top": 0, "side": 0},  # 은퇴 — 숨김
                        "hand_path": {"top": 66, "side": 0},
                    },
                },
                # 폐기 shadow 기제의 구 아카이브 필드 — 덤프에 안 나와야 함
                "loadcell_shadow": {"analyzer": "plateau", "delta": -155.0,
                                    "primary_delta": -155.0, "mismatch": False},
                "likelihood_shadow": [
                    {"channel": 1, "mismatch": True,
                     "current": {"items": [[27, 1]], "score": -5.6},
                     "top": {"items": [[13, 1]], "score": -0.6}},
                ],
            },
        )])
        out = render_session(doc)
        assert "rejected: c3:4표(share)" in out
        assert "baseline" not in out and "hand_path" in out
        assert "loadcell_shadow" not in out  # 폐기 — 구 아카이브 필드 무시
        assert "likelihood" not in out
        full = render_session(doc, full=True)
        assert "vote_summary.classes" in full
        assert "loadcell_shadow" not in full

    def test_session_dump_renders_tube_diag(self):
        from crk_model.adapters.analyze_cli import render_session

        doc = _doc(triggers=[_trigger(trace={"vote_summary": {"tube_diag": {
            "by_class": {"13": {"votes": 8, "minority": 8, "tube_conf": 0.7}},
            "tubes": {"top": [{"obs": 30, "classes": {"13": 22, "24": 8}}]},
        }}})])
        out = render_session(doc)
        assert "tube_diag: c13:8표(소수8/tconf0.7)" in out
        assert "tube_diag.tubes" in out

    def test_old_archive_without_class_id_skipped_quietly(self):
        doc = _doc(
            ground_truth=_gt({"zone": 2, "class_id": 27, "count": 1}),
            triggers=[_trigger(judgment={
                "status": "complete",
                "products": [{"product_id": "P27", "count": 1}],  # 구 스키마
            })],
        )
        report = analyze([doc])  # 예외 없이 완료, 잔차 표본 없음
        assert report["unit_residual"]["samples"] == []


class TestYDirectionReport:
    @staticmethod
    def trigger(ys=(240, 180, 100, 50, 100, 180, 240), zone=2):
        evidence = MotionEvidence(direction_config=YDirectionConfig())
        for pos, cy in enumerate(ys):
            evidence.observe(
                "top", [Detection(1, 0.9, bbox=(50, cy - 20, 90, cy + 20))], pos=pos
            )
        return _trigger(
            zone=zone, ts=123.0, video_paths={"top": "/device/episode.avi"},
            trace={"vote_summary": {"motion_evidence": evidence.summary()}},
        )

    def test_round_trip_shown_without_interpreting_final_bill_as_action_label(self):
        doc = _doc(ground_truth=_gt(), triggers=[self.trigger()])
        original = deepcopy(doc)
        report = analyze([doc])
        yd = report["y_direction"]
        assert yd["triggers"] == 1 and yd["tracks"] == 1
        assert yd["by_camera"] == {"top": {"mixed": 1}}
        row, = yd["records"]
        assert (row["session"], row["zone"], row["ts"]) == ("ses-1", 2, 123.0)
        assert row["video_path"] == "/device/episode.avi"
        assert [s["direction"] for s in row["y_direction"]["segments"]] == ["return", "take"]
        assert "카메라별 취출·반납 방향 (진단·결제 미반영)" in render(report)
        out = render_session(doc)
        assert "top/c1/track0 mixed" in out
        assert "return: pos 0~3 y 240.0->50.0 Δy=-190.0px obs=4" in out
        assert "take: pos 3~6 y 50.0->240.0 Δy=190.0px obs=4" in out
        assert doc == original  # 보고서와 상세 출력은 아카이브를 바꾸지 않는다.

    def test_missing_diagnostic_differs_from_observed_unknown(self):
        report = analyze([_doc(triggers=[_trigger(), self.trigger(ys=(180, 181, 182))])])
        yd = report["y_direction"]
        assert yd["triggers"] == 1 and yd["unrecorded_triggers"] == 1
        assert yd["by_camera"] == {"top": {"unknown": 1}}
        assert "미기록은 정지/빈손 판정이 아님" in render(report)

    def test_oblique_side_axis_and_boundary_rendered(self):
        direction = summarize_direction(
            [(0, 240), (1, 200), (2, 160)],
            MotionDirectionConfig(axis="x", return_direction="decreasing", boundary_px=200),
        )
        trigger = _trigger(trace={"vote_summary": {"motion_evidence": {
            "side": {1: {"track_detail": [{
                "first": 0, "last": 2, "obs": 3, "motion_direction": direction,
            }]}},
        }}})
        doc = _doc(triggers=[trigger])
        report = analyze([doc])
        assert report["y_direction"]["by_camera"] == {"side": {"return_candidate": 1}}
        assert report["y_direction"]["records"][0]["y_direction"]["axis"] == "x"
        out = render_session(doc)
        assert "X축 후보: side/c1/track0 return_candidate" in out
        assert "x 240->160 Δx=-80px" in out
        assert "경계통과=True" in out
        assert "반납 방향=decreasing 경계=200px" in out

    def test_shared_video_is_explicitly_counted_as_trigger_observations(self):
        report = analyze([_doc(triggers=[self.trigger(), self.trigger(zone=3)])])
        yd = report["y_direction"]
        assert yd["triggers"] == 2 and yd["tracks"] == 2
        assert yd["by_camera"] == {"top": {"mixed": 2}}
        assert [r["trigger_index"] for r in yd["records"]] == [0, 1]
        assert "공유 영상 중복 포함·상품 수 아님" in render(report)

    def test_segment_limit_is_visible(self):
        trigger = self.trigger()
        detail = trigger["trace"]["vote_summary"]["motion_evidence"]["top"][1]["track_detail"]
        detail[0]["y_direction"]["segment_count"] = 10
        out = render_session(_doc(triggers=[trigger]))
        assert "구간 2/10개 기록 (나머지 생략)" in out

    def test_incomplete_diagnostic_ignored_as_unrecorded(self):
        trigger = self.trigger()
        detail = trigger["trace"]["vote_summary"]["motion_evidence"]["top"][1]["track_detail"]
        detail[0]["y_direction"] = None
        report = analyze([_doc(triggers=[trigger])])
        assert report["y_direction"]["unrecorded_triggers"] == 1
        assert report["y_direction"]["tracks"] == 0
        assert "Y축 후보" not in render_session(_doc(triggers=[trigger]))

    def test_cli_json_and_session_detail(self, tmp_path, capsys):
        day = tmp_path / "2026-11-10"
        day.mkdir()
        (day / "ses-y.json").write_text(json.dumps(
            _doc("ses-y", triggers=[self.trigger()])
        ), encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["y_direction"]["by_camera"] == {"top": {"mixed": 1}}
        assert main(["--dir", str(tmp_path), "--session", "ses-y"]) == 0
        assert "top/c1/track0 mixed" in capsys.readouterr().out


class TestCli:
    def test_end_to_end_json_archive(self, tmp_path, capsys):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        doc = _doc(triggers=[_trigger()])
        (day / "ses-1.json").write_text(json.dumps(doc), encoding="utf-8")
        assert main(["--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "세션 아카이브 리포트" in out and "세션 1개" in out

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
        assert "이후 1 세션" in out
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

    def _count_loads(self, monkeypatch):
        """analyze_cli._load_document 호출을 세는 래퍼 — 전량 로드 우회 검증."""
        import crk_model.adapters.analyze_cli as mod

        calls: list[str] = []
        real = mod._load_document

        def counting(path):
            calls.append(path.name)
            return real(path)

        monkeypatch.setattr(mod, "_load_document", counting)
        return calls

    def test_session_lookup_parses_only_target_file(self, tmp_path, capsys, monkeypatch):
        # 단건 조회가 O(아카이브 전체)로 늘어나던 원인 수정: --session은
        # find()로 해당 파일만 파싱한다 — SAVE_DETECTIONS 대형 YAML이 쌓여도
        # 다른 세션 파일은 열지 않는다.
        day = tmp_path / "2026-07-23"
        day.mkdir()
        for sid in ("ses-1", "ses-2", "ses-3"):
            (day / f"{sid}.json").write_text(
                json.dumps(_doc(sid, triggers=[_trigger()])), encoding="utf-8"
            )
        calls = self._count_loads(monkeypatch)
        assert main(["--dir", str(tmp_path), "--session", "ses-2"]) == 0
        assert calls == ["ses-2.json"]

    def test_session_lookup_broken_file_reports_error(self, tmp_path, capsys):
        day = tmp_path / "2026-07-23"
        day.mkdir()
        (day / "ses-x.json").write_text("{not json", encoding="utf-8")
        assert main(["--dir", str(tmp_path), "--session", "ses-x"]) == 1
        assert "파싱 실패" in capsys.readouterr().err

    def test_since_prefilter_skips_parsing_old_files(self, tmp_path, capsys, monkeypatch):
        # --since는 파일명 epoch(stem == session_id 계약)으로 파싱 전에
        # 거른다 — 대상 밖 대형 YAML의 로드 비용 자체를 없앤다.
        day = tmp_path / "2026-07-23"
        day.mkdir()
        for sid in ("ses-1-1784700000", "ses-2-1784800000"):
            (day / f"{sid}.json").write_text(
                json.dumps(_doc(sid, triggers=[_trigger()])), encoding="utf-8"
            )
        calls = self._count_loads(monkeypatch)
        assert main(["--dir", str(tmp_path), "--since", "1784750000"]) == 0
        assert calls == ["ses-2-1784800000.json"]
