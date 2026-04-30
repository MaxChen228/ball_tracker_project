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

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from schemas import CaptureTelemetryPayload, DetectionPath, SessionResult

# Asia/Taipei. Hard-coded because the only operator runs out of TW and a
# zoneinfo dependency for one fixed offset is overkill. Adjust if the rig
# moves jurisdictions.
_LOCAL_TZ = timezone(timedelta(hours=8))

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
    for sid, cams_present, n_ball_frames_by_path, cam_capture_telemetry, result, created_at, is_live_only in snapshots:
        trashed = state.processing.is_trashed(sid)
        if bucket == "active" and trashed:
            continue
        if bucket == "trash" and not trashed:
            continue

        latest_mtime = _latest_pitch_mtime(state, cams_present, sid)
        created_day, created_hm = _format_local(created_at)
        authority_points = result.triangulated if result is not None else []
        n_triangulated = len(authority_points) if result is not None else 0
        n_segments = len(result.segments) if result is not None else 0
        error = result.error if result is not None else None

        status = _status_label(cams_present, n_triangulated, error, is_live_only)
        mean_res, duration = _point_cloud_summary(authority_points)
        mode = _legacy_mode_label(state, sid)
        path_status = _path_status_pills(result, n_ball_frames_by_path, is_live_only)
        processing_state, processing_resumable = state.processing.session_summary(sid)

        events.append(
            {
                "session_id": sid,
                "cameras": cams_present,
                "status": status,
                "mode": mode,
                "received_at": latest_mtime,
                # Original creation stamp + pre-formatted local strings so
                # SSR + client renderers don't both reimplement timezone math.
                "created_at": created_at,
                "created_day": created_day,
                "created_hm": created_hm,
                # Per-pipeline counts (live / server_post). Legacy flat name
                # kept for older consumers.
                "n_ball_frames_by_path": n_ball_frames_by_path,
                "n_ball_frames": n_ball_frames_by_path[DetectionPath.server_post.value],
                "n_triangulated": n_triangulated,
                "n_segments": n_segments,
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
                "server_post_errors": state.processing.errors_for(sid),
            }
        )

    # Sort by *original* creation stamp, not file mtime — server_post
    # backfill rewrites the pitch JSON and bumps mtime, which used to make
    # finished sessions jump to the top of the events list as if they'd just
    # arrived. `created_at` is set once on first /pitch and preserved across
    # re-records, so the order reflects when the operator actually threw.
    events.sort(
        key=lambda e: (e.get("created_at") or 0, e["session_id"]),
        reverse=True,
    )
    return events


def _format_local(ts: float | None) -> tuple[str | None, str | None]:
    if ts is None:
        return None, None
    dt = datetime.fromtimestamp(ts, tz=_LOCAL_TZ)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _snapshot_sessions_locked(
    state: "State",
) -> list[
    tuple[
        str,
        list[str],
        dict[str, dict[str, int]],
        dict[str, CaptureTelemetryPayload | None],
        SessionResult | None,
        float | None,
        bool,
    ]
]:
    """Grab everything we need from in-memory state under one lock acquisition.

    Returns one snapshot per session_id present in any of:
      * `state.pitches` — fully ingested (post-/pitch upload)
      * `state._live_pairings` — live frames buffered, /pitch not yet posted
      * `state._current_session` — armed but no live frame has landed yet
        (still surfaces an empty placeholder row in the events list)

    The trailing bool flag (`is_live_only`) tells the renderer the row is
    an in-flight session (no pitch JSON on disk yet) so it can paint a
    streaming placeholder instead of "—".

    Every subsequent step (file stats, summary derivation) runs outside the
    lock so the 5 s dashboard tick can't stall /pitch handlers that mutate
    the pitches/results maps.
    """
    with state._lock:
        pitched_sids = {sid for _, sid in state.pitches.keys()}
        live_sids = set(state._live_pairings.keys())
        armed = state._current_session
        if armed is not None and armed.ended_at is None:
            live_sids.add(armed.id)
        all_sids = sorted(pitched_sids | live_sids)
        snapshots: list[
            tuple[
                str,
                list[str],
                dict[str, dict[str, int]],
                dict[str, CaptureTelemetryPayload | None],
                SessionResult | None,
                float | None,
                bool,
            ]
        ] = []
        for sid in all_sids:
            is_pitched = sid in pitched_sids
            cams_present = sorted(cam for (cam, s) in state.pitches.keys() if s == sid)
            n_ball_frames_by_path: dict[str, dict[str, int]] = {
                path: {} for path, _ in _PATH_TO_FRAMES_ATTR
            }
            created_candidates: list[float] = []
            for cam in cams_present:
                pitch = state.pitches[(cam, sid)]
                for path, attr in _PATH_TO_FRAMES_ATTR:
                    frames = getattr(pitch, attr, ()) or ()
                    n_ball_frames_by_path[path][cam] = sum(
                        1 for f in frames if f.ball_detected
                    )
                if pitch.created_at is not None:
                    created_candidates.append(pitch.created_at)
            cam_capture_telemetry = {
                cam: state.pitches[(cam, sid)].capture_telemetry
                for cam in cams_present
            }
            # Live-only / partially-flushed sessions: merge buffered live
            # frame counts so the row reflects what's already streaming
            # in via WS, even before persist_live_frames flushes to disk.
            live = state._live_pairings.get(sid)
            if live is not None:
                live_counts = live.frame_counts_snapshot()
                for cam_id, n in live_counts.items():
                    if n <= 0:
                        continue
                    if cam_id not in cams_present:
                        cams_present = sorted({*cams_present, cam_id})
                    # Merge: pitched live count is the source of truth once
                    # the pitch JSON lands; until then the WS buffer count
                    # drives the row. Disk count >= buffer count after flush
                    # because the buffer is a strict subset.
                    existing = n_ball_frames_by_path[DetectionPath.live.value].get(cam_id, 0)
                    n_ball_frames_by_path[DetectionPath.live.value][cam_id] = max(existing, n)
            # `created_at` resolution order:
            #   1. earliest pitch.created_at (post-upload truth)
            #   2. armed/ended Session.started_at (pre-upload, in-memory)
            sess = state._lookup_session_locked(sid)
            if created_candidates:
                session_created_at = min(created_candidates)
            elif sess is not None:
                session_created_at = sess.started_at
            else:
                session_created_at = None
            snapshots.append(
                (
                    sid,
                    cams_present,
                    n_ball_frames_by_path,
                    cam_capture_telemetry,
                    state.results.get(sid),
                    session_created_at,
                    not is_pitched,
                )
            )
    return snapshots


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


def _status_label(
    cams_present: list[str],
    n_triangulated: int,
    error: str | None,
    is_live_only: bool = False,
) -> str:
    if error:
        return "error"
    if is_live_only:
        # Pitch JSON not yet on disk — session is either armed waiting for
        # the first frame, or actively streaming. Renderer treats this as
        # a placeholder and animates accordingly.
        return "streaming"
    if len(cams_present) >= 2 and n_triangulated > 0:
        return "paired"
    if len(cams_present) >= 2:
        return "paired_no_points"
    return "partial"


def _point_cloud_summary(
    authority_points: list,
) -> tuple[float | None, float | None]:
    if not authority_points:
        return None, None
    mean_res = float(
        sum(p.residual_m for p in authority_points) / len(authority_points)
    )
    ts = [p.t_rel_s for p in authority_points]
    duration = float(ts[-1] - ts[0])
    return mean_res, duration


def _legacy_mode_label(state: "State", sid: str) -> str:
    has_any_video = any(state._video_dir.glob(f"session_{sid}_*"))
    if has_any_video:
        return "camera_only"
    return "live_only"


def _path_status_pills(
    result: SessionResult | None,
    n_ball_frames_by_path: dict[str, dict[str, int]],
    is_live_only: bool = False,
) -> dict[str, str]:
    """Per-pipeline health pill. Resolves in this order (strongest wins):

    1. "done" if result.paths_completed includes it (triangulated on a
       paired session, or explicit mono-session finalization),
    2. "streaming" for live path on an in-flight (live-only) session —
       frames are arriving over WS but persist hasn't run; renderer
       animates the L chip to signal liveness without faking "done".
    3. "done" if any camera produced ≥1 detected frame on that pipeline —
       live-only single-camera runs ship no triangulation but still count
       as "that pipeline executed", which is what the user wants to see in
       the events list,
    4. "error" if abort_reasons has an entry for this pipeline,
    5. "-" (never ran / empty).
    """

    def pill(path_value: str, abort_prefix: str | None = None) -> str:
        if result is not None and path_value in result.paths_completed:
            return "done"
        has_any = any(c > 0 for c in n_ball_frames_by_path.get(path_value, {}).values())
        if is_live_only and path_value == DetectionPath.live.value:
            return "streaming" if has_any else "armed"
        if has_any:
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
