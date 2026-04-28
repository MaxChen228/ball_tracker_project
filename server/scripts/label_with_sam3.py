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
import sys
from pathlib import Path

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

    log.info(
        "labelling session=%s cam=%s clip=%s prompt=%r",
        session_id, camera_id, clip.name, prompt,
    )
    record = labeller.label_video(
        mov_path=clip,
        video_start_pts_s=video_start_pts_s,
        session_id=session_id,
        camera_id=camera_id,
        prompt=prompt,
        min_confidence=min_confidence,
        max_frames=max_frames,
    )
    out_path.write_text(record.model_dump_json(indent=2))
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
    parser.add_argument(
        "--limit-frames",
        type=int,
        default=None,
        help="stop after N decoded frames (dev iteration)",
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
