"""28 — Trajectory-RANSAC inlier emit.

Premise: real ball traces a parabola in screen-Y over consecutive frames.
Distractors don't. RANSAC ballistic fit across a sliding window picks
inlier candidates; emit only those.

Single physics constraint (no tuning knob beyond inlier tolerance).
Generalizable: same fit, same residual norm, every session.

Approach:
  - For each session, run V11 detector on every frame (full clip, not
    just GT frames). For each frame: list of (cx, cy, area).
  - Sliding window of W=15 frames.
  - For each window, take top-3-by-area cand from each frame as fit
    points (cap at 3 to bound RANSAC combinatorial; ball is usually
    largest blob even when not winner). RANSAC fit:
        y(t) = a·t² + b·t + c
    on (frame_idx, cy) pairs. Independent fit on cx for sanity. Use cy
    for inlier test (Y is dominated by gravity, more discriminative).
  - Inlier threshold: |cy_obs − cy_fit| ≤ 5 px.
  - Emit: candidates that are inliers AND pass V11 shape/area gate.

Score: R_top1 with production cost ranker, on GT-labelled frames only.
Compare to PROD baseline (0.615) and V11 alone (0.102).

Run: cd lab/research && uv run python scripts/28_traj_ransac.py
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import numpy as np
import cv2
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

sys.path.insert(0, str(ROOT / "server"))
from candidate_selector import Candidate, score_candidates  # noqa

OUT.mkdir(parents=True, exist_ok=True)
FIG_DIR = OUT / "_figures"; FIG_DIR.mkdir(parents=True, exist_ok=True)

TOL_PX = 10.0
WINDOW = 15
MAX_PER_FRAME = 3
INLIER_PX = 5.0
RANSAC_ITERS = 50

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

CandFeat = tuple[float, float, int, float, float]


def detect_v11(bgr: np.ndarray) -> list[CandFeat]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[CandFeat] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        asp = min(w, h) / max(w, h)
        if asp < V11["aspect"]: continue
        fill = a / (w * h)
        if fill < V11["fill"]: continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a, float(asp), float(fill)))
    return out


def ransac_y_parabola(ts: np.ndarray, ys: np.ndarray,
                      frame_idx: np.ndarray, target_t: int,
                      rng: np.random.Generator) -> np.ndarray | None:
    """Returns boolean inlier mask (over input array) or None if can't fit.
    Need >= 3 distinct frames; sample 3 at random per iter; pick the fit
    with most inliers AND that covers target_t (the frame we're emitting on)."""
    if len(ts) < 3:
        return None
    unique_frames = np.unique(frame_idx)
    if len(unique_frames) < 3:
        return None
    best_inliers: np.ndarray | None = None
    best_count = -1
    for _ in range(RANSAC_ITERS):
        # Sample 3 distinct frames, then 1 cand per frame
        chosen_frames = rng.choice(unique_frames, size=3, replace=False)
        idxs = []
        for f in chosen_frames:
            mask = frame_idx == f
            cand_ids = np.where(mask)[0]
            idxs.append(rng.choice(cand_ids))
        idxs = np.array(idxs)
        ts_s = ts[idxs]; ys_s = ys[idxs]
        try:
            coeffs = np.polyfit(ts_s, ys_s, 2)
        except np.linalg.LinAlgError:
            continue
        # Reject non-physical: a (gravity) must be positive (Y grows downward).
        if coeffs[0] <= 0:
            continue
        y_pred = np.polyval(coeffs, ts)
        inliers = np.abs(ys - y_pred) <= INLIER_PX
        # Must include at least one cand on the target frame
        if not inliers[frame_idx == target_t].any():
            continue
        if inliers.sum() > best_count:
            best_count = int(inliers.sum())
            best_inliers = inliers
    return best_inliers


def session_pass(slug: str, in_f: int) -> dict:
    """Return dict with per-frame {src: list[CandFeat after RANSAC]}."""
    frames_dir = WS / "items" / slug / "frames"
    # Step 1: run V11 on every frame, store top-MAX_PER_FRAME by area.
    raw: dict[int, list[CandFeat]] = {}
    for fp in sorted(frames_dir.glob("*.jpg")):
        local = int(fp.stem); src = local + in_f
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None: continue
        cands = detect_v11(bgr)
        cands.sort(key=lambda c: -c[2])  # desc by area
        raw[src] = cands[:MAX_PER_FRAME]
    sources = sorted(raw.keys())
    if len(sources) < 5:
        return {s: raw[s] for s in sources}

    # Step 2: per target frame, build sliding window, run RANSAC, emit inliers.
    rng = np.random.default_rng(42)
    out: dict[int, list[CandFeat]] = {}
    src_arr = np.array(sources)
    for target in sources:
        # Window [target-W/2, target+W/2]
        lo = target - WINDOW // 2
        hi = target + WINDOW // 2
        win_srcs = [s for s in sources if lo <= s <= hi]
        ts_list = []; ys_list = []; frame_list = []; cand_list = []
        for s in win_srcs:
            for c in raw[s]:
                ts_list.append(s)
                ys_list.append(c[1])
                frame_list.append(s)
                cand_list.append(c)
        if len(ts_list) < 3:
            out[target] = []
            continue
        ts = np.array(ts_list, dtype=float)
        ys = np.array(ys_list, dtype=float)
        frame_idx = np.array(frame_list, dtype=int)
        inliers = ransac_y_parabola(ts, ys, frame_idx, target, rng)
        if inliers is None:
            out[target] = []
            continue
        out[target] = [cand_list[i] for i in range(len(cand_list))
                       if inliers[i] and frame_idx[i] == target]
    return out


def gt_centroid(mask: np.ndarray) -> tuple[float, float]:
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def rank_by_cost(cands: list[CandFeat]) -> list[CandFeat]:
    if not cands: return []
    cs = [Candidate(cx=c[0], cy=c[1], area=c[2], aspect=c[3], fill=c[4]) for c in cands]
    costs = score_candidates(cs)
    return [c for _, c in sorted(zip(costs, cands), key=lambda p: p[0])]


def hit_top1(ranked: list[CandFeat], gx: float, gy: float) -> bool:
    if not ranked: return False
    c = ranked[0]
    return (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX


def hit_emit(cands: list[CandFeat], gx: float, gy: float) -> bool:
    return any((c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX for c in cands)


def main():
    t0 = time.time()
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    per_session: list[dict] = []
    grand = {"top1": [], "emit": [], "n_emit": []}
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set:
            continue
        emit_per_src = session_pass(slug, in_f)
        sess = {"slug": slug, "n_gt": 0, "top1": 0, "emit": 0, "n_emit_sum": 0}
        for src in sorted(gt_set):
            mask = read_mask(masks_dir / f"{src:05d}.png")
            if mask is None or (mask > 0).sum() < 20:
                continue
            gx, gy = gt_centroid(mask)
            cands = emit_per_src.get(src, [])
            ranked = rank_by_cost(cands)
            sess["n_gt"] += 1
            sess["n_emit_sum"] += len(cands)
            if hit_top1(ranked, gx, gy): sess["top1"] += 1
            if hit_emit(cands, gx, gy): sess["emit"] += 1
            grand["top1"].append(hit_top1(ranked, gx, gy))
            grand["emit"].append(hit_emit(cands, gx, gy))
            grand["n_emit"].append(len(cands))
        if sess["n_gt"] > 0:
            sess["R_top1"] = sess["top1"] / sess["n_gt"]
            sess["R_emit"] = sess["emit"] / sess["n_gt"]
            sess["mean_n"] = sess["n_emit_sum"] / sess["n_gt"]
        per_session.append(sess)
        print(f"  {slug:<28} n_gt={sess['n_gt']:>4}  "
              f"R_top1={sess.get('R_top1', 0):.3f}  "
              f"R_emit={sess.get('R_emit', 0):.3f}  "
              f"mean_n={sess.get('mean_n', 0):.2f}", flush=True)
    R_top1 = float(np.mean(grand["top1"]))
    R_emit = float(np.mean(grand["emit"]))
    mean_n = float(np.mean(grand["n_emit"]))

    print()
    print(f"AGGREGATE  N={len(grand['top1'])}  "
          f"R_top1={R_top1:.3f}  R_emit={R_emit:.3f}  mean_n={mean_n:.2f}")
    # PROD comparison
    prod_R_top1 = 0.615
    print(f"PROD baseline R_top1=0.615  → delta = {R_top1 - prod_R_top1:+.3f}")

    per_session_sorted = sorted(per_session, key=lambda s: s.get("R_top1", -1))
    print(f"\nWorst 5 sessions by R_top1:")
    for s in per_session_sorted[:5]:
        if "R_top1" in s:
            print(f"  {s['slug']:<28} R_top1={s['R_top1']:.3f} R_emit={s['R_emit']:.3f}")

    out_json = OUT / "28_traj_ransac.json"
    out_json.write_text(json.dumps({
        "R_top1": R_top1, "R_emit": R_emit, "mean_n": mean_n,
        "n_frames": len(grand["top1"]),
        "per_session": per_session,
        "params": {"WINDOW": WINDOW, "MAX_PER_FRAME": MAX_PER_FRAME,
                   "INLIER_PX": INLIER_PX, "RANSAC_ITERS": RANSAC_ITERS},
    }, indent=2))
    print(f"\n[saved] {out_json}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
