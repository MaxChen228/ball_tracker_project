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

import session_results
from detection import HSVRange, ShapeGate
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from pairing_tuning import PairingTuning
from pipeline import detect_pitch
from schemas import (
    CalibrationSnapshot,
    HSVRangePayload,
    PitchPayload,
    SessionResult,
    ShapeGatePayload,
)

logger = logging.getLogger("reprocess")

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
VIDEO_DIR = DATA_DIR / "videos"
RESULT_DIR = DATA_DIR / "results"
CAL_DIR = DATA_DIR / "calibrations"
HSV_PATH = DATA_DIR / "hsv_range.json"
SHAPE_GATE_PATH = DATA_DIR / "shape_gate.json"
PAIRING_TUNING_PATH = DATA_DIR / "pairing_tuning.json"

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


def rerun_detection(
    pitch_path: Path,
    hsv: HSVRange,
    shape_gate: ShapeGate,
    dry_run: bool,
    *,
    use_frozen_snapshot: bool = False,
) -> PitchPayload | None:
    """Re-run server-side detection on one persisted pitch.

    Default: use **current** disk config (`data/hsv_range.json` etc.) —
    matches the operator's tuning workflow ("I tweaked HSV, rerun this
    session and see if it improves"). The freshly-used values are
    stamped back onto the pitch so `pitch.*_used` always reflects the
    config of the most recent detection run.

    Opt-in `use_frozen_snapshot=True` (CLI: `--use-frozen-snapshot`):
    re-use the values stamped on the pitch by the original detection
    run. For reproducibility audits — answers "what would the original
    live/server_post run have produced if I rebuilt from scratch?"
    Falls back to disk for legacy pitches that pre-date the stamp,
    logging a warning so the operator knows that rerun isn't an
    unambiguous historical reproduction."""
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    video = find_video(pitch.session_id, pitch.camera_id)
    if video is None:
        logger.warning("  skip %s/%s — no MOV", pitch.session_id, pitch.camera_id)
        return None

    if not use_frozen_snapshot:
        hsv_eff, gate_eff = hsv, shape_gate
    else:
        if pitch.hsv_range_used is not None:
            p = pitch.hsv_range_used
            hsv_eff = HSVRange(
                h_min=p.h_min, h_max=p.h_max,
                s_min=p.s_min, s_max=p.s_max,
                v_min=p.v_min, v_max=p.v_max,
            )
        else:
            logger.warning(
                "  %s/%s legacy pitch lacks hsv_range_used — using current disk config",
                pitch.session_id, pitch.camera_id,
            )
            hsv_eff = hsv
        if pitch.shape_gate_used is not None:
            gate_eff = ShapeGate(
                aspect_min=pitch.shape_gate_used.aspect_min,
                fill_min=pitch.shape_gate_used.fill_min,
            )
        else:
            logger.warning(
                "  %s/%s legacy pitch lacks shape_gate_used — using current disk config",
                pitch.session_id, pitch.camera_id,
            )
            gate_eff = shape_gate

    old_hits = sum(1 for f in pitch.frames_server_post if f.px is not None)
    frames = detect_pitch(
        video_path=video,
        video_start_pts_s=pitch.video_start_pts_s,
        hsv_range=hsv_eff,
        shape_gate=gate_eff,
    )
    new_hits = sum(1 for f in frames if f.px is not None)
    logger.info(
        "  %s/%s  frames=%d  hits %d → %d",
        pitch.session_id, pitch.camera_id, len(frames), old_hits, new_hits,
    )
    pitch.frames_server_post = frames
    # Re-stamp with the values just used so legacy pitches stop being legacy.
    pitch.hsv_range_used = HSVRangePayload(
        h_min=hsv_eff.h_min, h_max=hsv_eff.h_max,
        s_min=hsv_eff.s_min, s_max=hsv_eff.s_max,
        v_min=hsv_eff.v_min, v_max=hsv_eff.v_max,
    )
    pitch.shape_gate_used = ShapeGatePayload(
        aspect_min=gate_eff.aspect_min, fill_min=gate_eff.fill_min,
    )
    if not dry_run:
        atomic_write(pitch_path, pitch.model_dump_json())
    return pitch


def load_pairing_tuning() -> PairingTuning:
    """Mirror of `state._load_pairing_tuning_from_disk` for the offline
    script. Falls back to `PairingTuning.default()` when the dashboard
    has never written the file."""
    if not PAIRING_TUNING_PATH.exists():
        d = PairingTuning.default()
        logger.info("no pairing_tuning.json — using default cost=%.2f gap=%.2fm",
                    d.cost_threshold, d.gap_threshold_m)
        return d
    obj = json.loads(PAIRING_TUNING_PATH.read_text())
    d = PairingTuning.default()
    t = PairingTuning(
        cost_threshold=float(obj.get("cost_threshold", d.cost_threshold)),
        gap_threshold_m=float(obj.get("gap_threshold_m", d.gap_threshold_m)),
    )
    logger.info("pairing_tuning cost=%.2f gap=%.2fm",
                t.cost_threshold, t.gap_threshold_m)
    return t


def triangulate_session(
    sid: str,
    pitches: dict[str, PitchPayload],
    calibrations: dict[str, CalibrationSnapshot],
    pairing_tuning: PairingTuning,
    dry_run: bool,
) -> None:
    a = pitches.get("A")
    b = pitches.get("B")
    # Stamp the active tuning onto the result so the viewer's per-session
    # Cost / Gap sliders re-init at the values that produced the points.
    # Without this they'd show "off" and an Apply would silently overwrite
    # the result with whatever the user happened to drag the sliders to.
    result = SessionResult(
        session_id=sid,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
        cost_threshold=pairing_tuning.cost_threshold,
        gap_threshold_m=pairing_tuning.gap_threshold_m,
    )
    # Mirror the pitch-level frozen config onto the result so the viewer
    # tuning strip / future audit can answer "what HSV / shape-gate /
    # selector tuning produced these points?" without reading the pitch
    # JSON. Aggregation policy (A-wins, fall-back-to-B, warn on divergence)
    # is shared with `session_results.aggregate_pitch_used_configs` so the
    # online rebuild and offline reprocess paths can never silently disagree.
    used = session_results.aggregate_pitch_used_configs(a, b, sid)
    result.hsv_range_used = used["hsv_range_used"]
    result.shape_gate_used = used["shape_gate_used"]
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
            pts = triangulate_cycle(
                scale(a), scale(b), source="server", tuning=pairing_tuning,
            )
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
        else:
            # Mirror session_results.rebuild_result_for_session's authority
            # contract: viewer reads `triangulated` (per-path map plus the
            # winner picked by server_post→live precedence). `points` is
            # the legacy field; keep it in sync so older readers still work.
            result.triangulated_by_path["server_post"] = pts
            result.paths_completed.add("server_post")
            result.triangulated = pts
            result.points = list(pts)

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
    ap.add_argument(
        "--use-frozen-snapshot",
        action="store_true",
        help="reuse the per-pitch frozen detection-config snapshot "
             "(pitch.{hsv,shape_gate}_used) instead of current "
             "data/*.json values. For reproducibility audits — default "
             "behavior is to pick up your current disk config so tuning "
             "workflows actually see new results.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    hsv = load_hsv()
    shape_gate = load_shape_gate()
    pairing_tuning = load_pairing_tuning()
    calibrations = load_calibrations()

    pitch_paths = select_pitch_files(args)
    logger.info("matched %d pitch JSON(s)", len(pitch_paths))
    if not pitch_paths:
        return

    # group by session, re-detect each pitch
    by_session: dict[str, dict[str, PitchPayload]] = {}
    for path in pitch_paths:
        logger.info("redetect %s", path.name)
        pitch = rerun_detection(
            path, hsv, shape_gate, args.dry_run,
            use_frozen_snapshot=args.use_frozen_snapshot,
        )
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
        triangulate_session(sid, cams, calibrations, pairing_tuning, args.dry_run)

    logger.info("done.")


if __name__ == "__main__":
    sys.exit(main())
