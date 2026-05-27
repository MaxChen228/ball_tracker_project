"""Dashboard status projections.

Pure read-only aggregations over `State` that the dashboard `/status` /
`/latest` / live-summary panels consume. None of these helpers mutate
state — they snapshot under `state._lock` and return plain dicts.

Mirrors the free-function + State-as-facade pattern already established
by state_events.py / session_results.py / detection_paths.py: helpers
take `state` as an explicit arg, State keeps same-named methods that
delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schemas import SessionResult

if TYPE_CHECKING:
    from state import State


def summary(state: "State") -> dict[str, Any]:
    with state._lock:
        sessions = sorted({sid for _, sid in state.pitches.keys()})
        completed = [
            k for k, r in state.results.items()
            if r.cameras_received
            and all(r.cameras_received.values())
            and not r.error
        ]
        return {
            "state": "receiving" if state.pitches else "idle",
            "received_sessions": sessions,
            "completed_sessions": sorted(completed),
        }


def latest(state: "State") -> SessionResult | None:
    """Most recently written result. File mtime on disk would be more
    correct, but the in-memory ordering is good enough for the /latest
    endpoint's "last thing uploaded" semantic — sessions sort
    lexicographically by id which is time-of-generation-adjacent."""
    with state._lock:
        if not state.results:
            return None
        return state.results[max(state.results.keys())]


def live_session_summary(state: "State") -> dict[str, Any] | None:
    session = state.session_snapshot()
    if session is None:
        return None
    with state._lock:
        live = state._live_pairings.get(session.id)
        result = state.results.get(session.id)
        missing_cal = sorted(state._live_missing_cal.get(session.id, set()))
    paths_completed = sorted(result.paths_completed) if result is not None else []
    if live is None:
        return {
            "session_id": session.id,
            "armed": session.armed,
            "paths": sorted(p.value for p in session.paths),
            "frame_counts": {},
            "point_count": 0,
            "paths_completed": paths_completed,
            "abort_reasons": {},
            "live_missing_calibration": missing_cal,
        }
    return {
        "session_id": session.id,
        "armed": session.armed,
        "paths": sorted(p.value for p in session.paths),
        "frame_counts": live.frame_counts_snapshot(),
        "point_count": live.triangulated_count(),
        "paths_completed": paths_completed,
        "completed_cameras": live.completed_cameras_snapshot(),
        "abort_reasons": live.abort_reasons_snapshot(),
        "live_missing_calibration": missing_cal,
    }


def auto_cal_status(state: "State") -> dict[str, Any]:
    with state._lock:
        return state._auto_cal_runs.status()


def all_calibration_last_solves(state: "State") -> dict[str, dict[str, Any]]:
    with state._lock:
        return state._last_solves.all_summaries()


def calibration_last_solve_summary(
    state: "State", camera_id: str,
) -> dict[str, Any] | None:
    with state._lock:
        return state._last_solves.summary(camera_id)
