"""Tests for /sim/events WebSocket (Godot trajectory viewer push channel)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
import schemas
from conftest import sid
from main import app
from routes.sim_events import _project_for_sim


# ---------------------------------------------------------------------------
# Pure projection function — exercised without an actual WS connection so we
# can sweep filter cases cheaply.
# ---------------------------------------------------------------------------

def _seed_live_segments(session_id: str) -> None:
    seg = schemas.SegmentRecord(
        indices=[0, 1, 2],
        original_indices=[0, 1, 2],
        p0=[0.0, 0.0, 1.5],
        v0=[0.0, 35.0, 5.0],
        t_anchor=0.0, t_start=0.0, t_end=0.4,
        rmse_m=0.01, speed_kph=126.0,
    )
    main.state.results[session_id] = schemas.SessionResult(
        session_id=session_id,
        cameras_received={"A": True, "B": True},
        segments_by_algorithm={schemas.IOS_CAPTURE_TIME_ALGORITHM_ID: [seg]},
    )


def test_project_ignores_unrelated_events():
    """Only `session_ended` is considered. Other SSE events (device_status,
    fit, calibration_changed, etc.) must not leak through."""
    sample = {"sid": sid(1), "anything": "goes"}
    for event in ["device_status", "fit", "calibration_changed",
                  "server_post_done", "session_armed"]:
        assert _project_for_sim(event, sample, main.state) is None


def test_project_skips_session_ended_without_live_path():
    """A session that completed only `server_post` (no live frames) must
    not trigger a push — the Godot viewer asks for live by default."""
    session_id = sid(2)
    _seed_live_segments(session_id)
    out = _project_for_sim(
        "session_ended",
        {"sid": session_id, "paths_completed": ["server_post"]},
        main.state,
    )
    assert out is None


def test_project_skips_when_no_live_segments_yet():
    """Live path ran but produced 0 segments (operator stopped before
    any pitch). Pushing would race the viewer into a 404."""
    session_id = sid(3)
    # No segments, but session exists.
    main.state.results[session_id] = schemas.SessionResult(
        session_id=session_id,
        cameras_received={"A": True, "B": True},
    )
    out = _project_for_sim(
        "session_ended",
        {"sid": session_id, "paths_completed": ["live"]},
        main.state,
    )
    assert out is None


def test_project_skips_when_session_result_missing():
    """state.results[sid] absent — pre-rebuild race. Suppress, don't crash."""
    out = _project_for_sim(
        "session_ended",
        {"sid": sid(4), "paths_completed": ["live"]},
        main.state,
    )
    assert out is None


def test_project_emits_session_trajectory_ready_for_live_done():
    session_id = sid(5)
    _seed_live_segments(session_id)
    out = _project_for_sim(
        "session_ended",
        {"sid": session_id, "paths_completed": ["live"]},
        main.state,
    )
    assert out == {
        "type": "session_trajectory_ready",
        "session_id": session_id,
        "algorithm_id": schemas.IOS_CAPTURE_TIME_ALGORITHM_ID,
        "cause": "live_done",
    }


def test_project_skips_blank_or_missing_sid():
    """Defence in depth: a malformed broadcast must not push garbage to
    the Godot client."""
    assert _project_for_sim(
        "session_ended",
        {"sid": "", "paths_completed": ["live"]},
        main.state,
    ) is None
    assert _project_for_sim(
        "session_ended",
        {"paths_completed": ["live"]},
        main.state,
    ) is None


# ---------------------------------------------------------------------------
# Live WS — confirm hello + actual push round-trip through sse_hub.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ws_sends_hello_on_connect():
    client = TestClient(app)
    with client.websocket_connect("/sim/events") as ws:
        msg = json.loads(ws.receive_text())
        assert msg == {"type": "hello"}


@pytest.mark.anyio
async def test_ws_forwards_session_ended_as_session_trajectory_ready():
    """End-to-end: sse_hub.broadcast → WS handler → text frame at client.
    Sanity-checks that the structured-queue refactor of SSEHub didn't
    silently drop notifications on the floor."""
    session_id = sid(7)
    _seed_live_segments(session_id)

    client = TestClient(app)
    with client.websocket_connect("/sim/events") as ws:
        # Drain the hello.
        assert json.loads(ws.receive_text())["type"] == "hello"

        # Fire the broadcast from the same event loop the WS handler is
        # listening on. starlette's TestClient runs the app in a thread
        # with its own loop; we use the helper portal to schedule.
        async def fire():
            await main.sse_hub.broadcast(
                "session_ended",
                {"sid": session_id, "paths_completed": ["live"]},
            )

        ws.portal.call(fire)

        msg = json.loads(ws.receive_text())
        assert msg == {
            "type": "session_trajectory_ready",
            "session_id": session_id,
            "algorithm_id": schemas.IOS_CAPTURE_TIME_ALGORITHM_ID,
            "cause": "live_done",
        }


@pytest.mark.anyio
async def test_ws_does_not_forward_unrelated_broadcasts():
    """A device_status broadcast (1 Hz heartbeat in production) must not
    forward — Godot would treat that as garbage."""
    client = TestClient(app)
    with client.websocket_connect("/sim/events") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"

        async def fire():
            await main.sse_hub.broadcast(
                "device_status",
                {"camera_id": "A", "online": True},
            )

        ws.portal.call(fire)

        # Hand over the loop briefly so the subscribe coroutine has a
        # chance to filter the event, then confirm no second frame
        # arrived.
        import time as _t
        _t.sleep(0.05)
        # If anything snuck through, the next receive would either
        # succeed with garbage or block. We deliberately set a short
        # timeout on the client; starlette TestClient defaults are
        # blocking, so we use a fresh broadcast to bracket the check
        # — emit a known-relevant event and confirm we get THAT, not
        # the device_status that should have been dropped.
        session_id = sid(8)
        _seed_live_segments(session_id)

        async def fire_relevant():
            await main.sse_hub.broadcast(
                "session_ended",
                {"sid": session_id, "paths_completed": ["live"]},
            )

        ws.portal.call(fire_relevant)
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "session_trajectory_ready"
        assert msg["session_id"] == session_id
