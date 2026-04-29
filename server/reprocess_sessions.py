"""Offline re-run of HSV detection + triangulation over already-recorded
sessions. Reads the current `data/hsv_range.json`, iterates pitch JSONs
paired with their stored MOVs, re-runs `detect_pitch`, rewrites the pitch
JSON, and re-triangulates sessions where both A and B are present.

Usage:
    uv run python reprocess_sessions.py --since today
    uv run python reprocess_sessions.py --since 2026-04-20
    uv run python reprocess_sessions.py --session s_c8d36fe2
    uv run python reprocess_sessions.py --all
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from detection import HSVRange, ShapeGate
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from pipeline import detect_pitch
from schemas import CalibrationSnapshot, PitchPayload, SessionResult

logger = logging.getLogger("reprocess")

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
VIDEO_DIR = DATA_DIR / "videos"
RESULT_DIR = DATA_DIR / "results"
CAL_DIR = DATA_DIR / "calibrations"
HSV_PATH = DATA_DIR / "hsv_range.json"
SHAPE_GATE_PATH = DATA_DIR / "shape_gate.json"
CANDIDATE_SELECTOR_TUNING_PATH = DATA_DIR / "candidate_selector_tuning.json"

VIDEO_EXTS = (".mov", ".mp4", ".m4v")


def load_hsv() -> HSVRange:
    if not HSV_PATH.exists():
        logger.warning("no hsv_range.json — using default")
        return HSVRange.default()
    obj = json.loads(HSV_PATH.read_text())
    rng = HSVRange(
        h_min=int(obj["h_min"]), h_max=int(obj["h_max"]),
        s_min=int(obj["s_min"]), s_max=int(obj["s_max"]),
        v_min=int(obj["v_min"]), v_max=int(obj["v_max"]),
    )
    logger.info(
        "hsv h[%d-%d] s[%d-%d] v[%d-%d]",
        rng.h_min, rng.h_max, rng.s_min, rng.s_max, rng.v_min, rng.v_max,
    )
    return rng


def load_shape_gate() -> ShapeGate:
    if not SHAPE_GATE_PATH.exists():
        return ShapeGate.default()
    obj = json.loads(SHAPE_GATE_PATH.read_text())
    gate = ShapeGate(
        aspect_min=float(obj["aspect_min"]),
        fill_min=float(obj["fill_min"]),
    )
    logger.info("shape_gate aspect>=%.2f fill>=%.2f", gate.aspect_min, gate.fill_min)
    return gate


def load_candidate_selector_tuning() -> "CandidateSelectorTuning":
    from candidate_selector import CandidateSelectorTuning
    if not CANDIDATE_SELECTOR_TUNING_PATH.exists():
        return CandidateSelectorTuning.default()
    obj = json.loads(CANDIDATE_SELECTOR_TUNING_PATH.read_text())
    t = CandidateSelectorTuning(
        r_px_expected=float(obj["r_px_expected"]),
        w_area=float(obj["w_area"]),
        w_dist=float(obj["w_dist"]),
        dist_cost_sat_radii=float(obj["dist_cost_sat_radii"]),
    )
    logger.info(
        "selector r=%.1f wA=%.2f wD=%.2f sat=%.1f",
        t.r_px_expected, t.w_area, t.w_dist, t.dist_cost_sat_radii,
    )
    return t


def load_calibrations() -> dict[str, CalibrationSnapshot]:
    cals: dict[str, CalibrationSnapshot] = {}
    if not CAL_DIR.exists():
        return cals
    for path in sorted(CAL_DIR.glob("*.json")):
        try:
            cals[path.stem] = CalibrationSnapshot.model_validate_json(path.read_text())
        except Exception as e:
            logger.warning("skip calibration %s: %s", path.name, e)
    return cals


def find_video(session_id: str, camera_id: str) -> Path | None:
    for ext in VIDEO_EXTS:
        p = VIDEO_DIR / f"session_{session_id}_{camera_id}{ext}"
        if p.exists():
            return p
    return None


def parse_since(s: str) -> datetime:
    if s == "today":
        now = datetime.now().astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    # accept YYYY-MM-DD or full ISO
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"bad --since value: {s!r}")
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def select_pitch_files(args: argparse.Namespace) -> list[Path]:
    paths = sorted(PITCH_DIR.glob("session_*.json"))
    if args.session:
        wanted = {s if s.startswith("s_") else f"s_{s}" for s in args.session}
        paths = [p for p in paths if any(f"_{sid}_" in p.name for sid in wanted)]
    if args.since:
        cutoff = parse_since(args.since).timestamp()
        paths = [p for p in paths if p.stat().st_mtime >= cutoff]
    return paths


def rerun_detection(pitch_path: Path, hsv: HSVRange, shape_gate: ShapeGate, selector_tuning, dry_run: bool) -> PitchPayload | None:
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    video = find_video(pitch.session_id, pitch.camera_id)
    if video is None:
        logger.warning("  skip %s/%s — no MOV", pitch.session_id, pitch.camera_id)
        return None
    old_hits = sum(1 for f in pitch.frames_server_post if f.px is not None)
    frames = detect_pitch(
        video_path=video,
        video_start_pts_s=pitch.video_start_pts_s,
        hsv_range=hsv,
        shape_gate=shape_gate,
        selector_tuning=selector_tuning,
    )
    new_hits = sum(1 for f in frames if f.px is not None)
    logger.info(
        "  %s/%s  frames=%d  hits %d → %d",
        pitch.session_id, pitch.camera_id, len(frames), old_hits, new_hits,
    )
    pitch.frames_server_post = frames
    if not dry_run:
        atomic_write(pitch_path, pitch.model_dump_json())
    return pitch


def triangulate_session(
    sid: str,
    pitches: dict[str, PitchPayload],
    calibrations: dict[str, CalibrationSnapshot],
    dry_run: bool,
) -> None:
    a = pitches.get("A")
    b = pitches.get("B")
    result = SessionResult(
        session_id=sid,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
    )
    if a is None or b is None:
        logger.info("  %s — solo (%s only); skipping triangulation",
                    sid, "A" if a else "B")
        if not dry_run:
            atomic_write(RESULT_DIR / f"session_{sid}.json", result.model_dump_json())
        return

    def scale(p: PitchPayload) -> PitchPayload:
        cal = calibrations.get(p.camera_id)
        dims = (cal.image_width_px, cal.image_height_px) if cal else None
        return scale_pitch_to_video_dims(p, dims)

    if a.frames_server_post and b.frames_server_post:
        try:
            result.points = triangulate_cycle(scale(a), scale(b), source="server")
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"

    n = len(result.points)
    logger.info(
        "  %s  triangulated %d pts%s",
        sid,
        n,
        f"  err={result.error}" if result.error else "",
    )
    if not dry_run:
        atomic_write(RESULT_DIR / f"session_{sid}.json", result.model_dump_json())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--since", help="YYYY-MM-DD or 'today' — filter by pitch JSON mtime")
    g.add_argument("--session", nargs="+", help="explicit session IDs (s_xxxx or xxxx)")
    g.add_argument("--all", action="store_true", help="process every pitch JSON")
    ap.add_argument("--dry-run", action="store_true", help="detect+triangulate but don't overwrite JSONs")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    hsv = load_hsv()
    shape_gate = load_shape_gate()
    selector_tuning = load_candidate_selector_tuning()
    calibrations = load_calibrations()

    pitch_paths = select_pitch_files(args)
    logger.info("matched %d pitch JSON(s)", len(pitch_paths))
    if not pitch_paths:
        return

    # group by session, re-detect each pitch
    by_session: dict[str, dict[str, PitchPayload]] = {}
    for path in pitch_paths:
        logger.info("redetect %s", path.name)
        pitch = rerun_detection(path, hsv, shape_gate, selector_tuning, args.dry_run)
        if pitch is not None:
            by_session.setdefault(pitch.session_id, {})[pitch.camera_id] = pitch

    # re-triangulate each affected session. Re-load the unchanged counterpart
    # from disk if it wasn't in our filter so A+B sessions still pair.
    for sid in sorted(by_session):
        cams = by_session[sid]
        for cam in ("A", "B"):
            if cam in cams:
                continue
            counterpart = PITCH_DIR / f"session_{sid}_{cam}.json"
            if counterpart.exists():
                cams[cam] = PitchPayload.model_validate_json(counterpart.read_text())
        triangulate_session(sid, cams, calibrations, args.dry_run)

    logger.info("done.")


if __name__ == "__main__":
    sys.exit(main())
