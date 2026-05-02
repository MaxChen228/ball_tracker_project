"""26 — Multi-scale Y-plane temporal diff evaluation.

Research question: does |Y[t] - Y[t-k]| for k=2,3 add signal at apex /
slow-ball frames where k=1 diff is weak?

Three diff streams at thr=15:
  D1: |Y[t] - Y[t-1]|   baseline (matches Track J / 21_yplane_diff)
  D2: |Y[t] - Y[t-2]|   2-frame gap  ~8.3 ms at 240fps
  D3: |Y[t] - Y[t-3]|   3-frame gap  ~12.5 ms at 240fps

Each stream: absdiff → threshold(15) → CLOSE(3×3) → V11 shape gate.
Frames without enough buffer: explicit 0 cands, no fallback.

Apex hypothesis: GT Y-trajectory → ballistic fit → frames near |vy|_min
are "apex-proxy"; compare D1 vs D2/D3 hit rate on those frames.

Run: cd lab/research && uv run python scripts/26_multiscale_ydiff.py
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
M    = load_manifest()

# ── V11 gate (canonical) ──────────────────────────────────────────────────
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

THR = 15  # unified threshold for all Dn streams


# ── Detectors ─────────────────────────────────────────────────────────────

def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def detect_ydiff_gap(older: np.ndarray, curr: np.ndarray) -> list[tuple[float, float, int]]:
    """Compute |curr - older|, threshold, morph, shape gate."""
    d = cv2.absdiff(curr, older)
    _, m = cv2.threshold(d, THR, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def _shape_gate(m: np.ndarray) -> list[tuple[float, float, int]]:
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[tuple[float, float, int]] = []
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


def union_cands(*cand_lists: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Merge candidate lists, dedup within 5 px of earlier-list entry."""
    merged: list[tuple[float, float, int]] = []
    for cl in cand_lists:
        for cand in cl:
            if not any((cand[0] - m[0]) ** 2 + (cand[1] - m[1]) ** 2 <= 25.0
                       for m in merged):
                merged.append(cand)
    return merged


# ── GT helpers ────────────────────────────────────────────────────────────

def hit_check(cands: list[tuple[float, float, int]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, _ in cands)


def classify_miss_mode(gt_s: float, gt_h: float) -> str:
    """Proxy mode classifier — same as 21_yplane_diff."""
    if gt_s < 80:
        return "M1"
    if gt_h < 100:
        return "M3"
    return "M2"


# ── Data loading ──────────────────────────────────────────────────────────

def find_items() -> list[dict]:
    items = []
    for it in M["items"]:
        slug = it["slug"]
        for seg in it.get("segments", []):
            if seg.get("propagate_status") == "done" and seg.get("in_frame") is not None:
                seg_id = seg["id"]
                masks_dir = WS / "items" / slug / "masks" / seg_id
                if masks_dir.exists() and any(masks_dir.glob("*.png")):
                    items.append({
                        "slug": slug,
                        "in_frame": seg["in_frame"],
                        "seg_id": seg_id,
                        "fps": it.get("fps", 240.0),
                    })
                    break
    return items


def load_session(item: dict) -> tuple[list[dict], dict[int, np.ndarray]]:
    slug = item["slug"]
    in_f = item["in_frame"]
    seg_id = item["seg_id"]
    masks_dir = WS / "items" / slug / "masks" / seg_id
    frames_dir = WS / "items" / slug / "frames"

    # Pre-load all grays (entire session window for buffer lookback)
    local_to_gray: dict[int, np.ndarray] = {}
    for fp in sorted(frames_dir.glob("*.jpg")):
        local = int(fp.stem)
        g = read_mask(fp)
        if g is not None:
            local_to_gray[local] = g

    rows: list[dict] = []
    for mp in sorted(masks_dir.glob("*.png")):
        src = int(mp.stem)
        local = src - in_f
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        gt = read_mask(mp)
        if gt is None:
            continue
        ball_in = int((gt > 0).sum()) >= 5
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        row: dict = dict(slug=slug, src=src, local=local, ball_in=ball_in, bgr=bgr)
        if ball_in:
            ys, xs = np.where(gt > 0)
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            row.update(
                gtc_x=float(xs.mean()),
                gtc_y=float(ys.mean()),
                gt_area=int(len(ys)),
                gt_s=float(hsv[ys, xs, 1].mean()),
                gt_v=float(hsv[ys, xs, 2].mean()),
                gt_h=float(hsv[ys, xs, 0].mean()),
            )
        rows.append(row)
    return rows, local_to_gray


# ── Apex hypothesis: ballistic fit ────────────────────────────────────────

def fit_ballistic_vy(locals_: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Fit parabola to (local, y) and return estimated vy at each local.

    Y increases downward in image coords; apex = minimum Y in image = peak
    of physical trajectory.  At apex, |vy| is minimised.
    Returns array of |vy_fit| aligned to locals_.
    """
    if len(locals_) < 4:
        return np.full(len(locals_), np.nan)
    coeffs = np.polyfit(locals_.astype(float), ys.astype(float), 2)
    # dy/dt = 2*a*t + b  (t = local frame index)
    vy = 2.0 * coeffs[0] * locals_.astype(float) + coeffs[1]
    return np.abs(vy)


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    items = find_items()
    print(f"Sessions with GT: {len(items)}")

    # Accumulators
    # V11 global
    v11_total = 0
    v11_hits = 0

    # Per stream: {hits, total_ball_in, no_buf, cands_noball, noball_frames}
    streams = {
        "D1": dict(hits=0, total=0, no_buf=0, fp_cands=0, noball_n=0),
        "D2": dict(hits=0, total=0, no_buf=0, fp_cands=0, noball_n=0),
        "D3": dict(hits=0, total=0, no_buf=0, fp_cands=0, noball_n=0),
    }

    # Union accumulators
    unions = {
        "V11_D1":       dict(hits=0),
        "V11_D1_D2":    dict(hits=0),
        "V11_D1_D3":    dict(hits=0),
        "V11_D1_D2_D3": dict(hits=0),
    }
    union_total = 0  # shared denominator = v11_total

    # Mode-breakdown for V11 misses: per-stream recovery
    mode_acc = {
        "D1": dict(M1=0, M1r=0, M2=0, M2r=0, M3=0, M3r=0),
        "D2": dict(M1=0, M1r=0, M2=0, M2r=0, M3=0, M3r=0),
        "D3": dict(hits=0, M1=0, M1r=0, M2=0, M2r=0, M3=0, M3r=0),
    }
    # V11+D1 residual misses (32 frames): how many does D2/D3 add
    v11d1_miss_d2_rec = 0
    v11d1_miss_d3_rec = 0
    v11d1_miss_both_rec = 0

    # Apex analysis: collect per-session (local, gtc_y, hits_d1, hits_d2, hits_d3)
    apex_rows: list[dict] = []

    for item in items:
        slug = item["slug"]
        rows, local_to_gray = load_session(item)
        print(f"\n  [{slug}] frames={len(rows)} "
              f"ball_in={sum(r['ball_in'] for r in rows)}")

        # ── Per-session GT trajectory for ballistic fit ───────────────────
        ball_rows = [r for r in rows if r["ball_in"]]
        if len(ball_rows) >= 4:
            sess_locals = np.array([r["local"] for r in ball_rows])
            sess_ys     = np.array([r["gtc_y"] for r in ball_rows])
            vy_abs = fit_ballistic_vy(sess_locals, sess_ys)
        else:
            sess_locals = np.array([])
            vy_abs = np.array([])

        for row in rows:
            bgr    = row["bgr"]
            local  = row["local"]
            ball_in = row["ball_in"]
            gray_c = local_to_gray.get(local)

            # Retrieve buffer frames (explicit — no fallback)
            gray_m1 = local_to_gray.get(local - 1)  # t-1
            gray_m2 = local_to_gray.get(local - 2)  # t-2
            gray_m3 = local_to_gray.get(local - 3)  # t-3

            # V11
            v11_cands = detect_v11(bgr)
            v11_hit = False
            if ball_in:
                v11_total += 1
                union_total += 1
                v11_hit = hit_check(v11_cands, row["gtc_x"], row["gtc_y"], row["gt_area"])
                if v11_hit:
                    v11_hits += 1

            # D1
            if gray_m1 is not None and gray_c is not None:
                d1_cands = detect_ydiff_gap(gray_m1, gray_c)
            else:
                d1_cands = []
                streams["D1"]["no_buf"] += 1

            # D2
            if gray_m2 is not None and gray_c is not None:
                d2_cands = detect_ydiff_gap(gray_m2, gray_c)
            else:
                d2_cands = []
                streams["D2"]["no_buf"] += 1

            # D3
            if gray_m3 is not None and gray_c is not None:
                d3_cands = detect_ydiff_gap(gray_m3, gray_c)
            else:
                d3_cands = []
                streams["D3"]["no_buf"] += 1

            if ball_in:
                gtc_x = row["gtc_x"]
                gtc_y = row["gtc_y"]
                gt_area = row["gt_area"]

                for stream_name, cands in [("D1", d1_cands), ("D2", d2_cands), ("D3", d3_cands)]:
                    streams[stream_name]["total"] += 1
                    if hit_check(cands, gtc_x, gtc_y, gt_area):
                        streams[stream_name]["hits"] += 1

                # Union hits
                u_v11_d1        = union_cands(v11_cands, d1_cands)
                u_v11_d1_d2     = union_cands(v11_cands, d1_cands, d2_cands)
                u_v11_d1_d3     = union_cands(v11_cands, d1_cands, d3_cands)
                u_v11_d1_d2_d3  = union_cands(v11_cands, d1_cands, d2_cands, d3_cands)

                for key, uc in [
                    ("V11_D1",       u_v11_d1),
                    ("V11_D1_D2",    u_v11_d1_d2),
                    ("V11_D1_D3",    u_v11_d1_d3),
                    ("V11_D1_D2_D3", u_v11_d1_d2_d3),
                ]:
                    if hit_check(uc, gtc_x, gtc_y, gt_area):
                        unions[key]["hits"] += 1

                # Mode breakdown for V11 misses
                if not v11_hit:
                    mode = classify_miss_mode(row["gt_s"], row["gt_h"])
                    for stream_name, cands in [("D1", d1_cands), ("D2", d2_cands), ("D3", d3_cands)]:
                        acc = mode_acc[stream_name]
                        if mode == "M1":
                            acc["M1"] += 1
                            if hit_check(cands, gtc_x, gtc_y, gt_area):
                                acc["M1r"] += 1
                        elif mode == "M2":
                            acc["M2"] += 1
                            if hit_check(cands, gtc_x, gtc_y, gt_area):
                                acc["M2r"] += 1
                        elif mode == "M3":
                            acc["M3"] += 1
                            if hit_check(cands, gtc_x, gtc_y, gt_area):
                                acc["M3r"] += 1

                # V11+D1 residual misses → D2/D3 recovery
                hit_v11d1 = hit_check(u_v11_d1, gtc_x, gtc_y, gt_area)
                if not hit_v11d1:
                    hit_d2 = hit_check(d2_cands, gtc_x, gtc_y, gt_area)
                    hit_d3 = hit_check(d3_cands, gtc_x, gtc_y, gt_area)
                    if hit_d2:
                        v11d1_miss_d2_rec += 1
                    if hit_d3:
                        v11d1_miss_d3_rec += 1
                    if hit_d2 or hit_d3:
                        v11d1_miss_both_rec += 1

                # Apex: per-frame vy annotation
                if len(sess_locals) >= 4:
                    idx = np.where(sess_locals == local)[0]
                    vy_val = float(vy_abs[idx[0]]) if len(idx) > 0 else float("nan")
                else:
                    vy_val = float("nan")
                apex_rows.append(dict(
                    slug=slug, local=local,
                    vy_abs=vy_val,
                    hit_d1=hit_check(d1_cands, gtc_x, gtc_y, gt_area),
                    hit_d2=hit_check(d2_cands, gtc_x, gtc_y, gt_area),
                    hit_d3=hit_check(d3_cands, gtc_x, gtc_y, gt_area),
                    no_d1=gray_m1 is None,
                    no_d2=gray_m2 is None,
                    no_d3=gray_m3 is None,
                ))
            else:
                # No-ball: FP counting
                for stream_name, cands in [("D1", d1_cands), ("D2", d2_cands), ("D3", d3_cands)]:
                    streams[stream_name]["noball_n"] += 1
                    streams[stream_name]["fp_cands"] += len(cands)

    # ── Print results ──────────────────────────────────────────────────────
    n = v11_total  # total ball_in frames
    r_v11 = v11_hits / n if n else 0.0
    print(f"\n=== V11 baseline ===")
    print(f"  R={r_v11:.4f}  ({v11_hits}/{n})")

    print(f"\n=== Per-stream (alone) @ thr={THR} ===")
    print(f"{'stream':>8}  {'R_alone':>8}  {'hits/total':>12}  {'FP cands/f (noball)':>20}  {'no_buf':>7}")
    for sn in ["D1", "D2", "D3"]:
        s = streams[sn]
        r_a = s["hits"] / s["total"] if s["total"] else 0.0
        fp = s["fp_cands"] / s["noball_n"] if s["noball_n"] else 0.0
        print(f"  {sn:>6}  {r_a:>8.4f}  {s['hits']:>5}/{s['total']:<5}  "
              f"{fp:>20.1f}  {s['no_buf']:>7}")

    print(f"\n=== Cumulative union R (V11 baseline + Dn) ===")
    # V11 alone as reference row
    print(f"  {'V11 alone':>20}  R={r_v11:.4f}  ({v11_hits}/{n})")
    for key in ["V11_D1", "V11_D1_D2", "V11_D1_D3", "V11_D1_D2_D3"]:
        h = unions[key]["hits"]
        r = h / n if n else 0.0
        delta = r - r_v11
        print(f"  {key:>20}  R={r:.4f}  ({h}/{n})  Δ={delta:+.4f}")

    print(f"\n=== Mode breakdown (V11 misses only) ===")
    v11_miss = n - v11_hits
    print(f"  V11 miss total: {v11_miss}")
    print(f"  {'stream':>6}  {'M1(n)':>7}  {'M1_rec%':>9}  {'M2(n)':>7}  {'M2_rec%':>9}  {'M3(n)':>7}  {'M3_rec%':>9}")
    for sn in ["D1", "D2", "D3"]:
        acc = mode_acc[sn]
        m1r = acc["M1r"] / acc["M1"] * 100 if acc["M1"] else 0.0
        m2r = acc["M2r"] / acc["M2"] * 100 if acc["M2"] else 0.0
        m3r = acc["M3r"] / acc["M3"] * 100 if acc["M3"] else 0.0
        print(f"  {sn:>6}  {acc['M1']:>7}  {m1r:>8.1f}%  {acc['M2']:>7}  {m2r:>8.1f}%  "
              f"{acc['M3']:>7}  {m3r:>8.1f}%")

    # V11+D1 residual
    v11d1_hits = unions["V11_D1"]["hits"]
    v11d1_miss = n - v11d1_hits
    print(f"\n=== V11+D1 residual miss analysis ===")
    print(f"  V11+D1 miss: {v11d1_miss}")
    print(f"  D2 recovers from V11+D1 misses: {v11d1_miss_d2_rec}  "
          f"({v11d1_miss_d2_rec/v11d1_miss*100:.1f}% of residual)" if v11d1_miss else "")
    print(f"  D3 recovers from V11+D1 misses: {v11d1_miss_d3_rec}  "
          f"({v11d1_miss_d3_rec/v11d1_miss*100:.1f}% of residual)" if v11d1_miss else "")
    print(f"  D2∪D3 recovers: {v11d1_miss_both_rec}  "
          f"({v11d1_miss_both_rec/v11d1_miss*100:.1f}% of residual)" if v11d1_miss else "")

    # ── Apex hypothesis analysis ───────────────────────────────────────────
    print(f"\n=== Apex hypothesis (ballistic fit |vy|) ===")
    valid_apex = [r for r in apex_rows
                  if not np.isnan(r["vy_abs"]) and not r["no_d1"] and not r["no_d2"] and not r["no_d3"]]
    if valid_apex:
        vy_vals = np.array([r["vy_abs"] for r in valid_apex])
        # Apex proxy: bottom 20 percentile of |vy| across all valid ball frames
        apex_thr = float(np.percentile(vy_vals, 20))
        apex_frames = [r for r in valid_apex if r["vy_abs"] <= apex_thr]
        fast_frames = [r for r in valid_apex if r["vy_abs"] > apex_thr]

        def hit_rate(rows_: list[dict], key: str) -> float:
            return sum(r[key] for r in rows_) / len(rows_) if rows_ else 0.0

        print(f"  apex_thr (p20 |vy|) = {apex_thr:.2f} px/frame")
        print(f"  apex frames: {len(apex_frames)} / fast frames: {len(fast_frames)}")
        print(f"  {'stream':>6}  {'apex hit%':>10}  {'fast hit%':>10}  {'apex>fast?':>12}")
        for sn in ["D1", "D2", "D3"]:
            key = f"hit_{sn.lower()}"
            a_r = hit_rate(apex_frames, key) * 100
            f_r = hit_rate(fast_frames, key) * 100
            stronger = "YES" if a_r > f_r else "no"
            print(f"  {sn:>6}  {a_r:>9.1f}%  {f_r:>9.1f}%  {stronger:>12}")

        # Δ(D2-D1) and Δ(D3-D1) at apex vs fast
        apex_d1 = hit_rate(apex_frames, "hit_d1")
        apex_d2 = hit_rate(apex_frames, "hit_d2")
        apex_d3 = hit_rate(apex_frames, "hit_d3")
        fast_d1 = hit_rate(fast_frames, "hit_d1")
        fast_d2 = hit_rate(fast_frames, "hit_d2")
        fast_d3 = hit_rate(fast_frames, "hit_d3")
        print(f"\n  D2−D1 advantage at apex: {(apex_d2-apex_d1)*100:+.1f}pp  "
              f"(fast: {(fast_d2-fast_d1)*100:+.1f}pp)")
        print(f"  D3−D1 advantage at apex: {(apex_d3-apex_d1)*100:+.1f}pp  "
              f"(fast: {(fast_d3-fast_d1)*100:+.1f}pp)")
    else:
        print("  insufficient data for apex analysis")

    # ── Save JSON ──────────────────────────────────────────────────────────
    results = dict(
        r_v11=r_v11, v11_hits=v11_hits, v11_total=n,
        streams={
            sn: dict(
                r_alone=streams[sn]["hits"] / streams[sn]["total"] if streams[sn]["total"] else 0.0,
                hits=streams[sn]["hits"],
                total=streams[sn]["total"],
                fp_cands_per_noball=streams[sn]["fp_cands"] / streams[sn]["noball_n"] if streams[sn]["noball_n"] else 0.0,
                no_buf=streams[sn]["no_buf"],
            ) for sn in ["D1", "D2", "D3"]
        },
        unions={
            key: dict(hits=unions[key]["hits"], r=unions[key]["hits"] / n if n else 0.0)
            for key in unions
        },
        mode_breakdown={
            sn: {
                "M1": mode_acc[sn]["M1"], "M1_rec": mode_acc[sn]["M1r"],
                "M2": mode_acc[sn]["M2"], "M2_rec": mode_acc[sn]["M2r"],
                "M3": mode_acc[sn]["M3"], "M3_rec": mode_acc[sn]["M3r"],
            } for sn in ["D1", "D2", "D3"]
        },
        v11d1_miss=v11d1_miss,
        v11d1_miss_d2_rec=v11d1_miss_d2_rec,
        v11d1_miss_d3_rec=v11d1_miss_d3_rec,
        v11d1_miss_both_rec=v11d1_miss_both_rec,
        thr=THR,
    )
    out_path = OUT / "26_multiscale_ydiff_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n[done] {out_path}")


if __name__ == "__main__":
    main()
