"""SAM 3 GT labeller for archived session MOVs.

Decodes a session's H.264 MOV (per camera), runs SAM 3 video segmentation
with a text prompt ("blue ball" by default), and writes a SAM3GTRecord
JSON to `data/gt/sam3/session_<sid>_<cam>.json`. Optionally also writes
an overlay MP4 for hand-eye review by piping into `sam3_visualize.py`.

The script lives under server/scripts/ for path consistency with
`reprocess_sessions.py` / `retrofit_image_dims.py`, but uses the
**tools** uv venv (torch + transformers main). Invocation:

    cd server
    uv run --project ../tools python scripts/label_with_sam3.py \
        --session s_xxxxxxxx --cam A --prompt "blue ball"

    # batch all archived sessions, both cameras:
    uv run --project ../tools python scripts/label_with_sam3.py --all

    # quick dev iteration, only first 60 frames:
    uv run --project ../tools python scripts/label_with_sam3.py \
        --session s_xxx --cam A --limit-frames 60 --image-size 560
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import cv2  # noqa: E402  -- used for preview JPEG composition
import numpy as np  # noqa: E402

# Resolve the server package (siblings of scripts/) on sys.path. Same
# pattern as retrofit_image_dims.py — keeps this script invokable from
# anywhere without a setup.py-style install.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from sam3_runtime import Sam3VideoLabeller  # noqa: E402
from schemas import PitchPayload  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("label_with_sam3")

# Queue worker reads stderr looking for these contract lines. Any other
# stderr output (warnings, tracebacks) is captured but ignored unless
# the subprocess exits non-zero. Regex on the worker side must stay in
# sync with these formats.
_PROGRESS_FMT = "PROGRESS: frame={current} total={total} elapsed={elapsed:.2f} ms_per_frame={mpf:.2f}"
_DONE_FMT = "DONE: labelled={labelled} decoded={decoded}"

# Validate `--queue-id` so the preview JPEG path can't be tricked into
# escaping the data/gt/preview/ directory.
_QUEUE_ID_RE = re.compile(r"^q_[0-9a-f]{8}$")


class _MissingPitchError(RuntimeError):
    """Raised when video_start_pts_s can't be recovered. Callers must
    skip the session; silently falling back to 0.0 would drop the GT
    onto the container clock and misalign three-way IoU by ~14681 s."""


def _load_video_start_pts_s(pitches_dir: Path, session_id: str, cam: str) -> float:
    """Read `video_start_pts_s` off the persisted pitch payload so the
    SAM 3 GT timestamps share the iOS session clock with the live and
    server_post frame buckets. **Hard-fails** on missing or malformed
    pitch JSON — there is no safe fallback, and per the project's
    no-silent-fallback rule the caller must explicitly handle the miss
    (skip the session, surface the error, etc)."""
    candidates = list(pitches_dir.glob(f"session_{session_id}_{cam}.json"))
    if not candidates:
        raise _MissingPitchError(
            f"no pitch JSON for session={session_id} cam={cam} under {pitches_dir} — "
            f"refusing to label without a session-clock anchor (would misalign GT)"
        )
    raw = candidates[0].read_text()
    try:
        pitch = PitchPayload.model_validate_json(raw)
    except Exception as e:
        raise _MissingPitchError(
            f"failed to parse {candidates[0]}: {e} — refusing to label with 0.0 fallback"
        ) from e
    return float(pitch.video_start_pts_s)


def _find_clip(videos_dir: Path, session_id: str, cam: str) -> Path | None:
    for path in videos_dir.glob(f"session_{session_id}_{cam}.*"):
        if path.suffix.lower() in (".mov", ".mp4", ".m4v"):
            return path
    return None


def _list_archived_sessions(pitches_dir: Path) -> list[tuple[str, str]]:
    """Return every (session_id, camera_id) with a persisted pitch JSON."""
    out: list[tuple[str, str]] = []
    for path in pitches_dir.glob("session_*.json"):
        # Format: session_s_xxxxxxxx_A.json
        stem = path.stem  # "session_s_xxxxxxxx_A"
        parts = stem.split("_")
        if len(parts) < 4 or parts[0] != "session" or parts[1] != "s":
            continue
        session_id = "_".join(parts[1:-1])  # s_xxxxxxxx
        camera_id = parts[-1]
        out.append((session_id, camera_id))
    return sorted(set(out))


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write `payload` to `path` via tmp + os.replace. Avoids readers
    (validate_three_way, GTIndex) seeing a half-written GT JSON when
    the worker overwrites a file mid-run."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _make_progress_emitter(*, queue_id: str | None):
    """Build a `progress_callback(current, total, ms_per_frame)` that
    writes the worker-parseable PROGRESS line to stderr. Density:
    every frame for the first 10 (so first thumbnail / ETA arrive
    quickly and the no-progress watchdog never fires on warmup), every
    10th frame thereafter to keep stderr noise bounded.

    `queue_id` is included in the log prefix when present so multi-job
    debugging stays readable. The PROGRESS contract line itself does
    NOT include queue_id — the worker correlates by association
    (one subprocess per item)."""
    elapsed_accumulator = {"start_ms": 0.0}

    def emit(current: int, total: int, ms_per_frame: float) -> None:
        # Only emit on the chosen cadence. `current` is 1-indexed.
        if current <= 10 or current % 10 == 0 or current == total:
            elapsed_accumulator["start_ms"] += ms_per_frame  # rolling sum
            line = _PROGRESS_FMT.format(
                current=current,
                total=total,
                elapsed=elapsed_accumulator["start_ms"] / 1000.0,
                mpf=ms_per_frame,
            )
            print(line, file=sys.stderr, flush=True)

    return emit


def _make_preview_writer(*, preview_path: Path | None):
    """Build a `preview_callback(frame_idx, bgr, mask)` that writes
    a JPEG with the SAM 3 mask outlined onto the BGR frame so the
    operator can visually confirm the model is tracking the ball.

    Cadence: every frame for the first 10, every 5th frame after.
    Returns a no-op if `preview_path` is None (CLI not driven by the
    queue worker)."""
    if preview_path is None:
        return None
    preview_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(frame_idx: int, bgr: np.ndarray, mask: np.ndarray) -> None:
        if frame_idx > 10 and (frame_idx % 5) != 0:
            return
        try:
            overlay = bgr.copy()
            # Tinted overlay for the mask area; cheap visual confirmation.
            color = np.array([0, 255, 0], dtype=np.uint8)  # green in BGR
            sel = mask > 0
            if sel.any():
                overlay[sel] = (0.6 * bgr[sel] + 0.4 * color).astype(np.uint8)
            # Outline so the mask edge is visible on small thumbnails.
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


def _label_one(
    labeller: Sam3VideoLabeller,
    *,
    data_dir: Path,
    session_id: str,
    camera_id: str,
    prompt: str,
    min_confidence: float,
    max_frames: int | None,
    overwrite: bool,
    time_range: tuple[float, float] | None = None,
    queue_id: str | None = None,
) -> Path | None:
    """Returns the GT JSON path on success, None on skip / error."""
    out_dir = data_dir / "gt" / "sam3"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"session_{session_id}_{camera_id}.json"
    if out_path.exists() and not overwrite:
        log.info("skip %s: GT exists (use --overwrite to re-label)", out_path.name)
        return None

    clip = _find_clip(data_dir / "videos", session_id, camera_id)
    if clip is None:
        log.warning("skip %s/%s: no MOV under %s/videos", session_id, camera_id, data_dir)
        return None

    try:
        video_start_pts_s = _load_video_start_pts_s(
            data_dir / "pitches", session_id, camera_id
        )
    except _MissingPitchError as e:
        log.error("skip %s/%s: %s", session_id, camera_id, e)
        return None

    preview_path: Path | None = None
    if queue_id is not None:
        preview_path = data_dir / "gt" / "preview" / f"{queue_id}.jpg"

    log.info(
        "labelling session=%s cam=%s clip=%s prompt=%r time_range=%s queue_id=%s",
        session_id, camera_id, clip.name, prompt, time_range, queue_id,
    )
    record = labeller.label_video(
        mov_path=clip,
        video_start_pts_s=video_start_pts_s,
        session_id=session_id,
        camera_id=camera_id,
        prompt=prompt,
        min_confidence=min_confidence,
        max_frames=max_frames,
        time_range=time_range,
        progress_callback=_make_progress_emitter(queue_id=queue_id),
        preview_callback=_make_preview_writer(preview_path=preview_path),
    )
    _atomic_write_text(out_path, record.model_dump_json(indent=2))
    print(
        _DONE_FMT.format(labelled=record.frames_labelled, decoded=record.frames_decoded),
        file=sys.stderr, flush=True,
    )
    log.info(
        "wrote %s: %d/%d frames labelled (confidence >= %.2f)",
        out_path.name, record.frames_labelled, record.frames_decoded, min_confidence,
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="server data dir (default: server/data)",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", help="single session id (s_xxxxxxxx)")
    target.add_argument("--all", action="store_true", help="label every archived session")
    parser.add_argument("--cam", help="single camera (A/B); required with --session")
    parser.add_argument("--prompt", default="blue ball", help="SAM 3 text prompt")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="drop detections below this score (default 0.5)",
    )
    # `--limit-frames` (head N frames) and `--time-range` (window into
    # the clip in video-relative seconds) are alternative ways to clamp
    # the propagation window. Mutually exclusive — passing both is a
    # programming error and we'd rather fail fast.
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--limit-frames",
        type=int,
        default=None,
        help="stop after N decoded frames (dev iteration)",
    )
    window.add_argument(
        "--time-range",
        type=float,
        nargs=2,
        metavar=("START_S", "END_S"),
        default=None,
        help="label only frames whose video-relative PTS falls in [START_S, END_S]",
    )
    parser.add_argument(
        "--queue-id",
        default=None,
        help=(
            "queue item id (q_<8 hex>) — when set, mask preview JPEGs are "
            "written to data/gt/preview/<queue-id>.jpg for the dashboard. "
            "Internal use by gt_queue_worker; ignore for manual CLI runs."
        ),
    )
    parser.add_argument(
        "--image-size",
        type=int,
        default=1008,
        help="SAM 3 inference resolution (default 1008, model native)",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="auto / mps / cuda / cpu (default auto)",
    )
    parser.add_argument(
        "--model-id",
        default=Sam3VideoLabeller.DEFAULT_MODEL_ID,
        help=f"HF model id (default {Sam3VideoLabeller.DEFAULT_MODEL_ID})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-label even if GT JSON already exists",
    )
    args = parser.parse_args(argv)

    if args.session and not args.cam:
        parser.error("--cam is required with --session")

    if args.queue_id is not None and not _QUEUE_ID_RE.match(args.queue_id):
        parser.error(
            f"--queue-id must match {_QUEUE_ID_RE.pattern!r} (got {args.queue_id!r})"
        )

    if args.time_range is not None:
        t_start, t_end = args.time_range
        if not (t_start >= 0.0 and t_end > t_start):
            parser.error(
                f"--time-range must satisfy 0 <= START < END (got {t_start}, {t_end})"
            )

    if args.all and (args.time_range is not None or args.queue_id is not None):
        parser.error(
            "--time-range and --queue-id are per-(session, cam) flags; "
            "they don't make sense with --all (each session has its own window)"
        )

    labeller = Sam3VideoLabeller(
        model_id=args.model_id,
        device=args.device,
        image_size=args.image_size,
    )

    if args.session:
        targets = [(args.session, args.cam)]
    else:
        targets = _list_archived_sessions(args.data_dir / "pitches")
        log.info("--all: %d (session, cam) targets found", len(targets))

    time_range = (
        (float(args.time_range[0]), float(args.time_range[1]))
        if args.time_range is not None
        else None
    )

    successes = 0
    for session_id, camera_id in targets:
        try:
            result = _label_one(
                labeller,
                data_dir=args.data_dir,
                session_id=session_id,
                camera_id=camera_id,
                prompt=args.prompt,
                min_confidence=args.min_confidence,
                max_frames=args.limit_frames,
                overwrite=args.overwrite,
                time_range=time_range,
                queue_id=args.queue_id,
            )
            if result is not None:
                successes += 1
        except KeyboardInterrupt:
            log.warning("interrupted; partial GT may exist")
            return 130
        except Exception as e:
            log.exception(
                "FAILED session=%s cam=%s: %s",
                session_id, camera_id, e,
            )

    log.info("done: %d/%d targets labelled", successes, len(targets))
    return 0 if successes == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
