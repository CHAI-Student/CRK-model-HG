"""analyze-sessions — 세션 아카이브 오프라인 실측 리포트 (research §6, 로드맵 단기 ②).

세 가지 질문에 아카이브(+ `label-session` 정답 라벨)만으로 답한다:

1. **shadow 정오** — BOCPD(loadcell_shadow)·무게 우도(likelihood_shadow)의
   mismatch 세션 목록과, 라벨이 있으면 "현행 판정 vs shadow 중 누가 맞았나"
   집계 (Phase 2 승격 게이트의 실측치).
2. **conformal 보정** — 라벨된 트리거에서 정답 상품의 투표 통계(votes/ratio/
   share/conf) 분위수 → 채택 임계(MIN_VOTE_*)의 근거 있는 제안값
   ("목표 재현율에서 역산" — 손튜닝 노브의 대체).
3. **σ_db 실측** — (delta, 정답 배정) 잔차의 개당 분포 →
   `MODEL__JUDGMENT__LIKELIHOOD_SIGMA_DB`·gate_n slack의 보정 입력.
4. **tray prior 개입** — likelihood shadow의 tray_prior가 score 1위를 실제로
   바꾼 entry(ranking의 log_p_tray를 빼면 무-prior 순위를 복원할 수 있다)와,
   라벨 대비 "prior 덕 정답 / prior 탓 오답" 집계 → penalty(기본 2.5) 보정.
5. **트랙릿 T1** — vote_summary.motion_evidence.track_detail의 트랙별
   head_obs 분포(정답 클래스 vs 비정답: held 강등 T2 임계 근거)와 클래스당
   트랙 수(단절/fragmentation → G2 재연관 창 도입 판단).
   docs/0723_tracklet_cost_benefit.md §8.

사용 예 (Jetson, 실험 후):

    analyze-sessions                      # data/sessions 전체 리포트
    analyze-sessions --dir data/sessions --json   # 기계 판독용

순수 stdlib(+아카이브가 YAML이면 PyYAML). 서비스 경로와 완전 분리된 읽기 전용
도구 — 판정·정산·아카이브 내용을 변경하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from crk_model.ledger.archive import SessionArchive, _load_document


def _quantiles(values: list[float]) -> dict:
    """소표본용 요약 — min/p5/p25/median/max (경험 분위수, 보간 없음)."""
    if not values:
        return {}
    s = sorted(values)
    n = len(s)

    def q(p: float) -> float:
        idx = min(n - 1, max(0, int(p * n)))
        return s[idx]

    return {
        "n": n,
        "min": s[0],
        "p5": q(0.05),
        "p25": q(0.25),
        "median": q(0.50),
        "max": s[-1],
    }


def _gt_items(doc: dict) -> list[dict]:
    gt = doc.get("ground_truth") or {}
    return list(gt.get("items") or [])


def _gt_multiset(items: list[dict], zone: int | None = None) -> list[tuple[int, int]]:
    """(class_id, count) 정렬 멀티셋 — class_id 없는(이름만) 항목은 제외."""
    out: dict[int, int] = {}
    for it in items:
        if zone is not None and it.get("zone") is not None and it["zone"] != zone:
            continue
        cid = it.get("class_id")
        if cid is None:
            continue
        out[int(cid)] = out.get(int(cid), 0) + int(it.get("count", 1))
    return sorted(out.items())


def _billed_multiset(products: list[dict]) -> list[tuple[int, int]] | None:
    """판정 products → (class_id, count) 멀티셋. class_id 미기록(구 아카이브)은
    None — 정오 판정 불가로 집계에서 제외한다."""
    out: dict[int, int] = {}
    for p in products:
        cid = p.get("class_id")
        if cid is None:
            return None
        out[int(cid)] = out.get(int(cid), 0) + int(p.get("count", 0))
    return sorted(out.items())


def _norm_items(items) -> list[tuple[int, int]]:
    """shadow ranking의 items([[cid, count], ...]) → 정렬 튜플 멀티셋."""
    return sorted((int(c), int(n)) for c, n in (items or []))


def _session_epoch(doc: dict) -> float | None:
    """세션 발생 시각 추정 — session_id 말미의 epoch(ses-1-1784790155),
    실패 시 파일 mtime. 코드 버전이 섞인 아카이브에서 '이 배포 이후'만
    골라내는 --since 필터의 기준이다 (finalized_at은 monotonic clock이라
    벽시계 비교에 못 쓴다)."""
    sid = str(doc.get("session_id") or "")
    tail = sid.rsplit("-", 1)[-1]
    if tail.isdigit() and len(tail) >= 9:  # epoch초(10자리대)만 신뢰
        return float(tail)
    path = doc.get("_path")
    if path:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None
    return None


def parse_since(raw: str) -> float:
    """--since 값 파싱: epoch 초 또는 ISO 날짜/일시("2026-07-23",
    "2026-07-23T21:00" — 로컬 시간)."""
    try:
        return float(raw)
    except ValueError:
        pass
    import datetime

    return datetime.datetime.fromisoformat(raw).timestamp()


def load_documents(archive_dir: str | Path) -> list[dict]:
    root = Path(archive_dir)
    if not root.exists():
        return []
    docs = []
    for date_dir in sorted(c for c in root.iterdir() if c.is_dir()):
        for path in sorted(date_dir.iterdir()):
            if path.suffix not in (".yaml", ".json"):
                continue
            try:
                doc = _load_document(path)
            except Exception as exc:  # noqa: BLE001 — 손상 파일은 건너뛰고 보고
                docs.append({"_path": str(path), "_load_error": type(exc).__name__})
                continue
            if isinstance(doc, dict):
                doc["_path"] = str(path)
                docs.append(doc)
    return docs


def analyze(docs: list[dict]) -> dict:
    report: dict = {
        "sessions": 0,
        "load_errors": [],
        "by_status": {},
        "labeled": 0,
        "bocpd": {"observed": 0, "mismatches": []},
        "likelihood": {
            "observed": 0,
            "mismatches": [],
            # 라벨 대비 정오 (Phase 2 승격 게이트): mismatch 트리거 한정 —
            # 일치 트리거는 양쪽이 같아 비교 정보가 없다.
            "labeled_eval": {"score_correct": 0, "current_correct": 0, "both_wrong": 0},
        },
        "calibration": {
            "true_candidate": {"votes": [], "ratio": [], "share": [], "conf": []},
            "missing_from_candidates": [],
        },
        "sigma_db": {"unit_residuals": []},
        # 과금 정오 총괄: 라벨된 세션의 최종 확정(zones products) vs GT.
        # shadow mismatch와 달리 "현행 판정이 결국 맞게 청구했는가"의 헤드라인.
        "billing": {"labeled": 0, "correct": 0, "unknown_schema": 0, "wrong": []},
        # tray prior 개입 (ledger/tray_memory.py Phase 1): observed = prior가
        # 실린 entry 수, flips = prior가 score 1위를 바꾼 entry (ranking에서
        # score − log_p_tray로 무-prior 순위 복원 — ranking이 상위 5로
        # 잘려 있어 근사이지만 후보 풀이 작아 실용상 충분).
        "tray_prior": {
            "observed": 0,
            "flips": [],
            # 라벨 정오는 단일 entry 트리거 한정 — 멀티트레이 entry는 채널
            # 단위라 존 GT와 직접 비교가 성립하지 않는다.
            "labeled_eval": {"prior_helped": 0, "prior_hurt": 0, "both_wrong": 0},
        },
        # 트랙릿 T1 (docs/0723_tracklet_cost_benefit.md §8): head_obs 분포와
        # 클래스당 트랙 수 — held 강등(T2) 임계·재연관 창(G2) 판단 입력.
        # 7차 실측 보정: 저신뢰 플리커 검출(entry 컷 미달도 트랙은 생성)이
        # 1~2관측 잔트랙을 대량 생산해 원지표를 잠식했다(비정답 n=1136,
        # median 0, 단절 의심 203건 범람) — ① 트랙 수는 실질 트랙(obs≥3)만,
        # ② head_obs는 이동(passed) 트랙만(정답 클래스의 진열 인스턴스
        # 정지 트랙도 함께 배제됨), ③ 동일 세션에서 에피소드 병합 영상을
        # 공유하는 존 트리거들의 중복 계수는 detail 동일성으로 제거.
        "tracklet": {
            "triggers": 0,
            "tracks_per_class": [],  # 실질(obs≥3) 트랙 수 / 카메라×클래스
            "gt_head_obs": [],  # 이동 트랙 한정
            "non_gt_head_obs": [],
            "fragmented": [],  # 실질 트랙 ≥ 4 — 단절 의심 (상위만 렌더)
        },
    }
    tracklet_seen: set = set()  # 공유 영상 중복 제거 키
    for doc in docs:
        if "_load_error" in doc:
            report["load_errors"].append(
                {"path": doc.get("_path"), "error": doc["_load_error"]}
            )
            continue
        report["sessions"] += 1
        status = doc.get("status", "?")
        report["by_status"][status] = report["by_status"].get(status, 0) + 1
        gt_items = _gt_items(doc)
        if gt_items:
            report["labeled"] += 1
        sid = doc.get("session_id", "?")

        if gt_items:
            self_billing = report["billing"]
            gt_zones = sorted(
                {it["zone"] for it in gt_items if it.get("zone") is not None}
            )
            billed_by_zone: dict[int, list[tuple[int, int]] | None] = {}
            for z in doc.get("zones") or []:
                billed_by_zone[z.get("zone")] = _billed_multiset(
                    z.get("products") or []
                )
            if any(v is None for v in billed_by_zone.values()):
                self_billing["unknown_schema"] += 1  # 구 아카이브 — 판정 불가
            else:
                self_billing["labeled"] += 1
                diffs = []
                for zone in sorted(
                    set(gt_zones) | set(billed_by_zone.keys())
                ):
                    gt_z = _gt_multiset(gt_items, zone)
                    billed_z = billed_by_zone.get(zone) or []
                    if gt_z != billed_z:
                        diffs.append(
                            {"zone": zone, "ground_truth": gt_z, "billed": billed_z}
                        )
                if diffs:
                    self_billing["wrong"].append({"session": sid, "diffs": diffs})
                else:
                    self_billing["correct"] += 1

        for trig in doc.get("triggers") or []:
            zone = trig.get("zone")
            trace = trig.get("trace") or {}
            delta = float(trig.get("delta_weight") or 0.0)
            gt_zone = _gt_multiset(gt_items, zone) if gt_items else []

            sh = trace.get("loadcell_shadow")
            if isinstance(sh, dict) and "delta" in sh:
                report["bocpd"]["observed"] += 1
                if sh.get("mismatch"):
                    report["bocpd"]["mismatches"].append({
                        "session": sid,
                        "zone": zone,
                        "primary_delta": sh.get("primary_delta"),
                        "primary_reason": sh.get("primary_reason"),
                        "shadow_delta": sh.get("delta"),
                        "delta_std": sh.get("delta_std"),
                    })

            entries = trace.get("likelihood_shadow") or []
            entries = [e for e in entries if isinstance(e, dict) and "top" in e]
            if entries:
                report["likelihood"]["observed"] += len(entries)
                mismatched = [e for e in entries if e.get("mismatch")]
                if mismatched:
                    # 트리거 단위 병합: 멀티트레이는 채널별 entry의 합이
                    # 존 GT와 비교 단위다.
                    cur_agg: dict[int, int] = {}
                    top_agg: dict[int, int] = {}
                    for e in entries:
                        for cid, cnt in (e.get("current") or {}).get("items") or []:
                            cur_agg[int(cid)] = cur_agg.get(int(cid), 0) + int(cnt)
                        for cid, cnt in (e.get("top") or {}).get("items") or []:
                            top_agg[int(cid)] = top_agg.get(int(cid), 0) + int(cnt)
                    record = {
                        "session": sid,
                        "zone": zone,
                        "delta": delta,
                        "current": sorted(cur_agg.items()),
                        "score_top": sorted(top_agg.items()),
                    }
                    if gt_zone:
                        cur_ok = sorted(cur_agg.items()) == gt_zone
                        top_ok = sorted(top_agg.items()) == gt_zone
                        record["ground_truth"] = gt_zone
                        record["current_correct"] = cur_ok
                        record["score_correct"] = top_ok
                        ev = report["likelihood"]["labeled_eval"]
                        if top_ok and not cur_ok:
                            ev["score_correct"] += 1
                        elif cur_ok and not top_ok:
                            ev["current_correct"] += 1
                        elif not cur_ok and not top_ok:
                            ev["both_wrong"] += 1
                    report["likelihood"]["mismatches"].append(record)

                # tray prior 개입 (report 키 주석 참조): entry별 무-prior 순위
                # 복원 후 1위가 달라진 것만 flip으로 집계
                tp = report["tray_prior"]
                for e in entries:
                    prior = e.get("tray_prior")
                    if not prior:
                        continue
                    tp["observed"] += 1
                    ranking = e.get("ranking") or []
                    if not ranking:
                        continue
                    top_with = ranking[0]  # scored 정렬 보존 (shadow 계약)
                    top_wo = max(
                        ranking,
                        key=lambda r: (r.get("score") or 0.0)
                        - (r.get("log_p_tray") or 0.0),
                    )
                    with_items = _norm_items(top_with.get("items"))
                    wo_items = _norm_items(top_wo.get("items"))
                    if with_items == wo_items:
                        continue
                    rec = {
                        "session": sid,
                        "zone": zone,
                        "channel": e.get("channel"),
                        "prior": prior,
                        "with_prior": with_items,
                        "without_prior": wo_items,
                    }
                    if gt_zone and len(entries) == 1:
                        with_ok = with_items == gt_zone
                        wo_ok = wo_items == gt_zone
                        rec["ground_truth"] = gt_zone
                        rec["prior_correct"] = with_ok
                        ev = tp["labeled_eval"]
                        if with_ok and not wo_ok:
                            ev["prior_helped"] += 1
                        elif wo_ok and not with_ok:
                            ev["prior_hurt"] += 1
                        elif not with_ok and not wo_ok:
                            ev["both_wrong"] += 1
                    tp["flips"].append(rec)

            # 트랙릿 T1 (report 키 주석 참조) — 라벨 없이도 트랙 수 분포는
            # 집계, head_obs의 GT 분리 실측은 라벨 트리거 한정
            me = (trace.get("vote_summary") or {}).get("motion_evidence") or {}
            if isinstance(me, dict):
                tk = report["tracklet"]
                gt_ids = {cid for cid, _ in gt_zone}
                saw_detail = False
                for camera, classes in me.items():
                    if not isinstance(classes, dict):
                        continue
                    for cid, info in classes.items():
                        detail = (info or {}).get("track_detail")
                        if detail is None:
                            continue  # 구 아카이브 (T1 이전) — 조용히 제외
                        saw_detail = True
                        # 공유 영상 중복 제거 (report 키 주석 ③): 에피소드
                        # 병합으로 같은 영상을 받은 형제 존 트리거의 detail은
                        # 완전 동일 — 세션 내에서 한 번만 계수한다.
                        key = (
                            sid,
                            camera,
                            int(cid),
                            tuple(
                                (t.get("first"), t.get("last"), t.get("obs"))
                                for t in detail
                            ),
                        )
                        if key in tracklet_seen:
                            continue
                        tracklet_seen.add(key)
                        substantial = [
                            t for t in detail if int(t.get("obs") or 0) >= 3
                        ]
                        tk["tracks_per_class"].append(float(len(substantial)))
                        if len(substantial) >= 4:
                            tk["fragmented"].append({
                                "session": sid,
                                "zone": zone,
                                "camera": camera,
                                "class_id": int(cid),
                                "tracks": len(substantial),
                            })
                        if gt_zone:
                            bucket = (
                                "gt_head_obs"
                                if int(cid) in gt_ids
                                else "non_gt_head_obs"
                            )
                            for t in substantial:
                                # 이동(passed) 트랙만 — T2(held 강등)의 모집단.
                                # 정답 클래스의 진열 인스턴스(정지)도 배제된다.
                                if int(t.get("first", -1)) >= 0 and t.get("passed"):
                                    tk[bucket].append(float(t.get("head_obs") or 0))
                if saw_detail:
                    tk["triggers"] += 1

            if not gt_zone:
                continue

            # conformal 보정 소재: 정답 class가 최종 후보에 남았는가 + 통계
            cands = {
                int(c["class_id"]): c for c in trig.get("vision_candidates") or []
            }
            top_votes = max(
                (int(c.get("vote_count") or 0) for c in cands.values()), default=0
            )
            for cid, _count in gt_zone:
                c = cands.get(cid)
                if c is None:
                    report["calibration"]["missing_from_candidates"].append(
                        {"session": sid, "zone": zone, "class_id": cid}
                    )
                    continue
                cal = report["calibration"]["true_candidate"]
                cal["votes"].append(float(c.get("vote_count") or 0))
                cal["ratio"].append(float(c.get("vote_ratio") or 0.0))
                cal["conf"].append(float(c.get("confidence") or 0.0))
                if top_votes > 0:
                    cal["share"].append(float(c.get("vote_count") or 0) / top_votes)

            # σ_db 실측: 단일 정체성 GT + removal delta + unit_weight 기록 시
            if len(gt_zone) == 1 and delta < 0:
                cid, count = gt_zone[0]
                weight = None
                for p in (trig.get("judgment") or {}).get("products") or []:
                    if p.get("class_id") == cid and p.get("unit_weight"):
                        weight = float(p["unit_weight"])
                        break
                if weight is None:
                    for z in doc.get("zones") or []:
                        for p in z.get("products") or []:
                            if p.get("class_id") == cid and p.get("unit_weight"):
                                weight = float(p["unit_weight"])
                                break
                if weight is not None and count > 0:
                    unit_r = (abs(delta) - count * weight) / count
                    report["sigma_db"]["unit_residuals"].append(round(unit_r, 2))

    # 요약 통계로 마감
    cal = report["calibration"]["true_candidate"]
    report["calibration"]["quantiles"] = {
        k: _quantiles(v) for k, v in cal.items() if v
    }
    residuals = report["sigma_db"]["unit_residuals"]
    if residuals:
        n = len(residuals)
        mean = sum(residuals) / n
        var = sum((r - mean) ** 2 for r in residuals) / n
        report["sigma_db"]["mean"] = round(mean, 2)
        report["sigma_db"]["std"] = round(var**0.5, 2)
        # 우도 σ_db 제안: 편향 포함 RMS — 잔차의 "전형적 크기"
        rms = (sum(r * r for r in residuals) / n) ** 0.5
        report["sigma_db"]["suggested_sigma_db"] = round(rms, 2)
    tk = report["tracklet"]
    tk["quantiles"] = {
        k: _quantiles(tk[k])
        for k in ("tracks_per_class", "gt_head_obs", "non_gt_head_obs")
        if tk[k]
    }
    return report


def render(report: dict) -> str:
    lines: list[str] = []
    lines.append("=== 세션 아카이브 리포트 ===")
    lines.append(
        f"세션 {report['sessions']}개 (라벨 {report['labeled']}개), "
        f"상태별 {report['by_status']}"
    )
    if report["load_errors"]:
        lines.append(f"읽기 실패 {len(report['load_errors'])}건: "
                     + ", ".join(e["path"] for e in report["load_errors"]))

    bill = report["billing"]
    if bill["labeled"] or bill["unknown_schema"]:
        lines.append("")
        lines.append("--- 과금 정오 (라벨 대비 최종 확정) ---")
        lines.append(
            f"정답 {bill['correct']}/{bill['labeled']} 세션"
            + (
                f" (구 스키마로 판정 불가 {bill['unknown_schema']}건)"
                if bill["unknown_schema"]
                else ""
            )
        )
        for w in bill["wrong"]:
            for d in w["diffs"]:
                lines.append(
                    f"  ✗ {w['session']} zone{d['zone']}: "
                    f"과금 {d['billed']} ← 정답 {d['ground_truth']}"
                )

    b = report["bocpd"]
    lines.append("")
    lines.append(f"--- BOCPD shadow (관측 {b['observed']}건) ---")
    if not b["mismatches"]:
        lines.append("mismatch 없음")
    for m in b["mismatches"]:
        lines.append(
            f"  {m['session']} zone{m['zone']}: primary {m['primary_delta']}g "
            f"({m['primary_reason'] or '-'}) vs shadow {m['shadow_delta']}g "
            f"±{m['delta_std']}"
        )

    lk = report["likelihood"]
    lines.append("")
    lines.append(f"--- 무게 우도 shadow (관측 {lk['observed']}건) ---")
    if not lk["mismatches"]:
        lines.append("mismatch 없음")
    for m in lk["mismatches"]:
        gt = m.get("ground_truth")
        verdict = ""
        if gt is not None:
            verdict = (
                f"  [GT {gt} → score {'O' if m['score_correct'] else 'X'} / "
                f"현행 {'O' if m['current_correct'] else 'X'}]"
            )
        lines.append(
            f"  {m['session']} zone{m['zone']} Δ{m['delta']}g: "
            f"현행 {m['current']} vs score 1위 {m['score_top']}{verdict}"
        )
    ev = lk["labeled_eval"]
    if any(ev.values()):
        lines.append(
            f"  라벨 정오 (mismatch 한정): score만 정답 {ev['score_correct']} / "
            f"현행만 정답 {ev['current_correct']} / 둘 다 오답 {ev['both_wrong']}"
        )
        lines.append("  → Phase 2 승격 게이트: score만 정답이 우세할 때만 진행")

    tp = report["tray_prior"]
    if tp["observed"]:
        lines.append("")
        lines.append(
            f"--- tray prior shadow (개입 {tp['observed']} entry, "
            f"1위 변경 {len(tp['flips'])}건) ---"
        )
        for f in tp["flips"]:
            ch = f" ch{f['channel']}" if f.get("channel") is not None else ""
            verdict = ""
            if "prior_correct" in f:
                verdict = (
                    f"  [GT {f['ground_truth']} → prior "
                    f"{'O' if f['prior_correct'] else 'X'}]"
                )
            lines.append(
                f"  {f['session']} zone{f['zone']}{ch}: prior {f['prior']} — "
                f"무-prior 1위 {f['without_prior']} → {f['with_prior']}{verdict}"
            )
        pev = tp["labeled_eval"]
        if any(pev.values()):
            lines.append(
                f"  라벨 정오 (단일 entry 한정): prior 덕 정답 "
                f"{pev['prior_helped']} / prior 탓 오답 {pev['prior_hurt']} / "
                f"둘 다 오답 {pev['both_wrong']}"
            )
            lines.append(
                "  → PENALTY(2.5) 보정: hurt > 0이면 완화 검토, "
                "helped 우세 지속 시 Phase 2 근거"
            )

    tk = report["tracklet"]
    if tk["triggers"]:
        lines.append("")
        lines.append(
            f"--- 트랙릿 T1 (track_detail 관측 트리거 {tk['triggers']}개) ---"
        )
        tq = tk.get("quantiles") or {}
        if tq.get("tracks_per_class"):
            q = tq["tracks_per_class"]
            lines.append(
                f"  실질(obs≥3) 트랙/클래스: n={q['n']} median={q['median']:.3g} "
                f"max={q['max']:.3g} — max가 물리 인스턴스 수를 넘으면 단절"
            )
        if tk["fragmented"]:
            worst = sorted(tk["fragmented"], key=lambda f: -f["tracks"])[:12]
            more = len(tk["fragmented"]) - len(worst)
            lines.append(
                f"  단절 의심(실질 트랙 ≥ 4) {len(tk['fragmented'])}건"
                f" (상위 {len(worst)}): "
                + ", ".join(
                    f"{f['session']}/z{f['zone']}/{f['camera']}/c{f['class_id']}"
                    f"({f['tracks']})"
                    for f in worst
                )
                + (f" 외 {more}건" if more > 0 else "")
            )
            lines.append("  → 빈발 시 재연관 창(G2, 0723 문서 §2) 도입")
        for key, label in (
            ("gt_head_obs", "정답 클래스"),
            ("non_gt_head_obs", "비정답(배경·held)"),
        ):
            if tq.get(key):
                q = tq[key]
                lines.append(
                    f"  head_obs(이동 트랙 한정) {label}: n={q['n']} "
                    f"median={q['median']:.3g} max={q['max']:.3g}"
                )
        if tq.get("gt_head_obs") and tq.get("non_gt_head_obs"):
            lines.append(
                "  → 두 분포가 분리되면 held 트랙 강등(T2) 임계 확정 가능 "
                "(0713 §10의 트랙 단위 재실측)"
            )

    lines.append("")
    lines.append("--- conformal 보정 (라벨된 정답 상품의 후보 통계) ---")
    q = report["calibration"].get("quantiles") or {}
    if not q:
        lines.append("라벨된 세션 없음 — label-session으로 정답 기입 후 재실행")
    for stat, qs in q.items():
        lines.append(
            f"  {stat}: n={qs['n']} min={qs['min']:.3g} p5={qs['p5']:.3g} "
            f"p25={qs['p25']:.3g} median={qs['median']:.3g} max={qs['max']:.3g}"
        )
    if q:
        lines.append(
            "  제안: 채택 임계(MIN_VOTE_RATIO/SHARE 등)는 p5 이하로 — "
            "정답 상품 95%가 후보에 남는 하한"
        )
    missing = report["calibration"]["missing_from_candidates"]
    if missing:
        lines.append(
            f"  ⚠ 정답 상품이 최종 후보에 없던 트리거 {len(missing)}건: "
            + ", ".join(
                f"{m['session']}/z{m['zone']}/c{m['class_id']}" for m in missing
            )
        )

    sd = report["sigma_db"]
    lines.append("")
    lines.append("--- σ_db 실측 (개당 잔차 = (|Δ| − n·w)/n) ---")
    if not sd["unit_residuals"]:
        lines.append("표본 없음 (단일 정체성 라벨 + removal + unit_weight 기록 필요)")
    else:
        lines.append(
            f"  n={len(sd['unit_residuals'])} mean={sd['mean']} std={sd['std']} "
            f"→ MODEL__JUDGMENT__LIKELIHOOD_SIGMA_DB 제안 {sd['suggested_sigma_db']}"
        )
    return "\n".join(lines)


def _fmt_products(products: list[dict]) -> str:
    if not products:
        return "-"
    return ", ".join(
        f"{p.get('class_id', p.get('name'))}x{p.get('count')}" for p in products
    )


def render_session(doc: dict) -> str:
    """세션 1건의 오판정 사후 분석 덤프 — YAML을 직접 뒤지지 않아도 판정
    전략·득표·탈락 사유·shadow까지 한 화면으로 재구성한다 (--session)."""
    lines = [f"=== {doc.get('session_id')} ({doc.get('status')}) ==="]
    gt = doc.get("ground_truth")
    if gt:
        items = ", ".join(
            f"z{i.get('zone')}:{i.get('class_id', i.get('name'))}x{i.get('count')}"
            for i in gt.get("items") or []
        )
        lines.append(f"GT: {items}  note={gt.get('note', '')}")
    if doc.get("notes"):
        lines.append(f"정산 notes: {doc['notes']}")
    for z in doc.get("zones") or []:
        lines.append(
            f"zone{z.get('zone')} 확정: {_fmt_products(z.get('products') or [])} "
            f"(Δ{z.get('weight_delta')}g, notes={z.get('notes')})"
        )
    for t in doc.get("triggers") or []:
        j = t.get("judgment") or {}
        lines.append(f"-- trigger zone{t.get('zone')} Δ{t.get('delta_weight')}g")
        segs = t.get("segments") or []
        if segs:
            lines.append(
                "   segments: "
                + ", ".join(f"{s.get('delta_grams')}g" for s in segs)
            )
        lines.append(
            f"   judgment: {j.get('status')} strategy={j.get('strategy')} "
            f"reason={j.get('reason')} conf={round(j.get('confidence') or 0, 3)}"
        )
        lines.append(f"   billed: {_fmt_products(j.get('products') or [])}")
        cands = t.get("vision_candidates") or []
        if cands:
            top = sorted(cands, key=lambda c: -(c.get("vote_count") or 0))[:8]

            def _cand_str(c: dict) -> str:
                s = (
                    f"c{c.get('class_id')}:{c.get('vote_count')}표"
                    f"/conf{round(c.get('confidence') or 0, 2)}"
                )
                # held-object A-1 신호 (0713 §3): head↑·span≈1이면 carried-in
                if c.get("span_ratio"):
                    s += f"/head{c.get('head_votes')}/span{c.get('span_ratio')}"
                return s

            lines.append("   candidates: " + ", ".join(_cand_str(c) for c in top))
        trace = t.get("trace") or {}
        if trace.get("reason_codes"):
            lines.append(f"   reason_codes: {trace['reason_codes']}")
        vs = trace.get("vote_summary") or {}
        if vs.get("classes"):
            lines.append(f"   vote_summary.classes: {vs['classes']}")
        for key in (
            "filter_drops_by_stage",
            "entry_dropped_by_camera",
            "motion_evidence",  # T1: track_detail 포함 — held/단절 사후 분석
        ):
            if vs.get(key):
                lines.append(f"   vote_summary.{key}: {vs[key]}")
        for key in ("loadcell_shadow", "likelihood_shadow"):
            if trace.get(key):
                lines.append(f"   {key}: {trace[key]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze-sessions",
        description="세션 아카이브 shadow 정오·conformal 보정 리포트",
    )
    parser.add_argument(
        "--dir", default="data/sessions", help="아카이브 루트 (기본: data/sessions)"
    )
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    parser.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="세션 1건 상세 덤프 (판정 전략·득표·탈락 사유·shadow)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="EPOCH|ISO일시",
        help=(
            "이 시각 이후 세션만 집계 (예: --since 2026-07-23T21:00 — 배포/"
            "튜닝 변경 이후만 평가할 때. 세션 id 말미 epoch 기준, 없으면 mtime)"
        ),
    )
    args = parser.parse_args(argv)

    archive = SessionArchive(args.dir)
    if not archive.enabled:
        print("아카이브 디렉토리가 지정되지 않았습니다", file=sys.stderr)
        return 1
    docs = load_documents(args.dir)
    if not docs:
        print(f"아카이브가 비어 있습니다: {args.dir}", file=sys.stderr)
        return 1
    if args.since:
        try:
            cutoff = parse_since(args.since)
        except ValueError:
            print(f"--since 형식 오류: {args.since}", file=sys.stderr)
            return 1
        total = len(docs)
        docs = [
            d for d in docs
            if (ep := _session_epoch(d)) is not None and ep >= cutoff
        ]
        if not docs:
            print(f"--since {args.since} 이후 세션이 없습니다", file=sys.stderr)
            return 1
        print(f"(대상: --since {args.since} 이후 {len(docs)}/{total} 세션)")
    if args.session:
        matches = [d for d in docs if d.get("session_id") == args.session]
        if not matches:
            print(f"세션을 찾을 수 없습니다: {args.session}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(matches[0], ensure_ascii=False, indent=2, default=str))
        else:
            print(render_session(matches[0]))
        return 0
    report = analyze(docs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
