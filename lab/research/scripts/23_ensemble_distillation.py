"""Ensemble distillation of orthogonal detection cues — research study.

See lab/research/notes/12_ensemble_distillation_design.md for the design.

Conditions:
  A: GT-only (CNN baseline, no teacher)             — primary loss only
  B: Distill no-aux (teacher channels backbone-fed) — primary + λ_ball
  C: Distill + cue-consistency aux loss             — primary + cue + ball

LOSO over 9 items. Cache 3-channel teacher heatmaps once before LOSO.

Run:
    cd lab/research
    uv run python scripts/23_ensemble_distillation.py [--conditions A,C] \
        [--epochs 30] [--ablation-folds 0,3,6]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

# ── Paths ─────────────────────────────────────────────────────────────────────

OUT.mkdir(exist_ok=True)
NOTES.mkdir(exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ball_detector import BallDetector  # noqa: E402

# Reuse FRST from script 19
import importlib.util
from _paths import ROOT, WS, OUT, NOTES
_frst_spec = importlib.util.spec_from_file_location(
    "frst_mod", Path(__file__).resolve().parent / "19_frst.py"
)
_frst_mod = importlib.util.module_from_spec(_frst_spec)
_frst_spec.loader.exec_module(_frst_mod)
frst_compute = _frst_mod.frst
frst_candidates = _frst_mod.frst_candidates

M = json.loads((WS / "manifest.json").read_text())


def _active_seg(it: dict) -> dict | None:
    """Return the active done segment dict, or None if not ready."""
    sid = it.get("active_segment_id")
    for s in it.get("segments", []):
        if (s.get("id") == sid and s.get("propagate_status") == "done"
                and s.get("in_frame") is not None):
            return s
    return None


# ── Config ────────────────────────────────────────────────────────────────────

INPUT_SIZE = 256
HEATMAP_SIGMA = 4
EPOCHS_DEFAULT = 30
BATCH = 8
LR = 3e-4
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

YDIFF_THR = 15  # best union threshold per 11_cue_independence
FRST_RADII = [3, 5, 8, 12]
FRST_THR = 0.8  # tuned in 19_frst on session_s_16ec069a_b
FRST_TOPK = 8   # cap teacher peaks to avoid noise flood

LAMBDA_CUE = 0.3
LAMBDA_BALL = 0.1

# ── Detectors (V11 / Y-diff) ──────────────────────────────────────────────────

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
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        if min(w, h) / max(w, h) < V11["aspect"]:
            continue
        if a / (w * h) < V11["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    return out


def detect_frst_topk(gray: np.ndarray, k: int = FRST_TOPK) -> list[tuple[float, float, float]]:
    cands = frst_candidates(gray, FRST_RADII, threshold=FRST_THR, nms_r=5)
    cands.sort(key=lambda c: -c[2])
    return cands[:k]


def hit_check(cands, gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, *_ in cands)


def classify_miss_mode(gt_s: float, gt_h: float) -> str:
    if gt_s < 80:
        return "M1"
    if gt_h < 100:
        return "M3"
    return "M2"


# ── Heatmap helpers ───────────────────────────────────────────────────────────

def gaussian_splat(centres_256: list[tuple[float, float]],
                   sigma: float = HEATMAP_SIGMA) -> np.ndarray:
    """Sum of 2D Gaussians at given (x, y) centres in 256² space, clipped to [0,1]."""
    hm = np.zeros((INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
    if not centres_256:
        return hm
    xs = np.arange(INPUT_SIZE, dtype=np.float32)
    ys = np.arange(INPUT_SIZE, dtype=np.float32)
    for cx, cy in centres_256:
        if not (0 <= cx < INPUT_SIZE and 0 <= cy < INPUT_SIZE):
            continue
        gx = np.exp(-0.5 * ((xs - cx) / sigma) ** 2)
        gy = np.exp(-0.5 * ((ys - cy) / sigma) ** 2)
        hm += np.outer(gy, gx).astype(np.float32)
    return np.clip(hm, 0.0, 1.0)


def make_gt_heatmap(gtc_x_256: float, gtc_y_256: float) -> np.ndarray:
    return gaussian_splat([(gtc_x_256, gtc_y_256)])


def scale_to_256(px_1080: float, py_1080: float, w: int, h: int) -> tuple[float, float]:
    return px_1080 * INPUT_SIZE / w, py_1080 * INPUT_SIZE / h


# ── Frame record + teacher cache ──────────────────────────────────────────────

class FrameRecord(NamedTuple):
    slug: str
    src: int
    local: int
    ball_in: bool
    gtc_x: float
    gtc_y: float
    gt_area: int
    gt_s: float
    gt_h: float
    gt_v: float
    rgb256: np.ndarray  # (3, 256, 256) float32
    gt_heatmap: np.ndarray   # (256, 256)
    teacher: np.ndarray       # (3, 256, 256) — channels: V11, Ydiff, FRST


def _build_teacher_for_frame(bgr_full: np.ndarray,
                              prev_gray_full: np.ndarray | None,
                              w: int, h: int) -> np.ndarray:
    """Return (3, 256, 256) teacher tensor."""
    # V11
    v11_c = detect_v11(bgr_full)
    v11_centres = [scale_to_256(c[0], c[1], w, h) for c in v11_c]
    t_v11 = gaussian_splat(v11_centres)

    # Y-diff
    if prev_gray_full is not None:
        gray_curr = cv2.cvtColor(bgr_full, cv2.COLOR_BGR2GRAY)
        yd_c = detect_ydiff(prev_gray_full, gray_curr)
        yd_centres = [scale_to_256(c[0], c[1], w, h) for c in yd_c]
        t_yd = gaussian_splat(yd_centres)
    else:
        t_yd = np.zeros((INPUT_SIZE, INPUT_SIZE), dtype=np.float32)

    # FRST top-K
    gray_full = cv2.cvtColor(bgr_full, cv2.COLOR_BGR2GRAY)
    frst_c = detect_frst_topk(gray_full)
    frst_centres = [scale_to_256(c[0], c[1], w, h) for c in frst_c]
    t_frst = gaussian_splat(frst_centres)

    return np.stack([t_v11, t_yd, t_frst], axis=0)


def load_all_records(verbose: bool = True) -> list[FrameRecord]:
    items = []
    for it in M["items"]:
        seg = _active_seg(it)
        if seg is not None:
            items.append((it, seg))
    teacher_cache_path = OUT / "23_teacher_heatmaps.npz"

    # First pass: enumerate frames with metadata
    enum: list[dict] = []
    for it, seg in items:
        slug = it["slug"]
        in_f = seg["in_frame"]
        seg_id = seg["id"]
        masks_dir = WS / "items" / slug / "masks" / seg_id
        frames_dir = WS / "items" / slug / "frames"
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem)
            local = src - in_f
            fp = frames_dir / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            enum.append(dict(slug=slug, src=src, local=local,
                             frame_path=fp, mask_path=mp))

    if verbose:
        print(f"[load] {len(enum)} frame records across {len(items)} items")

    # Try teacher cache
    teachers: dict[tuple[str, int], np.ndarray] = {}
    if teacher_cache_path.exists():
        if verbose:
            print(f"[load] teacher cache hit: {teacher_cache_path.name}")
        npz = np.load(teacher_cache_path)
        keys = npz["keys"]
        arr = npz["teachers"]
        for i, key in enumerate(keys):
            slug, src = key.split("|")
            teachers[(slug, int(src))] = arr[i]

    # Build records
    t0 = time.time()
    need_teacher = [e for e in enum if (e["slug"], e["src"]) not in teachers]
    if need_teacher and verbose:
        print(f"[load] computing teacher for {len(need_teacher)} frames "
              f"(FRST is the bottleneck — ~hundreds ms/frame)")

    # Group by slug for prev_gray lookup
    by_slug: dict[str, list[dict]] = {}
    for e in enum:
        by_slug.setdefault(e["slug"], []).append(e)
    for s in by_slug:
        by_slug[s].sort(key=lambda e: e["local"])

    records: list[FrameRecord] = []
    new_keys: list[str] = []
    new_teachers: list[np.ndarray] = []

    for slug, group in by_slug.items():
        local_to_gray: dict[int, np.ndarray] = {}
        # Pre-load grays for prev lookup as needed
        for i, e in enumerate(group):
            if verbose and i % 50 == 0:
                print(f"  [{slug}] frame {i}/{len(group)} "
                      f"(elapsed {time.time()-t0:.1f}s)")
            bgr = cv2.imread(str(e["frame_path"]), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            mask = cv2.imread(str(e["mask_path"]), cv2.IMREAD_GRAYSCALE)
            if mask is None:
                continue
            h, w = bgr.shape[:2]
            local_to_gray[e["local"]] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

            ball_in = int((mask > 0).sum()) >= 5
            bgr256 = cv2.resize(bgr, (INPUT_SIZE, INPUT_SIZE),
                                interpolation=cv2.INTER_LINEAR)
            rgb256 = bgr256[:, :, ::-1].copy().astype(np.float32) / 255.0
            rgb256_chw = rgb256.transpose(2, 0, 1)

            if ball_in:
                ys, xs = np.where(mask > 0)
                gtc_x = float(xs.mean()); gtc_y = float(ys.mean())
                gt_area = int(len(ys))
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                gt_s = float(hsv[ys, xs, 1].mean())
                gt_h = float(hsv[ys, xs, 0].mean())
                gt_v = float(hsv[ys, xs, 2].mean())
                gx256, gy256 = scale_to_256(gtc_x, gtc_y, w, h)
                gt_hm = make_gt_heatmap(gx256, gy256)
            else:
                gtc_x = gtc_y = 0.0
                gt_area = 0
                gt_s = gt_h = gt_v = 0.0
                gt_hm = np.zeros((INPUT_SIZE, INPUT_SIZE), dtype=np.float32)

            key = (slug, e["src"])
            if key in teachers:
                teacher = teachers[key]
            else:
                prev_gray = local_to_gray.get(e["local"] - 1)
                teacher = _build_teacher_for_frame(bgr, prev_gray, w, h)
                teachers[key] = teacher
                new_keys.append(f"{slug}|{e['src']}")
                new_teachers.append(teacher)

            records.append(FrameRecord(
                slug=slug, src=e["src"], local=e["local"], ball_in=bool(ball_in),
                gtc_x=gtc_x, gtc_y=gtc_y, gt_area=gt_area,
                gt_s=gt_s, gt_h=gt_h, gt_v=gt_v,
                rgb256=rgb256_chw,
                gt_heatmap=gt_hm,
                teacher=teacher,
            ))

    if new_teachers:
        if verbose:
            print(f"[load] saving {len(new_teachers)} new teacher tensors → cache")
        # Merge with existing cache
        all_keys = list(teachers.keys())
        all_arr = np.stack([teachers[k] for k in all_keys], axis=0).astype(np.float32)
        all_keys_str = np.array([f"{k[0]}|{k[1]}" for k in all_keys])
        np.savez_compressed(teacher_cache_path,
                            keys=all_keys_str, teachers=all_arr)
        if verbose:
            print(f"[load] teacher cache: {teacher_cache_path}")

    return records


# ── Sanity visualisation ──────────────────────────────────────────────────────

def save_teacher_sanity(records: list[FrameRecord], n: int = 4) -> None:
    ball_in = [r for r in records if r.ball_in]
    if not ball_in:
        return
    # Pick spread: first + every (len/n)
    step = max(1, len(ball_in) // n)
    samples = [ball_in[i] for i in range(0, len(ball_in), step)][:n]
    for i, r in enumerate(samples):
        img = (r.rgb256.transpose(1, 2, 0) * 255).astype(np.uint8)[:, :, ::-1]  # to BGR
        gt_vis = (r.gt_heatmap * 255).astype(np.uint8)
        gt_vis = cv2.cvtColor(gt_vis, cv2.COLOR_GRAY2BGR)
        cue_vis = []
        for c, name in enumerate(["V11", "Ydiff", "FRST"]):
            v = (r.teacher[c] * 255).astype(np.uint8)
            v = cv2.applyColorMap(v, cv2.COLORMAP_HOT)
            cv2.putText(v, name, (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 1)
            cue_vis.append(v)
        row = np.concatenate([img, gt_vis] + cue_vis, axis=1)
        out = OUT / f"23_teacher_sanity_{i}_{r.slug}_{r.src}.png"
        cv2.imwrite(str(out), row)


# ── Model ─────────────────────────────────────────────────────────────────────

class TinyFCNDistill(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.enc1 = nn.Sequential(
            nn.Conv2d(3, 16, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(16, 16, 3, padding=1), nn.ReLU())
        self.enc2 = nn.Sequential(
            nn.Conv2d(16, 32, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(32, 32, 3, padding=1), nn.ReLU())
        self.enc3 = nn.Sequential(
            nn.Conv2d(32, 64, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1), nn.ReLU())
        self.bottleneck = nn.Sequential(
            nn.Conv2d(64, 64, 3, stride=2, padding=1), nn.ReLU(),
            nn.Conv2d(64, 64, 3, padding=1), nn.ReLU())
        self.dec3 = nn.Sequential(nn.Conv2d(128, 32, 3, padding=1), nn.ReLU())
        self.dec2 = nn.Sequential(nn.Conv2d(64, 16, 3, padding=1), nn.ReLU())
        self.dec1 = nn.Sequential(nn.Conv2d(32, 16, 3, padding=1), nn.ReLU())
        self.dec0 = nn.Sequential(nn.Conv2d(16, 8, 3, padding=1), nn.ReLU())
        self.heatmap_head = nn.Conv2d(8, 1, 1)
        self.ball_head = nn.Sequential(
            nn.AdaptiveAvgPool2d(4), nn.Flatten(), nn.Linear(64 * 4 * 4, 1))
        # Cue head from bottleneck (16x16x64) → 3 channels, upsample to 256
        self.cue_head = nn.Conv2d(64, 3, 1)

    def forward(self, x: torch.Tensor):
        e1 = self.enc1(x); e2 = self.enc2(e1); e3 = self.enc3(e2)
        b = self.bottleneck(e3)
        d3 = nn.functional.interpolate(b, scale_factor=2, mode="bilinear", align_corners=False)
        d3 = self.dec3(torch.cat([d3, e3], dim=1))
        d2 = nn.functional.interpolate(d3, scale_factor=2, mode="bilinear", align_corners=False)
        d2 = self.dec2(torch.cat([d2, e2], dim=1))
        d1 = nn.functional.interpolate(d2, scale_factor=2, mode="bilinear", align_corners=False)
        d1 = self.dec1(torch.cat([d1, e1], dim=1))
        d0 = nn.functional.interpolate(d1, scale_factor=2, mode="bilinear", align_corners=False)
        d0 = self.dec0(d0)
        hm_logit = self.heatmap_head(d0).squeeze(1)
        ball_logit = self.ball_head(b).squeeze(1)
        cue_logit_lo = self.cue_head(b)  # (B, 3, 16, 16)
        cue_logit = nn.functional.interpolate(
            cue_logit_lo, size=(INPUT_SIZE, INPUT_SIZE),
            mode="bilinear", align_corners=False)  # (B, 3, 256, 256)
        return hm_logit, ball_logit, cue_logit


def count_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# ── Loss ──────────────────────────────────────────────────────────────────────

def focal_mse(pred: torch.Tensor, target: torch.Tensor, gamma: float = 2.0):
    w = (1.0 - torch.exp(-target.abs())).pow(gamma)
    return (w * (pred - target).pow(2)).mean()


def total_loss(hm_logit, ball_logit, cue_logit,
               gt_hm, ball_label, teacher,
               use_aux: bool):
    hm_pred = torch.sigmoid(hm_logit)
    L_primary = focal_mse(hm_pred, gt_hm)
    L_ball = nn.functional.binary_cross_entropy_with_logits(ball_logit, ball_label)
    if use_aux:
        # BCE with-logits per cue channel
        L_cue = nn.functional.binary_cross_entropy_with_logits(cue_logit, teacher)
    else:
        L_cue = torch.tensor(0.0, device=hm_logit.device)
    return L_primary + LAMBDA_BALL * L_ball + LAMBDA_CUE * L_cue, \
           dict(primary=L_primary.item(), ball=L_ball.item(),
                cue=float(L_cue.detach().cpu()))


# ── Dataset ───────────────────────────────────────────────────────────────────

class DistillDataset(Dataset):
    def __init__(self, records: list[FrameRecord], augment: bool = False):
        self.records = records
        self.augment = augment

    def __len__(self):
        return len(self.records)

    def __getitem__(self, idx: int):
        r = self.records[idx]
        img = torch.from_numpy(r.rgb256.copy())
        gt_hm = torch.from_numpy(r.gt_heatmap.copy())
        teacher = torch.from_numpy(r.teacher.copy())
        ball_label = torch.tensor(float(r.ball_in))

        if self.augment:
            if torch.rand(1).item() > 0.5:
                img = img.flip(-1); gt_hm = gt_hm.flip(-1); teacher = teacher.flip(-1)
            if torch.rand(1).item() > 0.5:
                img = img.flip(-2); gt_hm = gt_hm.flip(-2); teacher = teacher.flip(-2)
            for c in range(3):
                alpha = 0.85 + 0.30 * torch.rand(1).item()
                beta = -0.08 + 0.16 * torch.rand(1).item()
                img[c] = (img[c] * alpha + beta).clamp(0, 1)
        return img, gt_hm, teacher, ball_label


# ── Train one fold ────────────────────────────────────────────────────────────

def train_fold(records: list[FrameRecord], epochs: int,
               use_teacher: bool, use_aux: bool,
               fold_label: str) -> TinyFCNDistill:
    model = TinyFCNDistill().to(DEVICE)
    opt = optim.Adam(model.parameters(), lr=LR)
    sched = optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    ds = DistillDataset(records, augment=True)
    dl = DataLoader(ds, batch_size=BATCH, shuffle=True, num_workers=0)

    t0 = time.time()
    for ep in range(epochs):
        model.train()
        sums = dict(primary=0, ball=0, cue=0); n = 0
        for img, gt_hm, teacher, ball_label in dl:
            img = img.to(DEVICE); gt_hm = gt_hm.to(DEVICE)
            teacher = teacher.to(DEVICE); ball_label = ball_label.to(DEVICE)
            opt.zero_grad()
            hm_logit, ball_logit, cue_logit = model(img)
            loss, parts = total_loss(hm_logit, ball_logit, cue_logit,
                                      gt_hm, ball_label, teacher,
                                      use_aux=use_aux)
            loss.backward(); opt.step()
            for k, v in parts.items():
                sums[k] += v * img.size(0)
            n += img.size(0)
        sched.step()
        if ep == 0 or (ep + 1) % 10 == 0 or ep == epochs - 1:
            avg = {k: v / max(1, n) for k, v in sums.items()}
            print(f"  [{fold_label}] ep {ep+1}/{epochs}  "
                  f"primary={avg['primary']:.5f}  cue={avg['cue']:.5f}  "
                  f"t={time.time()-t0:.1f}s")
    return model


# ── Eval one fold ─────────────────────────────────────────────────────────────

def predict_centroid_1080p(hm_pred: np.ndarray, w: int = 1920, h: int = 1080):
    if hm_pred.max() < 0.1:
        return None
    py, px = np.unravel_index(np.argmax(hm_pred), hm_pred.shape)
    return float(px) * w / INPUT_SIZE, float(py) * h / INPUT_SIZE


@torch.inference_mode()
def eval_fold(model: TinyFCNDistill, test_records: list[FrameRecord],
              all_records: list[FrameRecord]) -> list[dict]:
    model.eval()
    # Pre-compute V11, Ydiff, FRST hits (for error decomposition) on full-res frames
    # We use cached gray for ydiff
    by_slug_local: dict[str, dict[int, np.ndarray]] = {}
    for r in all_records:
        # We don't store full-res grays; reload on demand for test only
        pass

    # Build gray cache for test slug
    test_slug = test_records[0].slug
    gray_by_local: dict[int, np.ndarray] = {}
    bgr_by_src: dict[int, np.ndarray] = {}
    for r in test_records:
        fp = WS / "items" / test_slug / "frames" / f"{r.local:05d}.jpg"
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        bgr_by_src[r.src] = bgr
        gray_by_local[r.local] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    rows = []
    for r in test_records:
        bgr = bgr_by_src.get(r.src)
        if bgr is None:
            continue
        img_t = torch.from_numpy(r.rgb256.copy()).unsqueeze(0).to(DEVICE)
        hm_logit, ball_logit, _ = model(img_t)
        hm_pred = torch.sigmoid(hm_logit).squeeze().cpu().numpy()
        student = predict_centroid_1080p(hm_pred)
        student_cands = [(student[0], student[1], 100)] if student is not None else []

        v11_c = detect_v11(bgr)
        prev_gray = gray_by_local.get(r.local - 1)
        gray_curr = gray_by_local.get(r.local)
        yd_c = detect_ydiff(prev_gray, gray_curr) if prev_gray is not None else []
        frst_c = detect_frst_topk(gray_curr) if gray_curr is not None else []

        union3 = list(v11_c)
        for x in yd_c + frst_c:
            if not any((x[0]-v[0])**2 + (x[1]-v[1])**2 <= 25.0 for v in union3):
                union3.append((x[0], x[1], 100))

        row = dict(slug=r.slug, src=r.src, ball_in=r.ball_in,
                   gt_s=r.gt_s, gt_h=r.gt_h, gtc_x=r.gtc_x, gtc_y=r.gtc_y,
                   gt_area=r.gt_area)
        if r.ball_in:
            row["v11_hit"] = hit_check(v11_c, r.gtc_x, r.gtc_y, r.gt_area)
            row["ydiff_hit"] = hit_check(yd_c, r.gtc_x, r.gtc_y, r.gt_area)
            row["frst_hit"] = hit_check(frst_c, r.gtc_x, r.gtc_y, r.gt_area)
            row["v11_yd_hit"] = row["v11_hit"] or row["ydiff_hit"]
            row["v11_yd_frst_hit"] = hit_check(union3, r.gtc_x, r.gtc_y, r.gt_area)
            row["student_hit"] = hit_check(student_cands, r.gtc_x, r.gtc_y, r.gt_area)
            row["mode"] = classify_miss_mode(r.gt_s, r.gt_h)
        rows.append(row)
    return rows


# ── Aggregate ─────────────────────────────────────────────────────────────────

def aggregate(condition: str, fold_rows: list[list[dict]]) -> dict:
    flat = [r for f in fold_rows for r in f if r.get("ball_in")]
    n = len(flat)
    methods = ["v11_hit", "ydiff_hit", "frst_hit",
               "v11_yd_hit", "v11_yd_frst_hit", "student_hit"]
    g = {m: sum(r[m] for r in flat) / n if n else 0.0 for m in methods}

    # Per-fold (per-session) recall
    per_session = {}
    for fold in fold_rows:
        bf = [r for r in fold if r.get("ball_in")]
        if not bf:
            continue
        slug = bf[0]["slug"]
        per_session[slug] = {"n": len(bf)}
        for m in methods:
            per_session[slug][m] = sum(r[m] for r in bf) / len(bf)

    # Mode breakdown — for V11 misses, what fraction does student recover?
    v11_miss = [r for r in flat if not r["v11_hit"]]
    mode_stats = {}
    for mode in ["M1", "M2", "M3"]:
        mm = [r for r in v11_miss if r.get("mode") == mode]
        if not mm:
            mode_stats[mode] = dict(n=0)
            continue
        mode_stats[mode] = dict(
            n=len(mm),
            student_rec=sum(r["student_hit"] for r in mm) / len(mm),
            ydiff_rec=sum(r["ydiff_hit"] for r in mm) / len(mm),
            frst_rec=sum(r["frst_hit"] for r in mm) / len(mm),
            v11yd_rec=sum(r["v11_yd_hit"] for r in mm) / len(mm),
            union3_rec=sum(r["v11_yd_frst_hit"] for r in mm) / len(mm),
        )

    # Error decomposition: where did student miss?
    student_miss = [r for r in flat if not r["student_hit"]]
    err = dict(
        total=len(student_miss),
        v11_could_have=sum(r["v11_hit"] for r in student_miss),
        ydiff_could_have=sum(r["ydiff_hit"] and not r["v11_hit"] for r in student_miss),
        frst_could_have=sum(r["frst_hit"] and not r["v11_hit"] and not r["ydiff_hit"]
                            for r in student_miss),
        all_three_miss=sum(not r["v11_yd_frst_hit"] for r in student_miss),
    )
    return dict(condition=condition, n_ball=n,
                global_recall=g, per_session=per_session,
                mode_stats=mode_stats, error_decomp=err)


# ── Run condition ─────────────────────────────────────────────────────────────

def run_condition(label: str, all_records: list[FrameRecord],
                  use_teacher: bool, use_aux: bool,
                  epochs: int, fold_filter: list[int] | None) -> dict:
    slugs = [it["slug"] for it in M["items"] if _active_seg(it) is not None]
    print(f"\n{'#'*64}\n# Condition {label}: teacher={use_teacher} aux={use_aux} epochs={epochs}\n{'#'*64}")

    fold_rows = []
    for fi, test_slug in enumerate(slugs):
        if fold_filter is not None and fi not in fold_filter:
            print(f"\n-- Fold {fi+1}/{len(slugs)} ({test_slug}) SKIPPED (fold_filter) --")
            continue
        print(f"\n-- Fold {fi+1}/{len(slugs)}: test={test_slug} --")
        train_recs = [r for r in all_records if r.slug != test_slug]
        test_recs = [r for r in all_records if r.slug == test_slug]
        print(f"  train={len(train_recs)} test={len(test_recs)} "
              f"test_ball_in={sum(r.ball_in for r in test_recs)}")
        t0 = time.time()
        model = train_fold(train_recs, epochs, use_teacher, use_aux,
                           f"{label}-f{fi+1}")
        rows = eval_fold(model, test_recs, all_records)
        bf = [r for r in rows if r.get("ball_in")]
        if bf:
            r_v11 = sum(r["v11_hit"] for r in bf) / len(bf)
            r_stu = sum(r["student_hit"] for r in bf) / len(bf)
            r_u3 = sum(r["v11_yd_frst_hit"] for r in bf) / len(bf)
            print(f"  fold done {time.time()-t0:.1f}s  "
                  f"V11={r_v11:.3f}  Student={r_stu:.3f}  Union3={r_u3:.3f}")
        fold_rows.append(rows)

    return aggregate(label, fold_rows)


# ── Report ────────────────────────────────────────────────────────────────────

def write_report(results: dict, params_n: int, n_records: int) -> None:
    lines = ["# 13 — Ensemble distillation results\n\n"]
    lines.append(f"Model: TinyFCNDistill, params={params_n:,}, no depthwise, "
                 f"input {INPUT_SIZE}², device={DEVICE}.\n")
    lines.append(f"Dataset: 9 items, {n_records} frames, "
                 f"{results.get('n_ball_in_total','?')} ball-in.\n")
    lines.append(f"Hyperparams: λ_cue={LAMBDA_CUE}, λ_ball={LAMBDA_BALL}, "
                 f"YDIFF_THR={YDIFF_THR}, FRST_TOPK={FRST_TOPK}, FRST_THR={FRST_THR}.\n\n")

    conds = results["conditions"]
    lines.append("## 1. Macro recall by condition\n\n")
    lines.append("| Condition | Student R | V11 | V11∪Ydiff | V11∪Ydiff∪FRST |\n")
    lines.append("|---|---|---|---|---|\n")
    for label, agg in conds.items():
        g = agg["global_recall"]
        lines.append(f"| {label} (n_ball={agg['n_ball']}) "
                     f"| **{g['student_hit']:.4f}** "
                     f"| {g['v11_hit']:.4f} "
                     f"| {g['v11_yd_hit']:.4f} "
                     f"| {g['v11_yd_frst_hit']:.4f} |\n")
    lines.append("\n")

    # Per-session recall — show condition C if present, else first
    primary = conds.get("C") or next(iter(conds.values()))
    lines.append(f"## 2. Per-session student recall (condition {primary['condition']})\n\n")
    lines.append("| Item | n_ball | V11 | Student | Ydiff | FRST | V11∪Yd∪FRST |\n")
    lines.append("|---|---|---|---|---|---|---|\n")
    for slug, d in primary["per_session"].items():
        lines.append(f"| {slug} | {d['n']} "
                     f"| {d['v11_hit']:.3f} "
                     f"| **{d['student_hit']:.3f}** "
                     f"| {d['ydiff_hit']:.3f} "
                     f"| {d['frst_hit']:.3f} "
                     f"| {d['v11_yd_frst_hit']:.3f} |\n")
    lines.append("\n")

    # Per-mode recovery
    lines.append("## 3. Per-mode V11-miss recovery\n\n")
    lines.append("| Cond | M1 (specular) | M2 (frag) | M3 (hue) |\n")
    lines.append("|---|---|---|---|\n")
    for label, agg in conds.items():
        ms = agg["mode_stats"]
        def fmt(m):
            d = ms.get(m, {})
            if not d.get("n"):
                return "—"
            return f"{d['student_rec']:.1%} (n={d['n']})"
        lines.append(f"| {label} | {fmt('M1')} | {fmt('M2')} | {fmt('M3')} |\n")
    lines.append("\n")

    # Cue ablation
    lines.append("## 4. Cue-consistency auxiliary loss ablation\n\n")
    if "C" in conds and "B" in conds:
        rB = conds["B"]["global_recall"]["student_hit"]
        rC = conds["C"]["global_recall"]["student_hit"]
        lines.append(f"- Condition B (teacher provided, aux loss off): R = {rB:.4f}\n")
        lines.append(f"- Condition C (teacher provided, aux loss on):  R = {rC:.4f}\n")
        lines.append(f"- Δ (C − B) = {rC-rB:+.4f}\n\n")
        if abs(rC - rB) < 0.005:
            lines.append("**Verdict: cue-consistency aux loss has no measurable effect** at "
                         f"this scale ({primary['n_ball']} ball-in frames).\n\n")
        elif rC > rB:
            lines.append(f"**Verdict: aux loss helps by {(rC-rB)*100:+.2f}pp** — forcing the "
                         "bottleneck to retain cue identity yields measurable improvement.\n\n")
        else:
            lines.append(f"**Verdict: aux loss hurts by {(rB-rC)*100:+.2f}pp** — likely the "
                         "FRST teacher noise is dominating bottleneck capacity.\n\n")
    elif "A" in conds and "C" in conds:
        rA = conds["A"]["global_recall"]["student_hit"]
        rC = conds["C"]["global_recall"]["student_hit"]
        lines.append(f"- Condition A (GT-only, no teacher): R = {rA:.4f}\n")
        lines.append(f"- Condition C (teacher + aux loss):  R = {rC:.4f}\n")
        lines.append(f"- Δ (C − A) = {rC-rA:+.4f}\n\n")
        lines.append("(Condition B not run; cannot isolate aux-loss effect alone.)\n\n")

    # Error decomposition
    lines.append("## 5. Where the student misses (condition C)\n\n")
    if "C" in conds:
        e = conds["C"]["error_decomp"]
        lines.append(f"- Total student misses: {e['total']}\n")
        lines.append(f"- of which V11 alone could have hit: {e['v11_could_have']} "
                     "(student didn't absorb V11)\n")
        lines.append(f"- of which Y-diff (not V11) could have hit: {e['ydiff_could_have']} "
                     "(student didn't absorb Y-diff)\n")
        lines.append(f"- of which FRST (not V11 nor Y-diff) could have hit: "
                     f"{e['frst_could_have']} (student didn't absorb FRST)\n")
        lines.append(f"- All-three-miss (cue ceiling): {e['all_three_miss']}\n\n")

    # Saturation
    if "C" in conds:
        gC = conds["C"]["global_recall"]
        gap = gC["v11_yd_frst_hit"] - gC["student_hit"]
        lines.append("## 6. Saturation analysis\n\n")
        lines.append(f"Oracle union ceiling (V11∪Yd∪FRST): R = {gC['v11_yd_frst_hit']:.4f}\n\n")
        lines.append(f"Student gap to ceiling: **{gap*100:+.2f}pp**\n\n")
        if gap < 0.01:
            lines.append("Student is essentially at the cue ceiling. Distillation succeeded.\n\n")
        elif gap < 0.05:
            lines.append("Student is close but not saturating. Likely limited by data scale.\n\n")
        else:
            lines.append("Student is leaving substantial information on the table; "
                         "more data or richer architecture needed.\n\n")

    lines.append("## 7. Conclusion\n\n")
    if "C" in conds:
        gC = conds["C"]["global_recall"]
        if gC["student_hit"] >= gC["v11_yd_hit"]:
            lines.append(f"**Student R={gC['student_hit']:.4f} matches/beats V11∪Y-diff "
                         f"({gC['v11_yd_hit']:.4f}).** Ensemble distillation viable for "
                         "5–8ms ANE budget (single forward pass, no multi-detector union).\n")
        else:
            lines.append(f"**Student R={gC['student_hit']:.4f} does NOT beat V11∪Y-diff "
                         f"({gC['v11_yd_hit']:.4f}).** At {primary['n_ball']} ball-in frames, "
                         "data scale dominates: distillation cannot compensate. Either collect "
                         "more data (~10× → ~10K frames) or accept the multi-detector union as "
                         "the deploy strategy.\n")
    lines.append("\n## 8. Limitations\n\n")
    lines.append("- 9 items / 5 unique pitches → cam-A/B leakage in LOSO\n")
    lines.append("- Mode classifier is proxy; canonical M3 count is 9 (notes 11)\n")
    lines.append("- FRST teacher uses top-K NMS peaks (not raw symmetry score map) to keep "
                 "supervision form consistent across cues; full-map distillation might "
                 "transfer more information at cost of noise\n")
    lines.append(f"- Anisotropic 256² resize distorts ball aspect; letterbox would be cleaner\n")
    lines.append(f"- No pretrained weights (pure-research baseline)\n")

    out_path = NOTES / "13_ensemble_distillation_results.md"
    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"\n[done] report: {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", default="A,C",
                    help="comma list of conditions to run from {A,B,C}")
    ap.add_argument("--epochs", type=int, default=EPOCHS_DEFAULT)
    ap.add_argument("--ablation-folds", default="",
                    help="comma list of fold indices (0-based) to limit B,C to. "
                         "Empty = all 9.")
    ap.add_argument("--sanity-only", action="store_true",
                    help="Only build teacher cache + sanity PNGs, then exit.")
    args = ap.parse_args()

    print(f"Device: {DEVICE}")
    print("[load] building records + teacher cache ...")
    t0 = time.time()
    records = load_all_records()
    print(f"[load] {len(records)} records in {time.time()-t0:.1f}s")
    n_ball = sum(r.ball_in for r in records)
    print(f"[load] ball_in={n_ball} ball_out={len(records)-n_ball}")

    # Sanity PNGs
    save_teacher_sanity(records, n=4)
    print(f"[load] saved teacher sanity PNGs to {OUT}")

    if args.sanity_only:
        return

    # Param count
    params_n = count_params(TinyFCNDistill())
    print(f"[model] params={params_n:,}")

    fold_filter = None
    if args.ablation_folds.strip():
        fold_filter = [int(s) for s in args.ablation_folds.split(",")]
        print(f"[run] fold filter for B/C: {fold_filter}")

    cond_set = [c.strip() for c in args.conditions.split(",")]
    results = {"conditions": {}, "n_ball_in_total": n_ball}

    if "A" in cond_set:
        agg = run_condition("A", records, use_teacher=False, use_aux=False,
                             epochs=args.epochs, fold_filter=None)
        results["conditions"]["A"] = agg

    if "B" in cond_set:
        agg = run_condition("B", records, use_teacher=True, use_aux=False,
                             epochs=args.epochs, fold_filter=fold_filter)
        results["conditions"]["B"] = agg

    if "C" in cond_set:
        agg = run_condition("C", records, use_teacher=True, use_aux=True,
                             epochs=args.epochs, fold_filter=fold_filter)
        results["conditions"]["C"] = agg

    out_json = OUT / "23_ensemble_distillation_results.json"
    out_json.write_text(json.dumps(results, indent=2, default=float))
    print(f"\n[done] JSON: {out_json}")

    write_report(results, params_n, len(records))


if __name__ == "__main__":
    main()
