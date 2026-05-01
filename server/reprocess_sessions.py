"""Offline re-run of HSV detection + triangulation over already-recorded
sessions. Reads the current `data/detection_config.json`, iterates pitch
JSONs paired with their stored MOVs, re-runs `detect_pitch`, rewrites the
pitch JSON, and re-triangulates sessions where both A and B are present.

Selection (mutually exclusive, one required):
    --since today | YYYY-MM-DD     filter pitch JSONs by mtime
    --session s_xxxx [s_yyyy ...]  explicit session IDs
    --all                          every pitch on disk

Config source (default → current disk config):
    --algorithm-id <id>            override only the algorithm_id slot
    --params <file.json>           load entire snapshot from JSON
    --use-frozen-snapshot          replay each pitch's stored snapshot

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
import session_results
from detection import HSVRange, ShapeGate
from detection_config import load_or_migrate
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from pairing_tuning import PairingTuning
from pipeline import detect_pitch
from schemas import (
    CalibrationSnapshot,
    DetectionConfigSnapshotPayload,
    PitchPayload,
    SessionResult,
)

logger = logging.getLogger("reprocess")

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
VIDEO_DIR = DATA_DIR / "videos"
RESULT_DIR = DATA_DIR / "results"
CAL_DIR = DATA_DIR / "calibrations"
PAIRING_TUNING_PATH = DATA_DIR / "pairing_tuning.json"

VIDEO_EXTS = (".mov", ".mp4", ".m4v")


def load_detection_config_snapshot() -> DetectionConfigSnapshotPayload:
    """Read the active detection config from disk and freeze a snapshot.
    On a fresh-empty data/, `load_or_migrate` returns its Tennis
    default without writing — the INFO log below surfaces "preset=tennis"
    so the operator can spot it (not a silent fallback)."""
    cfg = load_or_migrate(DATA_DIR, atomic_write=atomic_write)
    snapshot = DetectionConfigSnapshotPayload.from_detection_config(cfg)
    preset_label = snapshot.preset_name if snapshot.preset_name is not None else "custom"
    logger.info(
        "detection_config algorithm=%s preset=%s hsv h[%d-%d] s[%d-%d] v[%d-%d] "
        "aspect>=%.2f fill>=%.2f",
        snapshot.algorithm_id, preset_label,
        snapshot.hsv.h_min, snapshot.hsv.h_max,
        snapshot.hsv.s_min, snapshot.hsv.s_max,
        snapshot.hsv.v_min, snapshot.hsv.v_max,
        snapshot.shape_gate.aspect_min, snapshot.shape_gate.fill_min,
    )
    return snapshot


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
    snapshot: DetectionConfigSnapshotPayload,
    dry_run: bool,
    *,
    use_frozen_snapshot: bool = False,
) -> PitchPayload | None:
    """Re-run server-side detection on one persisted pitch.

    Default (`use_frozen_snapshot=False`): use the supplied `snapshot`
    (current disk config) — matches the operator's tuning workflow.
    `pitch.server_post_config_used` is overwritten with the snapshot
    that just produced these frames.

    `use_frozen_snapshot=True`: reuse the snapshot already stamped on
    `pitch.server_post_config_used`. Reproducibility audit path;
    pitches that pre-date the server_post freeze fall back to the
    supplied current-disk `snapshot` with a warning."""
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    video = find_video(pitch.session_id, pitch.camera_id)
    if video is None:
        logger.warning("  skip %s/%s — no MOV", pitch.session_id, pitch.camera_id)
        return None

    if use_frozen_snapshot:
        if pitch.server_post_config_used is not None:
            effective = pitch.server_post_config_used
        else:
            logger.warning(
                "  %s/%s legacy pitch lacks server_post_config_used — "
                "using current disk config",
                pitch.session_id, pitch.camera_id,
            )
            effective = snapshot
    else:
        effective = snapshot

    hsv_eff = HSVRange(
        h_min=effective.hsv.h_min, h_max=effective.hsv.h_max,
        s_min=effective.hsv.s_min, s_max=effective.hsv.s_max,
        v_min=effective.hsv.v_min, v_max=effective.hsv.v_max,
    )
    gate_eff = ShapeGate(
        aspect_min=effective.shape_gate.aspect_min,
        fill_min=effective.shape_gate.fill_min,
    )

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
    pitch.server_post_config_used = effective
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
    # Mirror per-pitch per-path frozen snapshots onto the result so the
    # viewer / future audit can answer "what config produced these
    # points?" without reading the pitch JSON. Aggregation policy
    # (A-wins, B-fallback, warn on divergence) is shared with
    # `session_results.aggregate_pitch_used_configs`.
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
            # Mirror session_results.rebuild_result_for_session's authority
            # contract: viewer reads `triangulated` (per-path map plus the
            # winner picked by server_post→live precedence). `points` is
            # the legacy field; keep it in sync so older readers still work.
            result.triangulated_by_path["server_post"] = pts
            result.paths_completed.add("server_post")
            result.triangulated = pts
            result.points = list(pts)

    # Run the segmenter so reprocessed results carry the same
    # `segments` payload as the live cycle_end / recompute paths.
    # `stamp_segments_on_result` is idempotent and safe on empty
    # `triangulated`.
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
        help="reuse `pitch.server_post_config_used` instead of the current "
             "disk detection config. For reproducibility audits — default "
             "behavior is to pick up your current disk config so tuning "
             "workflows actually see new results.",
    )
    ap.add_argument(
        "--algorithm-id",
        help="override the algorithm_id of the active disk config. Must "
             "be a registered id (see server/algorithms/__init__.py). "
             "Mutually exclusive with --params (--params already carries "
             "its own algorithm_id).",
    )
    ap.add_argument(
        "--params",
        type=Path,
        help="JSON file matching DetectionConfigSnapshotPayload shape; "
             "replaces the disk config for this run only.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any pitch fails to reprocess. Default "
             "behaviour is to log failures + return 0 so partial runs "
             "complete; --strict is for automation that wants a hard "
             "fail signal.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.algorithm_id is not None and args.params is not None:
        raise SystemExit(
            "--algorithm-id and --params are mutually exclusive; --params "
            "already carries its own algorithm_id"
        )
    # `--use-frozen-snapshot` reads each pitch's stored snapshot and
    # ignores the disk/CLI snapshot entirely — combining it with a
    # snapshot override would silently drop the override. Reject up
    # front so the operator picks one source of truth.
    if args.use_frozen_snapshot and (
        args.algorithm_id is not None or args.params is not None
    ):
        raise SystemExit(
            "--use-frozen-snapshot ignores --algorithm-id / --params; "
            "drop the override or remove --use-frozen-snapshot"
        )

    if args.params is not None:
        snapshot = _load_snapshot_from_file(args.params)
    else:
        snapshot = load_detection_config_snapshot()
        if args.algorithm_id is not None:
            try:
                algorithms.validate_id(args.algorithm_id)
            except ValueError as e:
                raise SystemExit(f"--algorithm-id: {e}") from None
            snapshot = snapshot.model_copy(
                update={"algorithm_id": args.algorithm_id}
            )
            logger.info("algorithm_id override → %s", args.algorithm_id)

    pairing_tuning = load_pairing_tuning()
    calibrations = load_calibrations()

    pitch_paths = select_pitch_files(args)
    logger.info("matched %d pitch JSON(s)", len(pitch_paths))
    if not pitch_paths:
        return

    # group by session, re-detect each pitch. Per-file try/except so a
    # single corrupt MOV / unreadable JSON doesn't abort the whole batch.
    # Failures tallied + reported at end so they're loud, not silent.
    by_session: dict[str, dict[str, PitchPayload]] = {}
    failures: list[tuple[str, str]] = []
    for path in pitch_paths:
        logger.info("redetect %s", path.name)
        try:
            pitch = rerun_detection(
                path, snapshot, args.dry_run,
                use_frozen_snapshot=args.use_frozen_snapshot,
            )
        except Exception as e:
            logger.error("FAIL %s: %s", path.name, e)
            failures.append((path.name, str(e)[:200]))
            continue
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
