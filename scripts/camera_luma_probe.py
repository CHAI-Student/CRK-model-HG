"""카메라 노출 안정성(luma) 프로브 — 이슈 #19 (camera fitting).

세션 AVI의 프레임별 luma 시계열로 두 가지를 판정한다:

1. **내부 AE(자동 노출) 작동 여부** — v4l2 컨트롤 목록에 auto_exposure가
   없어도(이슈 #19 로그) 저가 UVC 모듈은 ISP 내부에서 AE/AWB를 조용히
   돌릴 수 있다. 판별 신호는 "정적 영역들이 **동시에 같은 방향으로**
   움직이는 luma 변동": 손/상품이 한 모서리를 가리면 그 패치만 변하지만,
   노출이 바뀌면 화면 전체가 함께 변한다. 네 모서리 패치가 모두 같은
   방향으로 --ae-threshold 이상 이동한 프레임을 AE 의심으로 센다.
2. **노출 적정성** — Rahman et al.(EURASIP JIVP 2016) 기준: 정규화 평균
   μ < 0.5 = dark, 4σ ≤ 1/3 = low-contrast. 여기에 0/255 채널 클리핑
   비율을 더해 카메라단(brightness/contrast/exposure_time_absolute) 조정
   근거를 만든다 — 추론단 소프트웨어 감마보다 캡처단 수정이 문헌상 1순위.

luma는 채널 평균(gate_view와 동일 의미론, BT.601 가중 아님)으로 계산한다.

의존성: cv2 + numpy (Jetson system-site에 존재). matplotlib은 선택 —
있으면 파일별 luma 시계열 PNG를 그린다 (Agg 백엔드, 영문 라벨 —
detection_heatmap과 동일한 한글 tofu 회피).

사용 (Jetson):
    python scripts/camera_luma_probe.py data/videos              # 디렉토리 재귀
    python scripts/camera_luma_probe.py ses1_top.avi ses1_side.avi \
        --csv luma_series.csv --plot luma_plots/
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PATCH_MARGIN = 8  # 프레임 가장자리 인코딩 아티팩트 회피용 여백(px)


def iter_avis(paths: list[str]) -> list[Path]:
    """파일/디렉토리 인자를 AVI 목록으로 펼친다 (디렉토리는 재귀, 정렬)."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.avi")))
        elif p.exists():
            out.append(p)
        else:
            print(f"warning: not found, skipping: {p}", file=sys.stderr)
    return out


def _corner_patches(h: int, w: int, size: int) -> dict[str, tuple[int, int, int, int]]:
    """네 모서리 정적 패치 (x, y, w, h). 소형 프레임은 패치를 축소."""
    s = max(8, min(size, h // 4, w // 4))
    m = PATCH_MARGIN
    return {
        "tl": (m, m, s, s),
        "tr": (w - m - s, m, s, s),
        "bl": (m, h - m - s, s, s),
        "br": (w - m - s, h - m - s, s, s),
    }


def probe_video(path: Path, *, stride: int, patch_size: int, ae_threshold: float) -> dict:
    """단일 AVI의 luma 통계·AE 시그널을 계산한다."""
    import cv2  # lazy: Jetson system-site
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise OSError(f"cannot open video: {path}")
    hist = np.zeros(256, dtype=np.int64)
    clip_lo = clip_hi = px_total = 0
    global_means: list[float] = []
    patch_means: list[list[float]] = []
    frame_idx: list[int] = []
    patches: dict[str, tuple[int, int, int, int]] | None = None
    idx = -1
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if idx % stride:
                continue
            gray = frame.mean(axis=2)
            if patches is None:
                h, w = gray.shape
                patches = _corner_patches(h, w, patch_size)
            hist += np.bincount(gray.astype(np.uint8).ravel(), minlength=256)
            # 클리핑은 luma가 아니라 원 채널 기준 — 채널 평균은 한 채널만
            # 포화해도 255에 못 미쳐 하이라이트 클리핑을 숨긴다.
            clip_lo += int((frame == 0).sum())
            clip_hi += int((frame == 255).sum())
            px_total += frame.size
            global_means.append(float(gray.mean()))
            patch_means.append(
                [float(gray[y : y + ph, x : x + pw].mean()) for (x, y, pw, ph) in patches.values()]
            )
            frame_idx.append(idx)
    finally:
        cap.release()
    if not global_means:
        raise OSError(f"no frames decoded: {path}")

    total = int(hist.sum())
    levels = np.arange(256, dtype=np.float64)
    mean = float((hist * levels).sum() / total)
    std = float(np.sqrt((hist * (levels - mean) ** 2).sum() / total))
    cdf = np.cumsum(hist)

    def pct(q: float) -> float:
        return float(np.searchsorted(cdf, q * total))

    # AE 시그널: 패치별 시계열의 자기 중앙값 대비 편차가 네 패치 모두 같은
    # 방향으로 임계 초과 → 그 프레임의 공통 이동량(부호 보존 최소 크기).
    pm = np.asarray(patch_means)
    dev = pm - np.median(pm, axis=0)
    lo, hi = dev.min(axis=1), dev.max(axis=1)
    common = np.where(lo > ae_threshold, lo, np.where(hi < -ae_threshold, hi, 0.0))
    ae_mask = common != 0.0
    max_shift = float(common[np.abs(common).argmax()]) if ae_mask.any() else 0.0

    return {
        "path": path,
        "frames_read": len(global_means),
        "frames_total": idx + 1,
        "mean": mean,
        "std": std,
        "p5": pct(0.05),
        "p95": pct(0.95),
        "clip_lo_pct": 100.0 * clip_lo / px_total,
        "clip_hi_pct": 100.0 * clip_hi / px_total,
        "ae_frames": int(ae_mask.sum()),
        "ae_max_shift": max_shift,
        "frame_idx": frame_idx,
        "global_means": global_means,
        "patch_means": pm,
        "common_shift": common,
        "patch_names": list(_corner_patches(1000, 1000, patch_size)),
    }


def verdict(r: dict) -> str:
    """Rahman 2016 노출 기준 + AE 판정을 한 줄 문자열로."""
    notes: list[str] = []
    mu = r["mean"] / 255.0
    if mu < 0.5:
        notes.append(f"dark(mu={mu:.2f})")
    if 4.0 * r["std"] <= 255.0 / 3.0:
        notes.append(f"low-contrast(4s={4 * r['std']:.0f})")
    if r["clip_hi_pct"] > 0.5:
        notes.append(f"clip255={r['clip_hi_pct']:.1f}%")
    if r["clip_lo_pct"] > 0.5:
        notes.append(f"clip0={r['clip_lo_pct']:.1f}%")
    if r["ae_frames"] >= 3:
        notes.append(f"AE-SUSPECT({r['ae_frames']}f, {r['ae_max_shift']:+.1f})")
    elif r["ae_frames"]:
        notes.append(f"ae-borderline({r['ae_frames']}f)")
    return " ".join(notes) if notes else "ok"


def write_csv(results: list[dict], out: Path) -> None:
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        names = results[0]["patch_names"]
        w.writerow(["file", "frame", "global_mean", *names, "common_shift"])
        for r in results:
            for i, fi in enumerate(r["frame_idx"]):
                w.writerow(
                    [
                        r["path"],
                        fi,
                        f"{r['global_means'][i]:.2f}",
                        *[f"{v:.2f}" for v in r["patch_means"][i]],
                        f"{r['common_shift'][i]:.2f}",
                    ]
                )


def plot_results(results: list[dict], out_dir: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(r["frame_idx"], r["global_means"], lw=1.8, label="global")
        for i, name in enumerate(r["patch_names"]):
            ax.plot(r["frame_idx"], r["patch_means"][:, i], lw=0.8, label=f"corner {name}")
        flagged = [fi for fi, c in zip(r["frame_idx"], r["common_shift"], strict=True) if c]
        for fi in flagged:
            ax.axvline(fi, color="red", alpha=0.15, lw=1)
        ax.set_xlabel("frame")
        ax.set_ylabel("mean luma (0-255)")
        ax.set_title(f"{r['path'].name} — AE-flagged frames: {r['ae_frames']}")
        ax.legend(fontsize=8, ncol=5)
        fig.tight_layout()
        fig.savefig(out_dir / f"{r['path'].stem}_luma.png", dpi=120)
        plt.close(fig)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="AVI 파일 또는 디렉토리(재귀)")
    ap.add_argument("--stride", type=int, default=1, help="프레임 샘플 간격")
    ap.add_argument("--patch-size", type=int, default=48, help="모서리 패치 한 변(px)")
    ap.add_argument(
        "--ae-threshold",
        type=float,
        default=2.0,
        help="AE 판정: 네 패치 공통 이동 임계(luma level)",
    )
    ap.add_argument("--csv", type=Path, help="프레임별 시계열 CSV 출력 경로")
    ap.add_argument("--plot", type=Path, help="파일별 luma 시계열 PNG 출력 디렉토리")
    args = ap.parse_args()

    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        print(f"error: cv2/numpy required ({exc}) — run on the Jetson", file=sys.stderr)
        return 2

    avis = iter_avis(args.paths)
    if not avis:
        print("error: no AVI files found", file=sys.stderr)
        return 2

    results: list[dict] = []
    for path in avis:
        try:
            results.append(
                probe_video(
                    path,
                    stride=max(args.stride, 1),
                    patch_size=args.patch_size,
                    ae_threshold=args.ae_threshold,
                )
            )
        except OSError as exc:
            print(f"warning: {exc}", file=sys.stderr)
    if not results:
        print("error: no videos decoded", file=sys.stderr)
        return 2

    header = (
        f"{'file':<44} {'frames':>6} {'mean':>6} {'std':>5} {'p5':>4} "
        f"{'p95':>4} {'clip0%':>6} {'clip255%':>8} {'AEf':>4}  verdict"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{str(r['path'])[-44:]:<44} {r['frames_read']:>6} {r['mean']:>6.1f} "
            f"{r['std']:>5.1f} {r['p5']:>4.0f} {r['p95']:>4.0f} "
            f"{r['clip_lo_pct']:>6.2f} {r['clip_hi_pct']:>8.2f} "
            f"{r['ae_frames']:>4}  {verdict(r)}"
        )

    suspects = [r for r in results if r["ae_frames"] >= 3]
    print()
    if suspects:
        print(
            f"==> AE-SUSPECT in {len(suspects)}/{len(results)} videos: 내부 AE가 "
            "돌고 있을 가능성 — v4l2로는 잠글 수 없으므로 카메라단 노브 조정의 "
            "재현성이 보장되지 않는다. 모델단 강건화(증강 재학습)가 지렛대."
        )
    else:
        print(
            f"==> stable exposure in all {len(results)} videos: 노출 고정 확인 — "
            "카메라단 노브(brightness/contrast/exposure_time)가 신뢰 가능. "
            "dark/low-contrast/clip 플래그가 있으면 캡처단 조정이 1순위."
        )

    if args.csv:
        write_csv(results, args.csv)
        print(f"csv: {args.csv}")
    if args.plot:
        if plot_results(results, args.plot):
            print(f"plots: {args.plot}/")
        else:
            print("plot skipped: matplotlib not installed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
