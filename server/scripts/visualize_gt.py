"""Render a quick GT-overlay montage so the operator can eyeball SAM 2's
result without re-running inference.

Reads `data/gt/sam3/session_<sid>_<cam>.json`, decodes the corresponding
MOV at three sample timestamps (first / mid / last labelled frame),
draws the recorded `bbox` (red rectangle) + `centroid_px` (green crosshair)
on each frame, and writes a side-by-side montage JPEG to
`/tmp/gt_viz_<sid>_<cam>.jpg`.

Usage (from `server/`):
    uv run python scripts/visualize_gt.py --session s_xxxxxxxx --cam A

The JSON doesn't store masks (only stats), so we can't re-render exact
SAM 2 contours offline. The bbox + centroid is enough to spot the
"SAM 2 grabbed the wrong object" failure mode (centroid barely moves;
bbox is suspiciously huge or covers a non-ball region).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from video import iter_frames  # noqa: E402
from schemas import PitchPayload  # noqa: E402


def _draw_overlay(bgr, bbox, centroid_px, label: str):
    h, w = bgr.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in bbox)
    cx, cy = int(centroid_px[0]), int(centroid_px[1])
    # bbox
    cv2.rectangle(bgr, (x0, y0), (x1, y1), (0, 0, 255), 3)
    # centroid crosshair
    cv2.line(bgr, (cx - 20, cy), (cx + 20, cy), (0, 255, 0), 2)
    cv2.line(bgr, (cx, cy - 20), (cx, cy + 20), (0, 255, 0), 2)
    cv2.circle(bgr, (cx, cy), 5, (0, 255, 0), -1)
    # label
    cv2.rectangle(bgr, (0, 0), (520, 40), (0, 0, 0), -1)
    cv2.putText(
        bgr, label, (10, 28),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
    )
    return bgr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-dir", type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    ap.add_argument("--session", required=True)
    ap.add_argument("--cam", required=True, choices=["A", "B"])
    ap.add_argument(
        "--out", type=Path, default=None,
        help="output JPEG path (default: /tmp/gt_viz_<sid>_<cam>.jpg)",
    )
    ap.add_argument(
        "--samples", type=int, default=6,
        help="number of frames to sample across the GT range (default 6)",
    )
    args = ap.parse_args(argv)

    gt_path = args.data_dir / "gt" / "sam3" / f"session_{args.session}_{args.cam}.json"
    if not gt_path.is_file():
        ap.error(f"no GT JSON at {gt_path}")
    gt = json.loads(gt_path.read_text())
    frames = gt.get("frames", [])
    if not frames:
        ap.error("GT JSON has no labelled frames")

    pitch_path = args.data_dir / "pitches" / f"session_{args.session}_{args.cam}.json"
    if not pitch_path.is_file():
        ap.error(f"no pitch JSON at {pitch_path}")
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    video_start_pts_s = float(pitch.video_start_pts_s)

    mov_candidates = list((args.data_dir / "videos").glob(
        f"session_{args.session}_{args.cam}.*"
    ))
    mov = next((p for p in mov_candidates if p.suffix.lower() in (".mov", ".mp4", ".m4v")), None)
    if mov is None:
        ap.error(f"no MOV under {args.data_dir}/videos")

    # Pick `samples` evenly-spaced GT entries by frame_idx.
    n = min(args.samples, len(frames))
    step = max(1, len(frames) // n)
    chosen = [frames[i] for i in range(0, len(frames), step)][:n]
    chosen_idx = {int(f["frame_idx"]) for f in chosen}

    # Decode the MOV once, grab the matching frames.
    overlays: dict[int, "cv2.Mat"] = {}
    for absolute_pts_s, bgr in iter_frames(mov, video_start_pts_s):
        # Match GT entries by frame_idx — but iter_frames doesn't expose
        # the index directly. We rebuild the mapping by counting decoded
        # frames in MOV order, which matches how label_with_sam2 numbers them.
        # Cheaper than seeking.
        pass

    # Re-iterate counting indices.
    decoded_idx = 0
    for absolute_pts_s, bgr in iter_frames(mov, video_start_pts_s):
        if decoded_idx in chosen_idx:
            for f in chosen:
                if int(f["frame_idx"]) == decoded_idx:
                    t_rel = absolute_pts_s - video_start_pts_s
                    label = (
                        f"frame {decoded_idx}  t={t_rel:.2f}s  "
                        f"area={f['mask_area_px']}  "
                        f"asp={f['mask_aspect']:.2f}  "
                        f"fill={f['mask_fill']:.2f}  "
                        f"h={f['mask_hue_mean']:.0f}"
                    )
                    overlays[decoded_idx] = _draw_overlay(
                        bgr.copy(), f["bbox"], f["centroid_px"], label,
                    )
                    break
        decoded_idx += 1
        if len(overlays) == len(chosen_idx):
            break

    if not overlays:
        ap.error("could not extract any matching frames from MOV")

    # 2-row montage. Resize each panel to ~640 wide.
    panels = [overlays[int(f["frame_idx"])] for f in chosen if int(f["frame_idx"]) in overlays]
    panels = [cv2.resize(p, (640, int(p.shape[0] * 640 / p.shape[1]))) for p in panels]
    cols = 3
    rows = (len(panels) + cols - 1) // cols
    cell_h, cell_w = panels[0].shape[:2]
    import numpy as np
    grid = np.zeros((rows * cell_h, cols * cell_w, 3), dtype=np.uint8)
    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        grid[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = p

    out = args.out or Path(f"/tmp/gt_viz_{args.session}_{args.cam}.jpg")
    cv2.imwrite(str(out), grid, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    print(f"wrote {out}  ({grid.shape[1]}x{grid.shape[0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
