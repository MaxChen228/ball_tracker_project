"""Quantify HSV vs Lab separability for the deep-blue ball.

Metrics:
  1. Per-channel Fisher discriminant ratio
        F = (mu_b - mu_g)^2 / (var_b + var_g)
  2. Multivariate Mahalanobis ROC: fit Gaussian on ball pixels in
       (H,S,V) and (L,a,b), score every test pixel by Mahalanobis
       distance squared, sweep threshold, plot TPR-FPR.
  3. Compare against current iOS HSV cube gate
       (data/presets/blue_ball.json or data/detection_config.json).

Train/test split: leave-one-session-out (LOSO) macro average.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "lab-research" / "outputs"
DATA = np.load(OUT / "pixel_samples.npz", allow_pickle=True)


def fisher(b: np.ndarray, g: np.ndarray) -> float:
    return float((b.mean() - g.mean()) ** 2 / (b.var() + g.var() + 1e-9))


def mahalanobis_score(x: np.ndarray, mu: np.ndarray, cov_inv: np.ndarray) -> np.ndarray:
    d = x.astype(np.float64) - mu
    return np.einsum("ij,jk,ik->i", d, cov_inv, d)


def fit_gaussian(x: np.ndarray):
    mu = x.mean(axis=0)
    cov = np.cov(x, rowvar=False)
    cov += np.eye(cov.shape[0]) * 1e-3  # ridge
    return mu, np.linalg.inv(cov)


def roc(scores_pos: np.ndarray, scores_neg: np.ndarray, n_pts: int = 200):
    """Return (fpr, tpr, auc). Lower score = more likely ball."""
    lo = float(min(scores_pos.min(), scores_neg.min()))
    hi = float(max(scores_pos.max(), scores_neg.max()))
    ths = np.linspace(lo, hi, n_pts)
    tpr = np.array([(scores_pos <= t).mean() for t in ths])
    fpr = np.array([(scores_neg <= t).mean() for t in ths])
    # AUC via trapezoid; sort by fpr ascending
    order = np.argsort(fpr)
    auc = float(np.trapezoid(tpr[order], fpr[order]))
    return fpr, tpr, auc


def hsv_cube_gate(hsv: np.ndarray, lo: tuple[int,int,int], hi: tuple[int,int,int]) -> np.ndarray:
    H, S, V = hsv[:,0], hsv[:,1], hsv[:,2]
    return ((H>=lo[0])&(H<=hi[0])&(S>=lo[1])&(S<=hi[1])&(V>=lo[2])&(V<=hi[2]))


def main():
    ball_hsv = DATA["ball_hsv"]; bg_hsv = DATA["bg_hsv"]
    ball_lab = DATA["ball_lab"]; bg_lab = DATA["bg_lab"]
    meta = DATA["meta"]

    print("=== Per-pixel Fisher discriminant ratio (higher = more separable) ===")
    for name, ball, bg, ch_names in [
        ("HSV", ball_hsv, bg_hsv, list("HSV")),
        ("Lab", ball_lab, bg_lab, list("Lab")),
    ]:
        for i, c in enumerate(ch_names):
            f = fisher(ball[:, i].astype(np.float64), bg[:, i].astype(np.float64))
            print(f"  {name}.{c}: F = {f:.3f}")

    # LOSO: split by session for honest generalization
    sessions = [(slug, int(b), int(g)) for slug, b, g in meta]
    cum = 0; ranges = []
    for slug, nb, _ng in sessions:
        ranges.append((slug, cum, cum + nb)); cum += nb
    cum = 0; ranges_bg = []
    for slug, _nb, ng in sessions:
        ranges_bg.append((slug, cum, cum + ng)); cum += ng
    assert cum == len(bg_hsv)

    print("\n=== LOSO Mahalanobis ROC (trained on N-1 sessions, tested on held-out) ===")
    print(f"{'session':<26} {'space':<5} {'AUC':>7} {'TPR@FPR=1%':>11} {'TPR@FPR=5%':>11}")
    summaries = {"HSV": [], "Lab": []}
    for held_idx, (slug, b0, b1) in enumerate(ranges):
        bg0, bg1 = ranges_bg[held_idx][1], ranges_bg[held_idx][2]
        for space, ball, bg in [
            ("HSV", ball_hsv, bg_hsv),
            ("Lab", ball_lab, bg_lab),
        ]:
            train_ball = np.concatenate([ball[:b0], ball[b1:]], axis=0)
            test_ball  = ball[b0:b1]
            test_bg    = bg[bg0:bg1]
            if len(train_ball) < 100 or len(test_ball) < 50 or len(test_bg) < 50:
                continue
            mu, cov_inv = fit_gaussian(train_ball.astype(np.float64))
            sp = mahalanobis_score(test_ball, mu, cov_inv)
            sn = mahalanobis_score(test_bg, mu, cov_inv)
            fpr, tpr, auc = roc(sp, sn)
            tpr1 = float(np.interp(0.01, fpr, tpr))
            tpr5 = float(np.interp(0.05, fpr, tpr))
            summaries[space].append((auc, tpr1, tpr5))
            print(f"{slug:<26} {space:<5} {auc:>7.4f} {tpr1:>11.3f} {tpr5:>11.3f}")

    print("\n=== LOSO macro average ===")
    for space, rows in summaries.items():
        a = np.array(rows)
        print(f"  {space}: AUC={a[:,0].mean():.4f}  TPR@1%={a[:,1].mean():.3f}  TPR@5%={a[:,2].mean():.3f}")

    # Current iOS HSV gate operating point
    cfg_path = ROOT / "data" / "detection_config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        # Find blue_ball preset values; structure: {"presets": ..., "active": ...}
        # Or load directly from preset file.
    preset_path = ROOT / "data" / "presets" / "blue_ball.json"
    if preset_path.exists():
        preset = json.loads(preset_path.read_text())
        hsv_cfg = preset.get("hsv") or preset.get("hsv_range") or preset
        # Extract lo/hi tuples
        lo = (
            int(hsv_cfg.get("h_min", hsv_cfg.get("hue_min", 105))),
            int(hsv_cfg.get("s_min", hsv_cfg.get("sat_min", 140))),
            int(hsv_cfg.get("v_min", hsv_cfg.get("val_min", 40))),
        )
        hi = (
            int(hsv_cfg.get("h_max", hsv_cfg.get("hue_max", 112))),
            int(hsv_cfg.get("s_max", hsv_cfg.get("sat_max", 255))),
            int(hsv_cfg.get("v_max", hsv_cfg.get("val_max", 255))),
        )
        print(f"\n=== Current iOS HSV cube gate operating point: lo={lo}  hi={hi} ===")
        tpr = hsv_cube_gate(ball_hsv, lo, hi).mean()
        fpr = hsv_cube_gate(bg_hsv, lo, hi).mean()
        print(f"  pixel TPR = {tpr:.3f}   pixel FPR = {fpr:.5f}")
    else:
        print(f"\n[warn] {preset_path} not found, skipping current-gate evaluation")

    np.savez_compressed(
        OUT / "separability.npz",
        hsv_summary=np.array(summaries["HSV"]),
        lab_summary=np.array(summaries["Lab"]),
    )
    print(f"\n[saved] {OUT / 'separability.npz'}")


if __name__ == "__main__":
    main()
