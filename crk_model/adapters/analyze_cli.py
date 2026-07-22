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
    }
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze-sessions",
        description="세션 아카이브 shadow 정오·conformal 보정 리포트",
    )
    parser.add_argument(
        "--dir", default="data/sessions", help="아카이브 루트 (기본: data/sessions)"
    )
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = parser.parse_args(argv)

    archive = SessionArchive(args.dir)
    if not archive.enabled:
        print("아카이브 디렉토리가 지정되지 않았습니다", file=sys.stderr)
        return 1
    docs = load_documents(args.dir)
    if not docs:
        print(f"아카이브가 비어 있습니다: {args.dir}", file=sys.stderr)
        return 1
    report = analyze(docs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
