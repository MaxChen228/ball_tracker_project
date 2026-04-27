"""Retrofit existing sessions to use the MOV's actual pixel dims.

The iPhone's `IntrinsicsStore` shipped `image_width_px / image_height_px`
at calibration resolution (e.g. 1920×1080) even when the encoder produced
a smaller grid (720p). This broke downstream scaling: server detection
px/py landed in the MOV's real dims, but the payload claimed 1080p, so
the viewer's virtual canvas drew the ball at the wrong pixel and
triangulation rescaled intrinsics to the wrong grid.

The `/pitch` handler now reconciles on ingest (see `probe_dims`). This
script does the same for already-stored sessions: rewrites each pitch
JSON in place with the corrected dims, then re-runs triangulation and
rewrites the result JSON so the events panel and viewer pick up the
fix without needing a server restart's reload path to be fancy.

Usage:
    uv run python scripts/retrofit_image_dims.py              # fix all
    uv run python scripts/retrofit_image_dims.py --dry-run    # report only
    uv run python scripts/retrofit_image_dims.py --session s_xxx   # one
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure the server package imports resolve when this script is launched
# from the server/scripts directory.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from pairing import scale_pitch_to_video_dims, triangulate_cycle
from schemas import CalibrationSnapshot, PitchPayload, SessionResult
from video import probe_dims

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("retrofit")


def _find_clip(video_dir: Path, session_id: str, cam: str) -> Path | None:
    for path in video_dir.glob(f"session_{session_id}_{cam}.*"):
        return path
    return None


def _load_calibrations(calib_dir: Path) -> dict[str, CalibrationSnapshot]:
    out: dict[str, CalibrationSnapshot] = {}
    if not calib_dir.is_dir():
        return out
    for path in sorted(calib_dir.glob("*.json")):
        try:
            snap = CalibrationSnapshot.model_validate_json(path.read_text())
            out[snap.camera_id] = snap
        except Exception as e:
            log.warning("skip bad calibration %s: %s", path.name, e)
    return out


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def retrofit(data_dir: Path, session_filter: str | None, dry_run: bool) -> int:
    pitches_dir = data_dir / "pitches"
    videos_dir = data_dir / "videos"
    results_dir = data_dir / "results"
    calib_dir = data_dir / "calibrations"
    results_dir.mkdir(parents=True, exist_ok=True)

    calibrations = _load_calibrations(calib_dir)

    # Group pitch JSONs by session so we re-triangulate once per session
    # after both A and B (if present) have been updated.
    sessions: dict[str, dict[str, Path]] = {}
    for path in sorted(pitches_dir.glob("session_*.json")):
        stem = path.stem  # e.g. session_s_xxx_A
        parts = stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        prefix, cam = parts
        session_id = prefix.removeprefix("session_")
        if cam not in ("A", "B"):
            continue
        if session_filter and session_id != session_filter:
            continue
        sessions.setdefault(session_id, {})[cam] = path

    if not sessions:
        log.info("no sessions to retrofit (filter=%s)", session_filter)
        return 0

    fixed = 0
    for session_id in sorted(sessions.keys()):
        log.info("--- %s ---", session_id)
        cam_paths = sessions[session_id]
        updated_pitches: dict[str, PitchPayload] = {}

        for cam in ("A", "B"):
            path = cam_paths.get(cam)
            if path is None:
                continue
            try:
                pitch = PitchPayload.model_validate_json(path.read_text())
            except Exception as e:
                log.warning("  %s: cannot parse pitch: %s", path.name, e)
                continue

            clip = _find_clip(videos_dir, session_id, cam)
            if clip is None:
                log.info("  cam %s: no MOV on disk, skipping dim probe", cam)
                updated_pitches[cam] = pitch
                continue

            real = probe_dims(clip)
            if real is None:
                log.info("  cam %s: probe_dims failed", cam)
                updated_pitches[cam] = pitch
                continue
            rw, rh = real
            if pitch.image_width_px == rw and pitch.image_height_px == rh:
                log.info("  cam %s: dims already %dx%d, ok", cam, rw, rh)
                updated_pitches[cam] = pitch
                continue

            log.info(
                "  cam %s: rewrite dims %sx%s -> %dx%d",
                cam, pitch.image_width_px, pitch.image_height_px, rw, rh,
            )
            pitch = pitch.model_copy(update={"image_width_px": rw, "image_height_px": rh})
            if not dry_run:
                _atomic_write(path, pitch.model_dump_json())
                fixed += 1
            updated_pitches[cam] = pitch

        a, b = updated_pitches.get("A"), updated_pitches.get("B")
        if a is None or b is None:
            log.info("  missing pair, skipping triangulation")
            continue

        def _rescale(p: PitchPayload) -> PitchPayload:
            cal = calibrations.get(p.camera_id)
            dims = (cal.image_width_px, cal.image_height_px) if cal else None
            return scale_pitch_to_video_dims(p, dims)

        a_scaled = _rescale(a)
        b_scaled = _rescale(b)
        result = SessionResult(
            session_id=session_id,
            camera_a_received=True,
            camera_b_received=True,
        )
        if a_scaled.frames_server_post and b_scaled.frames_server_post:
            try:
                result.points = triangulate_cycle(a_scaled, b_scaled, source="server")
            except Exception as e:
                result.error = f"{type(e).__name__}: {e}"
                log.warning("  server triangulation failed: %s", e)
        log.info(
            "  re-triangulated: server=%d points%s",
            len(result.points),
            f" err={result.error}" if result.error else "",
        )
        if not dry_run:
            _atomic_write(results_dir / f"session_{session_id}.json",
                          result.model_dump_json())

    log.info("done. pitches rewritten: %d%s", fixed, " (dry-run)" if dry_run else "")
    return fixed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--session", default=None,
                        help="Only process this session id (e.g. s_50e743fc)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    retrofit(args.data_dir, args.session, args.dry_run)


if __name__ == "__main__":
    main()
