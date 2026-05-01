"""Diagnose why a session has 0 segments (or any other surprise outcome).

This script is intentionally exhaustive — when you don't know what's
wrong, dump everything and look. Distill once you've seen the data.

Sections:
  A. Top-line counts (raw frames, candidates, triangulated, surviving)
  B. Per-camera candidate timeline (does each camera see the ball?)
  C. Triangulated point distribution by path × time × residual
  D. Surviving point distribution (post residual gate) — spatial clustering
  E. Sub-burst analysis: are there any continuous time-segments that
     look ballistic if isolated?
  F. Run frozen segmenter with default + relaxed parameters
  G. Plots: top-down xy, side xz, residual-vs-time, point timeline

Usage: python analyses/01_diagnose_session.py <session_id>
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_loader import load_result, load_pitch, session_has_pitch  # noqa: E402
from runner import run_segmenter  # noqa: E402


def _section(title: str) -> None:
    print(f"\n{'═' * 70}\n{title}\n{'═' * 70}")


def _sub(title: str) -> None:
    print(f"\n── {title} " + "─" * (66 - len(title)))


def diagnose(sid: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = load_result(sid)

    _section(f"SESSION {sid}")

    # ── A. Top-line counts ─────────────────────────────────────────────
    _sub("A. Top-line counts")
    fc = result.get("frame_counts_by_path", {})
    print("frame_counts_by_path (raw candidate-bearing frames per camera):")
    for path, by_cam in fc.items():
        print(f"  {path:12s}: A={by_cam.get('A', 0):5d}  B={by_cam.get('B', 0):5d}")
    tbp = result.get("triangulated_by_path", {})
    print("triangulated_by_path:")
    for path, pts in tbp.items():
        print(f"  {path:12s}: {len(pts):5d} points")
    sbp = result.get("segments_by_path", {})
    print("segments_by_path:")
    for path, segs in sbp.items():
        print(f"  {path:12s}: {len(segs):5d} segments")
    print(f"gap_threshold_m: {result.get('gap_threshold_m')}")
    print(f"cost_threshold: {result.get('cost_threshold')}")

    # ── B. Per-camera candidate timeline ───────────────────────────────
    _sub("B. Per-camera raw candidate counts (from pitches)")
    for cam in ("A", "B"):
        if not session_has_pitch(sid, cam):
            print(f"  cam {cam}: no pitch JSON found")
            continue
        pitch = load_pitch(sid, cam)
        for path_key in ("frames_live", "frames_server_post"):
            frames = pitch.get(path_key, []) or []
            cand_counts = [len(f.get("candidates") or []) for f in frames]
            total_cands = sum(cand_counts)
            n_with = sum(1 for c in cand_counts if c > 0)
            n_zero = sum(1 for c in cand_counts if c == 0)
            multi = sum(1 for c in cand_counts if c > 1)
            print(f"  cam {cam} / {path_key}:")
            print(f"    frames={len(frames)}  with_cand={n_with}  zero={n_zero}  multi-cand={multi}  total_cands={total_cands}")
            if frames:
                ts = [f.get("timestamp_s", 0) for f in frames]
                print(f"    t span: {min(ts):.3f} – {max(ts):.3f}  ({max(ts)-min(ts):.3f}s)")
                # Time histogram of frames-with-candidate
                ts_arr = np.array(ts)
                cc_arr = np.array(cand_counts)
                t0 = ts_arr.min()
                bins = np.arange(t0, ts_arr.max() + 0.5, 0.5)
                if len(bins) >= 2:
                    n_total, _ = np.histogram(ts_arr, bins=bins)
                    n_hit, _ = np.histogram(ts_arr[cc_arr > 0], bins=bins)
                    print(f"    frames-with-candidate by 0.5s bucket:")
                    for i in range(len(n_total)):
                        bar = "█" * min(n_hit[i], 40)
                        print(f"      +{bins[i]-t0:5.1f}s: {n_hit[i]:3d}/{n_total[i]:3d}  {bar}")

    # ── C/D. Per-path triangulated + survivor analysis ─────────────────
    for path in ("live", "server_post"):
        raw = tbp.get(path, [])
        if not raw:
            print(f"\n[path={path}] no triangulated points — skip")
            continue
        _section(f"PATH={path}")
        gap_thr = result["gap_threshold_m"]

        ts = np.array([p["t_rel_s"] for p in raw])
        rs = np.array([p["residual_m"] for p in raw])
        xs = np.array([p["x_m"] for p in raw])
        ys = np.array([p["y_m"] for p in raw])
        zs = np.array([p["z_m"] for p in raw])

        # Sort by time
        order = np.argsort(ts)
        ts, rs, xs, ys, zs = ts[order], rs[order], xs[order], ys[order], zs[order]

        keep = rs <= gap_thr
        _sub(f"C. Residual distribution (gap_threshold_m={gap_thr})")
        bins = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0, 100.0]
        hist, _ = np.histogram(rs, bins=bins)
        for i, h in enumerate(hist):
            mark = " ✓" if bins[i + 1] <= gap_thr else ""
            print(f"  res ∈ [{bins[i]:5.2f}, {bins[i+1]:5.2f}): {h:5d}{mark}")
        print(f"  survivors (res ≤ {gap_thr}): {keep.sum()} / {len(rs)}")

        _sub("D. Spatial distribution of survivors")
        if keep.sum() == 0:
            print("  no survivors — nothing to summarize")
        else:
            sx, sy, sz = xs[keep], ys[keep], zs[keep]
            print(f"  x: mean={sx.mean():+.3f}  std={sx.std():.3f}  range=[{sx.min():+.2f}, {sx.max():+.2f}]")
            print(f"  y: mean={sy.mean():+.3f}  std={sy.std():.3f}  range=[{sy.min():+.2f}, {sy.max():+.2f}]")
            print(f"  z: mean={sz.mean():+.3f}  std={sz.std():.3f}  range=[{sz.min():+.2f}, {sz.max():+.2f}]")

        _sub("E. Time histogram (0.5s buckets)")
        bins_t = np.arange(ts.min(), ts.max() + 0.5, 0.5)
        if len(bins_t) >= 2:
            hist_all, _ = np.histogram(ts, bins=bins_t)
            hist_keep, _ = np.histogram(ts[keep], bins=bins_t)
            t0 = ts.min()
            for i, (ha, hk) in enumerate(zip(hist_all, hist_keep)):
                bar = "█" * min(hk, 50) + "·" * min(ha - hk, 50)
                print(f"  +{bins_t[i]-t0:5.1f}s: all={ha:4d}  kept={hk:4d}  {bar}")

        # Burst detection: continuous chunks of survivors with small dt
        if keep.sum() >= 5:
            _sub("F. Burst detection on survivors (chunks with all dt < 25ms)")
            sk_ts = ts[keep]
            sk_xs, sk_ys, sk_zs = xs[keep], ys[keep], zs[keep]
            dts = np.diff(sk_ts)
            break_idx = np.where(dts > 0.025)[0]
            chunks = np.split(np.arange(len(sk_ts)), break_idx + 1)
            chunks = [c for c in chunks if len(c) >= 5]
            print(f"  found {len(chunks)} continuous chunks of ≥5 points")
            for ci, chunk in enumerate(chunks[:10]):
                t_lo = sk_ts[chunk[0]]
                t_hi = sk_ts[chunk[-1]]
                dx = sk_xs[chunk[-1]] - sk_xs[chunk[0]]
                dy = sk_ys[chunk[-1]] - sk_ys[chunk[0]]
                dz = sk_zs[chunk[-1]] - sk_zs[chunk[0]]
                disp = np.sqrt(dx * dx + dy * dy + dz * dz)
                dur = t_hi - t_lo
                avg_speed = disp / max(dur, 1e-9)
                xs_c, ys_c, zs_c = sk_xs[chunk], sk_ys[chunk], sk_zs[chunk]
                print(
                    f"  chunk {ci}: t={t_lo - sk_ts[0]:5.2f}-{t_hi - sk_ts[0]:5.2f}s "
                    f"({dur*1000:5.0f}ms, n={len(chunk):3d})  "
                    f"disp={disp:.2f}m  avg_speed={avg_speed:5.2f}m/s  "
                    f"z∈[{zs_c.min():+.2f},{zs_c.max():+.2f}]  "
                    f"xy_span=({xs_c.max()-xs_c.min():.2f},{ys_c.max()-ys_c.min():.2f})"
                )

        # Run segmenter — default + relaxed
        _sub("G. Frozen segmenter runs (this path only)")
        for label, kwargs in [
            ("default", dict()),
            ("relaxed v_min=2", dict(v_min_mps=2.0)),
            ("relaxed v_min=1, min_disp=0.10", dict(v_min_mps=1.0, min_displacement_m=0.10)),
            ("super loose", dict(v_min_mps=0.5, v_max_mps=80, min_displacement_m=0.05, min_seg_len=4)),
            ("no residual gate, default", dict()),
        ]:
            apply_gate = label != "no residual gate, default"
            try:
                segs, _pts = run_segmenter(
                    raw, gap_threshold_m=gap_thr,
                    apply_residual_gate=apply_gate,
                    **kwargs,
                )
            except Exception as exc:
                print(f"  {label:50s}: ERROR {exc}")
                continue
            n_used = sum(len(s.indices) for s in segs)
            print(f"  {label:50s}: {len(segs)} segs, total points used={n_used}")
            for si, s in enumerate(segs[:3]):
                print(f"      seg{si}: n={len(s.indices)}  dur={s.t_end-s.t_start:.3f}s  "
                      f"|v0|={np.linalg.norm(s.v0):.2f}m/s  rmse={s.rmse_m:.3f}m")

    # ── Plots ──────────────────────────────────────────────────────────
    _make_plots(sid, result, out_dir)


def _make_plots(sid: str, result: dict, out_dir: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n[plot] matplotlib not available — skipping plots")
        return

    tbp = result.get("triangulated_by_path", {})
    gap_thr = result["gap_threshold_m"]

    for path in ("live", "server_post"):
        raw = tbp.get(path, [])
        if not raw:
            continue
        ts = np.array([p["t_rel_s"] for p in raw])
        rs = np.array([p["residual_m"] for p in raw])
        xs = np.array([p["x_m"] for p in raw])
        ys = np.array([p["y_m"] for p in raw])
        zs = np.array([p["z_m"] for p in raw])
        order = np.argsort(ts)
        ts, rs, xs, ys, zs = ts[order], rs[order], xs[order], ys[order], zs[order]
        keep = rs <= gap_thr

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(f"{sid}  /  path={path}  ({keep.sum()}/{len(rs)} survive res ≤ {gap_thr}m)")

        ax = axes[0, 0]
        ax.scatter(xs[~keep], ys[~keep], s=3, c="#cccccc", label=f"culled ({(~keep).sum()})")
        sc = ax.scatter(xs[keep], ys[keep], s=8, c=ts[keep], cmap="viridis", label=f"keep ({keep.sum()})")
        ax.set_xlabel("x_m"); ax.set_ylabel("y_m"); ax.set_title("top-down (x-y), color=t")
        ax.legend(loc="upper right"); ax.set_aspect("equal")
        plt.colorbar(sc, ax=ax, label="t_rel_s")

        ax = axes[0, 1]
        ax.scatter(xs[~keep], zs[~keep], s=3, c="#cccccc")
        ax.scatter(xs[keep], zs[keep], s=8, c=ts[keep], cmap="viridis")
        ax.axhline(0, color="brown", lw=0.5, alpha=0.5, label="z=0 (ground)")
        ax.set_xlabel("x_m"); ax.set_ylabel("z_m"); ax.set_title("side (x-z)")
        ax.legend()

        ax = axes[1, 0]
        ax.scatter(ts, rs, s=3, c=["g" if k else "r" for k in keep], alpha=0.5)
        ax.axhline(gap_thr, color="k", lw=0.8, ls="--", label=f"gap_threshold={gap_thr}")
        ax.set_xlabel("t_rel_s"); ax.set_ylabel("residual_m"); ax.set_title("residual vs time")
        ax.set_yscale("log"); ax.legend()

        ax = axes[1, 1]
        # Per-axis position vs time, survivors only
        if keep.any():
            ax.plot(ts[keep], xs[keep], ".", ms=3, label="x")
            ax.plot(ts[keep], ys[keep], ".", ms=3, label="y")
            ax.plot(ts[keep], zs[keep], ".", ms=3, label="z")
        ax.set_xlabel("t_rel_s"); ax.set_ylabel("position (m)")
        ax.set_title("survivor xyz vs time")
        ax.legend()

        plt.tight_layout()
        out = out_dir / f"{sid}_{path}.png"
        plt.savefig(out, dpi=110)
        plt.close(fig)
        print(f"[plot] {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python analyses/01_diagnose_session.py <session_id>")
        sys.exit(2)
    sid = sys.argv[1]
    out = Path(__file__).resolve().parent.parent / "reports" / "01_diagnose_session"
    diagnose(sid, out)
