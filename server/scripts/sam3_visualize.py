"""SAM 3 GT visualization — overlay bbox + centroid on the source MOV.

Reads a `SAM3GTRecord` JSON + the matching MOV, writes an MP4 with the
SAM 3 mask outline (semi-transparent green) drawn over each labelled
frame plus the bbox / centroid / score in the corner.

This is the operator's first-line check for SAM 3 quality: hand-eye
review the MP4 to catch false positives (background grabbed instead
of ball) before any of these labels feed into HSV / shape_gate fits.
The visualizer never reaches into model state — it only consumes the
already-written GT JSON, so this script can be run from the cheap
`server` venv (no torch).

Usage:

    cd server
    uv run python scripts/sam3_visualize.py --session s_xxxxxxxx --cam A
    uv run python scripts/sam3_visualize.py --all
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2
import numpy as np

# Path-insert is the same trick as label_with_sam3.py / retrofit.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from schemas import SAM3GTRecord  # noqa: E402
from video import iter_frames  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("sam3_visualize")


_GT_COLOR = (60, 220, 60)              # BGR — bright green for SAM 3 GT
_GT_COLOR_DIM = (40, 140, 40)          # bbox outline a touch darker
_TEXT_COLOR = (240, 240, 240)


def _load_record(gt_path: Path) -> SAM3GTRecord:
    return SAM3GTRecord.model_validate_json(gt_path.read_text())


def _find_clip(videos_dir: Path, session_id: str, cam: str) -> Path | None:
    for path in videos_dir.glob(f"session_{session_id}_{cam}.*"):
        if path.suffix.lower() in (".mov", ".mp4", ".m4v"):
            return path
    return None


def _draw_overlay(
    frame: np.ndarray,
    gt_frame: "SAM3GTFrame | None",  # type: ignore[name-defined]  (forward — schemas import)
) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    if gt_frame is None:
        cv2.putText(
            out, "SAM3: no detection", (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, _TEXT_COLOR, 2, cv2.LINE_AA,
        )
        return out
    x_min, y_min, x_max, y_max = gt_frame.bbox
    cx, cy = gt_frame.centroid_px
    cv2.rectangle(
        out,
        (int(round(x_min)), int(round(y_min))),
        (int(round(x_max)), int(round(y_max))),
        _GT_COLOR_DIM, 2,
    )
    cv2.circle(out, (int(round(cx)), int(round(cy))), 4, _GT_COLOR, -1)
    label = (
        f"SAM3 score={gt_frame.confidence:.2f}  "
        f"area={gt_frame.mask_area_px}px  "
        f"fill={gt_frame.mask_fill:.2f}  "
        f"aspect={gt_frame.mask_aspect:.2f}  "
        f"hue={gt_frame.mask_hue_mean:.0f}±{gt_frame.mask_hue_std:.0f}"
    )
    cv2.putText(
        out, label, (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX, 0.55, _TEXT_COLOR, 2, cv2.LINE_AA,
    )
    return out


def visualize_one(
    *,
    record: SAM3GTRecord,
    mov_path: Path,
    out_path: Path,
    video_start_pts_s: float,
) -> None:
    by_idx = {f.frame_idx: f for f in record.frames}
    width, height = record.video_dims
    fps = record.video_fps if record.video_fps > 0 else 240.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"failed to open VideoWriter for {out_path}")
    try:
        idx = -1
        for _abs_pts, bgr in iter_frames(mov_path, video_start_pts_s):
            idx += 1
            gt = by_idx.get(idx)
            writer.write(_draw_overlay(bgr, gt))
            if record.frames_total and idx >= record.frames_total - 1:
                break
    finally:
        writer.release()
    log.info("wrote %s (%d frames)", out_path.name, idx + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--all", action="store_true")
    parser.add_argument("--cam")
    parser.add_argument(
        "--video-start-pts-s",
        type=float,
        default=0.0,
        help="if you want absolute time labels in the overlay; default 0.0 = container-relative",
    )
    args = parser.parse_args(argv)

    if args.session and not args.cam:
        parser.error("--cam is required with --session")

    gt_dir = args.data_dir / "gt" / "sam3"
    if args.session:
        targets = [gt_dir / f"session_{args.session}_{args.cam}.json"]
    else:
        targets = sorted(gt_dir.glob("session_*.json"))

    successes = 0
    for gt_path in targets:
        if not gt_path.is_file():
            log.warning("skip %s: not a file", gt_path)
            continue
        try:
            record = _load_record(gt_path)
            mov = _find_clip(args.data_dir / "videos", record.session_id, record.camera_id)
            if mov is None:
                log.warning("skip %s: no MOV", gt_path.name)
                continue
            out_path = gt_path.with_suffix(".preview.mp4")
            visualize_one(
                record=record,
                mov_path=mov,
                out_path=out_path,
                video_start_pts_s=args.video_start_pts_s,
            )
            successes += 1
        except Exception as e:
            log.exception("FAILED %s: %s", gt_path.name, e)

    log.info("done: %d/%d records visualized", successes, len(targets))
    return 0 if successes else 1


if __name__ == "__main__":
    sys.exit(main())
