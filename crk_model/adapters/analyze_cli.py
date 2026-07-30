"""analyze-sessions — 세션 아카이브 오프라인 실측 리포트.

네 가지 질문에 아카이브(+ `label-session` 정답 라벨)만으로 답한다:

1. **과금 정오** — 라벨된 세션의 최종 확정(존별 products) vs 정답. 이 리포트의
   헤드라인 지표다.
2. **conformal 보정** — 라벨된 트리거에서 정답 상품의 투표 통계(votes/ratio/
   share/conf) 분위수 → 채택 임계(MIN_VOTE_*)의 근거 있는 제안값
   ("목표 재현율에서 역산" — 손튜닝 노브의 대체).
3. **개당 잔차 실측** — (delta, 정답 배정) 잔차의 개당 분포 → 개수 게이트
   가산(`MODEL__JUDGMENT__COUNT_UNIT_SLACK`)의 보정 입력.
4. **승격 대기 shadow 정오** — held 트랙 강등(`HELD_TRACK_DEMOTION`)과 세션
   고스트 원장(`MODEL__GHOST__MODE`)의 관측을 라벨과 대조한다. 두 기제의
   승격 게이트는 "정답 클래스 오플래그 0"이다. 폐기된 shadow 기제(무게 우도·
   tray prior·튜브 다수결·표 회수·BOCPD)의 구 아카이브 필드는 무시된다
   (docs/07-rejected-and-retired.md).

부가: 트랙릿 T1 계측 — track_detail의 head_obs 분포와 클래스당 트랙 수
(단절/fragmentation 감시).

사용 예 (Jetson, 실험 후):

    analyze-sessions                      # data/sessions 전체 리포트
    analyze-sessions --dir data/sessions --json   # 기계 판독용

순수 stdlib(+아카이브가 YAML이면 PyYAML). 서비스 경로와 완전 분리된 읽기 전용
도구 — 판정·정산·아카이브 내용을 변경하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
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
    """GT 항목 — class_id 0은 "무취출" 마커로 간주해 제외한다 (10차 ses-12:
    --take가 필수라 제스처-온리 세션을 0x1로 우회 기입 → 과금 []가 오답으로
    집계됐다. label-session --none이 정식 경로, 0 필터는 기존 라벨 호환)."""
    gt = doc.get("ground_truth") or {}
    return [it for it in (gt.get("items") or []) if it.get("class_id") != 0]


def _labeled(doc: dict) -> bool:
    """라벨 여부 — 항목이 비어도(무취출 GT) ground_truth 블록이 있으면 라벨.
    무취출 세션은 "청구 0이어야 정답"으로 과금 정오에 그대로 참여한다."""
    return doc.get("ground_truth") is not None


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


def _path_epoch(path: Path) -> float | None:
    """파싱 없이 파일만으로 세션 시각 추정 — 파일명 stem == session_id
    (SessionArchive._write 계약)이므로 _session_epoch과 같은 규칙을
    파일명에 적용한다. --since 프리필터용 (대상 밖 대형 YAML의 파싱 자체를
    건너뛰는 게 목적)."""
    tail = path.stem.rsplit("-", 1)[-1]
    if tail.isdigit() and len(tail) >= 9:  # epoch초(10자리대)만 신뢰
        return float(tail)
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def load_documents(
    archive_dir: str | Path, *, since: float | None = None
) -> list[dict]:
    """아카이브 전량 로드. since가 주어지면 파일명 epoch 기준으로 그 이전
    세션의 **파싱을 건너뛴다** — SAVE_DETECTIONS 세션은 수백 KB짜리 YAML이라
    파싱이 지배 비용이다 (시각 미상 파일은 보수적으로 파싱, doc 레벨
    --since 필터가 최종 판정)."""
    root = Path(archive_dir)
    if not root.exists():
        return []
    docs = []
    for date_dir in sorted(c for c in root.iterdir() if c.is_dir()):
        for path in sorted(date_dir.iterdir()):
            if path.suffix not in (".yaml", ".json"):
                continue
            if since is not None:
                ep = _path_epoch(path)
                if ep is not None and ep < since:
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
        "calibration": {
            "true_candidate": {"votes": [], "ratio": [], "share": [], "conf": []},
            "missing_from_candidates": [],
        },
        # 개당 잔차 실측: 개수 게이트 가산(COUNT_UNIT_SLACK)의 보정 입력.
        "unit_residual": {"samples": []},
        # 과금 정오 총괄: 라벨된 세션의 최종 확정(zones products) vs GT.
        # shadow mismatch와 달리 "현행 판정이 결국 맞게 청구했는가"의 헤드라인.
        "billing": {"labeled": 0, "correct": 0, "unknown_schema": 0, "wrong": []},
        # 트랙릿 T1 (docs/devdoc/design/0723_tracklet_cost_benefit.md §8): head_obs 분포와
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
            # T2 held 강등 관측 (vote_summary.held_shadow): 정답 클래스에
            # held 플래그가 서면 active 승격 보류 신호 (진짜 취출 표를 깎을
            # 뻔한 사례 — 0713 S1 계열), 비정답 건수는 강등의 기대 효과.
            "held_gt_flags": [],
            "held_non_gt": 0,
        },
        # 세션 고스트 원장 shadow (ledger/ghost_ledger.py, 0723 이슈 #17 P1):
        # 정산 notes의 ghost_classes/ghost_shadow를 집계 — MODEL__GHOST__MODE
        # =active 승격 게이트. gt_flagged(정답 클래스 오플래그)는 승격 보류
        # 신호 (held_gt_flags와 동일 절차).
        "ghost": {
            "observed": 0,
            "gt_flagged": [],
            "shadow": [],
            "labeled_eval": {
                "shadow_correct": 0,
                "current_correct": 0,
                "both_wrong": 0,
            },
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
        labeled = _labeled(doc)
        if labeled:
            report["labeled"] += 1
        sid = doc.get("session_id", "?")

        if labeled:
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

        # 세션 고스트 원장 shadow — 정산 notes에서 검출·시뮬레이션을 읽는다
        session_notes = [n for n in (doc.get("notes") or []) if isinstance(n, str)]
        gnote = next(
            (n for n in session_notes if n.startswith("ghost_classes:")), None
        )
        if gnote is not None:
            gh = report["ghost"]
            gh["observed"] += 1
            ghost_cids = {int(m) for m in re.findall(r"class(\d+)", gnote)}
            if labeled:
                gt_cids = {
                    int(it["class_id"])
                    for it in gt_items
                    if it.get("class_id") is not None
                }
                flagged = sorted(ghost_cids & gt_cids)
                if flagged:
                    gh["gt_flagged"].append({"session": sid, "classes": flagged})
            for n in session_notes:
                m = re.match(r"zone(\d+):ghost_shadow:billed=([^:]+):would=(.+)", n)
                if not m:
                    continue
                zone_n = int(m.group(1))
                would_raw = m.group(3)
                rec: dict = {
                    "session": sid,
                    "zone": zone_n,
                    "billed_ghost": m.group(2),
                    "would": would_raw,
                }
                if labeled and would_raw != "keep_original":
                    zdoc = next(
                        (
                            z
                            for z in doc.get("zones") or []
                            if z.get("zone") == zone_n
                        ),
                        None,
                    )
                    billed_z = (
                        _billed_multiset(zdoc.get("products") or [])
                        if zdoc
                        else []
                    )
                    if billed_z is not None:
                        gt_z = _gt_multiset(gt_items, zone_n)
                        would_items = sorted(
                            (int(c), int(k))
                            for c, k in re.findall(r"class(\d+)x(\d+)", would_raw)
                        )
                        # 주의: would는 트리거 재판정, billed는 존 최종 확정 —
                        # 수준이 섞인 근사 비교다 (근사 캐비앳).
                        rec["ground_truth"] = gt_z
                        rec["shadow_correct"] = would_items == gt_z
                        rec["current_correct"] = billed_z == gt_z
                        lv = gh["labeled_eval"]
                        if rec["shadow_correct"] and not rec["current_correct"]:
                            lv["shadow_correct"] += 1
                        elif rec["current_correct"] and not rec["shadow_correct"]:
                            lv["current_correct"] += 1
                        elif not rec["current_correct"]:
                            lv["both_wrong"] += 1
                gh["shadow"].append(rec)

        for trig in doc.get("triggers") or []:
            zone = trig.get("zone")
            trace = trig.get("trace") or {}
            delta = float(trig.get("delta_weight") or 0.0)
            gt_zone = _gt_multiset(gt_items, zone) if gt_items else []

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

            # T2 held 강등 관측 (report 키 주석 참조) — 라벨 없는 트리거의
            # held는 정오를 가릴 수 없어 비정답 쪽으로 세지 않고 건너뛴다
            held = (trace.get("vote_summary") or {}).get("held_shadow") or {}
            if isinstance(held, dict) and gt_zone:
                tk = report["tracklet"]
                gt_ids = {cid for cid, _ in gt_zone}
                for camera, by_class in held.items():
                    if not isinstance(by_class, dict):
                        continue
                    for cid, pair in by_class.items():
                        rec = {
                            "session": sid,
                            "zone": zone,
                            "camera": camera,
                            "class_id": int(cid),
                            "held_votes": pair[0],
                            "total_votes": pair[1],
                        }
                        if int(cid) in gt_ids:
                            tk["held_gt_flags"].append(rec)
                        else:
                            tk["held_non_gt"] += 1

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

            # 개당 잔차 실측: 단일 정체성 GT + removal delta + unit_weight 기록 시
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
                    report["unit_residual"]["samples"].append(round(unit_r, 2))

    # 요약 통계로 마감
    cal = report["calibration"]["true_candidate"]
    report["calibration"]["quantiles"] = {
        k: _quantiles(v) for k, v in cal.items() if v
    }
    residuals = report["unit_residual"]["samples"]
    if residuals:
        n = len(residuals)
        mean = sum(residuals) / n
        var = sum((r - mean) ** 2 for r in residuals) / n
        report["unit_residual"]["mean"] = round(mean, 2)
        report["unit_residual"]["std"] = round(var**0.5, 2)
        # slack 제안: 편향 포함 RMS — 잔차의 "전형적 크기"
        rms = (sum(r * r for r in residuals) / n) ** 0.5
        report["unit_residual"]["suggested_slack"] = round(rms, 2)
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

    tk = report["tracklet"]
    if tk["triggers"] or tk["held_non_gt"] or tk["held_gt_flags"]:
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
        if tk["held_non_gt"] or tk["held_gt_flags"]:
            lines.append(
                f"  held 강등 관측: 비정답 {tk['held_non_gt']}건 / "
                f"정답 클래스 플래그 {len(tk['held_gt_flags'])}건"
            )
            for r in tk["held_gt_flags"][:8]:
                lines.append(
                    f"    ⚠ {r['session']}/z{r['zone']}/{r['camera']}/"
                    f"c{r['class_id']} held {r['held_votes']}/{r['total_votes']}표"
                    " — 진짜 취출일 수 있음, active 승격 보류 신호"
                )
            if not tk["held_gt_flags"]:
                lines.append(
                    "  → 정답 플래그 0 지속 시 HELD_TRACK_DEMOTION=active 승격 가능"
                )
    gh = report.get("ghost") or {}
    if gh.get("observed"):
        lines.append("")
        lines.append(
            f"--- 고스트 shadow (ghost_ledger — 검출 세션 {gh['observed']}건) ---"
        )
        for r in gh["gt_flagged"][:8]:
            lines.append(
                f"  ⚠ {r['session']}: 정답 클래스 {r['classes']}가 ghost 오플래그"
                " — active 승격 보류 신호"
            )
        for r in gh["shadow"][:8]:
            if "shadow_correct" in r:
                mark = " ✓shadow" if r["shadow_correct"] else (
                    " ✓현행" if r["current_correct"] else " 둘 다 ✗"
                )
            else:
                mark = ""
            lines.append(
                f"  {r['session']}/z{r['zone']}: 과금된 유령 {r['billed_ghost']}"
                f" → 재판정 {r['would']}{mark}"
            )
        lv = gh["labeled_eval"]
        if any(lv.values()):
            lines.append(
                f"  라벨 정오: shadow만 정답 {lv['shadow_correct']} / "
                f"현행만 정답 {lv['current_correct']} / 둘 다 오답 {lv['both_wrong']}"
            )
        if not gh["gt_flagged"]:
            lines.append(
                "  → 정답 오플래그 0 + shadow 우세 지속 시 MODEL__GHOST__MODE="
                "active 승격 근거"
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

    sd = report["unit_residual"]
    lines.append("")
    lines.append("--- 개당 잔차 실측 (= (|Δ| − n·w)/n) ---")
    if not sd["samples"]:
        lines.append("표본 없음 (단일 정체성 라벨 + removal + unit_weight 기록 필요)")
    else:
        lines.append(
            f"  n={len(sd['samples'])} mean={sd['mean']} std={sd['std']} "
            f"→ MODEL__JUDGMENT__COUNT_UNIT_SLACK 제안 {sd['suggested_slack']}"
        )
    return "\n".join(lines)


def _fmt_products(products: list[dict]) -> str:
    if not products:
        return "-"
    return ", ".join(
        f"{p.get('class_id', p.get('name'))}x{p.get('count')}" for p in products
    )


def render_session(doc: dict, *, full: bool = False) -> str:
    """세션 1건의 오판정 사후 분석 덤프 — YAML을 직접 뒤지지 않아도 판정
    전략·득표·탈락 사유·shadow까지 한 화면으로 재구성한다 (--session).

    기본은 압축 출력 (11차 정리): 승격·은퇴로 "일치가 기본값"이 된 필드
    (은퇴 스테이지 0 드랍, candidates와 중복인 생존 클래스, motion 전체
    트랙 덤프)를 접고 예외(mismatch·몰수·held)만 보여준다.
    원자료 전체는 --full."""
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
        if full:
            if vs.get("classes"):
                lines.append(f"   vote_summary.classes: {vs['classes']}")
            for key in (
                "filter_drops_by_stage",
                "entry_dropped_by_camera",
                "motion_evidence",
            ):
                if vs.get(key):
                    lines.append(f"   vote_summary.{key}: {vs[key]}")
        else:
            # 압축 덤프 (11차 정리): 생존 클래스는 candidates 줄이 이미
            # 보여준다 — 여기서는 탈락 클래스(+사유)만. 전체 원자료는 --full.
            rejected = {
                cid: r
                for cid, r in (vs.get("classes") or {}).items()
                if r.get("rejected_by")
            }
            if rejected:
                lines.append(
                    "   rejected: "
                    + ", ".join(
                        f"c{cid}:{r.get('votes')}표({r['rejected_by']})"
                        for cid, r in sorted(
                            rejected.items(), key=lambda kv: -(kv[1].get("votes") or 0)
                        )
                    )
                )
            drops = {
                stage: by_cam
                for stage, by_cam in (vs.get("filter_drops_by_stage") or {}).items()
                if any(by_cam.values())
            }  # 은퇴 스테이지(baseline/static_track 등)의 0 행 숨김
            if drops:
                lines.append(f"   filter_drops: {drops}")
            if vs.get("entry_dropped_by_camera"):
                lines.append(
                    f"   entry_dropped: {vs['entry_dropped_by_camera']}"
                )
            me = vs.get("motion_evidence") or {}
            vetoed, held_tracks = [], []
            for camera, classes in me.items():
                if not isinstance(classes, dict):
                    continue
                for cid, info in classes.items():
                    if not isinstance(info, dict):
                        continue
                    if info.get("passed") is False:
                        vetoed.append(f"{camera}/c{cid}")
                    for tr in info.get("track_detail") or []:
                        if tr.get("held"):
                            held_tracks.append(
                                f"{camera}/c{cid} first{tr.get('first')}"
                                f" obs{tr.get('obs')} head{tr.get('head_obs')}"
                                f" hp{tr.get('head_path', '?')}"
                            )
            if vetoed:
                lines.append("   motion 몰수: " + ", ".join(vetoed))
            if held_tracks:
                lines.append("   held 트랙: " + "; ".join(held_tracks[:6]))
        tube = vs.get("tube_diag")
        if isinstance(tube, dict) and tube.get("by_class"):
            # 튜브 진단: 클래스별 유효표 / 결정적 소수 표 / 튜브 conf —
            # "한 궤적, 여러 클래스"(의류 산탄) 확인용. 판정 영향 없음.
            parts = ", ".join(
                f"c{cid}:{r['votes']}표(소수{r['minority']}/tconf{r['tube_conf']})"
                for cid, r in sorted(
                    tube["by_class"].items(),
                    key=lambda kv: -(kv[1].get("votes") or 0),
                )
            )
            lines.append(f"   tube_diag: {parts}")
            if tube.get("tubes"):
                lines.append(f"   tube_diag.tubes: {tube['tubes']}")
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
        "--full",
        action="store_true",
        help="--session 덤프를 원자료 전체로 (기본은 압축 — 일치/0/중복 필드 접힘)",
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
    if args.session:
        # 단건 조회는 전량 로드를 우회한다 — 파일명 stem == session_id
        # (SessionArchive._write 계약)라 find()로 해당 파일만 파싱하면 되고,
        # 전량 로드는 SAVE_DETECTIONS 대형 YAML이 쌓일수록 O(아카이브 전체
        # 바이트)로 늘어난다 (실측: 세션 단건 조회가 분 단위까지 악화).
        path = archive.find(args.session)
        if path is None:
            print(f"세션을 찾을 수 없습니다: {args.session}", file=sys.stderr)
            return 1
        try:
            doc = _load_document(path)
        except Exception as exc:  # noqa: BLE001 — 손상 파일 명시 보고
            print(f"세션 파일 파싱 실패: {path} ({type(exc).__name__})", file=sys.stderr)
            return 1
        doc["_path"] = str(path)
        if args.json:
            print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_session(doc, full=args.full))
        return 0
    cutoff = None
    if args.since:
        try:
            cutoff = parse_since(args.since)
        except ValueError:
            print(f"--since 형식 오류: {args.since}", file=sys.stderr)
            return 1
    docs = load_documents(args.dir, since=cutoff)
    if not docs:
        if args.since:
            print(f"--since {args.since} 이후 세션이 없습니다", file=sys.stderr)
        else:
            print(f"아카이브가 비어 있습니다: {args.dir}", file=sys.stderr)
        return 1
    if cutoff is not None:
        # 프리필터(파일명 epoch)가 못 거른 시각 미상 파일의 최종 판정
        docs = [
            d for d in docs
            if (ep := _session_epoch(d)) is not None and ep >= cutoff
        ]
        if not docs:
            print(f"--since {args.since} 이후 세션이 없습니다", file=sys.stderr)
            return 1
        print(f"(대상: --since {args.since} 이후 {len(docs)} 세션)")
    report = analyze(docs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
