"""Sample ball / background pixels from SAM2 GT masks.

For every (frame_jpg, mask_png) pair in done sessions:
  - ball pixels  = mask == 255
  - bg pixels    = mask == 0, sampled within a ring around ball centroid
                   (radius 4r ~ 12r, where r = sqrt(ball_area/pi))
  - Stores BGR / HSV / Lab values for both classes.

Output: lab/research/outputs/pixel_samples.npz
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
import cv2
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT

OUT.mkdir(parents=True, exist_ok=True)

MANIFEST = json.loads((WS / "manifest.json").read_text())

BG_PER_BALL = 5  # background pixels sampled per ball pixel (per frame)
RNG = np.random.default_rng(0)


def iter_done() -> list[dict]:
    return [
        it for it in MANIFEST["items"]
        if it.get("propagate_status") == "done"
        and it.get("in_frame") is not None
    ]


def load_frame(item: dict, source_idx: int) -> np.ndarray | None:
    local = source_idx - item["in_frame"]
    p = WS / "items" / item["slug"] / "frames" / f"{local:05d}.jpg"
    if not p.exists():
        return None
    return cv2.imread(str(p), cv2.IMREAD_COLOR)


def sample_bg_indices(mask: np.ndarray, n: int, ring: tuple[float, float]) -> np.ndarray:
    """Return (n,2) array of (y,x) bg coords within ring*r around ball centroid."""
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return np.empty((0, 2), dtype=np.int64)
    cy, cx = ys.mean(), xs.mean()
    r = float(np.sqrt(len(ys) / np.pi))
    r_lo, r_hi = ring[0] * r, ring[1] * r
    H, W = mask.shape
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot(yy - cy, xx - cx)
    sel = (d >= r_lo) & (d <= r_hi) & (mask == 0)
    coords = np.argwhere(sel)
    if len(coords) == 0:
        return coords
    if len(coords) > n:
        idx = RNG.choice(len(coords), n, replace=False)
        coords = coords[idx]
    return coords


def main():
    t0 = time.time()
    items = iter_done()
    print(f"[info] {len(items)} done sessions")

    ball_bgr_all, bg_bgr_all = [], []
    ball_meta = []  # per-session count

    for item in items:
        slug = item["slug"]
        in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks"
        mask_files = sorted(masks_dir.glob("*.png"))
        sess_ball, sess_bg = 0, 0
        sample_step = max(1, len(mask_files) // 60)  # cap ~60 frames per session
        for mp in mask_files[::sample_step]:
            src_idx = int(mp.stem)
            frame = load_frame(item, src_idx)
            if frame is None:
                continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if mask is None or mask.shape != frame.shape[:2]:
                continue
            ys, xs = np.where(mask > 0)
            if len(ys) < 20:
                continue
            ball_bgr = frame[ys, xs]
            bg_yx = sample_bg_indices(mask, n=len(ys) * BG_PER_BALL, ring=(4.0, 12.0))
            if len(bg_yx) == 0:
                continue
            bg_bgr = frame[bg_yx[:, 0], bg_yx[:, 1]]
            ball_bgr_all.append(ball_bgr)
            bg_bgr_all.append(bg_bgr)
            sess_ball += len(ball_bgr)
            sess_bg += len(bg_bgr)
        ball_meta.append((slug, sess_ball, sess_bg))
        print(f"  {slug}: ball={sess_ball}  bg={sess_bg}")

    ball_bgr = np.concatenate(ball_bgr_all, axis=0).astype(np.uint8)
    bg_bgr = np.concatenate(bg_bgr_all, axis=0).astype(np.uint8)
    print(f"[total] ball={len(ball_bgr)}  bg={len(bg_bgr)}")

    def to_hsv(bgr): return cv2.cvtColor(bgr.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    def to_lab(bgr): return cv2.cvtColor(bgr.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3)

    out = OUT / "pixel_samples.npz"
    np.savez_compressed(
        out,
        ball_bgr=ball_bgr, bg_bgr=bg_bgr,
        ball_hsv=to_hsv(ball_bgr), bg_hsv=to_hsv(bg_bgr),
        ball_lab=to_lab(ball_bgr), bg_lab=to_lab(bg_bgr),
        meta=np.array(ball_meta, dtype=object),
    )
    print(f"[done] {out}  {(time.time()-t0):.1f}s")


if __name__ == "__main__":
    main()
