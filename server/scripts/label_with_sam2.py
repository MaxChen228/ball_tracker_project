"""SAM 2 GT labeller for archived session MOVs (replaces label_with_sam3.py).

Spawned as a subprocess by `gt_queue_worker.py`. Args:

    cd server
    uv run --project ../tools python scripts/label_with_sam2.py \\
        --session s_xxxxxxxx --cam A \\
        --time-range 0.50 2.40 \\
        --click-x 950 --click-y 540 --click-t 0.50 \\
        --queue-id q_deadbeef --overwrite

`--click-x` / `--click-y` are image-pixel coordinates on the source video
(typically 1920×1080). `--click-t` is video-relative seconds — must fall
within `--time-range`. The script seeds SAM 2 at the decoded frame
nearest to that timestamp and propagates forward to the range end.

stderr contract (parsed by `gt_queue_worker.py`):
    PROGRESS: frame={current} total={total} elapsed={s} ms_per_frame={ms}
    DONE: labelled={labelled} decoded={decoded}

Any other stderr line goes into the worker's 4 KB ring buffer and is
surfaced as the `error` field on the queue item if the subprocess exits
non-zero.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np

# Resolve `server/` (parent of scripts/) on sys.path so we can import
# server modules without a setup.py-style install.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from sam2_runtime import Sam2VideoLabeller  # noqa: E402
from schemas import PitchPayload  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("label_with_sam2")

_PROGRESS_FMT = "PROGRESS: frame={current} total={total} elapsed={elapsed:.2f} ms_per_frame={mpf:.2f}"
_DONE_FMT = "DONE: labelled={labelled} decoded={decoded}"

_QUEUE_ID_RE = re.compile(r"^q_[0-9a-f]{8}$")


class _MissingPitchError(RuntimeError):
    pass


def _load_video_start_pts_s(pitches_dir: Path, session_id: str, cam: str) -> float:
    """Pull `video_start_pts_s` off the persisted pitch JSON. Hard-fails
    on miss — silently using 0.0 would misalign GT with live/server_post
    by the full session-clock offset (typically 14 681 s)."""
    candidates = list(pitches_dir.glob(f"session_{session_id}_{cam}.json"))
    if not candidates:
        raise _MissingPitchError(
            f"no pitch JSON for session={session_id} cam={cam} under {pitches_dir}"
        )
    raw = candidates[0].read_text()
    try:
        pitch = PitchPayload.model_validate_json(raw)
    except Exception as e:
        raise _MissingPitchError(f"failed to parse {candidates[0]}: {e}") from e
    return float(pitch.video_start_pts_s)


def _find_clip(videos_dir: Path, session_id: str, cam: str) -> Path | None:
    for path in videos_dir.glob(f"session_{session_id}_{cam}.*"):
        if path.suffix.lower() in (".mov", ".mp4", ".m4v"):
            return path
    return None


def _atomic_write_text(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _make_progress_emitter(*, queue_id: str | None):
    """`progress_callback(current, total, ms_per_frame)` → stderr line.

    Density: every frame for first 10 (so the first preview JPEG + ETA
    arrive before the 60 s no-progress watchdog can fire), every 10th
    frame thereafter. Same contract as the SAM 3 era — worker regex is
    unchanged."""
    elapsed_accumulator = {"start_ms": 0.0}

    def emit(current: int, total: int, ms_per_frame: float) -> None:
        if current <= 10 or current % 10 == 0 or current == total:
            elapsed_accumulator["start_ms"] += ms_per_frame
            line = _PROGRESS_FMT.format(
                current=current,
                total=total,
                elapsed=elapsed_accumulator["start_ms"] / 1000.0,
                mpf=ms_per_frame,
            )
            print(line, file=sys.stderr, flush=True)

    return emit


def _make_preview_writer(*, preview_path: Path | None):
    """`preview_callback(frame_idx, bgr, mask)` → JPEG with mask outlined.

    Cadence: first 10 frames every frame, then every 5th frame. No-op
    when `preview_path` is None (manual CLI runs).

    Marker addition (vs SAM 3 era): we ALSO draw the click point as a
    small red dot so the operator can verify they clicked the right
    object on the running thumbnail."""
    if preview_path is None:
        return None
    preview_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(frame_idx: int, bgr: np.ndarray, mask: np.ndarray) -> None:
        if frame_idx > 10 and (frame_idx % 5) != 0:
            return
        try:
            overlay = bgr.copy()
            color = np.array([0, 255, 0], dtype=np.uint8)  # green BGR
            sel = mask > 0
            if sel.any():
                overlay[sel] = (0.6 * bgr[sel] + 0.4 * color).astype(np.uint8)
            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(overlay, contours, -1, (0, 255, 0), 2)
            tmp = preview_path.with_suffix(preview_path.suffix + ".tmp")
            cv2.imwrite(
                str(tmp), overlay,
                [int(cv2.IMWRITE_JPEG_QUALITY), 70],
            )
            os.replace(tmp, preview_path)
        except Exception as e:
            log.warning("preview write failed for frame %d: %s", frame_idx, e)

    return emit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="server data dir (default: server/data)",
    )
    parser.add_argument("--session", required=True, help="session id (s_xxxxxxxx)")
    parser.add_argument("--cam", required=True, choices=["A", "B"])
    parser.add_argument(
        "--time-range",
        type=float, nargs=2, required=True, metavar=("START_S", "END_S"),
        help="propagation window in video-relative seconds",
    )
    parser.add_argument(
        "--click-x", type=int, required=True,
        help="seed click X coordinate (image pixels)",
    )
    parser.add_argument(
        "--click-y", type=int, required=True,
        help="seed click Y coordinate (image pixels)",
    )
    parser.add_argument(
        "--click-t", type=float, required=True,
        help="seed click timestamp (video-relative seconds; must lie in --time-range)",
    )
    parser.add_argument(
        "--queue-id", default=None,
        help="queue item id (q_<8 hex>); enables preview JPEG writes",
    )
    parser.add_argument(
        "--device", default="auto",
        help="auto / mps / cuda / cpu (default auto)",
    )
    parser.add_argument(
        "--model-id", default=Sam2VideoLabeller.DEFAULT_MODEL_ID,
        help=f"HF model id (default {Sam2VideoLabeller.DEFAULT_MODEL_ID})",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="re-label even if GT JSON already exists",
    )
    args = parser.parse_args(argv)

    if args.queue_id is not None and not _QUEUE_ID_RE.match(args.queue_id):
        parser.error(f"--queue-id must match {_QUEUE_ID_RE.pattern!r}")

    t_start, t_end = args.time_range
    if not (t_start >= 0.0 and t_end > t_start):
        parser.error(
            f"--time-range must satisfy 0 <= START < END (got {t_start}, {t_end})"
        )
    if not (t_start <= args.click_t <= t_end):
        parser.error(
            f"--click-t={args.click_t} must lie in --time-range [{t_start}, {t_end}]"
        )

    out_dir = args.data_dir / "gt" / "sam3"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"session_{args.session}_{args.cam}.json"
    if out_path.exists() and not args.overwrite:
        log.info("skip %s: GT exists (use --overwrite)", out_path.name)
        return 0

    clip = _find_clip(args.data_dir / "videos", args.session, args.cam)
    if clip is None:
        log.error("no MOV under %s/videos for %s/%s", args.data_dir, args.session, args.cam)
        return 2

    try:
        video_start_pts_s = _load_video_start_pts_s(
            args.data_dir / "pitches", args.session, args.cam
        )
    except _MissingPitchError as e:
        log.error("%s", e)
        return 2

    preview_path: Path | None = None
    if args.queue_id is not None:
        preview_path = args.data_dir / "gt" / "preview" / f"{args.queue_id}.jpg"

    log.info(
        "labelling %s/%s clip=%s range=[%.2f, %.2f] click=(%d, %d)@%.2f queue=%s",
        args.session, args.cam, clip.name, t_start, t_end,
        args.click_x, args.click_y, args.click_t, args.queue_id,
    )

    labeller = Sam2VideoLabeller(model_id=args.model_id, device=args.device)
    record = labeller.label_video(
        mov_path=clip,
        video_start_pts_s=video_start_pts_s,
        session_id=args.session,
        camera_id=args.cam,
        click_xy_px=(args.click_x, args.click_y),
        click_t_video_rel=float(args.click_t),
        time_range=(float(t_start), float(t_end)),
        progress_callback=_make_progress_emitter(queue_id=args.queue_id),
        preview_callback=_make_preview_writer(preview_path=preview_path),
    )
    _atomic_write_text(out_path, record.model_dump_json(indent=2))
    print(
        _DONE_FMT.format(labelled=record.frames_labelled, decoded=record.frames_decoded),
        file=sys.stderr, flush=True,
    )
    log.info(
        "wrote %s: %d/%d frames labelled",
        out_path.name, record.frames_labelled, record.frames_decoded,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("interrupted")
        sys.exit(130)
    except Exception as e:
        log.exception("FAILED: %s", e)
        sys.exit(1)
