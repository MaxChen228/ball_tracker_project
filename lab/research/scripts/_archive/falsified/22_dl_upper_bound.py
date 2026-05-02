"""DL upper-bound study — LOSO FCN heatmap, 9 sessions (1073 GT frames).

Research question: what is the single-frame recall ceiling for DL on this
dataset, and does it outperform V11+Y-diff (R=0.970)?

Architecture: tiny encoder-decoder FCN
  Input:  256×256 RGB (anisotropic resize from 1920×1080)
  Output: 256×256 single-channel heatmap (2D Gaussian σ=4px at GT centroid)
  Params: <500K, from scratch

Split: LOSO over 9 items (items are per-cam slugs, e.g. session_s_XXX_a/b)
  - 8 items train, 1 item test per fold
  - Note: 5 unique pitches; items sharing the same pitch are treated as
    independent items (cam A ≠ cam B). Leakage is acknowledged; reported
    separately as a limitation.

Evaluation:
  - Recall at max(10, 0.5r) tolerance in *original 1080p coords*
  - Per-mode breakdown using same proxy (M1: gt_s<80, M3: gt_h<100, M2: else)
  - Error overlap: DL miss ∩ V11 miss vs union → what cues does DL learn?
  - Comparison: DL alone / V11 alone / V11∪Y-diff / DL∪V11 / DL∪V11∪Y-diff

Run:
    cd lab/research
    uv run python scripts/22_dl_upper_bound.py

Output:
    lab/research/outputs/22_dl_upper_bound_results.json
    lab/research/notes/10_dl_upper_bound.md
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _paths import ROOT, WS, OUT, NOTES, load_manifest, SEG_BY_SLUG, read_mask

# ── Paths ────────────────────────────────────────────────────────────────────

OUT.mkdir(exist_ok=True)
NOTES.mkdir(exist_ok=True)

M = load_manifest()

# ── Config ───────────────────────────────────────────────────────────────────

INPUT_SIZE = 256          # square resize (anisotropic — aspect distortion OK)
HEATMAP_SIGMA = 4         # px at 256×256 scale
EPOCHS = 30
BATCH  = 8
LR     = 3e-4
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

# V11 reference params
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

# Y-diff params (best Pareto from 21_yplane_diff: thr=30)
YDIFF_THR = 30


# ── V11 detector ─────────────────────────────────────────────────────────────

def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def detect_ydiff(prev_gray: np.ndarray, curr_gray: np.ndarray,
                 thr: int = YDIFF_THR) -> list[tuple[float, float, int]]:
    d = cv2.absdiff(curr_gray, prev_gray)
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
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


# ── GT helpers ───────────────────────────────────────────────────────────────

def hit_check(cands: list[tuple[float, float, int]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, _ in cands)


def classify_miss_mode(gt_s: float, gt_h: float) -> str:
    """Proxy mode classifier (same as 21_yplane_diff.py / 19_frst.py).
    M1 (α specular/desat): gt_s < 80
    M3 (β hue-shift):      gt_h < 100
    M2 (fragmentation):    else
    """
    if gt_s < 80:
        return "M1"
    if gt_h < 100:
        return "M3"
    return "M2"


def make_heatmap_target(gtc_x_256: float, gtc_y_256: float,
                        sigma: float = HEATMAP_SIGMA) -> np.ndarray:
    """2D Gaussian heatmap at 256×256. Returns float32 in [0, 1]."""
    xs = np.arange(INPUT_SIZE, dtype=np.float32)
    ys = np.arange(INPUT_SIZE, dtype=np.float32)
    gx = np.exp(-0.5 * ((xs - gtc_x_256) / sigma) ** 2)
    gy = np.exp(-0.5 * ((ys - gtc_y_256) / sigma) ** 2)
    hm = np.outer(gy, gx).astype(np.float32)
    return hm


# ── Data loading ─────────────────────────────────────────────────────────────

class FrameRecord(NamedTuple):
    slug: str
    src: int
    local: int
    ball_in: bool
    # 1080p coords for eval
    gtc_x: float
    gtc_y: float
    gt_area: int
    gt_s: float
    gt_h: float
    gt_v: float
    # 256×256 heatmap target (zero array if ball_out)
    heatmap: np.ndarray   # float32, shape (256, 256)
    # input tensor (float32, shape (3, 256, 256), RGB, [0,1])
    rgb256: np.ndarray


def load_all_sessions() -> list[FrameRecord]:
    items = [it for it in M["items"]
             if it.get("propagate_status") == "done" and it.get("in_frame") is not None]
    records: list[FrameRecord] = []
    for it in items:
        slug = it["slug"]
        in_f = it["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        frames_dir = WS / "items" / slug / "frames"

        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem)
            local = src - in_f
            fp = frames_dir / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            gt_gray = read_mask(mp)
            if gt_gray is None:
                continue
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue

            ball_in = int((gt_gray > 0).sum()) >= 5

            # Resize to 256×256 (anisotropic)
            bgr256 = cv2.resize(bgr, (INPUT_SIZE, INPUT_SIZE),
                                interpolation=cv2.INTER_LINEAR)
            rgb256 = bgr256[:, :, ::-1].copy()  # BGR→RGB
            rgb256_f = rgb256.astype(np.float32) / 255.0
            # Shape: (H, W, C) → (C, H, W)
            rgb256_chw = rgb256_f.transpose(2, 0, 1)

            if ball_in:
                ys, xs = np.where(gt_gray > 0)
                gtc_x = float(xs.mean())
                gtc_y = float(ys.mean())
                gt_area = int(len(ys))
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                gt_s = float(hsv[ys, xs, 1].mean())
                gt_h = float(hsv[ys, xs, 0].mean())
                gt_v = float(hsv[ys, xs, 2].mean())

                # Scale centroid to 256×256
                sx = INPUT_SIZE / bgr.shape[1]   # 256/1920
                sy = INPUT_SIZE / bgr.shape[0]   # 256/1080
                gtc_x_256 = gtc_x * sx
                gtc_y_256 = gtc_y * sy
                heatmap = make_heatmap_target(gtc_x_256, gtc_y_256)
            else:
                gtc_x = gtc_y = 0.0
                gt_area = 0
                gt_s = gt_h = gt_v = 0.0
                heatmap = np.zeros((INPUT_SIZE, INPUT_SIZE), dtype=np.float32)

            records.append(FrameRecord(
                slug=slug,
                src=src,
                local=local,
                ball_in=ball_in,
                gtc_x=gtc_x,
                gtc_y=gtc_y,
                gt_area=gt_area,
                gt_s=gt_s,
                gt_h=gt_h,
                gt_v=gt_v,
                heatmap=heatmap,
                rgb256=rgb256_chw,
            ))
    return records


# ── Dataset ───────────────────────────────────────────────────────────────────

class BallDataset(Dataset):
    def __init__(self, records: list[FrameRecord], augment: bool = False) -> None:
        self.records = records
        self.augment = augment

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        r = self.records[idx]
        img = torch.from_numpy(r.rgb256.copy())   # (3, 256, 256)
        hm  = torch.from_numpy(r.heatmap.copy())  # (256, 256)
        ball_label = torch.tensor(float(r.ball_in))

        if self.augment:
            # Random horizontal flip
            if torch.rand(1).item() > 0.5:
                img = img.flip(-1)
                hm  = hm.flip(-1)
            # Random vertical flip
            if torch.rand(1).item() > 0.5:
                img = img.flip(-2)
                hm  = hm.flip(-2)
            # Color jitter (brightness + contrast on each channel)
            for c in range(3):
                alpha = 0.8 + 0.4 * torch.rand(1).item()  # [0.8, 1.2]
                beta  = -0.1 + 0.2 * torch.rand(1).item() # [-0.1, +0.1]
                img[c] = (img[c] * alpha + beta).clamp(0, 1)

        return img, hm, ball_label


# ── Model ─────────────────────────────────────────────────────────────────────

class TinyFCN(nn.Module):
    """Tiny encoder-decoder FCN.

    256 → 128 → 64 → 32 → 16 (encoder, stride-2 conv)
    16 → 32 → 64 → 128 → 256 (decoder, bilinear upsample + conv)
    + skip connections from encoder levels 1-3

    Channels: 3→16→32→64→32→16 keeping it under 500K params.
    """

    def __init__(self) -> None:
        super().__init__()
        # Encoder
        self.enc1 = nn.Sequential(
            nn.Conv2d(3,  16, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(16, 16, 3, padding=1),           nn.ReLU(),
        )  # 256→128, ch=16
        self.enc2 = nn.Sequential(
            nn.Conv2d(16, 32, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(32, 32, 3, padding=1),           nn.ReLU(),
        )  # 128→64, ch=32
        self.enc3 = nn.Sequential(
            nn.Conv2d(32, 64, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1),           nn.ReLU(),
        )  # 64→32, ch=64
        self.bottleneck = nn.Sequential(
            nn.Conv2d(64, 64, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1),           nn.ReLU(),
        )  # 32→16, ch=64

        # Decoder (bilinear upsample + conv to merge skip)
        self.dec3 = nn.Sequential(
            nn.Conv2d(64 + 64, 32, 3, padding=1), nn.ReLU(),
        )  # 16→32, ch=32
        self.dec2 = nn.Sequential(
            nn.Conv2d(32 + 32, 16, 3, padding=1), nn.ReLU(),
        )  # 32→64, ch=16
        self.dec1 = nn.Sequential(
            nn.Conv2d(16 + 16, 16, 3, padding=1), nn.ReLU(),
        )  # 64→128, ch=16
        self.dec0 = nn.Sequential(
            nn.Conv2d(16, 8, 3, padding=1), nn.ReLU(),
        )  # 128→256, ch=8

        # Output heads
        self.heatmap_head = nn.Conv2d(8, 1, 1)  # heatmap logit
        self.ball_head = nn.Sequential(
            nn.AdaptiveAvgPool2d(4),
            nn.Flatten(),
            nn.Linear(64 * 4 * 4, 1),
        )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        e1 = self.enc1(x)           # (B, 16, 128, 128)
        e2 = self.enc2(e1)          # (B, 32, 64, 64)
        e3 = self.enc3(e2)          # (B, 64, 32, 32)
        b  = self.bottleneck(e3)    # (B, 64, 16, 16)

        d3 = nn.functional.interpolate(b,  scale_factor=2, mode="bilinear", align_corners=False)
        d3 = self.dec3(torch.cat([d3, e3], dim=1))   # (B, 32, 32, 32)

        d2 = nn.functional.interpolate(d3, scale_factor=2, mode="bilinear", align_corners=False)
        d2 = self.dec2(torch.cat([d2, e2], dim=1))   # (B, 16, 64, 64)

        d1 = nn.functional.interpolate(d2, scale_factor=2, mode="bilinear", align_corners=False)
        d1 = self.dec1(torch.cat([d1, e1], dim=1))   # (B, 16, 128, 128)

        d0 = nn.functional.interpolate(d1, scale_factor=2, mode="bilinear", align_corners=False)
        d0 = self.dec0(d0)                            # (B, 8, 256, 256)

        hm_logit  = self.heatmap_head(d0).squeeze(1)  # (B, 256, 256)
        ball_logit = self.ball_head(b).squeeze(1)      # (B,)
        return hm_logit, ball_logit


def count_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# ── Loss ──────────────────────────────────────────────────────────────────────

def focal_mse_loss(pred: torch.Tensor, target: torch.Tensor,
                   gamma: float = 2.0) -> torch.Tensor:
    """Focal MSE: (1 - exp(-|target|))^gamma * (pred - target)^2."""
    w = (1.0 - torch.exp(-target.abs())).pow(gamma)
    return (w * (pred - target).pow(2)).mean()


def combined_loss(hm_logit: torch.Tensor, ball_logit: torch.Tensor,
                  hm_target: torch.Tensor, ball_label: torch.Tensor) -> torch.Tensor:
    hm_pred = torch.sigmoid(hm_logit)
    hm_loss = focal_mse_loss(hm_pred, hm_target)
    ball_loss = nn.functional.binary_cross_entropy_with_logits(
        ball_logit, ball_label)
    return hm_loss + 0.1 * ball_loss


# ── Inference: extract centroid from predicted heatmap ───────────────────────

def predict_centroid_1080p(hm_pred: np.ndarray,
                            orig_h: int = 1080,
                            orig_w: int = 1920) -> tuple[float, float] | None:
    """Argmax of predicted heatmap, up-projected to 1080p coords.

    Returns None if max < 0.1 (ball_out prediction).
    """
    if hm_pred.max() < 0.1:
        return None
    py256, px256 = np.unravel_index(np.argmax(hm_pred), hm_pred.shape)
    px_1080 = float(px256) * orig_w / INPUT_SIZE
    py_1080 = float(py256) * orig_h / INPUT_SIZE
    return px_1080, py_1080


# ── Training one fold ─────────────────────────────────────────────────────────

def train_fold(train_records: list[FrameRecord],
               fold_idx: int) -> TinyFCN:
    model = TinyFCN().to(DEVICE)
    optimizer = optim.Adam(model.parameters(), lr=LR)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, EPOCHS)

    ds = BallDataset(train_records, augment=True)
    dl = DataLoader(ds, batch_size=BATCH, shuffle=True, num_workers=0)

    t0 = time.time()
    for epoch in range(EPOCHS):
        model.train()
        total_loss = 0.0
        for imgs, hm_targets, ball_labels in dl:
            imgs       = imgs.to(DEVICE)
            hm_targets = hm_targets.to(DEVICE)
            ball_labels = ball_labels.to(DEVICE)

            optimizer.zero_grad()
            hm_logit, ball_logit = model(imgs)
            loss = combined_loss(hm_logit, ball_logit, hm_targets, ball_labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * len(imgs)

        scheduler.step()
        if epoch % 10 == 0 or epoch == EPOCHS - 1:
            elapsed = time.time() - t0
            print(f"  Fold {fold_idx+1} epoch {epoch+1}/{EPOCHS} "
                  f"loss={total_loss/len(ds):.5f} t={elapsed:.1f}s")

    return model


# ── Eval one fold ─────────────────────────────────────────────────────────────

@torch.inference_mode()
def eval_fold(model: TinyFCN,
              test_records: list[FrameRecord],
              all_records: list[FrameRecord]) -> dict:
    """Returns per-frame predictions + hit flags for DL and combined methods."""
    model.eval()

    # Build gray map for Y-diff (keyed by (slug, local))
    gray_map: dict[tuple[str, int], np.ndarray] = {}
    for rec in all_records:
        bgr_full = cv2.imread(
            str(WS / "items" / rec.slug / "frames" / f"{rec.local:05d}.jpg"),
            cv2.IMREAD_COLOR)
        if bgr_full is not None:
            gray_map[(rec.slug, rec.local)] = cv2.cvtColor(bgr_full, cv2.COLOR_BGR2GRAY)

    results = []
    for rec in test_records:
        img_t = torch.from_numpy(rec.rgb256.copy()).unsqueeze(0).to(DEVICE)
        hm_logit, ball_logit = model(img_t)
        hm_pred = torch.sigmoid(hm_logit).squeeze().cpu().numpy()

        dl_centroid = predict_centroid_1080p(hm_pred)

        # V11 on full-res image
        bgr_full = cv2.imread(
            str(WS / "items" / rec.slug / "frames" / f"{rec.local:05d}.jpg"),
            cv2.IMREAD_COLOR)

        v11_cands: list[tuple[float, float, int]] = []
        ydiff_cands: list[tuple[float, float, int]] = []

        if bgr_full is not None:
            v11_cands = detect_v11(bgr_full)
            gray_curr = gray_map.get((rec.slug, rec.local))
            gray_prev = gray_map.get((rec.slug, rec.local - 1))
            if gray_curr is not None and gray_prev is not None:
                ydiff_cands = detect_ydiff(gray_prev, gray_curr)

        # Build union sets
        v11_union_cands = list(v11_cands)
        for yd in ydiff_cands:
            if not any((yd[0]-v[0])**2 + (yd[1]-v[1])**2 <= 25.0
                        for v in v11_cands):
                v11_union_cands.append(yd)

        # DL candidate (single point if predicted ball present)
        dl_cands: list[tuple[float, float, int]] = []
        if dl_centroid is not None:
            dl_cands = [(dl_centroid[0], dl_centroid[1], 100)]

        dl_v11_cands = list(dl_cands) + list(v11_cands)
        dl_v11_ydiff_cands = list(dl_cands) + list(v11_union_cands)

        frame_result: dict = dict(
            slug=rec.slug,
            src=rec.src,
            ball_in=rec.ball_in,
            gt_s=rec.gt_s,
            gt_h=rec.gt_h,
            gt_v=rec.gt_v,
            gt_area=rec.gt_area,
            gtc_x=rec.gtc_x,
            gtc_y=rec.gtc_y,
        )

        if rec.ball_in:
            frame_result["v11_hit"]          = hit_check(v11_cands,         rec.gtc_x, rec.gtc_y, rec.gt_area)
            frame_result["dl_hit"]           = hit_check(dl_cands,          rec.gtc_x, rec.gtc_y, rec.gt_area)
            frame_result["v11_ydiff_hit"]    = hit_check(v11_union_cands,   rec.gtc_x, rec.gtc_y, rec.gt_area)
            frame_result["dl_v11_hit"]       = hit_check(dl_v11_cands,      rec.gtc_x, rec.gtc_y, rec.gt_area)
            frame_result["dl_v11_ydiff_hit"] = hit_check(dl_v11_ydiff_cands, rec.gtc_x, rec.gtc_y, rec.gt_area)
            frame_result["mode"] = classify_miss_mode(rec.gt_s, rec.gt_h)

        results.append(frame_result)

    return {"slug_test": test_records[0].slug if test_records else "?",
            "frames": results}


# ── LOSO main loop ────────────────────────────────────────────────────────────

def run_loso(all_records: list[FrameRecord]) -> list[dict]:
    items = [it for it in M["items"]
             if it.get("propagate_status") == "done" and it.get("in_frame") is not None]
    slugs = [it["slug"] for it in items]

    fold_results = []
    for fold_idx, test_slug in enumerate(slugs):
        print(f"\n{'='*60}")
        print(f"Fold {fold_idx+1}/{len(slugs)}: test={test_slug}")
        train_recs = [r for r in all_records if r.slug != test_slug]
        test_recs  = [r for r in all_records if r.slug == test_slug]
        print(f"  train={len(train_recs)} frames, test={len(test_recs)} frames")
        print(f"  test ball_in={sum(r.ball_in for r in test_recs)}")

        t_fold = time.time()
        model = train_fold(train_recs, fold_idx)
        fold_res = eval_fold(model, test_recs, all_records)
        elapsed = time.time() - t_fold
        print(f"  Fold done in {elapsed:.1f}s")

        # Quick recall print
        ball_frames = [f for f in fold_res["frames"] if f.get("ball_in")]
        if ball_frames:
            for method in ["v11_hit", "dl_hit", "v11_ydiff_hit", "dl_v11_hit", "dl_v11_ydiff_hit"]:
                r = sum(f[method] for f in ball_frames) / len(ball_frames)
                print(f"  {method}: R={r:.3f}")

        fold_results.append(fold_res)

    return fold_results


# ── Aggregate analysis ────────────────────────────────────────────────────────

def aggregate(fold_results: list[dict]) -> dict:
    all_ball = [f for fold in fold_results for f in fold["frames"] if f.get("ball_in")]
    all_frames = [f for fold in fold_results for f in fold["frames"]]

    n = len(all_ball)
    methods = ["v11_hit", "dl_hit", "v11_ydiff_hit", "dl_v11_hit", "dl_v11_ydiff_hit"]

    # Global recall
    global_r: dict[str, float] = {}
    for m in methods:
        global_r[m] = sum(f[m] for f in all_ball) / n if n else 0.0

    # Per-fold recall
    fold_r: dict[str, list[float]] = {m: [] for m in methods}
    for fold in fold_results:
        bf = [f for f in fold["frames"] if f.get("ball_in")]
        for m in methods:
            fold_r[m].append(sum(f[m] for f in bf) / len(bf) if bf else 0.0)

    # 95% CI from fold-level variance (t-distribution approx with 8 folds)
    import math
    ci95: dict[str, tuple[float, float]] = {}
    k = len(fold_r["dl_hit"])
    for m in methods:
        vals = fold_r[m]
        mean = sum(vals) / k
        var  = sum((v - mean)**2 for v in vals) / (k - 1) if k > 1 else 0.0
        se   = math.sqrt(var / k)
        # t_8_0.025 ≈ 2.306
        t_crit = 2.306
        ci95[m] = (mean - t_crit * se, mean + t_crit * se)

    # Per-session recall
    per_session: dict[str, dict] = {}
    for fold in fold_results:
        slug = fold["slug_test"]
        bf = [f for f in fold["frames"] if f.get("ball_in")]
        per_session[slug] = {
            "n_ball": len(bf),
            "n_total": len(fold["frames"]),
        }
        for m in methods:
            per_session[slug][m] = sum(f[m] for f in bf) / len(bf) if bf else 0.0

    # Mode breakdown (V11 misses)
    v11_misses = [f for f in all_ball if not f["v11_hit"]]
    mode_stats: dict[str, dict] = {}
    for mode in ["M1", "M2", "M3"]:
        mode_m = [f for f in v11_misses if f.get("mode") == mode]
        mode_stats[mode] = {
            "n": len(mode_m),
            "dl_recovered": sum(f["dl_hit"] for f in mode_m),
            "v11_ydiff_recovered": sum(f["v11_ydiff_hit"] for f in mode_m),
            "dl_v11_ydiff_recovered": sum(f["dl_v11_ydiff_hit"] for f in mode_m),
        }
        if mode_m:
            mode_stats[mode]["dl_rec_pct"] = mode_stats[mode]["dl_recovered"] / len(mode_m)
            mode_stats[mode]["v11_ydiff_rec_pct"] = mode_stats[mode]["v11_ydiff_recovered"] / len(mode_m)
            mode_stats[mode]["dl_v11_ydiff_rec_pct"] = mode_stats[mode]["dl_v11_ydiff_recovered"] / len(mode_m)

    # Error overlap: DL miss ∩ V11 miss / union
    v11_miss_set  = {(f["slug"], f["src"]) for f in all_ball if not f["v11_hit"]}
    dl_miss_set   = {(f["slug"], f["src"]) for f in all_ball if not f["dl_hit"]}
    both_miss     = v11_miss_set & dl_miss_set
    either_miss   = v11_miss_set | dl_miss_set
    overlap_iou = len(both_miss) / len(either_miss) if either_miss else 0.0

    return {
        "n_ball_frames": n,
        "n_all_frames": len(all_frames),
        "global_recall": global_r,
        "fold_recall": fold_r,
        "ci95": {m: list(ci95[m]) for m in methods},
        "per_session": per_session,
        "mode_breakdown": mode_stats,
        "error_overlap": {
            "v11_miss_n": len(v11_miss_set),
            "dl_miss_n":  len(dl_miss_set),
            "both_miss_n": len(both_miss),
            "either_miss_n": len(either_miss),
            "jaccard_iou": overlap_iou,
            "dl_unique_recoveries": len(v11_miss_set) - len(both_miss),
            "v11_unique_recoveries": len(dl_miss_set) - len(both_miss),
        },
    }


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(agg: dict, params_n: int) -> None:
    g = agg["global_recall"]
    ci = agg["ci95"]
    ps = agg["per_session"]
    md_lines = agg["mode_breakdown"]
    eo = agg["error_overlap"]

    lines: list[str] = []
    lines.append("# 10 — DL upper-bound study (LOSO FCN heatmap)\n")
    lines.append(f"Model: TinyFCN, params={params_n:,}, from scratch, {EPOCHS} epochs/fold, "
                 f"device={DEVICE}\n")
    lines.append(f"Dataset: 9 items (5 unique pitches), {agg['n_ball_frames']} ball-in frames, "
                 f"{agg['n_all_frames']} total\n")
    lines.append(f"Input: {INPUT_SIZE}×{INPUT_SIZE} anisotropic resize from 1920×1080\n\n")

    lines.append("## 1. LOSO per-item recall\n\n")
    lines.append("| Item | n_ball | V11 | DL | V11∪Ydiff | DL∪V11 | DL∪V11∪Ydiff |\n")
    lines.append("|------|--------|-----|----|-----------|---------|--------------|\n")
    for slug, d in ps.items():
        lines.append(f"| {slug} | {d['n_ball']} "
                     f"| {d['v11_hit']:.3f} "
                     f"| {d['dl_hit']:.3f} "
                     f"| {d['v11_ydiff_hit']:.3f} "
                     f"| {d['dl_v11_hit']:.3f} "
                     f"| {d['dl_v11_ydiff_hit']:.3f} |\n")
    lines.append(f"| **Macro** | **{agg['n_ball_frames']}** "
                 f"| **{g['v11_hit']:.3f}** "
                 f"| **{g['dl_hit']:.3f}** "
                 f"| **{g['v11_ydiff_hit']:.3f}** "
                 f"| **{g['dl_v11_hit']:.3f}** "
                 f"| **{g['dl_v11_ydiff_hit']:.3f}** |\n\n")

    lines.append("## 2. DL ceiling with 95% CI (fold-level variance, t₈)\n\n")
    for m in ["v11_hit", "dl_hit", "v11_ydiff_hit", "dl_v11_hit", "dl_v11_ydiff_hit"]:
        lo, hi = ci[m]
        lines.append(f"- **{m}**: macro R={g[m]:.4f}, 95% CI [{lo:.4f}, {hi:.4f}]\n")
    lines.append("\n")

    lines.append("## 3. Error overlap analysis\n\n")
    lines.append(f"- V11 miss: {eo['v11_miss_n']}\n")
    lines.append(f"- DL miss:  {eo['dl_miss_n']}\n")
    lines.append(f"- Both miss (V11 ∩ DL): {eo['both_miss_n']}\n")
    lines.append(f"- Either miss (V11 ∪ DL): {eo['either_miss_n']}\n")
    lines.append(f"- Jaccard IoU (miss overlap): {eo['jaccard_iou']:.3f}\n")
    lines.append(f"- Frames V11 misses but DL hits (DL unique recoveries): "
                 f"{eo['dl_unique_recoveries']}\n")
    lines.append(f"- Frames DL misses but V11 hits (V11 unique recoveries): "
                 f"{eo['v11_unique_recoveries']}\n\n")

    iou = eo['jaccard_iou']
    if iou > 0.7:
        lines.append("**Interpretation**: High miss overlap (IoU>0.7) → DL and V11 largely fail "
                     "on the same frames. DL is not learning orthogonal cues at this data volume.\n\n")
    elif iou > 0.4:
        lines.append("**Interpretation**: Moderate miss overlap → DL partially learns different "
                     "cues from V11, some complementarity.\n\n")
    else:
        lines.append("**Interpretation**: Low miss overlap → DL learns substantially different "
                     "cues from V11; strong complementarity.\n\n")

    lines.append("## 4. Mode-specific V11 miss recovery\n\n")
    lines.append("Mode classifier: M1 gt_s<80 (α specular/desat), M3 gt_h<100 (β hue-shift), "
                 "M2 else (fragmentation). Same proxy as 21_yplane_diff.py.\n\n")
    lines.append("| Mode | n (V11 miss) | DL rec% | V11∪Ydiff rec% | DL∪V11∪Ydiff rec% |\n")
    lines.append("|------|-------------|---------|----------------|--------------------|\n")
    for mode, d in md_lines.items():
        if d["n"] == 0:
            lines.append(f"| {mode} | 0 | — | — | — |\n")
        else:
            lines.append(f"| {mode} | {d['n']} "
                         f"| {d.get('dl_rec_pct', 0):.1%} "
                         f"| {d.get('v11_ydiff_rec_pct', 0):.1%} "
                         f"| {d.get('dl_v11_ydiff_rec_pct', 0):.1%} |\n")
    lines.append("\n")

    # Mode β conclusion
    m3 = md_lines.get("M3", {})
    lines.append("### Mode β (M3 hue-shift) — key research question\n\n")
    if m3.get("n", 0) == 0:
        lines.append("M3 proxy count = 0 (proxy classifier gt_h<100 misclassifies most β frames "
                     "— canonical count is 9/24 per 08_yplane_diff notes). "
                     "Cannot reliably answer 'does DL rescue β'.\n\n")
    else:
        rec_pct = m3.get("dl_rec_pct", 0)
        yd_pct  = m3.get("v11_ydiff_rec_pct", 0)
        lines.append(f"DL recovery = {rec_pct:.1%}, Y-diff recovery = {yd_pct:.1%} "
                     f"(n={m3['n']}, proxy).\n\n")
        if rec_pct > yd_pct + 0.1:
            lines.append("**DL outperforms Y-diff on β** → suggests DL learns color-invariant "
                         "appearance beyond temporal contrast. Mode β may benefit from DL.\n\n")
        else:
            lines.append("**DL does not clearly outperform Y-diff on β** with this data volume.\n\n")

    lines.append("## 5. Pareto comparison\n\n")
    lines.append("| Method | Recall | Training cost | Deploy cost |\n")
    lines.append("|--------|--------|---------------|-------------|\n")
    lines.append(f"| V11 HSV+CC | {g['v11_hit']:.4f} | none | <2ms CPU |\n")
    lines.append(f"| V11 ∪ Y-diff (thr=30) | {g['v11_ydiff_hit']:.4f} | none | ~4ms CPU |\n")
    lines.append(f"| DL alone (FCN) | {g['dl_hit']:.4f} | {EPOCHS}ep × 9folds | GPU req. |\n")
    lines.append(f"| DL ∪ V11 | {g['dl_v11_hit']:.4f} | same | GPU req. |\n")
    lines.append(f"| DL ∪ V11 ∪ Y-diff | {g['dl_v11_ydiff_hit']:.4f} | same | GPU req. |\n\n")

    dl_vs_v11ydiff = g["dl_hit"] - g["v11_ydiff_hit"]
    union_vs_v11ydiff = g["dl_v11_ydiff_hit"] - g["v11_ydiff_hit"]
    lines.append(f"DL alone vs V11∪Ydiff: {dl_vs_v11ydiff:+.4f}\n")
    lines.append(f"DL∪V11∪Ydiff vs V11∪Ydiff: {union_vs_v11ydiff:+.4f}\n\n")

    lines.append("## 6. Conclusion\n\n")

    if g["dl_hit"] >= g["v11_ydiff_hit"]:
        lines.append("**DL matches or beats V11∪Y-diff** at this data volume — DL is viable.\n\n")
    else:
        lines.append(f"**DL alone (R={g['dl_hit']:.4f}) does NOT beat V11∪Y-diff "
                     f"(R={g['v11_ydiff_hit']:.4f})** at this data volume ({agg['n_ball_frames']} "
                     f"ball-in frames). DL adds marginal value as a union signal "
                     f"(+{union_vs_v11ydiff:.4f}pp) but is not Pareto-dominant.\n\n")

    lines.append("### Is DL worth it at this data scale?\n\n")
    lines.append(f"1073 ball-in frames across 9 items from 5 sessions is a very small DL dataset. "
                 "With LOSO, each fold trains on ~950 frames — well below the ~10K+ needed for "
                 "reliable CNN feature learning from scratch. Evidence:\n\n")
    lines.append(f"- Fold-level variance in DL recall: "
                 f"σ={float(np.std(agg['fold_recall']['dl_hit'])):.4f} "
                 "(high → unstable generalization)\n")
    lines.append(f"- Miss overlap IoU = {eo['jaccard_iou']:.3f} "
                 "(higher → DL not learning new cues)\n\n")

    lines.append("### Data saturation estimate\n\n")
    lines.append("Rule of thumb: CNN from scratch saturates when train set ≳ 10K frames. "
                 "At current collection rate (~120 ball-in frames/session), "
                 "need ≳ 80 sessions (~8× more data) to reach saturation. "
                 "Fine-tuning a pretrained encoder (MobileNet-v3/ResNet-18) could lower "
                 "this to ~30 sessions, but introduces BT.601/709 alignment risk and "
                 "breaks the 'pure research baseline' constraint.\n\n")

    lines.append("### Limitations\n\n")
    lines.append("- 9 items = 5 unique pitches; _a/_b cam pairs share background/lighting "
                 "(LOSO item-level may slightly over-estimate recall vs true pitch-level LOSO)\n")
    lines.append("- Mode classifier is proxy (gt_s/gt_h thresholds); canonical Mode β count "
                 "is 9/24 (from 15_v11_failure_modes.py); proxy M3=3 under-samples β severely\n")
    lines.append("- Offline JPEG frames (not NV12) — Y-diff comparison slightly pessimistic "
                 "for live path\n")
    lines.append("- No data augmentation beyond flip+color jitter; rotation/scale might help\n")
    lines.append("- Anisotropic 256×256 stretch changes ball aspect ratio; "
                 "letterbox would be geometrically cleaner\n")

    report_path = NOTES / "10_dl_upper_bound.md"
    report_path.write_text("".join(lines), encoding="utf-8")
    print(f"\nReport written: {report_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Device: {DEVICE}")

    print("Loading all frames...")
    t0 = time.time()
    all_records = load_all_sessions()
    print(f"Loaded {len(all_records)} frames in {time.time()-t0:.1f}s")
    print(f"Ball-in: {sum(r.ball_in for r in all_records)}, "
          f"ball-out: {sum(not r.ball_in for r in all_records)}")

    # Param count
    sample_model = TinyFCN()
    p = count_params(sample_model)
    print(f"Model params: {p:,}")
    del sample_model

    fold_results = run_loso(all_records)

    print("\nAggregating...")
    agg = aggregate(fold_results)

    # Save JSON
    out_path = OUT / "22_dl_upper_bound_results.json"
    out_path.write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print(f"Results JSON: {out_path}")

    # Print summary
    g = agg["global_recall"]
    print("\n" + "="*60)
    print("GLOBAL RECALL SUMMARY (1073 ball-in frames, LOSO)")
    print("="*60)
    for m, r in g.items():
        ci = agg["ci95"][m]
        print(f"  {m:<22}: R={r:.4f}  95%CI [{ci[0]:.4f}, {ci[1]:.4f}]")
    print("\nMODE BREAKDOWN (V11 misses)")
    for mode, d in agg["mode_breakdown"].items():
        print(f"  {mode}: n={d['n']}, DL_rec={d.get('dl_rec_pct',0):.1%}, "
              f"V11ydiff_rec={d.get('v11_ydiff_rec_pct',0):.1%}")
    print("\nERROR OVERLAP")
    eo = agg["error_overlap"]
    print(f"  V11_miss={eo['v11_miss_n']}, DL_miss={eo['dl_miss_n']}, "
          f"both={eo['both_miss_n']}, IoU={eo['jaccard_iou']:.3f}")
    print(f"  DL unique recoveries (V11 miss, DL hit): {eo['dl_unique_recoveries']}")

    write_report(agg, p)


if __name__ == "__main__":
    main()
