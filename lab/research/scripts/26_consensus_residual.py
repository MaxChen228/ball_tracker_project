"""26 — Consensus residual analysis.

Identifies frames that V11 ∪ Y-diff(15) ∪ Y-diff(30) all miss, then deeply
quantifies their physical signature. Goal is to characterise the
single-frame stateless dead zone — where future work cannot use HSV/Y-diff
no matter how tuned.

Outputs:
  outputs/26_consensus_residual.npz   — per-residual metrics
  outputs/26_residual_visu_*.png      — annotated frames
  outputs/26_baseline_stats.json      — hit-baseline distributions

Run:
  cd lab/research && uv run python scripts/26_consensus_residual.py
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

OUT.mkdir(parents=True, exist_ok=True)

M = load_manifest()

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)
YDIFF_THRS = [15, 30]

# ── Detectors (mirror 22_cue_independence.py) ────────────────────────────────

def detect_v11(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m), m, hsv


def detect_ydiff(prev_gray, curr_gray, thr):
    d = cv2.absdiff(curr_gray, prev_gray)
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def _shape_gate(m):
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        asp = min(w, h) / max(w, h)
        if asp < V11["aspect"]:
            continue
        fill = a / (w * h)
        if fill < V11["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    return out


def hit_check(cands, gtc_x, gtc_y, gt_area):
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, _ in cands)


def classify_miss_canonical(mask_v11, gt_mask, cands):
    ys, xs = np.where(gt_mask > 0)
    gtc = (float(xs.mean()), float(ys.mean()))
    r = float(np.sqrt(len(ys) / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    if mask_v11[ys, xs].sum() == 0:
        return "M1"
    for cx, cy, _ in cands:
        if (cx - gtc[0]) ** 2 + (cy - gtc[1]) ** 2 <= tol2:
            return "HIT"
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask_v11, connectivity=8)
    near = []
    for i in range(1, n):
        cx2, cy2 = float(cents[i, 0]), float(cents[i, 1])
        if (cx2 - gtc[0]) ** 2 + (cy2 - gtc[1]) ** 2 <= tol2:
            near.append(i)
    if not near:
        return "M2"
    best = max(near, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    a = int(stats[best, cv2.CC_STAT_AREA])
    w = int(stats[best, cv2.CC_STAT_WIDTH])
    h_s = int(stats[best, cv2.CC_STAT_HEIGHT])
    if a < V11["area"][0]:
        return "M2"
    asp = min(w, h_s) / max(w, h_s)
    fill = a / (w * h_s)
    if asp < V11["aspect"]:
        return "M3"
    if fill < V11["fill"]:
        return "M4"
    return "M5"


# ── Physical metrics ─────────────────────────────────────────────────────────

def measure_frame(bgr, gt_mask, hsv, mask_v11, v11_cands):
    """Return rich physical-signature dict for a single frame."""
    H, W = bgr.shape[:2]
    ys, xs = np.where(gt_mask > 0)
    gtc_x, gtc_y = float(xs.mean()), float(ys.mean())
    gt_area = int(len(ys))

    # Bbox
    bx0, bx1 = int(xs.min()), int(xs.max())
    by0, by1 = int(ys.min()), int(ys.max())
    bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
    bbox_aspect = min(bw, bh) / max(bw, bh)
    bbox_fill = gt_area / (bw * bh)

    # GT region BGR/HSV stats
    gt_b = float(bgr[ys, xs, 0].mean()); gt_b_std = float(bgr[ys, xs, 0].std())
    gt_g = float(bgr[ys, xs, 1].mean()); gt_g_std = float(bgr[ys, xs, 1].std())
    gt_r = float(bgr[ys, xs, 2].mean()); gt_r_std = float(bgr[ys, xs, 2].std())
    gt_h = float(hsv[ys, xs, 0].mean()); gt_h_std = float(hsv[ys, xs, 0].std())
    gt_s = float(hsv[ys, xs, 1].mean()); gt_s_std = float(hsv[ys, xs, 1].std())
    gt_v = float(hsv[ys, xs, 2].mean()); gt_v_std = float(hsv[ys, xs, 2].std())

    # Surrounding ring (dilate GT by 2× equivalent radius, exclude GT)
    r_eq = max(2, int(np.sqrt(gt_area / np.pi)))
    k_dil = 2 * r_eq + 1
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_dil, k_dil))
    dil = cv2.dilate(gt_mask, kern)
    ring = (dil > 0) & (gt_mask == 0)
    if ring.any():
        ring_v = float(hsv[ring, 2].mean())
        ring_s = float(hsv[ring, 1].mean())
        ring_h = float(hsv[ring, 0].mean())
        ring_b = float(bgr[ring, 0].mean())
    else:
        ring_v = ring_s = ring_h = ring_b = float("nan")

    # Contrast (signed: GT - ring)
    contrast_v = gt_v - ring_v
    contrast_s = gt_s - ring_s
    contrast_b = gt_b - ring_b

    # Edge strength inside GT (Sobel on V channel)
    sobel_x = cv2.Sobel(hsv[..., 2], cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(hsv[..., 2], cv2.CV_32F, 0, 1, ksize=3)
    smag = np.sqrt(sobel_x ** 2 + sobel_y ** 2)
    edge_in_gt = float(smag[ys, xs].mean())
    edge_ring = float(smag[ring].mean()) if ring.any() else float("nan")
    edge_global = float(smag.mean())

    # Global frame stats
    g_v_mean = float(hsv[..., 2].mean())
    g_v_std  = float(hsv[..., 2].std())
    g_s_mean = float(hsv[..., 1].mean())

    # Frame-edge proximity
    edge_dist = min(gtc_x, gtc_y, W - 1 - gtc_x, H - 1 - gtc_y)

    # Clutter: V11 candidates within 100 px of GT
    clutter = sum(1 for cx, cy, _ in v11_cands
                  if (cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= 100 ** 2)

    return dict(
        gtc_x=gtc_x, gtc_y=gtc_y, gt_area=gt_area,
        bbox_w=bw, bbox_h=bh, bbox_aspect=float(bbox_aspect), bbox_fill=float(bbox_fill),
        gt_b=gt_b, gt_g=gt_g, gt_r=gt_r,
        gt_b_std=gt_b_std, gt_g_std=gt_g_std, gt_r_std=gt_r_std,
        gt_h=gt_h, gt_s=gt_s, gt_v=gt_v,
        gt_h_std=gt_h_std, gt_s_std=gt_s_std, gt_v_std=gt_v_std,
        ring_v=ring_v, ring_s=ring_s, ring_h=ring_h, ring_b=ring_b,
        contrast_v=contrast_v, contrast_s=contrast_s, contrast_b=contrast_b,
        edge_in_gt=edge_in_gt, edge_ring=edge_ring, edge_global=edge_global,
        g_v_mean=g_v_mean, g_v_std=g_v_std, g_s_mean=g_s_mean,
        edge_dist=float(edge_dist), clutter=int(clutter),
    )


# ── Iterate sessions ─────────────────────────────────────────────────────────

def collect():
    items = []
    for it in M["items"]:
        slug = it["slug"]
        for seg in it.get("segments", []):
            if seg.get("propagate_status") == "done" and seg.get("in_frame") is not None:
                seg_id = seg["id"]
                masks_dir = WS / "items" / slug / "masks" / seg_id
                if masks_dir.exists() and any(masks_dir.glob("*.png")):
                    items.append(dict(slug=slug, in_frame=seg["in_frame"], seg_id=seg_id))
                    break
    print(f"Sessions: {len(items)}")

    all_records = []
    for item in items:
        slug = item["slug"]
        in_f = item["in_frame"]
        seg_id = item["seg_id"]
        masks_dir = WS / "items" / slug / "masks" / seg_id
        frames_dir = WS / "items" / slug / "frames"

        # Pre-load grayscales for all neighboring frames
        local_to_gray = {}
        local_to_path = {}
        for fp in sorted(frames_dir.glob("*.jpg")):
            local = int(fp.stem)
            local_to_path[local] = fp
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is not None:
                local_to_gray[local] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        mask_paths = sorted(masks_dir.glob("*.png"))
        for mp in mask_paths:
            src = int(mp.stem)
            local = src - in_f
            fp = frames_dir / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            gt = read_mask(mp)
            if gt is None or (gt > 0).sum() < 5:
                continue
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue

            ys, xs = np.where(gt > 0)
            gtc_x, gtc_y = float(xs.mean()), float(ys.mean())
            gt_area = int(len(ys))

            v11_cands, mask_v11, hsv = detect_v11(bgr)
            v11_hit = hit_check(v11_cands, gtc_x, gtc_y, gt_area)
            mode = "HIT" if v11_hit else classify_miss_canonical(mask_v11, gt, v11_cands)

            gray_curr = local_to_gray.get(local)
            gray_prev = local_to_gray.get(local - 1)
            has_prev = gray_curr is not None and gray_prev is not None

            yd_hit = {}
            for thr in YDIFF_THRS:
                if has_prev:
                    yd_hit[thr] = hit_check(detect_ydiff(gray_prev, gray_curr, thr),
                                             gtc_x, gtc_y, gt_area)
                else:
                    yd_hit[thr] = False

            phys = measure_frame(bgr, gt, hsv, mask_v11, v11_cands)

            # Y-diff GT-region magnitude (signal that even threshold can't catch)
            if has_prev:
                d = cv2.absdiff(gray_curr, gray_prev)
                yd_gt_mag = float(d[ys, xs].mean())
                yd_gt_max = float(d[ys, xs].max())
            else:
                yd_gt_mag = float("nan")
                yd_gt_max = float("nan")

            rec = dict(
                slug=slug, src=src, local=local,
                in_frame=in_f,
                v11=int(v11_hit),
                yd15=int(yd_hit[15]), yd30=int(yd_hit[30]),
                has_prev=int(has_prev),
                mode=mode,
                yd_gt_mag=yd_gt_mag, yd_gt_max=yd_gt_max,
                **phys,
            )
            all_records.append(rec)

        print(f"  {slug}: {sum(1 for r in all_records if r['slug']==slug)} GT frames")

    return all_records, items


def trajectory_context(records):
    """For each record, attach prev/next hit + miss-run stats."""
    by_slug = {}
    for r in records:
        by_slug.setdefault(r["slug"], []).append(r)
    for slug, recs in by_slug.items():
        recs.sort(key=lambda r: r["local"])
        consensus = [(max(r["v11"], r["yd15"], r["yd30"])) for r in recs]
        for i, r in enumerate(recs):
            r["prev_hit"] = int(consensus[i - 1]) if i > 0 else -1
            r["next_hit"] = int(consensus[i + 1]) if i + 1 < len(recs) else -1
            # miss run length: count consecutive miss including self
            j = i
            while j >= 0 and consensus[j] == 0:
                j -= 1
            run_start = j + 1
            j = i
            while j < len(recs) and consensus[j] == 0:
                j += 1
            run_end = j  # exclusive
            r["miss_run_len"] = run_end - run_start
            r["miss_run_pos"] = i - run_start  # 0..len-1, position within run
            # flight position: fraction (0..1) along ball-in segment
            r["flight_pos"] = i / max(1, len(recs) - 1)
    return records


# ── Visualisation ────────────────────────────────────────────────────────────

def visualize(records_residual, items_by_slug, out_dir):
    in_frame_by_slug = {it["slug"]: it["in_frame"] for it in items_by_slug}
    paths = []
    for r in records_residual:
        slug = r["slug"]
        local = r["local"]
        frames_dir = WS / "items" / slug / "frames"
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        H, W = bgr.shape[:2]
        gtc_x, gtc_y = int(r["gtc_x"]), int(r["gtc_y"])
        # GT yellow circle
        r_eq = max(4, int(np.sqrt(r["gt_area"] / np.pi)))
        cv2.circle(bgr, (gtc_x, gtc_y), r_eq + 2, (0, 255, 255), 2)
        cv2.drawMarker(bgr, (gtc_x, gtc_y), (0, 255, 255),
                       markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)

        # V11 candidates (red)
        v11_cands, _, _ = detect_v11(bgr)
        for cx, cy, _ in v11_cands:
            cv2.circle(bgr, (int(cx), int(cy)), 6, (0, 0, 255), 1)

        # Y-diff candidates (magenta)
        gray_curr = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        prev_fp = frames_dir / f"{local-1:05d}.jpg"
        if prev_fp.exists():
            prev_bgr = cv2.imread(str(prev_fp), cv2.IMREAD_COLOR)
            if prev_bgr is not None:
                gray_prev = cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2GRAY)
                for cx, cy, _ in detect_ydiff(gray_prev, gray_curr, 15):
                    cv2.circle(bgr, (int(cx), int(cy)), 5, (255, 0, 255), 1)

        # Stats overlay
        lines = [
            f"{slug} src={r['src']} mode={r['mode']}",
            f"GT HSV=({r['gt_h']:.0f},{r['gt_s']:.0f},{r['gt_v']:.0f}) area={r['gt_area']}",
            f"ring V={r['ring_v']:.0f} S={r['ring_s']:.0f} contrast_V={r['contrast_v']:+.0f}",
            f"global V={r['g_v_mean']:.0f} edge_in={r['edge_in_gt']:.0f}",
            f"yd_gt_mag={r['yd_gt_mag']:.1f} max={r['yd_gt_max']:.0f}  clutter={r['clutter']}",
            f"prev_hit={r['prev_hit']} next_hit={r['next_hit']} run_len={r['miss_run_len']}",
        ]
        y0 = 30
        for ln in lines:
            cv2.putText(bgr, ln, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(bgr, ln, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 1, cv2.LINE_AA)
            y0 += 24

        out_path = out_dir / f"26_residual_{slug}_src{r['src']}.png"
        cv2.imwrite(str(out_path), bgr)
        paths.append(str(out_path))
    return paths


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    records, items = collect()
    records = trajectory_context(records)
    print(f"\nTotal GT frames: {len(records)}")

    n_v11 = sum(r["v11"] for r in records)
    n_yd15 = sum(r["yd15"] for r in records)
    n_yd30 = sum(r["yd30"] for r in records)
    n_or = sum(1 for r in records if r["v11"] or r["yd15"] or r["yd30"])
    print(f"V11 hits={n_v11}  yd15={n_yd15}  yd30={n_yd30}  OR={n_or}")

    residuals = [r for r in records if r["v11"] == 0 and r["yd15"] == 0 and r["yd30"] == 0]
    hits      = [r for r in records if r["v11"] == 1]
    print(f"Residual (all-3 miss): {len(residuals)}")
    print(f"Hit baseline:          {len(hits)}")

    no_prev = [r for r in residuals if r["has_prev"] == 0]
    print(f"  residuals without prev frame (structural artifact): {len(no_prev)}")

    # Mode breakdown
    from collections import Counter
    print(f"  mode dist: {Counter(r['mode'] for r in residuals)}")

    # ── Effect-size table (residual vs hit baseline) ─────────────────────────
    feats = ["gt_area", "gt_v", "gt_s", "gt_h",
             "ring_v", "ring_s",
             "contrast_v", "contrast_s", "contrast_b",
             "edge_in_gt", "edge_ring",
             "g_v_mean", "g_v_std", "g_s_mean",
             "edge_dist", "clutter",
             "yd_gt_mag", "yd_gt_max",
             "bbox_aspect", "bbox_fill",
             "miss_run_len", "flight_pos"]

    def stats(arr):
        a = np.array([x for x in arr if not (isinstance(x, float) and np.isnan(x))], dtype=float)
        if len(a) == 0:
            return dict(mean=float("nan"), std=float("nan"), p50=float("nan"))
        return dict(mean=float(a.mean()), std=float(a.std()),
                    p50=float(np.median(a)), p25=float(np.percentile(a, 25)),
                    p75=float(np.percentile(a, 75)))

    print(f"\n=== Effect Size: residual vs hit ===")
    print(f"  {'feature':<16}  {'res_med':>9}  {'hit_med':>9}  {'cohen_d':>8}")
    effect_size = {}
    for f in feats:
        rv = [r[f] for r in residuals]
        hv = [r[f] for r in hits]
        rs = stats(rv); hs = stats(hv)
        # Cohen's d (pooled std)
        sd_p = np.sqrt(0.5 * (rs["std"] ** 2 + hs["std"] ** 2)) if rs["std"] > 0 or hs["std"] > 0 else 0
        d = (rs["mean"] - hs["mean"]) / sd_p if sd_p > 0 else 0
        effect_size[f] = dict(res=rs, hit=hs, cohen_d=float(d))
        print(f"  {f:<16}  {rs['p50']:>9.2f}  {hs['p50']:>9.2f}  {d:>+8.2f}")

    # ── Save ─────────────────────────────────────────────────────────────────
    np.savez_compressed(
        OUT / "26_consensus_residual.npz",
        records=np.array([json.dumps(r) for r in records]),
        residuals=np.array([json.dumps(r) for r in residuals]),
        effect_size=np.array([json.dumps(effect_size)]),
    )

    # Residual table dump (CSV-like for the .md)
    table_rows = []
    for r in sorted(residuals, key=lambda r: (r["slug"], r["src"])):
        table_rows.append(dict(
            slug=r["slug"], src=r["src"], mode=r["mode"],
            gtc=(int(r["gtc_x"]), int(r["gtc_y"])),
            area=r["gt_area"],
            gt_hsv=(round(r["gt_h"], 1), round(r["gt_s"], 1), round(r["gt_v"], 1)),
            ring_v=round(r["ring_v"], 1),
            contrast_v=round(r["contrast_v"], 1),
            edge_in=round(r["edge_in_gt"], 1),
            yd_max=round(r["yd_gt_max"], 1) if not np.isnan(r["yd_gt_max"]) else None,
            clutter=r["clutter"],
            prev_hit=r["prev_hit"], next_hit=r["next_hit"],
            run_len=r["miss_run_len"],
            flight_pos=round(r["flight_pos"], 2),
            edge_dist=round(r["edge_dist"], 0),
            has_prev=r["has_prev"],
        ))
    (OUT / "26_residual_table.json").write_text(json.dumps(table_rows, indent=2))

    # ── Visualise residuals ──────────────────────────────────────────────────
    paths = visualize(residuals, items, OUT)
    print(f"\nVisualised {len(paths)} residual frames to {OUT}")

    print(f"\n[done] outputs/26_consensus_residual.npz + 26_residual_table.json")


if __name__ == "__main__":
    main()
