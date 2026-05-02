"""Session result builder and triangulation coordinator.

`rebuild_result_for_session` is the authoritative constructor for
`SessionResult` — events / viewer all read it. It combines per-pipeline
frame counts, triangulation output (live, server_post), sync validation,
and legacy `points` semantics into one immutable-ish snapshot.

Lock discipline: `State._lock` is a `threading.Lock` (non-reentrant). The
two-phase pattern here — snapshot pitches / live / session under the lock,
then call path selection / triangulation / sync validation **outside** it
— is load-bearing; each of those downstream helpers re-acquires the lock
internally. Do not fold them into the outer `with state._lock:` block or
the process will deadlock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from detection_paths import (
    algorithm_id_for_path,
    get_path_frames,
    normalize_paths,
    paths_for_pitch,
    pitch_with_algorithm_frames,
    pitch_with_path_frames,
)
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from schemas import (
    DetectionPath,
    FramePayload,
    PitchPayload,
    SegmentRecord,
    SessionResult,
    TriangulatedPoint,
    _DEFAULT_PATHS,
)
from segmenter import Segment, find_segments

if TYPE_CHECKING:
    from pairing_tuning import PairingTuning
    from state import State


logger = logging.getLogger(__name__)


_FROZEN_USED_FIELDS = (
    "live_config_used",
    "server_post_config_used",
)


def aggregate_pitch_used_configs(
    a: PitchPayload | None,
    b: PitchPayload | None,
    sid: str,
) -> dict[str, object | None]:
    """Aggregate the per-pitch per-path frozen config snapshots into a
    single mapping. Policy: A wins, fall back to B. Divergence (operator
    edited config mid-cycle) logs warning, doesn't raise — diagnostic,
    not crashable. Shared by `rebuild_result` and reprocess so both paths
    enforce identical aggregation."""
    out: dict[str, object | None] = {}
    for field_name in _FROZEN_USED_FIELDS:
        va = getattr(a, field_name) if a is not None else None
        vb = getattr(b, field_name) if b is not None else None
        if va is not None and vb is not None and va != vb:
            logger.warning(
                "session %s A/B %s diverged (operator edited config "
                "mid-cycle?) — using A", sid, field_name,
            )
        out[field_name] = va if va is not None else vb
    return out


def _stamp_frozen_config_on_result(
    result: SessionResult,
    a: PitchPayload | None,
    b: PitchPayload | None,
) -> None:
    """Mirror per-pitch per-path frozen snapshots onto the SessionResult.
    Aggregation policy (A wins, fall back to B) lives in
    `aggregate_pitch_used_configs` so reprocess + rebuild share one
    source of truth."""
    used = aggregate_pitch_used_configs(a, b, result.session_id)
    for field_name, value in used.items():
        setattr(result, field_name, value)


def triangulate_pair(
    state: "State",
    a: PitchPayload,
    b: PitchPayload,
    *,
    source: str = "server",
    tuning: "PairingTuning | None" = None,
) -> list[TriangulatedPoint]:
    """Scale each pitch's intrinsics + homography to its MOV's actual pixel
    grid (using the cached calibration snapshot as the reference resolution)
    and then triangulate. When no snapshot is cached for a camera the scale
    factor falls back to 1.0 and the pitch is passed through unchanged — the
    legacy behaviour for pre-resolution-picker builds that always recorded
    at the calibration resolution.

    `source` picks the detection stream (`"server"` default reads
    `pitch.frames_server_post`).

    `tuning` overrides the pairing fan-out cost/gap thresholds; defaults to
    `state.pairing_tuning()` (operator's currently-applied global tuning)."""
    if tuning is None:
        tuning = state.pairing_tuning()
    with state._lock:
        cal_a = state._calibration_store.get(a.camera_id)
        cal_b = state._calibration_store.get(b.camera_id)
    a_scaled = scale_pitch_to_video_dims(
        a,
        (cal_a.image_width_px, cal_a.image_height_px) if cal_a else None,
    )
    b_scaled = scale_pitch_to_video_dims(
        b,
        (cal_b.image_width_px, cal_b.image_height_px) if cal_b else None,
    )
    return triangulate_cycle(a_scaled, b_scaled, source=source, tuning=tuning)


def _triangulate_non_current_algorithms(
    state: "State",
    a: PitchPayload | None,
    b: PitchPayload | None,
    sync_error: str | None,
    result: SessionResult,
    *,
    tuning: "PairingTuning | None" = None,
) -> None:
    """Phase 7 multi-algorithm result builder.

    The path-loop in `rebuild_result_for_session` triangulates only
    the *current* server_post slot (whatever
    `server_post_config_used.algorithm_id` names), feeding it into
    `triangulated_by_path["server_post"]`. The dict mirror fans that
    one entry into `triangulated_by_algorithm[<current_alg>]`.

    But Phase 7's `stamp_server_post_run` keeps history: running v11
    then v12 leaves *both* algorithms' frames in
    `pitch.frames_by_algorithm`. This helper triangulates each
    non-current algorithm bucket and writes it directly into
    `result.triangulated_by_algorithm[<alg_id>]` so the events list,
    viewer, and any future Phase-8 N-track UI can read v11 trajectories
    without re-running detection.

    Skipped buckets:
    - `ios_capture_time` (the live data source) — already surfaced via
      the live aggregator; not a server-side triangulation target.
    - The *current* server_post alg — already handled by the path-loop.
      Considered "current" if EITHER cam's snapshot names it (union),
      so a partial-failure mismatch (A=v11, B=v12) skips both v11 and
      v12 from this helper, leaving the path-loop's mixed pairing as
      the sole — and visibly logged — source of those frames.
    """
    if sync_error is not None or a is None or b is None:
        return
    from algorithms import IOS_CAPTURE_TIME

    current_alg_a = algorithm_id_for_path(a, DetectionPath.server_post)
    current_alg_b = algorithm_id_for_path(b, DetectionPath.server_post)
    if current_alg_a != current_alg_b:
        logger.warning(
            "session %s server_post algorithm mismatch A=%s B=%s — path-loop "
            "will pair frames across algorithms; rerun /run_server_post on "
            "both cams to recover",
            result.session_id, current_alg_a, current_alg_b,
        )
    current_algs = {current_alg_a, current_alg_b}
    candidate_algs: set[str] = (
        set(a.frames_by_algorithm) | set(b.frames_by_algorithm)
    )
    for alg_id in sorted(candidate_algs):
        if alg_id == IOS_CAPTURE_TIME or alg_id in current_algs:
            continue
        frames_a = a.frames_by_algorithm.get(alg_id) or []
        frames_b = b.frames_by_algorithm.get(alg_id) or []
        if not frames_a or not frames_b:
            continue
        try:
            pts = triangulate_pair(
                state,
                pitch_with_algorithm_frames(a, alg_id),
                pitch_with_algorithm_frames(b, alg_id),
                source="server",
                tuning=tuning,
            )
        except Exception as exc:
            result.abort_reasons[f"alg:{alg_id}"] = (
                f"{type(exc).__name__}: {exc}"
            )
            continue
        result.triangulated_by_algorithm[alg_id] = pts
        result.algorithms_completed.add(alg_id)
        result.frame_counts_by_algorithm[alg_id] = {
            "A": len(frames_a),
            "B": len(frames_b),
        }


def live_frames_for_camera_locked(
    state: "State", session_id: str, camera_id: str
) -> list[FramePayload]:
    live = state._live_pairings.get(session_id)
    if live is None:
        return []
    return live.frames_for_camera(camera_id)


def session_sync_id_locked(state: "State", session_id: str) -> str | None:
    session = state._lookup_session_locked(session_id)
    if session is not None:
        return session.sync_id
    return None


def validate_pair_sync(
    state: "State", a: PitchPayload, b: PitchPayload
) -> str | None:
    """Return a stable error string when the paired payloads do not belong
    to the same legacy chirp sync run."""
    if a.sync_anchor_timestamp_s is None or b.sync_anchor_timestamp_s is None:
        return "no time sync"
    with state._lock:
        expected_sync_id = session_sync_id_locked(state, a.session_id)
    if a.sync_id is None or b.sync_id is None:
        return "sync id missing"
    if a.sync_id != b.sync_id:
        return "sync id mismatch"
    if expected_sync_id is not None and a.sync_id != expected_sync_id:
        return "sync id mismatch for armed session"
    return None


def empty_result_for_session(
    state: "State",
    session_id: str,
    *,
    camera_a_received: bool,
    camera_b_received: bool,
) -> SessionResult:
    """Lock-free pure constructor — do not wrap the call in `state._lock`;
    the only state read is `state.now()`, which is an injectable
    callable."""
    return SessionResult(
        session_id=session_id,
        camera_a_received=camera_a_received,
        camera_b_received=camera_b_received,
        solved_at=state.now(),
    )


def rebuild_result_for_session(state: "State", session_id: str) -> SessionResult:
    with state._lock:
        a = state.pitches.get(("A", session_id))
        b = state.pitches.get(("B", session_id))
        live = state._live_pairings.get(session_id)
        session_obj = state._lookup_session_locked(session_id)
        pairing_tuning = state._pairing_tuning

    result = empty_result_for_session(
        state,
        session_id,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
    )
    result.cost_threshold = pairing_tuning.cost_threshold
    result.gap_threshold_m = pairing_tuning.gap_threshold_m
    # Aggregate the two cams' last-run timestamps — the more recent one
    # wins so a partial rerun (only one cam's MOV reprocessed) still
    # advances the session's "last server_post" age.
    server_post_ts = [
        p.server_post_ran_at for p in (a, b)
        if p is not None and p.server_post_ran_at is not None
    ]
    if server_post_ts:
        result.server_post_ran_at = max(server_post_ts)

    candidate_paths: set[DetectionPath] = set()
    if session_obj is not None:
        candidate_paths |= set(session_obj.paths)
    for pitch in (a, b):
        if pitch is not None:
            candidate_paths |= paths_for_pitch(state, pitch)
            # Auto-include server_post when the bucket is populated so
            # reprocessing can flow through even without an explicit paths
            # snapshot on the pitch JSON.
            if pitch.frames_server_post:
                candidate_paths.add(DetectionPath.server_post)
            # Same for live: persisted `frames_live` (from an old WS
            # streaming run, or `persist_live_frames`) is enough to drive
            # the live triangulation path on rebuild even after restart.
            if pitch.frames_live:
                candidate_paths.add(DetectionPath.live)
    live_frame_counts = live.frame_counts_snapshot() if live is not None else {}
    if any(c for c in live_frame_counts.values()):
        candidate_paths.add(DetectionPath.live)
    if not candidate_paths:
        candidate_paths = set(_DEFAULT_PATHS)
    legacy_points_path = _legacy_points_path(candidate_paths)

    if live is not None:
        with live._lock:
            triangulated_copy = list(live.triangulated)
            abort_reasons_copy = dict(live.abort_reasons)
        result.frame_counts_by_path[DetectionPath.live.value] = {
            cam: int(count) for cam, count in live_frame_counts.items() if count
        }
        if triangulated_copy:
            result.triangulated_by_path[DetectionPath.live.value] = triangulated_copy
            result.paths_completed.add(DetectionPath.live.value)
        if abort_reasons_copy:
            result.abort_reasons.update(
                {f"live:{cam}": why for cam, why in abort_reasons_copy.items()}
            )

    sync_error = None
    if a is not None and b is not None:
        sync_error = validate_pair_sync(state, a, b)
        if sync_error is not None:
            result.error = sync_error

    mono_session = (a is None) != (b is None)
    for path in sorted(candidate_paths, key=lambda p: p.value):
        # When the streaming live aggregator already populated this path
        # above, skip — it's authoritative. Otherwise (rebuild for a session
        # restored from disk after server restart, or an offline replay),
        # fall through to the same triangulate_pair flow used by other paths
        # so persisted `frames_live` can still drive the live trajectory.
        if path == DetectionPath.live and live is not None:
            continue
        frames_a = get_path_frames(a, path) if a is not None else []
        frames_b = get_path_frames(b, path) if b is not None else []
        frame_counts: dict[str, int] = {}
        if a is not None and frames_a:
            frame_counts["A"] = len(frames_a)
        if b is not None and frames_b:
            frame_counts["B"] = len(frames_b)
        if frame_counts:
            result.frame_counts_by_path[path.value] = frame_counts

        if sync_error is None and a is not None and b is not None:
            if not frames_a or not frames_b:
                continue
            try:
                pts = triangulate_pair(
                    state,
                    pitch_with_path_frames(a, path),
                    pitch_with_path_frames(b, path),
                    source="server",
                )
            except Exception as exc:
                result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                continue
            result.triangulated_by_path[path.value] = pts
            result.paths_completed.add(path.value)
        elif mono_session and frame_counts:
            # Single-camera sessions cannot triangulate, but once the path
            # has finalized frames on the sole uploaded camera it should be
            # surfaced as completed instead of lingering in "stopped".
            result.paths_completed.add(path.value)

    # Phase 7 multi-algorithm: triangulate every algorithm bucket
    # present in `pitch.frames_by_algorithm` so a v11 → v12 rerun
    # leaves both algorithms' trajectories surfaced on the result.
    # The path-loop above already covers the *current* server_post
    # alg via the after-validator mirror; this loop handles the
    # non-current ones (the "history" the dict accumulates). live
    # path is excluded — `ios_capture_time` is already surfaced via
    # the live aggregator at the top of this function.
    _triangulate_non_current_algorithms(
        state, a, b, sync_error, result, tuning=pairing_tuning,
    )

    authority: list[TriangulatedPoint] = []
    for path in (
        DetectionPath.server_post.value,
        DetectionPath.live.value,
    ):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority

    if not result.triangulated and result.error is None and (a is not None or b is not None):
        if result.abort_reasons:
            result.aborted = True
        elif a is not None and b is not None:
            result.error = "no detection completed"
    _stamp_frozen_config_on_result(result, a, b)
    stamp_segments_on_result(result, legacy_points_path=legacy_points_path)
    return result


def _passes_stamped_filter(
    p: TriangulatedPoint,
    *,
    cost_threshold: float,
    gap_threshold_m: float,
) -> bool:
    """Stamped-tuning filter applied to TriangulatedPoint before segmenter
    consumption (and mirrored client-side by the viewer's `Cost ≤` /
    `Gap ≤` slider mask). A point passes when:
      - `residual_m ≤ gap_threshold_m`, AND
      - `max(cost_a, cost_b) ≤ cost_threshold` (None costs treated as
        "no info" → pass; matches the JS `_passCostFilter` legacy
        fall-through semantics).
    """
    if p.residual_m > gap_threshold_m:
        return False
    cost_max = -1.0
    if p.cost_a is not None:
        cost_max = max(cost_max, p.cost_a)
    if p.cost_b is not None:
        cost_max = max(cost_max, p.cost_b)
    if cost_max < 0:
        return True  # both costs None → no cost info → pass
    return cost_max <= cost_threshold


def _apply_stamped_filter(
    pts: list[TriangulatedPoint],
    *,
    cost_threshold: float,
    gap_threshold_m: float,
) -> list[TriangulatedPoint]:
    return [p for p in pts if _passes_stamped_filter(
        p, cost_threshold=cost_threshold, gap_threshold_m=gap_threshold_m,
    )]


def _legacy_points_path(candidate_paths: set[DetectionPath]) -> DetectionPath | None:
    """Path used by legacy `result.points` / `result.segments`.

    Explicit branch order is intentional: if server_post was requested but
    produced no points, the legacy surface must be empty instead of silently
    substituting live. That keeps live-vs-server_post comparisons honest.
    """
    if DetectionPath.server_post in candidate_paths:
        return DetectionPath.server_post
    if DetectionPath.live in candidate_paths:
        return DetectionPath.live
    return None


def stamp_segments_on_result(
    result: SessionResult,
    *,
    legacy_points_path: DetectionPath | None = None,
) -> None:
    """Run `find_segments` on the stamped-filter SUBSET of every available
    path and write both `result.segments_by_path` and the legacy
    single-surface `result.segments`. Idempotent — overwrites whatever
    was there.

    Architecture: `result.triangulated` / `result.triangulated_by_path`
    carry the FULL emitted set (every
    candidate pair under pairing's absolute emit ceiling). The segmenter
    runs against the operator's stamped subset
    (`cost_threshold` / `gap_threshold_m` from `SessionResult`); the
    viewer slider mirrors the same predicate client-side. This decouples
    "what the operator sees" from "what gets fit".

    `result.points` / `result.segments` follow `legacy_points_path` when
    the caller supplies one; this prevents a missing server_post surface from
    silently substituting live points after `rebuild_result_for_session`
    already selected the no-fallback legacy path.

    Sorts every persisted path list by `t_rel_s` BEFORE running the
    segmenter so `Segment.original_indices` is a stable index into a
    time-sorted list.

    `result.cost_threshold` / `gap_threshold_m` may be None only on
    legacy/manual callers that bypass `rebuild_result_for_session` /
    `recompute_result_for_session`. In that case fall back to
    `PairingTuning.default()` so those test-only / migration surfaces
    still render deterministically.

    Empty `triangulated_by_path` ⇒ empty segments (no log noise;
    "nothing to fit" is not an error)."""
    if result.cost_threshold is None or result.gap_threshold_m is None:
        from pairing_tuning import PairingTuning
        defaults = PairingTuning.default()
        cost = result.cost_threshold if result.cost_threshold is not None else defaults.cost_threshold
        gap = result.gap_threshold_m if result.gap_threshold_m is not None else defaults.gap_threshold_m
    else:
        cost = result.cost_threshold
        gap = result.gap_threshold_m
    if not result.triangulated_by_path and result.triangulated:
        # Legacy/unit-test ingress: older callers still construct a
        # SessionResult with only `triangulated` populated. At this
        # internal boundary, project that single surface into the
        # canonical per-path map so the rest of the function can stay
        # path-native. Prefer the historical default path = server_post.
        result.triangulated_by_path = {
            DetectionPath.server_post.value: list(result.triangulated)
        }

    path_priority = (
        DetectionPath.server_post.value,
        DetectionPath.live.value,
    )
    result.segments_by_path = {}
    for path, pts in list(result.triangulated_by_path.items()):
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda p: p.t_rel_s)
        result.triangulated_by_path[path] = pts_sorted
        fit_input: list[TriangulatedPoint] = []
        fit_to_full: list[int] = []
        for full_idx, p in enumerate(pts_sorted):
            if _passes_stamped_filter(p, cost_threshold=cost, gap_threshold_m=gap):
                fit_input.append(p)
                fit_to_full.append(full_idx)
        segs, _pts_sorted = find_segments(fit_input)
        records: list[SegmentRecord] = []
        for s in segs:
            rec = _segment_record_from_segment(s)
            rec.original_indices = [fit_to_full[i] for i in rec.original_indices]
            records.append(rec)
        result.segments_by_path[path] = records

    authority_path: str | None = None
    for path in path_priority:
        pts = result.triangulated_by_path.get(path) or []
        if pts:
            authority_path = path
            break
    if authority_path is None:
        result.triangulated = []
        result.points = []
        result.segments = []
        return
    authority_pts = result.triangulated_by_path[authority_path]
    result.triangulated = authority_pts
    legacy_path = (
        legacy_points_path.value
        if legacy_points_path is not None
        else authority_path
    )
    result.points = list(result.triangulated_by_path.get(legacy_path, []))
    result.segments = list(result.segments_by_path.get(legacy_path, []))


def _segment_record_from_segment(seg: Segment) -> SegmentRecord:
    return SegmentRecord(
        indices=list(seg.indices),
        original_indices=list(seg.original_indices),
        p0=seg.p0.tolist(),
        v0=seg.v0.tolist(),
        t_anchor=float(seg.t_anchor),
        t_start=float(seg.t_start),
        t_end=float(seg.t_end),
        rmse_m=float(seg.rmse_m),
        speed_kph=float(seg.speed_kph),
    )


def recompute_result_for_session(
    state: "State",
    session_id: str,
    *,
    cost_threshold: float,
    gap_threshold_m: float,
) -> SessionResult:
    """Re-run pairing fan-out + segmenter on this session's already-
    detected frames using per-session `cost_threshold` + `gap_threshold_m`
    overrides.

    Differences from `rebuild_result_for_session`:
      - Always re-triangulates the live path (does NOT reuse
        `LivePairingSession.triangulated`, which was built incrementally
        under the old/global tuning at ingest time).
      - Both live and server_post paths use a `PairingTuning` built from
        BOTH caller-supplied values — no fallback to global tuning here;
        the route is responsible for resolving defaults before calling.
      - Stamps both chosen values into `SessionResult.cost_threshold` /
        `SessionResult.gap_threshold_m` for viewer slider re-init.

    Caller is the `POST /sessions/{sid}/recompute` route. No MOV decode,
    no HSV — candidates are read from the persisted `frames_live` /
    `frames_server_post` directly. Sub-second on a typical session."""
    from pairing_tuning import PairingTuning

    tuning = PairingTuning(
        cost_threshold=float(cost_threshold),
        gap_threshold_m=float(gap_threshold_m),
    )

    with state._lock:
        a = state.pitches.get(("A", session_id))
        b = state.pitches.get(("B", session_id))

    result = empty_result_for_session(
        state,
        session_id,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
    )
    result.cost_threshold = float(cost_threshold)
    result.gap_threshold_m = float(gap_threshold_m)

    server_post_ts = [
        p.server_post_ran_at for p in (a, b)
        if p is not None and p.server_post_ran_at is not None
    ]
    if server_post_ts:
        result.server_post_ran_at = max(server_post_ts)

    candidate_paths: set[DetectionPath] = set()
    for pitch in (a, b):
        if pitch is None:
            continue
        if pitch.frames_server_post:
            candidate_paths.add(DetectionPath.server_post)
        if pitch.frames_live:
            candidate_paths.add(DetectionPath.live)
    legacy_points_path = _legacy_points_path(candidate_paths)

    sync_error = None
    if a is not None and b is not None:
        sync_error = validate_pair_sync(state, a, b)
        if sync_error is not None:
            result.error = sync_error

    if a is not None and b is not None and sync_error is None:
        for path in sorted(candidate_paths, key=lambda p: p.value):
            frames_a = get_path_frames(a, path)
            frames_b = get_path_frames(b, path)
            if not frames_a or not frames_b:
                continue
            result.frame_counts_by_path[path.value] = {
                "A": len(frames_a),
                "B": len(frames_b),
            }
            try:
                pts = triangulate_pair(
                    state,
                    pitch_with_path_frames(a, path),
                    pitch_with_path_frames(b, path),
                    source="server",
                    tuning=tuning,
                )
            except Exception as exc:
                result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                continue
            result.triangulated_by_path[path.value] = pts
            result.paths_completed.add(path.value)

    # Phase 7-fix-2: also triangulate non-current algorithm buckets
    # so Recompute (per-session tuning) preserves multi-alg history
    # the same way rebuild does. Helper takes the same `tuning` so
    # the sliders apply to every alg, not just the current server_post.
    _triangulate_non_current_algorithms(
        state, a, b, sync_error, result, tuning=tuning,
    )

    authority: list[TriangulatedPoint] = []
    for path in (DetectionPath.server_post.value, DetectionPath.live.value):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority

    if not result.triangulated and result.error is None and (a is not None or b is not None):
        if result.abort_reasons:
            result.aborted = True
        elif a is not None and b is not None:
            result.error = "no detection completed"
    _stamp_frozen_config_on_result(result, a, b)
    stamp_segments_on_result(result, legacy_points_path=legacy_points_path)
    return result


# Re-export `normalize_paths` so `State._normalize_paths` (and the handful
# of external callers that still reach for `state._normalize_paths`) can
# route through session_results without importing detection_paths
# separately.
__all__ = [
    "empty_result_for_session",
    "live_frames_for_camera_locked",
    "normalize_paths",
    "paths_for_pitch",
    "rebuild_result_for_session",
    "recompute_result_for_session",
    "session_sync_id_locked",
    "stamp_segments_on_result",
    "triangulate_pair",
    "validate_pair_sync",
]
