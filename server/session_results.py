"""Session result builder and triangulation coordinator.

`rebuild_result_for_session` is the authoritative constructor for
`SessionResult` — events / viewer / pitch_analysis all read it. It combines
per-pipeline frame counts, triangulation output (live, ios_post,
server_post), sync validation, and legacy `points` / `points_on_device`
semantics into one immutable-ish snapshot.

Lock discipline: `State._lock` is a `threading.Lock` (non-reentrant). The
two-phase pattern here — snapshot pitches / live / session under the lock,
then call path selection / triangulation / sync validation **outside** it
— is load-bearing; each of those downstream helpers re-acquires the lock
internally. Do not fold them into the outer `with state._lock:` block or
the process will deadlock.
"""

from __future__ import annotations

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
    SessionResult,
    TriangulatedPoint,
    _DEFAULT_PATHS,
)

if TYPE_CHECKING:
    from state import State


def triangulate_pair(
    state: "State",
    a: PitchPayload,
    b: PitchPayload,
    *,
    source: str = "server",
) -> list[TriangulatedPoint]:
    """Scale each pitch's intrinsics + homography to its MOV's actual pixel
    grid (using the cached calibration snapshot as the reference resolution)
    and then triangulate. When no snapshot is cached for a camera the scale
    factor falls back to 1.0 and the pitch is passed through unchanged — the
    legacy behaviour for pre-resolution-picker builds that always recorded
    at the calibration resolution.

    `source` picks the detection stream (`"server"` default reads
    `pitch.frames`; `"on_device"` reads `pitch.frames_on_device`). Dual mode
    calls this twice per session to keep the two point clouds separate."""
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
    return triangulate_cycle(a_scaled, b_scaled, source=source)


def live_frames_for_camera_locked(
    state: "State", session_id: str, camera_id: str
) -> list[FramePayload]:
    live = state._live_pairings.get(session_id)
    if live is None:
        return []
    return live.frames_for_camera(camera_id)


def session_sync_id_locked(state: "State", session_id: str) -> str | None:
    for session in (state._current_session, state._last_ended_session):
        if session is not None and session.id == session_id:
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
    if a.sync_id is None and b.sync_id is None:
        # Backward-compat: historical sessions recorded before sync_id
        # existed still carry valid anchor-relative timing, so keep loading
        # them unless this armed session explicitly expected a shared sync
        # id snapshot.
        return "sync id missing" if expected_sync_id is not None else None
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
    the only state read is `state._time_fn`, which is an injectable
    callable."""
    return SessionResult(
        session_id=session_id,
        camera_a_received=camera_a_received,
        camera_b_received=camera_b_received,
        solved_at=state._time_fn(),
    )


def rebuild_result_for_session(state: "State", session_id: str) -> SessionResult:
    with state._lock:
        a = state.pitches.get(("A", session_id))
        b = state.pitches.get(("B", session_id))
        live = state._live_pairings.get(session_id)
        current = (
            state._current_session
            if state._current_session and state._current_session.id == session_id
            else None
        )
        ended = (
            state._last_ended_session
            if state._last_ended_session and state._last_ended_session.id == session_id
            else None
        )
        session_obj = current or ended

    result = empty_result_for_session(
        state,
        session_id,
        camera_a_received=a is not None,
        camera_b_received=b is not None,
    )

    candidate_paths: set[DetectionPath] = set()
    if session_obj is not None:
        candidate_paths |= set(session_obj.paths)
    for pitch in (a, b):
        if pitch is not None:
            candidate_paths |= paths_for_pitch(state, pitch)
            # Auto-include paths when frames are actually present, even if
            # neither the session nor the pitch explicitly listed them —
            # /pitch_analysis can attach iOS frames post-hoc into a session
            # that was armed server_post-only, and we still owe the caller
            # a triangulation over those frames.
            if pitch.frames_ios_post or pitch.frames_on_device:
                candidate_paths.add(DetectionPath.ios_post)
            # Only auto-include server_post when the bucket is populated.
            # Legacy `pitch.frames` alone is ambiguous — in on_device-only
            # sessions it should map to ios_post, not resurrect server_post.
            if pitch.frames_server_post:
                candidate_paths.add(DetectionPath.server_post)
    if live is not None and live.frame_counts:
        candidate_paths.add(DetectionPath.live)
    if not candidate_paths:
        candidate_paths = set(_DEFAULT_PATHS)

    if live is not None:
        result.frame_counts_by_path[DetectionPath.live.value] = {
            cam: int(count) for cam, count in live.frame_counts.items() if count
        }
        if live.triangulated:
            result.triangulated_by_path[DetectionPath.live.value] = list(live.triangulated)
            result.paths_completed.add(DetectionPath.live.value)
        if live.abort_reasons:
            result.abort_reasons.update(
                {f"live:{cam}": why for cam, why in live.abort_reasons.items()}
            )

    sync_error = None
    if a is not None and b is not None:
        sync_error = validate_pair_sync(state, a, b)
        if sync_error is not None:
            result.error = sync_error
            result.error_on_device = sync_error

    mono_session = (a is None) != (b is None)
    for path in sorted(candidate_paths, key=lambda p: p.value):
        if path == DetectionPath.live:
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
        DetectionPath.ios_post.value,
        DetectionPath.server_post.value,
        DetectionPath.live.value,
    ):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority
    # Legacy `points` semantics: in dual-like sessions (server_post is in
    # the candidate set) keep it strictly on the server stream so iOS
    # post-pass doesn't leak across the points/points_on_device boundary.
    # In mono-like sessions (on_device-only, live-only, etc) collapse to
    # whichever path actually produced data — older consumers (viewer,
    # /events) expect `points` to hold the session's single result when no
    # dual split is in play.
    if DetectionPath.server_post in candidate_paths:
        legacy_points = result.triangulated_by_path.get(DetectionPath.server_post.value, [])
    else:
        legacy_points = (
            result.triangulated_by_path.get(DetectionPath.ios_post.value)
            or result.triangulated_by_path.get(DetectionPath.live.value)
            or []
        )
    result.points = list(legacy_points)
    if result.triangulated_by_path.get(DetectionPath.ios_post.value):
        result.points_on_device = list(result.triangulated_by_path[DetectionPath.ios_post.value])
    elif result.triangulated_by_path.get(DetectionPath.live.value):
        result.points_on_device = list(result.triangulated_by_path[DetectionPath.live.value])

    if not result.triangulated and result.error is None and (a is not None or b is not None):
        if result.abort_reasons:
            result.aborted = True
        elif a is not None and b is not None:
            result.error = "no detection completed"
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
    "session_sync_id_locked",
    "triangulate_pair",
    "validate_pair_sync",
]
