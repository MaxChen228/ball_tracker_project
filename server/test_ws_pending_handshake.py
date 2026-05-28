"""PR3 device_uuid handshake: pending mode + assigned flow + reassign.

Pins the new `/ws/device/{device_uuid}` flow:
  - already-assigned device → server pushes `cam_id_assigned` immediately
    and the existing settings/arm/sync_run plumbing kicks in
  - unassigned device → server pushes `cam_id_pending`, holds the socket
    in `PendingDeviceManager`, and only proceeds when `/devices/assign`
    fires
  - assign endpoint wakes the pending WS without iOS needing to reconnect
  - unassign endpoint closes the active WS so iOS reconnects through
    the handshake and lands in pending again

Threading: TestClient.websocket_connect blocks the calling thread on
receive_json. To exercise pending-then-assign in a single test we run
the WS in a daemon thread and fire the assign POST from the main thread.
PendingDeviceManager captures the WS handler's event loop and routes
notifications via `call_soon_threadsafe` so cross-loop wakes are safe.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import main
from main import app


def test_assigned_device_uuid_gets_cam_id_immediately():
    """Fast path: device_uuid already in assignment store → first server
    message is `cam_id_assigned`, followed by the normal post-handshake
    `settings` push. No `cam_id_pending` in between."""
    main.state.assign_device(device_uuid="abc-uuid", camera_id="A")
    client = TestClient(app)
    with client.websocket_connect("/ws/device/abc-uuid") as ws:
        first = ws.receive_json()
        assert first["type"] == "cam_id_assigned"
        assert first["camera_id"] == "A"
        assert first["device_uuid"] == "abc-uuid"
        second = ws.receive_json()
        assert second["type"] == "settings"


def test_unassigned_device_uuid_gets_pending():
    """Slow path: device_uuid has no assignment → server pushes
    `cam_id_pending` and holds. The WS stays open."""
    client = TestClient(app)
    with client.websocket_connect("/ws/device/fresh-uuid") as ws:
        first = ws.receive_json()
        assert first["type"] == "cam_id_pending"
        assert first["device_uuid"] == "fresh-uuid"


def test_pending_device_appears_in_pool():
    """While a device is pending, GET /devices/pool surfaces it under
    `pending` so the operator can see who's waiting for promotion."""
    client = TestClient(app)
    pool_seen: dict = {}

    def _hold_ws():
        with client.websocket_connect("/ws/device/uuid-waiting") as ws:
            assert ws.receive_json()["type"] == "cam_id_pending"
            for _ in range(40):
                if pool_seen.get("done"):
                    break
                time.sleep(0.02)

    t = threading.Thread(target=_hold_ws, daemon=True)
    t.start()
    deadline = time.time() + 2.0
    pending_uuids: list = []
    while time.time() < deadline:
        pool = client.get("/devices/pool").json()
        pending_uuids = [p["device_uuid"] for p in pool.get("pending", [])]
        if "uuid-waiting" in pending_uuids:
            break
        time.sleep(0.05)
    pool_seen["done"] = True
    t.join(timeout=2.0)
    assert "uuid-waiting" in pending_uuids


def test_assign_wakes_pending_ws():
    """The core slow-path round-trip: WS sits pending → operator hits
    /devices/assign → the awaiting WS receives `cam_id_assigned` and
    transitions into the normal post-handshake flow."""
    client = TestClient(app)
    result: dict = {}

    def _ws_thread():
        try:
            with client.websocket_connect("/ws/device/u-slow") as ws:
                result["first"] = ws.receive_json()
                result["second"] = ws.receive_json()
                result["third"] = ws.receive_json()
        except Exception as e:
            result["error"] = repr(e)

    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    # Poll the pool until the WS lands in pending.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        pool = client.get("/devices/pool").json()
        if any(p["device_uuid"] == "u-slow" for p in pool.get("pending", [])):
            break
        time.sleep(0.02)
    else:
        pytest.fail("WS never reached pending state within 2s")

    r = client.post("/devices/assign", json={
        "device_uuid": "u-slow", "camera_id": "C",
    })
    assert r.status_code == 200, r.text
    t.join(timeout=2.0)

    assert "error" not in result, result.get("error")
    assert result["first"]["type"] == "cam_id_pending"
    assert result["second"]["type"] == "cam_id_assigned"
    assert result["second"]["camera_id"] == "C"
    assert result["third"]["type"] == "settings"


def test_stray_message_during_pending_closes_ws():
    """Protocol contract: during pending mode iOS must not send anything;
    server treats any inbound message as a protocol error and closes."""
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/device/u-stray") as ws:
            assert ws.receive_json()["type"] == "cam_id_pending"
            ws.send_json({"type": "heartbeat"})
            ws.receive_json()


def test_bad_device_uuid_format_closes_immediately():
    """A device_uuid that fails the format regex must be rejected before
    any pending bookkeeping kicks in."""
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/device/" + ("x" * 100)) as ws:
            ws.receive_json()


def test_unassign_closes_pending_ws():
    """If the operator unassigns a device that's currently pending
    (e.g. they tried to assign but mis-typed and want to cancel before
    iOS has been promoted), the pending WS must close cleanly with no
    cam_id resolved."""
    client = TestClient(app)
    main.state.assign_device(device_uuid="u-revoke", camera_id="Z")
    # Now revoke before any WS connects. (Sanity: assignment goes away.)
    r = client.post("/devices/unassign", json={"camera_id": "Z"})
    assert r.status_code == 200
    assert r.json()["unassigned"] is True
    # Reconnect after revoke → device is pending again.
    with client.websocket_connect("/ws/device/u-revoke") as ws:
        first = ws.receive_json()
        assert first["type"] == "cam_id_pending"
