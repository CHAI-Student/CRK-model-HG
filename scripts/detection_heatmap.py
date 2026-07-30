"""detection-heatmap — 세션 아카이브의 frame_detections로 존×프레임위치 conf 히트맵 생성.

가설 검증용 (이슈 #18 계열): "side 카메라는 zone 2(학습 대표존)에서만 잘 잡고,
top은 카메라에 가까울수록 잘 잡는다"가 사실이면 존별 검출 conf가 프레임 내
특정 위치·특정 존에 몰려 있는 패턴이 수치로 나와야 한다.

입력: SessionArchive 산출물 (data/sessions/{YYYY-MM-DD}/{session_id}.yaml).
      trace.frame_detections는 MODEL__SESSION__SAVE_DETECTIONS=1로 저장된
      세션에만 있다 — 없는 세션은 건너뛰고 개수만 보고한다.

출력 (--out 디렉터리):
  - heatmap_{camera}.png   : 존별 [상품 검출수 / 상품 mean conf / 손 mean conf]
                             격자 히트맵 (matplotlib 있을 때만)
  - cells.csv              : camera,zone,kind,row,col,count,mean_conf (기계 판독용)
  - class_summary.csv      : camera,zone,class_id,name,count,mean_conf,max_conf
  - stdout                 : 존×클래스 요약 + (matplotlib 없으면) ASCII 히트맵

사용:
  python scripts/detection_heatmap.py --dir data/sessions --out heatmaps
  python scripts/detection_heatmap.py --dir data/sessions/2026-07-28/ses-6-xxx.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

HAND_CLASS_ID = 0


def _load_document(path: Path) -> dict | None:
    """archive._load_document 동형 — libyaml C 로더 우선, json 폴백."""
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        return yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except Exception as exc:  # 손상 파일은 진단을 막지 않는다 — 건너뛰고 보고
        print(f"[skip] {path}: {exc}", file=sys.stderr)
        return None


def _iter_session_files(root: Path):
    if root.is_file():
        yield root
        return
    for suffix in ("*.yaml", "*.json"):
        yield from sorted(root.rglob(suffix))


class _CellGrid:
    """grid×grid 셀별 (검출수, conf 합) 누적기 — 순수 파이썬 (numpy 비의존)."""

    def __init__(self, grid: int):
        self.grid = grid
        self.count = [[0] * grid for _ in range(grid)]
        self.conf_sum = [[0.0] * grid for _ in range(grid)]

    def add(self, cx: float, cy: float, conf: float, size: float) -> None:
        g = self.grid
        col = min(g - 1, max(0, int(cx / size * g)))
        row = min(g - 1, max(0, int(cy / size * g)))
        self.count[row][col] += 1
        self.conf_sum[row][col] += conf

    def mean(self) -> list[list[float]]:
        return [
            [
                (self.conf_sum[r][c] / self.count[r][c]) if self.count[r][c] else math.nan
                for c in range(self.grid)
            ]
            for r in range(self.grid)
        ]

    @property
    def total(self) -> int:
        return sum(sum(row) for row in self.count)


def collect(root: Path, grid: int, size: float, min_conf: float = 0.0):
    """아카이브를 훑어 (camera, zone)별 셀 격자·클래스 통계를 누적한다.

    min_conf: 이 값 미만 검출은 집계에서 제외. SAVE_DETECTIONS는 투표
    하한보다 낮은 기록용 검출까지 동봉하므로, 고정 배경 오탐(예: 전 존
    top (0,6) 셀의 conf~0.1 class 48 유령)을 걷어내고 보려면 0.25~0.3 권장.
    """
    grids: dict = defaultdict(lambda: {"product": _CellGrid(grid), "hand": _CellGrid(grid)})
    # (camera, zone, class_id) -> [count, conf_sum, conf_max]
    by_class: dict = defaultdict(lambda: [0, 0.0, 0.0])
    # (camera, zone) -> 판정입력 프레임 수 — 존별 검출수 비교의 분모
    frames_by: dict = defaultdict(int)
    class_names: dict[int, str] = {}
    crops_seen: dict[str, set] = defaultdict(set)
    n_files = n_with_fd = n_frames = 0

    for path in _iter_session_files(root):
        doc = _load_document(path)
        if not isinstance(doc, dict):
            continue
        n_files += 1
        for zb in doc.get("zones") or []:
            for p in zb.get("products") or []:
                if p.get("class_id") is not None and p.get("name"):
                    class_names[p["class_id"]] = p["name"]
        file_has_fd = False
        for trig in doc.get("triggers") or []:
            zone = trig.get("zone")
            trace = trig.get("trace") or {}
            frames = trace.get("frame_detections")
            if not frames or zone is None:
                continue
            file_has_fd = True
            for j in (trig.get("judgment") or {}).get("products") or []:
                if j.get("class_id") is not None and j.get("name"):
                    class_names[j["class_id"]] = j["name"]
            for cam, mode in (trig.get("camera_crops") or {}).items():
                crops_seen[cam].add(mode)
            for fr in frames:
                cam = fr.get("camera")
                if cam is None:
                    continue
                n_frames += 1
                frames_by[(cam, zone)] += 1
                for det in fr.get("detections") or []:
                    bbox = det.get("bbox")
                    conf = det.get("conf")
                    if not bbox or len(bbox) != 4 or conf is None or conf < min_conf:
                        continue
                    cx = (bbox[0] + bbox[2]) / 2.0
                    cy = (bbox[1] + bbox[3]) / 2.0
                    kind = "hand" if det.get("hand") else "product"
                    grids[(cam, zone)][kind].add(cx, cy, conf, size)
                    cid = det.get("class_id")
                    if cid is not None:
                        st = by_class[(cam, zone, cid)]
                        st[0] += 1
                        st[1] += conf
                        st[2] = max(st[2], conf)
        n_with_fd += 1 if file_has_fd else 0

    return {
        "grids": grids,
        "by_class": by_class,
        "frames_by": frames_by,
        "class_names": class_names,
        "crops_seen": crops_seen,
        "n_files": n_files,
        "n_with_fd": n_with_fd,
        "n_frames": n_frames,
    }


def write_csvs(out: Path, data: dict) -> None:
    with (out / "cells.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["camera", "zone", "kind", "row", "col", "count", "mean_conf"])
        for (cam, zone), kinds in sorted(data["grids"].items()):
            for kind, cg in kinds.items():
                mean = cg.mean()
                for r in range(cg.grid):
                    for c in range(cg.grid):
                        if cg.count[r][c]:
                            w.writerow([cam, zone, kind, r, c, cg.count[r][c], f"{mean[r][c]:.4f}"])
    with (out / "class_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["camera", "zone", "class_id", "name", "count", "mean_conf", "max_conf"])
        for (cam, zone, cid), (n, s, mx) in sorted(data["by_class"].items()):
            name = "HAND" if cid == HAND_CLASS_ID else data["class_names"].get(cid, "?")
            w.writerow([cam, zone, cid, name, n, f"{s / n:.4f}", f"{mx:.4f}"])


def print_summary(data: dict) -> None:
    print(
        f"세션 파일 {data['n_files']}개 중 frame_detections 보유 {data['n_with_fd']}개, "
        f"판정입력 프레임 {data['n_frames']}개"
    )
    for cam, modes in sorted(data["crops_seen"].items()):
        if len(modes) > 1:
            # 크롭 모드가 섞이면 같은 카메라라도 프레임 좌표가 다른 세계 영역을
            # 가리킨다 — 존 간 위치 비교가 무효이므로 반드시 경고.
            print(f"[경고] {cam} 카메라 crop 모드 혼재: {sorted(modes)} — 위치 비교 주의")
    # 존별 분모(판정입력 프레임 수)와 프레임당 검출률 — 원시 count는 존별
    # 세션 수 차이에 좌우되므로 존 간 비교는 rate로 해야 공정하다.
    print(f"\n{'camera':<7}{'zone':<6}{'frames':>8}{'product':>9}{'per-frame':>11}")
    for (cam, zone), nf in sorted(data["frames_by"].items()):
        np_ = data["grids"][(cam, zone)]["product"].total if (cam, zone) in data["grids"] else 0
        print(f"{cam:<7}{zone:<6}{nf:>8}{np_:>9}{np_ / nf if nf else 0:>11.2f}")
    rows: dict = defaultdict(dict)
    for (cam, zone, cid), (n, s, _mx) in data["by_class"].items():
        rows[(cam, zone)][cid] = (n, s / n)
    print(f"\n{'camera':<7}{'zone':<6}{'class':<34}{'count':>7}{'mean_conf':>11}")
    for (cam, zone), classes in sorted(rows.items()):
        for cid, (n, m) in sorted(classes.items()):
            name = "HAND" if cid == HAND_CLASS_ID else data["class_names"].get(cid, "?")
            print(f"{cam:<7}{zone:<6}{f'[{cid}] {name}':<34}{n:>7}{m:>11.3f}")


def print_ascii(data: dict) -> None:
    """matplotlib 부재 시 폴백 — mean conf를 0~9 한 자리로 렌더 ('·'=검출 없음)."""
    for (cam, zone), kinds in sorted(data["grids"].items()):
        for kind in ("product", "hand"):
            cg = kinds[kind]
            if not cg.total:
                continue
            print(f"\n[{cam}] zone {zone} — {kind} mean conf (n={cg.total}, 위=프레임 상단)")
            for row in cg.mean():
                print(
                    "  "
                    + " ".join(
                        "·" if math.isnan(v) else str(min(9, int(round(v * 9)))) for v in row
                    )
                )


def render_png(out: Path, data: dict, size: float) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    cameras = sorted({cam for cam, _ in data["grids"]})
    # 플롯 내 텍스트는 영문 고정 — matplotlib 기본 폰트에 한글 글리프가 없어
    # 엣지(젯슨)에서 □로 깨진다.
    cols = [
        ("product", "count", "product count", "magma", None),
        ("product", "mean", "product mean conf", "viridis", (0.0, 1.0)),
        ("hand", "mean", "hand mean conf", "viridis", (0.0, 1.0)),
    ]
    for cam in cameras:
        zones = sorted(z for c, z in data["grids"] if c == cam)
        fig, axes = plt.subplots(
            len(zones), len(cols), figsize=(3.9 * len(cols), 3.4 * len(zones)), squeeze=False
        )
        for ri, zone in enumerate(zones):
            kinds = data["grids"][(cam, zone)]
            for ci, (kind, stat, title, cmap, vrange) in enumerate(cols):
                cg = kinds[kind]
                ax = axes[ri][ci]
                if stat == "count":
                    img = [[float(v) if v else math.nan for v in row] for row in cg.count]
                    vmin, vmax = 0.0, max(1.0, max((v for r in cg.count for v in r), default=1))
                else:
                    img = cg.mean()
                    vmin, vmax = vrange
                im = ax.imshow(
                    img, cmap=cmap, vmin=vmin, vmax=vmax,
                    extent=(0, size, size, 0), interpolation="nearest",
                )
                fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
                if ri == 0:
                    ax.set_title(f"{title} (n={cg.total})", fontsize=10)
                elif stat == "count":
                    ax.set_title(f"n={cg.total}", fontsize=9)
                if ci == 0:
                    ax.set_ylabel(f"zone {zone}", fontsize=11)
                ax.set_xticks([]), ax.set_yticks([])
        fig.suptitle(f"{cam} camera — detections by zone (frame coords, up=top)", fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        path = out / f"heatmap_{cam}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"저장: {path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(prog="detection-heatmap", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default="data/sessions", help="아카이브 루트 또는 세션 파일 1개")
    ap.add_argument("--out", default="heatmaps", help="출력 디렉터리 (기본: heatmaps)")
    ap.add_argument("--grid", type=int, default=12, help="히트맵 격자 분할 수 (기본: 12)")
    ap.add_argument(
        "--size", type=float, default=480.0,
        help="프레임 한 변 픽셀 (크롭 후 정방 크기, 기본: 480)",
    )
    ap.add_argument(
        "--min-conf", type=float, default=0.0,
        help="이 conf 미만 검출 제외 (기본: 0 — 기록 전부. 배경 오탐 제거엔 0.25~0.3)",
    )
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"경로 없음: {root}", file=sys.stderr)
        return 1
    data = collect(root, args.grid, args.size)
    if not data["grids"]:
        print(
            "frame_detections 있는 세션이 없습니다 — "
            "MODEL__SESSION__SAVE_DETECTIONS=1로 저장된 세션이 필요합니다.",
            file=sys.stderr,
        )
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print_summary(data)
    write_csvs(out, data)
    print(f"\n저장: {out / 'cells.csv'}\n저장: {out / 'class_summary.csv'}")
    if not render_png(out, data, args.size):
        print("\n(matplotlib 없음 — PNG 생략, ASCII로 대체)")
        print_ascii(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
