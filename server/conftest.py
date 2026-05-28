"""Shared fixtures for the server test suite.

Previously each test module declared its own `_reset_state` autouse — that
worked when there was only one test file. Now that `test_viewer.py` also
hits the global `main.state`, the fixture lives here so both files get
the same isolated-state guarantee without duplicating code.
"""
from __future__ import annotations

import pytest

import main


@pytest.fixture(autouse=True)
def _reset_main_state(tmp_path, monkeypatch):
    """Replace `main.state` with a fresh per-test State rooted at tmp_path.

    Route modules in routes/* use `from main import state` inside function
    bodies (late import), so they always read the current `main.state`
    value — no extra patching needed."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    # Also reset the pending-devices registry so a previous test's stuck
    # pending entry can't leak into the next test's WS handshake.
    monkeypatch.setattr(main, "pending_devices", main.PendingDeviceManager())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    yield


def preassign_and_open_ws(client, identifier: str):
    """PR3-aware WS helper: pre-assign device_uuid=identifier → cam_id=
    identifier so the handshake takes the fast path, then open the
    WebSocket and consume the `cam_id_assigned` server message.

    Returns the context manager so callers can use it with `with`. After
    the handshake is drained, the next `receive_json()` returns the
    first post-handshake server message (typically `settings`).

    Why a helper: PR3 moved the WS endpoint from `/ws/device/{camera_id}`
    to `/ws/device/{device_uuid}` with a mandatory handshake. Tests
    written against the old endpoint used `identifier` as cam_id; now
    we treat it as device_uuid and pre-assign the matching cam_id so
    existing assertions keep working without rewriting test bodies.
    """
    import main
    main.state.assign_device(device_uuid=identifier, camera_id=identifier)
    ctx = client.websocket_connect(f"/ws/device/{identifier}")
    ws = ctx.__enter__()
    first = ws.receive_json()
    assert first.get("type") == "cam_id_assigned", first
    assert first.get("camera_id") == identifier, first
    return _WSContext(ctx, ws)


class _WSContext:
    """Minimal context-manager wrapper that mimics
    `client.websocket_connect(...)` semantics but holds an already-drained
    WS so `with preassign_and_open_ws(...) as ws:` yields the post-
    handshake socket directly."""

    def __init__(self, inner_ctx, ws):
        self._inner_ctx = inner_ctx
        self._ws = ws

    def __enter__(self):
        return self._ws

    def __exit__(self, exc_type, exc, tb):
        return self._inner_ctx.__exit__(exc_type, exc, tb)


def sid(n: int | str) -> str:
    """Session-id helper for tests — returns a value that matches the
    server's `s_[0-9a-f]{4,32}` regex. Use a stable int or a readable
    suffix so assertions on session identity stay legible."""
    if isinstance(n, int):
        return f"s_{n:08x}"
    return f"s_{n}"
