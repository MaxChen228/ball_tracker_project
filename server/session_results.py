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
    get_path_frames,
    normalize_paths,
    paths_for_pitch,
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
    "hsv_range_used",
    "shape_gate_used",
)


def aggregate_pitch_used_configs(
    a: PitchPayload | None,
    b: PitchPayload | None,
    sid: str,
) -> dict[str, object | None]:
    """Aggregate the per-pitch frozen `*_used` fields into a single mapping.
    Divergence (A and B carry different values because operator edited the
    config mid-cycle) is logged as a warning but does not raise — diagnostic,
    not crashable. Policy: A wins, fall back to B. Shared by `rebuild_result`
    here and by `reprocess_sessions._build_session_result` so both paths
    enforce the same A-wins-B-fallback policy and emit identical warnings.
    """
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
    """Mirror the per-pitch frozen detection config onto the SessionResult.
    Thin wrapper around `aggregate_pitch_used_configs` that does the
    setattr loop; the aggregation policy lives in the helper so reprocess
    and rebuild share one source of truth."""
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

    result = empty_result_for_session(
        state,
        session_id,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
    )
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
        frames_a = get_path_frames(state, a, path) if a is not None else []
        frames_b = get_path_frames(state, b, path) if b is not None else []
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
                    pitch_with_path_frames(state, a, path),
                    pitch_with_path_frames(state, b, path),
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
    # Legacy `points` semantics: prefer server_post when present, else
    # fall back to live. Older consumers (viewer, /events) expect `points`
    # to hold the session's single result.
    if DetectionPath.server_post in candidate_paths:
        legacy_points = result.triangulated_by_path.get(DetectionPath.server_post.value, [])
    else:
        legacy_points = (
            result.triangulated_by_path.get(DetectionPath.live.value)
            or []
        )
    result.points = list(legacy_points)

    if not result.triangulated and result.error is None and (a is not None or b is not None):
        if result.abort_reasons:
            result.aborted = True
        elif a is not None and b is not None:
            result.error = "no detection completed"
    _stamp_frozen_config_on_result(result, a, b)
    stamp_segments_on_result(result)
    return result


def stamp_segments_on_result(result: SessionResult) -> None:
    """Run `find_segments` on the chosen authoritative path's points and
    write `result.segments`. Idempotent — overwrites whatever was there.

    Segments are pure visualisation data derived from `result.triangulated`
    (the authoritative path's points); they do not need to be re-derivable
    from disk on next rebuild because the rebuild always recomputes them.
    Empty `triangulated` ⇒ empty segments (no log noise; "I have nothing
    to fit" is not an error)."""
    if not result.triangulated:
        result.segments = []
        return
    segs, _pts_sorted = find_segments(result.triangulated)
    result.segments = [_segment_record_from_segment(s) for s in segs]


def _segment_record_from_segment(seg: Segment) -> SegmentRecord:
    return SegmentRecord(
        indices=list(seg.indices),
        original_indices=list(seg.original_indices),
        p0=[float(x) for x in seg.p0.tolist()],
        v0=[float(x) for x in seg.v0.tolist()],
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

    sync_error = None
    if a is not None and b is not None:
        sync_error = validate_pair_sync(state, a, b)
        if sync_error is not None:
            result.error = sync_error

    if a is not None and b is not None and sync_error is None:
        for path in sorted(candidate_paths, key=lambda p: p.value):
            frames_a = get_path_frames(state, a, path)
            frames_b = get_path_frames(state, b, path)
            if not frames_a or not frames_b:
                continue
            result.frame_counts_by_path[path.value] = {
                "A": len(frames_a),
                "B": len(frames_b),
            }
            try:
                pts = triangulate_pair(
                    state,
                    pitch_with_path_frames(state, a, path),
                    pitch_with_path_frames(state, b, path),
                    source="server",
                    tuning=tuning,
                )
            except Exception as exc:
                result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                continue
            result.triangulated_by_path[path.value] = pts
            result.paths_completed.add(path.value)

    authority: list[TriangulatedPoint] = []
    for path in (DetectionPath.server_post.value, DetectionPath.live.value):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority
    result.points = list(
        result.triangulated_by_path.get(DetectionPath.server_post.value, [])
        or result.triangulated_by_path.get(DetectionPath.live.value, [])
    )

    if not result.triangulated and result.error is None and (a is not None or b is not None):
        if result.abort_reasons:
            result.aborted = True
        elif a is not None and b is not None:
            result.error = "no detection completed"
    _stamp_frozen_config_on_result(result, a, b)
    stamp_segments_on_result(result)
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
