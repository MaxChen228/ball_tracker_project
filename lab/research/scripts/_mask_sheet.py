"""Mask overlay contact sheet — visual audit of SAM2 ball masks.

Renders a grid of (frame + mask overlay + GT centroid + bbox) tiles, so
the human eye can decide what aspect/fill numbers can't: is the mask
actually on the ball?

Usage::

    # auto: top-K worst by aspect/fill/n_comp + random controls per session
    python _mask_sheet.py --slug session_s_2546618f_b --auto worst --k 16

    # explicit frame list
    python _mask_sheet.py --slug session_s_2546618f_b --frames 1234 1245 1267

    # dump for ALL propagate-done sessions, K worst each
    python _mask_sheet.py --all-sessions --auto worst --k 16

Output: outputs/_mask_audit/<slug>__<tag>.jpg
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WS, OUT, load_manifest, SEG_BY_SLUG, read_mask


AUDIT_OUT = OUT / "_mask_audit"

_BLUE_HSV_LO = np.array([105, 140, 40], dtype=np.uint8)
_BLUE_HSV_HI = np.array([112, 255, 255], dtype=np.uint8)


def mask_stats(mask: np.ndarray) -> dict:
    ys, xs = np.where(mask > 0)
    if len(ys) < 5:
        return {"area": int(len(ys)), "aspect": 0, "fill": 0, "n_comp": 0,
                "cx": 0.0, "cy": 0.0, "bbox": (0, 0, 0, 0)}
    n_comp, _, _, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    x_min, y_min, x_max, y_max = xs.min(), ys.min(), xs.max(), ys.max()
    w, h = x_max - x_min + 1, y_max - y_min + 1
    aspect = min(w, h) / max(w, h) if max(w, h) > 0 else 0
    fill = float(len(ys)) / (w * h) if w * h > 0 else 0
    return {
        "area": int(len(ys)),
        "aspect": float(aspect),
        "fill": float(fill),
        "n_comp": int(n_comp - 1),
        "cx": float(xs.mean()),
        "cy": float(ys.mean()),
        "bbox": (int(x_min), int(y_min), int(x_max), int(y_max)),
    }


def _compute_hsv_ratio(bgr: np.ndarray, mask: np.ndarray) -> tuple[float, int]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.inRange(hsv, _BLUE_HSV_LO, _BLUE_HSV_HI)
    inter = cv2.bitwise_and(mask, hsv_mask)
    area = int((mask > 0).sum())
    hsv_area = int((inter > 0).sum())
    return (hsv_area / area if area > 0 else 0.0, hsv_area)


def collect_session(slug: str) -> list[dict]:
    """Return rows with mask stats + paths, sorted by src frame index."""
    manifest = load_manifest()  # populates SEG_BY_SLUG
    in_f = None
    for it in manifest["items"]:
        if it["slug"] == slug:
            in_f = it["in_frame"]
            break
    if in_f is None:
        raise SystemExit(f"slug {slug} not in manifest")
    seg_id = SEG_BY_SLUG[slug]
    masks_dir = WS / "items" / slug / "masks" / seg_id
    frames_dir = WS / "items" / slug / "frames"

    rows = []
    for mp in sorted(masks_dir.glob("*.png")):
        src = int(mp.stem)
        local = src - in_f
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        m = read_mask(mp)
        if m is None:
            continue
        s = mask_stats(m)
        if s["area"] < 5:
            continue
        s["src"] = src
        s["local"] = local
        s["mask_path"] = mp
        s["frame_path"] = fp
        rows.append(s)
    return rows


def _score_worst(rows: list[dict]) -> np.ndarray:
    """Composite per-frame static-shape suspiciousness score."""
    a = np.array([r["area"] for r in rows], dtype=float)
    med_a = np.median(a)
    scores = np.zeros(len(rows))
    for i, r in enumerate(rows):
        s = 0.0
        s += max(0, 1.0 - r["aspect"]) * 2
        s += abs(r["fill"] - 0.785) * 2
        s += 1.0 if r["n_comp"] > 1 else 0
        s += 1.0 if r["area"] > 3 * med_a else 0
        s += 1.0 if r["area"] < 0.3 * med_a else 0
        scores[i] = s
    return scores


def _score_drift(rows: list[dict]) -> np.ndarray:
    """Per-frame centroid jump vs immediate neighbours (max of left/right)."""
    if len(rows) < 3:
        return np.zeros(len(rows))
    cx = np.array([r["cx"] for r in rows])
    cy = np.array([r["cy"] for r in rows])
    srcs = np.array([r["src"] for r in rows])
    scores = np.zeros(len(rows))
    for i in range(len(rows)):
        d = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(rows) and abs(srcs[j] - srcs[i]) <= 3:
                d.append(np.hypot(cx[j] - cx[i], cy[j] - cy[i]))
        scores[i] = max(d) if d else 0.0
    return scores


def _score_temporal(rows: list[dict], window: int = 5) -> np.ndarray:
    """Residual of centroid from rolling-median path. Robust to multi-arc."""
    if len(rows) < window:
        return np.zeros(len(rows))
    cx = np.array([r["cx"] for r in rows])
    cy = np.array([r["cy"] for r in rows])
    half = window // 2
    sx = np.array([np.median(cx[max(0, i - half):i + half + 1]) for i in range(len(rows))])
    sy = np.array([np.median(cy[max(0, i - half):i + half + 1]) for i in range(len(rows))])
    return np.hypot(cx - sx, cy - sy)


def pick_frames(rows: list[dict], mode: str, k: int) -> list[tuple[str, dict]]:
    """Return list of (reason_tag, row). mode ∈ worst|drift|temporal|random|mixed."""
    if not rows:
        return []
    rng = np.random.default_rng(seed=42)
    if mode == "random":
        idx = rng.choice(len(rows), size=min(k, len(rows)), replace=False)
        return [("RAND", rows[int(i)]) for i in sorted(idx)]
    if mode == "worst":
        scores = _score_worst(rows)
        order = np.argsort(scores)[::-1][:k]
        return [("WORST", rows[int(i)]) for i in sorted(order)]
    if mode == "drift":
        scores = _score_drift(rows)
        order = np.argsort(scores)[::-1][:k]
        return [("DRIFT", rows[int(i)]) for i in sorted(order)]
    if mode == "temporal":
        scores = _score_temporal(rows)
        order = np.argsort(scores)[::-1][:k]
        return [("TEMP", rows[int(i)]) for i in sorted(order)]
    if mode == "mixed":
        # Take top-(k//4) from each independent signal + a few random controls.
        per = max(1, k // 4)
        bucket = {}  # src -> (reason, row)  (dedup; first reason wins)
        for tag, score_fn in (
            ("WORST", _score_worst),
            ("DRIFT", _score_drift),
            ("TEMP", _score_temporal),
        ):
            scores = score_fn(rows)
            for i in np.argsort(scores)[::-1][:per]:
                src = rows[int(i)]["src"]
                bucket.setdefault(src, (tag, rows[int(i)]))
        # random fill
        remaining = [r for r in rows if r["src"] not in bucket]
        if remaining:
            rand_n = min(per, len(remaining))
            ridx = rng.choice(len(remaining), size=rand_n, replace=False)
            for i in ridx:
                bucket.setdefault(remaining[int(i)]["src"], ("RAND", remaining[int(i)]))
        out = sorted(bucket.values(), key=lambda x: x[1]["src"])
        return out
    raise ValueError(f"unknown mode {mode}")


def _hsv_clean_mask(bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Intersect SAM2 mask with deep-blue HSV range. Pixels that survive
    are ball-coloured; pixels that drop are likely merger noise."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.inRange(hsv, _BLUE_HSV_LO, _BLUE_HSV_HI)
    return cv2.bitwise_and(mask, hsv_mask)


def render_cell(row: dict, cell_w: int, cell_h: int, reason: str = "") -> np.ndarray:
    bgr = cv2.imread(str(row["frame_path"]), cv2.IMREAD_COLOR)
    mask = read_mask(row["mask_path"])
    if bgr is None or mask is None:
        return np.zeros((cell_h, cell_w, 3), dtype=np.uint8)
    H, W = bgr.shape[:2]
    x0, y0, x1, y1 = row["bbox"]
    raw_area = int((mask > 0).sum())
    hsv_mask = _hsv_clean_mask(bgr, mask)
    hsv_area = int((hsv_mask > 0).sum())
    ratio = (hsv_area / raw_area) if raw_area > 0 else 0.0

    # Cell layout: top half = full frame with raw mask, bottom half =
    # zoom panel (left: raw mask | right: HSV-cleaned mask).
    half_h = cell_h // 2

    # ---- Top: full frame letterboxed ----
    full = bgr.copy()
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cv2.drawContours(full, contours, -1, (255, 0, 255), 2)
    bx0 = max(0, x0 - 8); by0 = max(0, y0 - 8)
    bx1 = min(W - 1, x1 + 8); by1 = min(H - 1, y1 + 8)
    cv2.rectangle(full, (bx0, by0), (bx1, by1), (0, 255, 255), 2)
    fh, fw = full.shape[:2]
    fscale = min(cell_w / fw, half_h / fh)
    fnw, fnh = max(1, int(fw * fscale)), max(1, int(fh * fscale))
    full_resized = cv2.resize(full, (fnw, fnh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((cell_h, cell_w, 3), dtype=np.uint8)
    fx_off = (cell_w - fnw) // 2; fy_off = (half_h - fnh) // 2
    canvas[fy_off:fy_off + fnh, fx_off:fx_off + fnw] = full_resized

    # ---- Bottom: 2 zoom panels side-by-side ----
    pad_z = max(40, int(0.8 * max(x1 - x0, y1 - y0)))
    zx0 = max(0, x0 - pad_z); zy0 = max(0, y0 - pad_z)
    zx1 = min(W, x1 + pad_z); zy1 = min(H, y1 + pad_z)
    base_crop = bgr[zy0:zy1, zx0:zx1]
    raw_zm = mask[zy0:zy1, zx0:zx1]
    hsv_zm = hsv_mask[zy0:zy1, zx0:zx1]

    panel_w = cell_w // 2
    bottom_h = cell_h - half_h

    def _make_panel(raw_or_hsv: np.ndarray, color: tuple) -> np.ndarray:
        z = base_crop.copy()
        if raw_or_hsv.max() > 0:
            ctrs, _ = cv2.findContours(raw_or_hsv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            cv2.drawContours(z, ctrs, -1, color, 2)
        zh, zw = z.shape[:2]
        s = min(panel_w / zw, bottom_h / zh)
        nw, nh = max(1, int(zw * s)), max(1, int(zh * s))
        rz = cv2.resize(z, (nw, nh), interpolation=cv2.INTER_AREA)
        out = np.zeros((bottom_h, panel_w, 3), dtype=np.uint8)
        ox = (panel_w - nw) // 2; oy = (bottom_h - nh) // 2
        out[oy:oy + nh, ox:ox + nw] = rz
        return out

    left_panel = _make_panel(raw_zm, (255, 0, 255))   # raw = magenta
    right_panel = _make_panel(hsv_zm, (0, 255, 255))  # hsv-clean = yellow
    canvas[half_h:half_h + bottom_h, 0:panel_w] = left_panel
    canvas[half_h:half_h + bottom_h, panel_w:panel_w + (cell_w - panel_w)] = right_panel
    # Stash ratio for label band
    row["_ratio"] = ratio
    row["_hsv_area"] = hsv_area

    # Label band
    tag = f"[{reason}] " if reason else ""
    label1 = f"{tag}f={row['src']:05d} a={row['area']:>5d}"
    ratio = row.get("_ratio", 0.0)
    hsv_area = row.get("_hsv_area", 0)
    label2 = f"asp={row['aspect']:.2f} fill={row['fill']:.2f} hsv={hsv_area} r={ratio:.2f}"
    band = np.zeros((34, cell_w, 3), dtype=np.uint8)
    cv2.putText(band, label1, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(band, label2, (4, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (200, 200, 200), 1, cv2.LINE_AA)
    return np.vstack([band, canvas])


def compose_sheet(cells: list[np.ndarray], grid_n: int) -> np.ndarray:
    if not cells:
        return np.zeros((100, 100, 3), dtype=np.uint8)
    h, w = cells[0].shape[:2]
    border = 3
    rows = (len(cells) + grid_n - 1) // grid_n
    sheet_w = grid_n * w + (grid_n + 1) * border
    sheet_h = rows * h + (rows + 1) * border
    sheet = np.full((sheet_h, sheet_w, 3), 32, dtype=np.uint8)
    for i, cell in enumerate(cells):
        r, c = divmod(i, grid_n)
        y = border + r * (h + border); x = border + c * (w + border)
        sheet[y:y + h, x:x + w] = cell
    return sheet


def render_for_slug(slug: str, mode: str, k: int, cell_w: int, cell_h: int,
                    grid_n: int, tag: str | None = None) -> Path:
    rows = collect_session(slug)
    picks = pick_frames(rows, mode, k)
    cells = [render_cell(r, cell_w, cell_h, reason=reason) for reason, r in picks]
    sheet = compose_sheet(cells, grid_n)
    AUDIT_OUT.mkdir(parents=True, exist_ok=True)
    fname = f"{slug}__{tag or mode}.jpg"
    path = AUDIT_OUT / fname
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    # Sidecar JSON: candidate metadata for downstream review aggregation
    sidecar = {
        "slug": slug,
        "mode": mode,
        "candidates": [
            {
                "src": r["src"],
                "reason": reason,
                "area": r["area"],
                "aspect": round(r["aspect"], 3),
                "fill": round(r["fill"], 3),
                "n_comp": r["n_comp"],
                "cx": round(r["cx"], 1),
                "cy": round(r["cy"], 1),
            }
            for reason, r in picks
        ],
    }
    (AUDIT_OUT / f"{slug}__{tag or mode}.json").write_text(
        json.dumps(sidecar, indent=2))
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--slug")
    p.add_argument("--all-sessions", action="store_true")
    p.add_argument("--frames", type=int, nargs="*", default=None,
                   help="explicit src frame indices (requires --slug)")
    p.add_argument("--auto", choices=("worst", "drift", "temporal", "random", "mixed"))
    p.add_argument("--k", type=int, default=16)
    p.add_argument("--grid", type=int, default=4)
    p.add_argument("--cell-w", type=int, default=420)
    p.add_argument("--cell-h", type=int, default=300)
    p.add_argument("--tag", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    if args.frames:
        if not args.slug:
            raise SystemExit("--frames requires --slug")
        rows = collect_session(args.slug)
        wanted = set(args.frames)
        picks = [r for r in rows if r["src"] in wanted]
        cells = [render_cell(r, args.cell_w, args.cell_h, reason="EXPL") for r in picks]
        sheet = compose_sheet(cells, args.grid)
        AUDIT_OUT.mkdir(parents=True, exist_ok=True)
        path = AUDIT_OUT / f"{args.slug}__{args.tag or 'explicit'}.jpg"
        cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
        print(f"wrote {path}")
        return
    if args.all_sessions:
        if not args.auto:
            raise SystemExit("--all-sessions requires --auto")
        manifest = load_manifest()
        slugs = [it["slug"] for it in manifest["items"]
                 if it.get("propagate_status") == "done"]
        for slug in slugs:
            try:
                p = render_for_slug(slug, args.auto, args.k,
                                    args.cell_w, args.cell_h, args.grid,
                                    tag=args.tag)
                print(f"wrote {p}")
            except Exception as e:
                print(f"!! {slug}: {e}")
        return
    if args.slug and args.auto:
        p = render_for_slug(args.slug, args.auto, args.k,
                            args.cell_w, args.cell_h, args.grid, tag=args.tag)
        print(f"wrote {p}")
        return
    raise SystemExit("need --slug + (--auto or --frames), or --all-sessions --auto")


if __name__ == "__main__":
    main()
