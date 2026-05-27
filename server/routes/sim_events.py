"""WebSocket fan-out for the Godot trajectory viewer (`sim/`).

Sole purpose: turn "a session just got new triangulation results" into a
push notification that the Godot client can react to. The actual
trajectory data still comes from `GET /sessions/{sid}/trajectory` —
this socket carries only `{type, session_id, algorithm_id, cause}` so
there is a single source of truth for the JSON shape.

Mode 1 (push, this file): server pushes when a live session finishes
with at least one segment in the canonical live bucket.

Mode 2 (pull): operator types a session id in the Godot viewer and
presses Load. Exact same downstream code path as mode 1 — both end at
HttpRequest.Request(/sessions/{sid}/trajectory).

Why filter at the server, not at the Godot client:
  * We already know inside the server whether segments exist. Pushing a
    notification for a 0-segment session would just race the viewer
    into a 404 — better to suppress at source.
  * Future N-camera rigs may want algorithm fan-out (one event per
    algorithm with new segments). Doing the projection here keeps the
    Godot client schema-light.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from schemas import IOS_CAPTURE_TIME_ALGORITHM_ID

logger = logging.getLogger(__name__)

router = APIRouter()


def _project_for_sim(event: str, data: dict, state) -> dict | None:
    """Filter / project SSE events for Godot trajectory viewer consumption.

    Returns the Godot-facing payload, or None when the event is not
    relevant to the viewer (most events fall through).

    Current scope: only `session_ended` events where the live path
    completed AND `state.results[sid]` carries non-empty segments for
    the canonical live algorithm. `fit` events (server_post / recompute
    / active_run_switch) are NOT forwarded yet because those broadcasts
    don't carry an `algorithm_id` field, so the viewer can't pick the
    right bucket without a follow-up schema change to the fit payload.
    """
    if event != "session_ended":
        return None
    sid = data.get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    paths_completed = data.get("paths_completed") or []
    if "live" not in paths_completed:
        return None
    result = state.get(sid)
    if result is None:
        return None
    if not result.segments_by_algorithm.get(IOS_CAPTURE_TIME_ALGORITHM_ID):
        return None
    return {
        "type": "session_trajectory_ready",
        "session_id": sid,
        "algorithm_id": IOS_CAPTURE_TIME_ALGORITHM_ID,
        "cause": "live_done",
    }


@router.websocket("/sim/events")
async def sim_events(ws: WebSocket) -> None:
    from main import sse_hub, state

    await ws.accept()
    # Send a hello on connect so the Godot client gets one frame
    # confirming the socket is wired up — useful when the operator opens
    # the viewer before any session has ended, otherwise the connection
    # looks indistinguishable from a hung socket.
    await ws.send_text(json.dumps({"type": "hello"}))

    try:
        async for event, data in sse_hub.subscribe():
            projected = _project_for_sim(event, data, state)
            if projected is None:
                continue
            try:
                await ws.send_text(json.dumps(projected))
            except WebSocketDisconnect:
                return
    except WebSocketDisconnect:
        return
    except Exception as exc:
        # Don't let one bad subscriber tear down the broadcast loop;
        # log and exit this handler cleanly so sse_hub drops the queue.
        logger.warning("sim/events handler aborted: %s", exc)
        return
