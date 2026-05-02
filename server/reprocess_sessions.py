"""Offline re-run of HSV detection + triangulation over already-recorded
sessions. For each pitch JSON, looks up the preset that produced it (via
the frozen `server_post_config_used.preset_name` or, on first server_post
run, `live_config_used.preset_name`), reads that preset's CURRENT values
from `data/presets/<name>.json`, re-runs `detect_pitch`, rewrites the
pitch JSON, and re-triangulates sessions where both A and B are present.

Selection (mutually exclusive, one required):
    --since today | YYYY-MM-DD     filter pitch JSONs by mtime
    --session s_xxxx [s_yyyy ...]  explicit session IDs
    --all                          every pitch on disk

Snapshot source (default → per-pitch frozen preset, current values):
    --force-preset <name>          load one preset from disk, apply to all
    --params <file.json>           load entire snapshot from JSON
    --use-frozen-snapshot          replay each pitch's stored snapshot
    --algorithm-id <id>            override only the algorithm_id slot
                                   (combinable with default or --force-preset)

Workflow:
    --dry-run                      detect+triangulate, don't overwrite
    --strict                       exit non-zero on any per-pitch failure
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import algorithms
import presets
import session_results
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from pairing_tuning import PairingTuning
from schemas import (
    CalibrationSnapshot,
    DetectionConfigSnapshotPayload,
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
PAIRING_TUNING_PATH = DATA_DIR / "pairing_tuning.json"

VIDEO_EXTS = (".mov", ".mp4", ".m4v")


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _snapshot_from_preset(preset: presets.Preset) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=preset.algorithm_id,
        hsv=HSVRangePayload(
            h_min=preset.hsv.h_min, h_max=preset.hsv.h_max,
            s_min=preset.hsv.s_min, s_max=preset.hsv.s_max,
            v_min=preset.hsv.v_min, v_max=preset.hsv.v_max,
        ),
        shape_gate=ShapeGatePayload(
            aspect_min=preset.shape_gate.aspect_min,
            fill_min=preset.shape_gate.fill_min,
        ),
        preset_name=preset.name,
    )


def _frozen_preset_name(pitch: PitchPayload) -> str | None:
    """Per-pitch identity claim for which preset produced this session.
    server_post side wins because it was the most recent detection; live
    side is the fallback for pitches that never ran server_post yet
    (first reprocess sweep). No silent fallback past these two slots."""
    if pitch.server_post_config_used is not None:
        if pitch.server_post_config_used.preset_name is not None:
            return pitch.server_post_config_used.preset_name
    if pitch.live_config_used is not None:
        return pitch.live_config_used.preset_name
    return None


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
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"bad --since value: {s!r}")
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def select_pitch_files(args: argparse.Namespace) -> list[Path]:
    paths = sorted(PITCH_DIR.glob("session_*.json"))
    if args.session:
        wanted = {s if s.startswith("s_") else f"s_{s}" for s in args.session}
        paths = [p for p in paths if any(f"_{sid}_" in p.name for sid in wanted)]
    if args.since:
        cutoff = parse_since(args.since).timestamp()
        paths = [p for p in paths if p.stat().st_mtime >= cutoff]
    return paths


def resolve_snapshot_for_pitch(
    pitch: PitchPayload,
    *,
    use_frozen_snapshot: bool,
    params_snapshot: DetectionConfigSnapshotPayload | None,
    force_preset_snapshot: DetectionConfigSnapshotPayload | None,
    algorithm_id_override: str | None,
) -> DetectionConfigSnapshotPayload | None:
    """Pick the snapshot for one pitch given the operator's flags. Returns
    None when the pitch should be skipped (with a logged reason). The
    four sources are mutually exclusive at the CLI parse layer; here we
    just dispatch in priority order: frozen > params > force-preset >
    per-pitch frozen preset lookup."""
    if use_frozen_snapshot:
        if pitch.server_post_config_used is None:
            logger.warning(
                "  skip %s/%s — --use-frozen-snapshot but pitch has no "
                "server_post_config_used (legacy pre-freeze pitch); "
                "rerun under --force-preset to stamp one",
                pitch.session_id, pitch.camera_id,
            )
            return None
        return pitch.server_post_config_used

    if params_snapshot is not None:
        return params_snapshot

    if force_preset_snapshot is not None:
        snap = force_preset_snapshot
    else:
        name = _frozen_preset_name(pitch)
        if name is None:
            logger.warning(
                "  skip %s/%s — no frozen preset_name on pitch (legacy "
                "session pre-dating preset identity stamp); rerun under "
                "--force-preset <name> to apply a preset explicitly",
                pitch.session_id, pitch.camera_id,
            )
            return None
        try:
            preset = presets.load_preset(DATA_DIR, name)
        except KeyError:
            logger.warning(
                "  skip %s/%s — frozen preset %r no longer exists on disk; "
                "restore the preset file or rerun under --force-preset",
                pitch.session_id, pitch.camera_id, name,
            )
            return None
        snap = _snapshot_from_preset(preset)

    if algorithm_id_override is not None:
        snap = snap.model_copy(update={"algorithm_id": algorithm_id_override})
    return snap


def rerun_detection(
    pitch_path: Path,
    snapshot: DetectionConfigSnapshotPayload,
    dry_run: bool,
) -> PitchPayload | None:
    """Run server-side detection on one persisted pitch using the supplied
    snapshot. Caller (main) is responsible for picking the snapshot via
    `resolve_snapshot_for_pitch`. `pitch.server_post_config_used` is
    overwritten with the snapshot that just produced these frames."""
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    video = find_video(pitch.session_id, pitch.camera_id)
    if video is None:
        logger.warning("  skip %s/%s — no MOV", pitch.session_id, pitch.camera_id)
        return None

    old_hits = sum(1 for f in pitch.frames_server_post if f.px is not None)
    frames = algorithms.run_detection(
        snapshot.algorithm_id,
        video,
        pitch.video_start_pts_s,
        {"hsv": snapshot.hsv, "shape_gate": snapshot.shape_gate},
    )
    new_hits = sum(1 for f in frames if f.px is not None)
    logger.info(
        "  %s/%s  preset=%s  frames=%d  hits %d → %d",
        pitch.session_id, pitch.camera_id,
        snapshot.preset_name if snapshot.preset_name is not None else "custom",
        len(frames), old_hits, new_hits,
    )
    pitch.frames_server_post = frames
    pitch.server_post_config_used = snapshot
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
    result = SessionResult(
        session_id=sid,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
        cost_threshold=pairing_tuning.cost_threshold,
        gap_threshold_m=pairing_tuning.gap_threshold_m,
    )
    used = session_results.aggregate_pitch_used_configs(a, b, sid)
    result.live_config_used = used["live_config_used"]
    result.server_post_config_used = used["server_post_config_used"]
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
            result.triangulated_by_path["server_post"] = pts
            result.paths_completed.add("server_post")
            result.triangulated = pts
            result.points = list(pts)

    session_results.stamp_segments_on_result(result)

    n = len(result.points)
    logger.info(
        "  %s  triangulated %d pts%s",
        sid,
        n,
        f"  err={result.error}" if result.error else "",
    )
    if not dry_run:
        atomic_write(RESULT_DIR / f"session_{sid}.json", result.model_dump_json())


def _load_force_preset_snapshot(name: str) -> DetectionConfigSnapshotPayload:
    try:
        preset = presets.load_preset(DATA_DIR, name)
    except KeyError:
        raise SystemExit(
            f"--force-preset {name!r}: preset does not exist on disk; "
            f"check `data/presets/` or use --params"
        ) from None
    snap = _snapshot_from_preset(preset)
    logger.info(
        "force-preset %s — algorithm=%s hsv h[%d-%d] s[%d-%d] v[%d-%d] "
        "aspect>=%.2f fill>=%.2f",
        name, snap.algorithm_id,
        snap.hsv.h_min, snap.hsv.h_max,
        snap.hsv.s_min, snap.hsv.s_max,
        snap.hsv.v_min, snap.hsv.v_max,
        snap.shape_gate.aspect_min, snap.shape_gate.fill_min,
    )
    return snap


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
        help="replay each pitch's stored server_post_config_used. "
             "Reproducibility-audit path; pitches that pre-date the freeze "
             "are skipped with a warning.",
    )
    ap.add_argument(
        "--force-preset",
        metavar="NAME",
        help="load one preset by name and apply to every pitch. Overrides "
             "the per-pitch frozen preset lookup. Use when you want to "
             "re-evaluate all sessions under one preset (e.g., consolidating "
             "history under tennis).",
    )
    ap.add_argument(
        "--algorithm-id",
        help="override the algorithm_id slot of whichever snapshot is "
             "selected (per-pitch frozen preset / --force-preset). Must be "
             "a registered id (see server/algorithms/__init__.py). "
             "Mutually exclusive with --params (--params already carries "
             "its own algorithm_id) and --use-frozen-snapshot.",
    )
    ap.add_argument(
        "--params",
        type=Path,
        help="JSON file matching DetectionConfigSnapshotPayload shape; "
             "applied to every pitch. Overrides per-pitch lookup and "
             "--force-preset.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any pitch fails to reprocess.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Mutex matrix — every snapshot source is exclusive of the others;
    # --algorithm-id is the only one that combines with default-B1 or
    # --force-preset (it overrides the id slot of whichever snapshot
    # got chosen). --params and --use-frozen-snapshot already carry
    # their own algorithm_id, so combining is ambiguous.
    if args.params is not None and args.force_preset is not None:
        raise SystemExit(
            "--params and --force-preset are mutually exclusive; pick one "
            "snapshot source"
        )
    if args.algorithm_id is not None and args.params is not None:
        raise SystemExit(
            "--algorithm-id and --params are mutually exclusive; --params "
            "already carries its own algorithm_id"
        )
    if args.use_frozen_snapshot and (
        args.algorithm_id is not None
        or args.params is not None
        or args.force_preset is not None
    ):
        raise SystemExit(
            "--use-frozen-snapshot replays the stamp on each pitch and "
            "ignores --algorithm-id / --params / --force-preset; drop the "
            "override or remove --use-frozen-snapshot"
        )

    params_snapshot: DetectionConfigSnapshotPayload | None = None
    force_preset_snapshot: DetectionConfigSnapshotPayload | None = None
    algorithm_id_override: str | None = None

    if args.params is not None:
        params_snapshot = _load_snapshot_from_file(args.params)
    elif args.force_preset is not None:
        force_preset_snapshot = _load_force_preset_snapshot(args.force_preset)

    if args.algorithm_id is not None:
        try:
            algorithms.validate_id(args.algorithm_id)
        except ValueError as e:
            raise SystemExit(f"--algorithm-id: {e}") from None
        algorithm_id_override = args.algorithm_id
        logger.info("algorithm_id override → %s", algorithm_id_override)

    pairing_tuning = load_pairing_tuning()
    calibrations = load_calibrations()

    pitch_paths = select_pitch_files(args)
    logger.info("matched %d pitch JSON(s)", len(pitch_paths))
    if not pitch_paths:
        return

    by_session: dict[str, dict[str, PitchPayload]] = {}
    failures: list[tuple[str, str]] = []
    for path in pitch_paths:
        logger.info("redetect %s", path.name)
        try:
            pitch_for_resolve = PitchPayload.model_validate_json(path.read_text())
            snapshot = resolve_snapshot_for_pitch(
                pitch_for_resolve,
                use_frozen_snapshot=args.use_frozen_snapshot,
                params_snapshot=params_snapshot,
                force_preset_snapshot=force_preset_snapshot,
                algorithm_id_override=algorithm_id_override,
            )
            if snapshot is None:
                continue
            pitch = rerun_detection(path, snapshot, args.dry_run)
        except Exception as e:
            logger.error("FAIL %s: %s", path.name, e)
            failures.append((path.name, str(e)[:200]))
            continue
        if pitch is not None:
            by_session.setdefault(pitch.session_id, {})[pitch.camera_id] = pitch

    for sid in sorted(by_session):
        cams = by_session[sid]
        for cam in ("A", "B"):
            if cam in cams:
                continue
            counterpart = PITCH_DIR / f"session_{sid}_{cam}.json"
            if counterpart.exists():
                cams[cam] = PitchPayload.model_validate_json(counterpart.read_text())
        triangulate_session(sid, cams, calibrations, pairing_tuning, args.dry_run)

    if failures:
        logger.error("%d pitch file(s) failed to reprocess:", len(failures))
        for name, err in failures:
            logger.error("  %s — %s", name, err)
        if args.strict:
            raise SystemExit(
                f"--strict: aborting with non-zero exit due to "
                f"{len(failures)} reprocess failure(s)"
            )
    logger.info("done.")


def _load_snapshot_from_file(path: Path) -> DetectionConfigSnapshotPayload:
    """Read a DetectionConfigSnapshotPayload from a JSON file. Strict
    parse via Pydantic. Wraps file-not-found / malformed JSON / schema
    errors in `SystemExit` with the path so operator gets an actionable
    one-line message instead of an opaque stack trace."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise SystemExit(f"--params {path}: file does not exist") from None
    except OSError as e:
        raise SystemExit(f"--params {path}: {e}") from None
    try:
        return DetectionConfigSnapshotPayload.model_validate_json(raw)
    except Exception as e:
        raise SystemExit(f"--params {path}: {e}") from None


if __name__ == "__main__":
    sys.exit(main())
