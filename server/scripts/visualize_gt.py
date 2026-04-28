"""Render a GT-overlay montage or slow-motion MP4 so the operator can
eyeball SAM 2's labelling result.

Two modes:
  default  → 2×3 JPEG montage of evenly-spaced GT frames with bbox +
             centroid + per-frame stats. Good for spotting "SAM 2
             grabbed the wrong object" failures (huge bbox, hue 19 =
             orange bucket / floor instead of 100 = blue ball).
  --video  → MP4 with bbox + centroid drawn on every frame in the GT's
             time window, padded with 5 frames of context on each
             side. Time-aligned to the source MOV via t_pts_s — using
             frame_idx alone would mis-align if the GT was written by
             an older runtime that used time_range-local indices.

Usage (from server/):
    uv run python scripts/visualize_gt.py --session s_xxxxxxxx --cam A
    uv run python scripts/visualize_gt.py --session s_xxxxxxxx --cam A --video
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from video import iter_frames  # noqa: E402
from schemas import PitchPayload  # noqa: E402


def _draw_overlay(bgr, bbox, centroid_px, label: str):
    x0, y0, x1, y1 = (int(v) for v in bbox)
    cx, cy = int(centroid_px[0]), int(centroid_px[1])
    cv2.rectangle(bgr, (x0, y0), (x1, y1), (0, 0, 255), 3)
    cv2.line(bgr, (cx - 20, cy), (cx + 20, cy), (0, 255, 0), 2)
    cv2.line(bgr, (cx, cy - 20), (cx, cy + 20), (0, 255, 0), 2)
    cv2.circle(bgr, (cx, cy), 5, (0, 255, 0), -1)
    cv2.rectangle(bgr, (0, 0), (520, 40), (0, 0, 0), -1)
    cv2.putText(
        bgr, label, (10, 28),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
    )
    return bgr


def _load_inputs(args):
    """Resolve GT JSON, pitch JSON, and MOV path. Returns dict-of-paths
    or raises argparse.ArgumentError via parser.error in the caller."""
    gt_path = args.data_dir / "gt" / "sam3" / f"session_{args.session}_{args.cam}.json"
    pitch_path = args.data_dir / "pitches" / f"session_{args.session}_{args.cam}.json"
    mov_candidates = list((args.data_dir / "videos").glob(
        f"session_{args.session}_{args.cam}.*"
    ))
    mov = next((p for p in mov_candidates if p.suffix.lower() in (".mov", ".mp4", ".m4v")), None)
    return gt_path, pitch_path, mov


def _gt_pts_lookup(frames, tolerance_s: float = 0.0021):
    """Build a `pts → gt_frame` dict. Tolerance is half a 240-fps frame
    period, so a 1-frame jitter still matches. Also returns a sorted
    list of pts values for binary-search-free linear lookup (we iterate
    a few thousand frames at most)."""
    by_pts = {round(f["t_pts_s"], 4): f for f in frames}
    return by_pts, tolerance_s


def _find_gt_for_pts(by_pts, tolerance_s: float, absolute_pts_s: float):
    for pts_key, g in by_pts.items():
        if abs(pts_key - absolute_pts_s) < tolerance_s:
            return g
    return None


def _render_montage(args, gt, pitch, mov, out_path: Path) -> None:
    frames = gt["frames"]
    if not frames:
        raise SystemExit("GT JSON has no labelled frames")
    n = min(args.samples, len(frames))
    step = max(1, len(frames) // n)
    chosen = [frames[i] for i in range(0, len(frames), step)][:n]
    by_pts, tol = _gt_pts_lookup(chosen)

    overlays = []
    video_start = float(pitch.video_start_pts_s)
    for absolute_pts_s, bgr in iter_frames(mov, video_start):
        g = _find_gt_for_pts(by_pts, tol, absolute_pts_s)
        if g is None:
            continue
        t_rel = absolute_pts_s - video_start
        label = (
            f"frame {g['frame_idx']}  t={t_rel:.2f}s  "
            f"area={g['mask_area_px']}  "
            f"asp={g['mask_aspect']:.2f}  "
            f"fill={g['mask_fill']:.2f}  "
            f"h={g['mask_hue_mean']:.0f}"
        )
        overlays.append(_draw_overlay(bgr.copy(), g["bbox"], g["centroid_px"], label))
        if len(overlays) == len(chosen):
            break

    if not overlays:
        raise SystemExit("could not extract any matching frames from MOV")

    panels = [cv2.resize(p, (640, int(p.shape[0] * 640 / p.shape[1]))) for p in overlays]
    cols = 3
    rows = (len(panels) + cols - 1) // cols
    cell_h, cell_w = panels[0].shape[:2]
    grid = np.zeros((rows * cell_h, cols * cell_w, 3), dtype=np.uint8)
    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        grid[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = p

    cv2.imwrite(str(out_path), grid, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    print(f"wrote {out_path}  ({grid.shape[1]}x{grid.shape[0]})")


def _render_video(args, gt, pitch, mov, out_path: Path) -> None:
    frames = gt["frames"]
    if not frames:
        raise SystemExit("GT JSON has no labelled frames")
    by_pts, tol = _gt_pts_lookup(frames)
    pts_min = min(by_pts.keys())
    pts_max = max(by_pts.keys())

    # Parse click coords from prompt_strategy (best-effort; we tolerate
    # missing match silently — older GTs may use a different label).
    import re
    click_x = click_y = None
    m = re.search(r"click:\((\d+),(\d+)\)", gt.get("prompt_strategy", ""))
    if m:
        click_x, click_y = int(m.group(1)), int(m.group(2))

    video_start = float(pitch.video_start_pts_s)

    # Probe first frame to size the writer.
    first_bgr = next(iter(iter_frames(mov, video_start)))[1]
    H, W = first_bgr.shape[:2]
    out_w, out_h = args.out_width, int(H * args.out_width / W)
    sx, sy = out_w / W, out_h / H

    # Output 15 fps default — for source 240 fps that's 16× slow-mo.
    fps = args.out_fps
    writer = cv2.VideoWriter(
        str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, out_h)
    )
    pad = args.pad_frames / 240.0  # assume 240 fps source for context window

    written = 0
    for absolute_pts_s, bgr in iter_frames(mov, video_start):
        if absolute_pts_s < pts_min - pad:
            continue
        if absolute_pts_s > pts_max + pad:
            break

        frame = cv2.resize(bgr, (out_w, out_h))
        t_rel = absolute_pts_s - video_start

        if click_x is not None:
            cv2.circle(
                frame, (int(click_x * sx), int(click_y * sy)),
                14, (255, 100, 0), 2,
            )

        g = _find_gt_for_pts(by_pts, tol, absolute_pts_s)
        if g is not None:
            x0, y0, x1, y1 = g["bbox"]
            cv2.rectangle(
                frame, (int(x0 * sx), int(y0 * sy)),
                (int(x1 * sx), int(y1 * sy)),
                (0, 0, 255), 2,
            )
            cx, cy = g["centroid_px"]
            cv2.drawMarker(
                frame, (int(cx * sx), int(cy * sy)),
                (0, 255, 0), cv2.MARKER_CROSS, 18, 2,
            )
            stats_label = (
                f"GT  area={g['mask_area_px']}  "
                f"hue={g['mask_hue_mean']:.0f}  "
                f"sat={g['mask_sat_mean']:.0f}"
            )
        else:
            stats_label = "(no GT mask this frame)"

        cv2.rectangle(frame, (0, 0), (out_w, 30), (0, 0, 0), -1)
        cv2.putText(
            frame,
            f"{args.session}/{args.cam}  t={t_rel:.3f}s  {stats_label}",
            (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
        )
        writer.write(frame)
        written += 1

    writer.release()
    real_s = written / 240.0
    print(
        f"wrote {out_path}: {written} frames @ {fps}fps "
        f"= {written / fps:.1f}s playback ({real_s:.2f}s real time, "
        f"{240 / fps:.0f}× slow-mo)"
    )


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
        help="output path (default: /tmp/gt_viz_<sid>_<cam>.{jpg,mp4})",
    )
    ap.add_argument(
        "--video", action="store_true",
        help="render an MP4 with overlays on every frame (default: JPEG montage)",
    )
    ap.add_argument(
        "--samples", type=int, default=6,
        help="(montage mode) number of frames to sample (default 6)",
    )
    ap.add_argument(
        "--out-width", type=int, default=960,
        help="(video mode) output width in px; height auto from aspect (default 960)",
    )
    ap.add_argument(
        "--out-fps", type=int, default=15,
        help="(video mode) output fps; 15 over 240 source = 16× slow-mo (default 15)",
    )
    ap.add_argument(
        "--pad-frames", type=int, default=5,
        help="(video mode) frames of context before/after GT range (default 5)",
    )
    args = ap.parse_args(argv)

    gt_path, pitch_path, mov = _load_inputs(args)
    if not gt_path.is_file():
        ap.error(f"no GT JSON at {gt_path}")
    if not pitch_path.is_file():
        ap.error(f"no pitch JSON at {pitch_path}")
    if mov is None:
        ap.error(f"no MOV under {args.data_dir}/videos for {args.session}/{args.cam}")

    gt = json.loads(gt_path.read_text())
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())

    suffix = ".mp4" if args.video else ".jpg"
    out = args.out or Path(f"/tmp/gt_viz_{args.session}_{args.cam}{suffix}")

    if args.video:
        _render_video(args, gt, pitch, mov, out)
    else:
        _render_montage(args, gt, pitch, mov, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
