"""Dashboard /events row builder.

One row per `session_id`, collapsing A/B uploads into a single entry with
per-pipeline frame counts, path status pills, triangulation summary, and
processing state. The data is derived (not stored), so this module is
pure read — it never mutates `State`.

Lock discipline: `_snapshot_sessions_locked` gathers everything that
depends on in-memory state in one lock acquisition, then all downstream
work (disk `stat()`, trash lookup, processing summary) runs outside the
lock so the dashboard's 5 s tick can't stall /pitch handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schemas import CaptureTelemetryPayload, DetectionPath, SessionResult

if TYPE_CHECKING:
    from state import State


# Each pipeline (live / server_post) maps to one of the PitchPayload frame
# buckets. Keep the mapping in one table so the events loop can't drift
# out of sync with schemas.DetectionPath.
_PATH_TO_FRAMES_ATTR: tuple[tuple[str, str], ...] = (
    (DetectionPath.live.value, "frames_live"),
    (DetectionPath.server_post.value, "frames_server_post"),
)


def build_events(state: "State", *, bucket: str = "active") -> list[dict[str, Any]]:
    """Summary row per session for the events panel — one entry per
    session_id, collapsing A/B uploads into a single event.

    `received_at` is derived from the pitch file's mtime so we don't have to
    extend the Pydantic payload with server-side timestamps. Disk `stat()`
    happens AFTER releasing the state lock so the dashboard's 5 s tick can't
    block heartbeats / /pitch handlers that need to mutate the state map.
    """
    snapshots = _snapshot_sessions_locked(state)

    events: list[dict[str, Any]] = []
    for sid, cams_present, n_ball_frames_by_path, cam_capture_telemetry, result in snapshots:
        trashed = state._processing.is_trashed(sid)
        if bucket == "active" and trashed:
            continue
        if bucket == "trash" and not trashed:
            continue

        latest_mtime = _latest_pitch_mtime(state, cams_present, sid)
        authority_points = result.triangulated if result is not None else []
        n_triangulated = len(authority_points) if result is not None else 0
        error = result.error if result is not None else None

        status = _status_label(cams_present, n_triangulated, error)
        peak_z, mean_res, duration = _point_cloud_summary(authority_points)
        mode = _legacy_mode_label(state, sid)
        path_status = _path_status_pills(result, n_ball_frames_by_path)
        ballistic_speed_mph: float | None = None
        ballistic_g_fit: float | None = None
        if result is not None:
            summary = (
                result.ballistic_server_post
                or result.ballistic_live
            )
            if summary is not None:
                ballistic_speed_mph = summary.speed_mph
                ballistic_g_fit = summary.g_fit
        processing_state, processing_resumable = state._processing.session_summary(sid)

        events.append(
            {
                "session_id": sid,
                "cameras": cams_present,
                "status": status,
                "mode": mode,
                "received_at": latest_mtime,
                # Per-pipeline counts (live / server_post). Legacy flat name
                # kept for older consumers.
                "n_ball_frames_by_path": n_ball_frames_by_path,
                "n_ball_frames": n_ball_frames_by_path[DetectionPath.server_post.value],
                "n_triangulated": n_triangulated,
                "peak_z_m": peak_z,
                "mean_residual_m": mean_res,
                "duration_s": duration,
                "capture_telemetry": {
                    cam: (tele.model_dump(mode="json") if tele is not None else None)
                    for cam, tele in cam_capture_telemetry.items()
                },
                "error": error,
                "path_status": path_status,
                "trashed": trashed,
                "processing_state": processing_state,
                "processing_resumable": processing_resumable,
                # Cams whose live frames arrived without a calibration on
                # file — dashboard surfaces this so the operator can spot a
                # silent live path instead of tailing the server log.
                # TODO: pill UI in render_dashboard_events.py.
                "live_missing_calibration": state.live_missing_calibration_for(sid),
                # Latest server_post background-task error per cam. Cleared
                # on the next successful run. Empty dict = no pending
                # failure. Surfaced as an inline chip on the events row.
                "server_post_errors": state._processing.errors_for(sid),
                # GT pipeline status per cam (existence-only — cheap I/O):
                #   has_gt[cam]            True iff data/gt/sam3/session_<sid>_<cam>.json exists
                #   has_validation[cam]    True iff data/gt/validation/session_<sid>_<cam>.json exists
                # The render layer uses these to gate the "Run GT" /
                # "Validate" buttons + the G|✓·✓ path chip. Frame counts
                # are not loaded per tick — the report page reads the
                # JSON contents on demand.
                "has_gt": _gt_existence(state, sid, "sam3", cams_present),
                "has_validation": _gt_existence(state, sid, "validation", cams_present),
                "ballistic_speed_mph": ballistic_speed_mph,
                "ballistic_g_fit": ballistic_g_fit,
            }
        )

    # Latest events first — session ids carry 4 bytes of random hex so we
    # sort by `received_at` (fallback to id) to surface the most recently
    # uploaded session at the top.
    events.sort(
        key=lambda e: (e["received_at"] or 0, e["session_id"]),
        reverse=True,
    )
    return events


def _snapshot_sessions_locked(
    state: "State",
) -> list[
    tuple[
        str,
        list[str],
        dict[str, dict[str, int]],
        dict[str, CaptureTelemetryPayload | None],
        SessionResult | None,
    ]
]:
    """Grab everything we need from in-memory state under one lock acquisition.

    Every subsequent step (file stats, summary derivation) runs outside the
    lock so the 5 s dashboard tick can't stall /pitch handlers that mutate
    the pitches/results maps.
    """
    with state._lock:
        sessions = sorted({sid for _, sid in state.pitches.keys()})
        snapshots: list[
            tuple[
                str,
                list[str],
                dict[str, dict[str, int]],
                dict[str, CaptureTelemetryPayload | None],
                SessionResult | None,
            ]
        ] = []
        for sid in sessions:
            cams_present = sorted(cam for (cam, s) in state.pitches.keys() if s == sid)
            n_ball_frames_by_path: dict[str, dict[str, int]] = {
                path: {} for path, _ in _PATH_TO_FRAMES_ATTR
            }
            for cam in cams_present:
                pitch = state.pitches[(cam, sid)]
                for path, attr in _PATH_TO_FRAMES_ATTR:
                    frames = getattr(pitch, attr, ()) or ()
                    n_ball_frames_by_path[path][cam] = sum(
                        1 for f in frames if f.ball_detected
                    )
            cam_capture_telemetry = {
                cam: state.pitches[(cam, sid)].capture_telemetry
                for cam in cams_present
            }
            snapshots.append(
                (
                    sid,
                    cams_present,
                    n_ball_frames_by_path,
                    cam_capture_telemetry,
                    state.results.get(sid),
                )
            )
    return snapshots


def _gt_existence(
    state: "State", sid: str, kind: str, cams_present: list[str]
) -> dict[str, bool]:
    """Cheap existence-only check for GT artefacts. `kind` ∈ {"sam3",
    "validation"} resolves to the right subdirectory under data/gt/.
    Returns a {cam: True/False} map keyed by every cam that appears in
    this session — so the renderer can show G|✓·— style chips when only
    one cam has been labelled."""
    base = state.data_dir / "gt" / kind
    out: dict[str, bool] = {}
    for cam in cams_present:
        out[cam] = (base / f"session_{sid}_{cam}.json").is_file()
    return out


def _latest_pitch_mtime(state: "State", cams_present: list[str], sid: str) -> float | None:
    latest: float | None = None
    for cam in cams_present:
        try:
            mtime = state._pitch_path(cam, sid).stat().st_mtime
        except FileNotFoundError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def _status_label(cams_present: list[str], n_triangulated: int, error: str | None) -> str:
    if error:
        return "error"
    if len(cams_present) >= 2 and n_triangulated > 0:
        return "paired"
    if len(cams_present) >= 2:
        return "paired_no_points"
    return "partial"


def _point_cloud_summary(
    authority_points: list,
) -> tuple[float | None, float | None, float | None]:
    if not authority_points:
        return None, None, None
    zs = [p.z_m for p in authority_points]
    peak_z = float(max(zs))
    mean_res = float(
        sum(p.residual_m for p in authority_points) / len(authority_points)
    )
    ts = [p.t_rel_s for p in authority_points]
    duration = float(ts[-1] - ts[0])
    return peak_z, mean_res, duration


def _legacy_mode_label(state: "State", sid: str) -> str:
    has_any_video = any(state._video_dir.glob(f"session_{sid}_*"))
    if has_any_video:
        return "camera_only"
    return "live_only"


def _path_status_pills(
    result: SessionResult | None,
    n_ball_frames_by_path: dict[str, dict[str, int]],
) -> dict[str, str]:
    """Per-pipeline health pill. Resolves in this order (strongest wins):

    1. "done" if result.paths_completed includes it (triangulated on a
       paired session, or explicit mono-session finalization),
    2. "done" if any camera produced ≥1 detected frame on that pipeline —
       live-only single-camera runs ship no triangulation but still count
       as "that pipeline executed", which is what the user wants to see in
       the events list,
    3. "error" if abort_reasons has an entry for this pipeline,
    4. "-" (never ran / empty).
    """

    def pill(path_value: str, abort_prefix: str | None = None) -> str:
        if result is not None and path_value in result.paths_completed:
            return "done"
        if any(c > 0 for c in n_ball_frames_by_path.get(path_value, {}).values()):
            return "done"
        if result is not None:
            if path_value in result.abort_reasons:
                return "error"
            if abort_prefix is not None and any(
                key.startswith(abort_prefix) for key in result.abort_reasons
            ):
                return "error"
        return "-"

    return {
        DetectionPath.live.value: pill(DetectionPath.live.value, "live:"),
        DetectionPath.server_post.value: pill(DetectionPath.server_post.value),
    }
