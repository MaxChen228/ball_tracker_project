"""End-to-end sync flow tests with two fake-iPhone WS clients.

Replays exactly the wire protocol the iOS app drives:
  - WS connect to /ws/device/{cam}
  - send `hello` carrying battery + sync state
  - send `heartbeat` periodically with current time_sync_id + anchor
  - receive `arm` / `disarm` / `sync_command` / `settings` from server

That makes these tests catch the class of bug pytest's per-module unit
tests missed:
  1. SSE `device_heartbeat` payload diverging from /status's gated
     `time_synced` (Bug 1: dashboard LED flicker because two sources
     fought every heartbeat).
  2. Asymmetric chirp loss leaving one cam on a stale anchor while the
     peer locks onto a fresh one — Bug 2's actual root cause, where
     readiness signed off on a "stereo · ready" session whose A and B
     anchors pointed at different physical chirps.

Layer A (gate consistency): `test_sse_heartbeat_time_synced_*`.
Layer B (two-cam wire flow): everything else.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

import main
from main import app


# ---------------------------------------------------------------- #
# Test infra: capture-only SSE hub so we can assert what the
# dashboard would have seen, without spinning a real EventSource.
# ---------------------------------------------------------------- #
class _CaptureHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    async def subscribe(self):  # pragma: no cover - dashboard side
        if False:
            yield ""

    def find_last(self, event: str, predicate: Callable[[dict], bool] | None = None) -> dict | None:
        for name, data in reversed(self.events):
            if name == event and (predicate is None or predicate(data)):
                return data
        return None

    def wait_for(self, event: str, predicate: Callable[[dict], bool], timeout_s: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            hit = self.find_last(event, predicate)
            if hit is not None:
                return hit
            time.sleep(0.005)
        # Final attempt for clear failure reporting.
        hit = self.find_last(event, predicate)
        if hit is None:
            raise AssertionError(
                f"timeout waiting for {event!r}; events so far={self.events!r}"
            )
        return hit


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """Replace the singleton state + sse hub + ws manager so each test
    starts from a known-empty registry. mirrors the existing test_ws
    pattern but folded into a fixture."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    hub = _CaptureHub()
    monkeypatch.setattr(main, "sse_hub", hub)
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    return hub


def _drain_initial_settings(ws) -> dict:
    """Server sends `settings` on connect; consume so subsequent reads
    see real reactive messages."""
    msg = ws.receive_json()
    assert msg["type"] == "settings", msg
    return msg


def _hello(ws, cam: str, *, sync_id: str | None, anchor: float | None) -> None:
    ws.send_json({
        "type": "hello",
        "cam": cam,
        "time_synced": sync_id is not None and anchor is not None,
        "time_sync_id": sync_id,
        "sync_anchor_timestamp_s": anchor,
    })
    # Server replies with a settings push after every hello/heartbeat.
    ack = ws.receive_json()
    assert ack["type"] == "settings"


def _heartbeat(ws, cam: str, *, sync_id: str | None, anchor: float | None) -> None:
    ws.send_json({
        "type": "heartbeat",
        "cam": cam,
        "time_synced": sync_id is not None and anchor is not None,
        "time_sync_id": sync_id,
        "sync_anchor_timestamp_s": anchor,
    })


def _status_device(client: TestClient, cam: str) -> dict[str, Any]:
    body = client.get("/status").json()
    for d in body.get("devices", []):
        if d["camera_id"] == cam:
            return d
    raise AssertionError(f"cam {cam} not in /status devices")


def _status_blockers(client: TestClient) -> list[str]:
    return list(client.get("/status").json()["arm_readiness"]["blockers"])


# =============================================================== #
# Layer A — SSE/HTTP gate-consistency contracts.
# =============================================================== #

def test_sse_heartbeat_time_synced_matches_status_when_id_mismatches(fresh_state):
    """The bug we just shipped a fix for: /status gated time_synced
    through `id_match` but SSE `device_heartbeat` did not. Dashboard
    cached both sources and flipped the LED every second. Lock the
    invariant: SSE's `time_synced` MUST equal /status's for the same
    cam at the same instant, regardless of expected_id state."""
    hub = fresh_state
    client = TestClient(app)
    with client.websocket_connect("/ws/device/A") as ws_a:
        _drain_initial_settings(ws_a)
        # Cam reports an old id, but server expects a different one
        # (e.g. operator just fired Quick chirp for a fresh attempt).
        _hello(ws_a, "A", sync_id="sy_old", anchor=10.0)
        main.state.set_expected_sync_id(["A"], "sy_new")
        _heartbeat(ws_a, "A", sync_id="sy_old", anchor=10.0)

        sse = hub.wait_for(
            "device_heartbeat",
            lambda d: d.get("cam") == "A",
        )
        status_a = _status_device(client, "A")
        assert sse["time_synced"] == status_a["time_synced"], (sse, status_a)
        # And both must be False under the gate (id mismatch).
        assert sse["time_synced"] is False
        assert status_a["time_synced"] is False


def test_sse_heartbeat_time_synced_matches_status_when_id_matches(fresh_state):
    """Inverse of the previous test: when the cam DOES echo the
    expected id, both /status and SSE must agree on True."""
    hub = fresh_state
    client = TestClient(app)
    with client.websocket_connect("/ws/device/A") as ws_a:
        _drain_initial_settings(ws_a)
        main.state.set_expected_sync_id(["A"], "sy_match")
        _hello(ws_a, "A", sync_id="sy_match", anchor=10.0)
        _heartbeat(ws_a, "A", sync_id="sy_match", anchor=10.0)

        sse = hub.wait_for(
            "device_heartbeat",
            lambda d: d.get("cam") == "A",
        )
        status_a = _status_device(client, "A")
        assert sse["time_synced"] == status_a["time_synced"]
        assert sse["time_synced"] is True


def test_sse_heartbeat_time_synced_matches_status_when_no_expected_set(fresh_state):
    """No expected_id pinned (clean boot) — gate passes any reported
    id+anchor pair. Both sources must say True."""
    hub = fresh_state
    client = TestClient(app)
    with client.websocket_connect("/ws/device/A") as ws_a:
        _drain_initial_settings(ws_a)
        _hello(ws_a, "A", sync_id="sy_anything", anchor=5.0)
        _heartbeat(ws_a, "A", sync_id="sy_anything", anchor=5.0)

        sse = hub.wait_for("device_heartbeat", lambda d: d.get("cam") == "A")
        status_a = _status_device(client, "A")
        assert sse["time_synced"] is True
        assert status_a["time_synced"] is True


# =============================================================== #
# Layer B — Two-cam wire flow.
#
# These connect both A and B as fake iPhones and step through the
# Quick-chirp lifecycle the operator drives from the dashboard.
# =============================================================== #

def _calibrate_both(client: TestClient) -> None:
    """Minimal calibration so arm_readiness considers the cams usable.
    Values are placeholders — readiness only checks presence in the
    calibrations registry."""
    K = {"fx": 1500.0, "fy": 1500.0, "cx": 960.0, "cy": 540.0}
    H = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    for cam in ("A", "B"):
        r = client.post("/calibration", json={
            "camera_id": cam,
            "intrinsics": K,
            "homography": H,
            "image_width_px": 1920,
            "image_height_px": 1080,
        })
        assert r.status_code == 200, r.text


def test_quick_chirp_dispatch_clears_anchors_and_blocks_arm_until_both_resync(fresh_state):
    """Operator path: both cams already synced from a previous round,
    operator hits Quick chirp, only A actually hears the new chirp,
    B times out. Readiness must keep arm blocked."""
    client = TestClient(app)
    _calibrate_both(client)

    with client.websocket_connect("/ws/device/A") as ws_a, \
         client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_a)
        _drain_initial_settings(ws_b)
        # Both cams report the same prior anchor so initial readiness
        # is "ready" — this is the realistic starting point right
        # before an operator fires another Quick chirp.
        _hello(ws_a, "A", sync_id="sy_prior", anchor=10.0)
        _hello(ws_b, "B", sync_id="sy_prior", anchor=20.0)
        assert _status_blockers(client) == []

        # Quick chirp.
        r = client.post("/sync/trigger", json={"camera_ids": ["A", "B"]})
        assert r.status_code == 200, r.text
        dispatched = r.json()["dispatched_to"]
        assert sorted(dispatched) == ["A", "B"]

        # Server-side anchors must have been cleared immediately.
        for cam in ("A", "B"):
            d = main.state.device_snapshot(cam)
            assert d is not None and d.time_synced is False
            assert d.time_sync_id is None
            assert d.sync_anchor_timestamp_s is None

        # WS sync_command is broadcast — pick up the new id off the wire.
        cmd_a = ws_a.receive_json()
        cmd_b = ws_b.receive_json()
        assert cmd_a["type"] == "sync_command" and cmd_b["type"] == "sync_command"
        new_id = cmd_a["sync_command_id"]
        assert new_id == cmd_b["sync_command_id"], "both cams must get the same id"

        # /status must reflect blocked-arm right now.
        blockers = _status_blockers(client)
        assert any("not time-synced" in b for b in blockers), blockers

        arm = client.post("/sessions/arm",
                          headers={"Accept": "application/json"})
        assert arm.status_code == 409, arm.text

        # A "hears" the chirp and echoes new id; B times out and keeps
        # reporting nothing (post-fix iOS clears its anchor on listen).
        _heartbeat(ws_a, "A", sync_id=new_id, anchor=100.0)
        _heartbeat(ws_b, "B", sync_id=None, anchor=None)

        blockers = _status_blockers(client)
        assert any("B not time-synced" in b for b in blockers), blockers
        assert not any("A not time-synced" in b for b in blockers), blockers

        arm2 = client.post("/sessions/arm",
                           headers={"Accept": "application/json"})
        assert arm2.status_code == 409

        # B finally hears the chirp.
        _heartbeat(ws_b, "B", sync_id=new_id, anchor=200.0)
        blockers = _status_blockers(client)
        assert blockers == [], blockers

        arm3 = client.post("/sessions/arm",
                           headers={"Accept": "application/json"})
        assert arm3.status_code == 200, arm3.text


def test_quick_chirp_pre_fix_regression_simulation(fresh_state):
    """Reproduce the user's bug 2 directly via the wire: A and B end up
    holding different sync_ids (each from a separate trigger event in
    which only that cam happened to detect the chirp). Pair-check in
    readiness must catch it even though every per-cam gate passes."""
    client = TestClient(app)
    _calibrate_both(client)

    with client.websocket_connect("/ws/device/A") as ws_a, \
         client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_a)
        _drain_initial_settings(ws_b)
        _hello(ws_a, "A", sync_id=None, anchor=None)
        _hello(ws_b, "B", sync_id=None, anchor=None)

        # Round 1: trigger A only, A hears chirp.
        r1 = client.post("/sync/trigger", json={"camera_ids": ["A"]})
        assert r1.json()["dispatched_to"] == ["A"]
        cmd_a = ws_a.receive_json()
        id_round1 = cmd_a["sync_command_id"]
        _heartbeat(ws_a, "A", sync_id=id_round1, anchor=100.0)

        # Round 2: trigger B only, B hears chirp — but it's a NEW id.
        r2 = client.post("/sync/trigger", json={"camera_ids": ["B"]})
        assert r2.json()["dispatched_to"] == ["B"]
        cmd_b = ws_b.receive_json()
        id_round2 = cmd_b["sync_command_id"]
        assert id_round1 != id_round2, "two independent triggers must mint distinct ids"
        _heartbeat(ws_b, "B", sync_id=id_round2, anchor=200.0)

        # Each cam passes its own per-cam id_match gate.
        assert _status_device(client, "A")["time_synced"] is True
        assert _status_device(client, "B")["time_synced"] is True

        # …but they're locked onto different physical chirp events.
        # Pair-check must surface that.
        blockers = _status_blockers(client)
        assert any("sync ids mismatch" in b for b in blockers), blockers

        arm = client.post("/sessions/arm",
                          headers={"Accept": "application/json"})
        assert arm.status_code == 409


def test_quick_chirp_during_armed_session_is_a_no_op(fresh_state):
    """Safety: Quick chirp while a session is armed must not disrupt
    the recording. trigger_sync_command returns dispatched=[] and no
    anchors get cleared."""
    client = TestClient(app)
    _calibrate_both(client)

    with client.websocket_connect("/ws/device/A") as ws_a, \
         client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_a)
        _drain_initial_settings(ws_b)
        _hello(ws_a, "A", sync_id="sy_same", anchor=10.0)
        _hello(ws_b, "B", sync_id="sy_same", anchor=20.0)
        arm = client.post("/sessions/arm",
                          headers={"Accept": "application/json"})
        assert arm.status_code == 200
        # Drain the arm WS broadcast.
        assert ws_a.receive_json()["type"] == "arm"
        assert ws_b.receive_json()["type"] == "arm"

        a_before = main.state.device_snapshot("A")
        assert a_before is not None and a_before.time_sync_id == "sy_same"

        r = client.post("/sync/trigger", json={"camera_ids": ["A", "B"]})
        assert r.status_code == 200
        assert r.json()["dispatched_to"] == []

        # No clear should have happened — armed-session guard kept the
        # cam's anchor intact.
        a_after = main.state.device_snapshot("A")
        assert a_after is not None and a_after.time_sync_id == "sy_same"
        assert a_after.sync_anchor_timestamp_s == 10.0


def test_offline_cam_re_sync_then_pair_check_catches_drift(fresh_state):
    """Edge case: A goes offline mid-test, operator reruns Quick chirp
    while only B is online, B locks onto a new id, then A reconnects
    with its stale id. Per-cam gate passes for both (each matches its
    own expected) but ids differ → pair-check blocks."""
    client = TestClient(app)
    _calibrate_both(client)

    # Round 1 — both online, both lock to the same id.
    with client.websocket_connect("/ws/device/A") as ws_a, \
         client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_a)
        _drain_initial_settings(ws_b)
        _hello(ws_a, "A", sync_id=None, anchor=None)
        _hello(ws_b, "B", sync_id=None, anchor=None)
        client.post("/sync/trigger", json={"camera_ids": ["A", "B"]})
        cmd_a = ws_a.receive_json()
        cmd_b = ws_b.receive_json()
        assert cmd_a["sync_command_id"] == cmd_b["sync_command_id"]
        id_first = cmd_a["sync_command_id"]
        _heartbeat(ws_a, "A", sync_id=id_first, anchor=100.0)
        _heartbeat(ws_b, "B", sync_id=id_first, anchor=200.0)
        assert _status_blockers(client) == []

    # A drops. Force its registry entry stale by replacing with an
    # offline-marked record so /status's online filter excludes it.
    # (DeviceRegistry.mark_offline kicks last_seen back past the stale
    # threshold without dropping its sync id.)
    main.state._device_registry.mark_offline("A")

    # Round 2 — only B online, operator re-triggers, B locks new id.
    with client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_b)
        _heartbeat(ws_b, "B", sync_id=id_first, anchor=200.0)
        client.post("/sync/trigger", json={"camera_ids": ["B"]})
        cmd_b = ws_b.receive_json()
        id_second = cmd_b["sync_command_id"]
        assert id_second != id_first
        _heartbeat(ws_b, "B", sync_id=id_second, anchor=300.0)

    # Round 3 — A reconnects with its stale id_first, B still on
    # id_second. expected[A] is whatever round 1 left (id_first).
    with client.websocket_connect("/ws/device/A") as ws_a, \
         client.websocket_connect("/ws/device/B") as ws_b:
        _drain_initial_settings(ws_a)
        _drain_initial_settings(ws_b)
        _hello(ws_a, "A", sync_id=id_first, anchor=100.0)
        _hello(ws_b, "B", sync_id=id_second, anchor=300.0)

        # Both cams individually pass id_match (each echoes its own
        # expected), but readiness must surface the cross-cam drift.
        a = _status_device(client, "A")
        b = _status_device(client, "B")
        assert a["time_synced"] is True
        assert b["time_synced"] is True
        blockers = _status_blockers(client)
        assert any("sync ids mismatch" in b_ for b_ in blockers), blockers
