"""Dashboard-control tests: heartbeat registry, session state machine,
/status command dispatch, HTML-form endpoints.

State-only behaviour is unit-tested against a freshly-built `main.State`
instance with an injected clock so timeout logic runs in microseconds
instead of minutes. HTTP-level behaviour uses the global app + the
autouse `_reset_main_state` fixture from `conftest.py`.
"""
from __future__ import annotations

import json as _json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import chirp
import main
from conftest import sid
from main import app


# --- Device heartbeat + staleness ------------------------------------------


def test_heartbeat_registers_device(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    online = s.online_devices()
    assert [d.camera_id for d in online] == ["A"]


def test_online_devices_filters_stale_entries(tmp_path):
    """Injected clock so we can age devices deterministically."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    clock["now"] = 1000.5
    s.heartbeat("B")
    # Default stale threshold is 3 s; both within 0.5 s should still be fresh.
    assert {d.camera_id for d in s.online_devices()} == {"A", "B"}

    clock["now"] = 1005.0  # +5 s from A's heartbeat, +4.5 s from B's
    assert s.online_devices() == []


def test_online_devices_custom_threshold(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    clock["now"] = 1000.5
    assert [d.camera_id for d in s.online_devices(stale_after_s=1.0)] == ["A"]
    assert s.online_devices(stale_after_s=0.1) == []


def test_heartbeat_prunes_stale_entries_on_write(tmp_path):
    """A malformed client hammering /heartbeat with random camera_ids must
    not grow `_devices` without bound — entries older than the GC window
    get dropped on the next heartbeat write."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    for i in range(10):
        s.heartbeat(f"ghost_{i}")
    # Advance well past the GC window (main._DEVICE_GC_AFTER_S = 60 s).
    clock["now"] = 1000.0 + main._DEVICE_GC_AFTER_S + 1.0
    # One fresh heartbeat should sweep the 10 stale entries.
    s.heartbeat("A")
    assert set(s._devices.keys()) == {"A"}


def test_heartbeat_enforces_registry_cap(tmp_path):
    """If a client spams N > _DEVICE_REGISTRY_CAP distinct camera_ids within
    the GC window, the cap evicts the oldest so the dict never exceeds the
    cap size."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    cap = main._DEVICE_REGISTRY_CAP
    # Fire cap+5 unique ids, staggering timestamps so "oldest" is well-defined.
    for i in range(cap + 5):
        clock["now"] = 1000.0 + i * 0.01
        s.heartbeat(f"dev_{i:03d}")
    assert len(s._devices) == cap
    # The 5 oldest (dev_000..dev_004) should have been evicted, newest kept.
    assert "dev_000" not in s._devices
    assert f"dev_{cap + 4:03d}" in s._devices


# --- /chirp.wav caching ----------------------------------------------------


def test_chirp_wav_is_cached_across_calls(tmp_path):
    """Chirp synthesis is deterministic; the endpoint must return the same
    bytes from a cache instead of re-running the PCM generation each time."""
    # Reset the lru_cache so the first call below is guaranteed to populate.
    chirp.chirp_wav_bytes.cache_clear()
    client = TestClient(app)
    r1 = client.get("/chirp.wav")
    r2 = client.get("/chirp.wav")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content
    info = chirp.chirp_wav_bytes.cache_info()
    assert info.hits >= 1  # second call was a cache hit, not a recompute


# --- Session state machine -------------------------------------------------


def test_arm_session_creates_session_and_is_idempotent(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_a = s.arm_session()
    assert session_a.armed is True
    assert session_a.id.startswith("s_")
    # Double-click: second arm returns the same session (no double-arm bug).
    session_b = s.arm_session()
    assert session_b.id == session_a.id


def test_cancel_session_transitions_to_ended(tmp_path):
    s = main.State(data_dir=tmp_path)
    session = s.arm_session()
    ended = s.cancel_session()
    assert ended is not None
    assert ended.id == session.id
    assert ended.armed is False
    assert ended.end_reason == "cancelled"
    assert s.current_session() is None


def test_cancel_without_armed_session_returns_none(tmp_path):
    s = main.State(data_dir=tmp_path)
    assert s.cancel_session() is None


def test_session_times_out_automatically(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.arm_session(max_duration_s=5.0)
    assert s.current_session() is not None

    clock["now"] = 1004.9
    assert s.current_session() is not None  # still within window

    clock["now"] = 1006.0   # past max_duration
    assert s.current_session() is None

    # `_last_ended_session` should reflect the timeout reason so
    # commands_for_devices can emit disarm.
    assert s._last_ended_session is not None
    assert s._last_ended_session.end_reason == "timeout"


# --- Cross-camera command dispatch ----------------------------------------


def test_commands_dispatch_arm_to_online_devices_only(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    s.arm_session()
    # B never heartbeated, so it isn't online and gets no command.
    assert s.commands_for_devices() == {"A": "arm"}

    s.heartbeat("B")
    assert s.commands_for_devices() == {"A": "arm", "B": "arm"}


def test_commands_emit_disarm_after_session_ends(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")
    s.arm_session()
    s.cancel_session()

    # Immediately after cancel: both phones should see disarm.
    assert s.commands_for_devices() == {"A": "disarm", "B": "disarm"}

    # After _DISARM_ECHO_S (5 s) the command drops off — steady state, no cmd.
    clock["now"] = 1006.0
    assert s.commands_for_devices() == {}


def test_upload_ends_armed_session(tmp_path):
    """The key one-shot behaviour: first upload for the current session
    ends it with reason=cycle_uploaded. Uploads tagged with a different
    session_id (e.g. a phone flushing a prior armed window) don't disturb
    the currently armed session."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    s.heartbeat("B")
    session = s.arm_session()

    pitch_a = _minimal_pitch("A", session_id=session.id)
    s.record(pitch_a)

    # Session ended; `_last_ended_session` carries the upload record.
    assert s.current_session() is None
    assert s._last_ended_session is not None
    assert s._last_ended_session.id == session.id
    assert s._last_ended_session.end_reason == "cycle_uploaded"
    assert set(s._last_ended_session.uploads_received) == {"A"}


def test_upload_from_stale_session_does_not_disarm_current(tmp_path):
    """An upload arriving while a newer session is armed, but tagged with
    the previous session's id, must not end the newer session."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    first = s.arm_session()
    s.cancel_session()
    second = s.arm_session()

    # A phone flushing a recording tied to the already-cancelled session.
    stale = _minimal_pitch("A", session_id=first.id)
    s.record(stale)

    current = s.current_session()
    assert current is not None
    assert current.id == second.id
    assert current.armed is True


# --- HTTP endpoints --------------------------------------------------------


def test_heartbeat_endpoint_registers_device_and_returns_status():
    client = TestClient(app)
    r = client.post("/heartbeat", json={"camera_id": "A"})
    assert r.status_code == 200
    body = r.json()
    assert any(d["camera_id"] == "A" for d in body["devices"])
    # Shape check: heartbeat response carries the same /status fields.
    for key in ("state", "devices", "session", "commands"):
        assert key in body


def test_heartbeat_rejects_path_traversal_in_camera_id():
    client = TestClient(app)
    r = client.post("/heartbeat", json={"camera_id": "../etc"})
    assert r.status_code == 422


def test_status_surfaces_session_and_commands_during_arm():
    client = TestClient(app)
    client.post("/heartbeat", json={"camera_id": "A"})
    client.post("/heartbeat", json={"camera_id": "B"})

    assert client.post("/sessions/arm", headers={"Accept": "application/json"}).status_code == 200

    status = client.get("/status").json()
    assert status["session"] is not None
    assert status["session"]["armed"] is True
    assert status["commands"] == {"A": "arm", "B": "arm"}


def test_sessions_arm_cancel_json_api():
    client = TestClient(app)
    r = client.post(
        "/sessions/arm",
        headers={"Accept": "application/json"},
        params={"max_duration_s": 30.0},
    )
    assert r.status_code == 200
    session_id = r.json()["session"]["id"]

    r2 = client.post("/sessions/cancel", headers={"Accept": "application/json"})
    assert r2.status_code == 200
    assert r2.json()["session"]["id"] == session_id
    assert r2.json()["session"]["end_reason"] == "cancelled"


def test_sessions_cancel_returns_409_when_nothing_armed():
    client = TestClient(app)
    r = client.post("/sessions/cancel", headers={"Accept": "application/json"})
    assert r.status_code == 409


def test_sessions_arm_html_form_redirects_to_dashboard():
    """Browser form submission should redirect, not dump JSON on the page."""
    client = TestClient(app)
    r = client.post(
        "/sessions/arm",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_sessions_cancel_html_form_redirects_even_if_not_armed():
    """The dashboard cancel button should never look broken, even when
    pressed on an idle session — 303 redirect, not 409."""
    client = TestClient(app)
    r = client.post(
        "/sessions/cancel",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_pitch_upload_ends_armed_session_end_to_end():
    client = TestClient(app)
    client.post("/heartbeat", json={"camera_id": "A"})
    client.post("/heartbeat", json={"camera_id": "B"})
    arm_reply = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm_reply["session"]["id"]

    # Simulate A's phone uploading a pitch tagged with the armed session.
    body = _minimal_pitch_body("A", session_id=session_id)
    r = client.post("/pitch", data={"payload": _json.dumps(body)})
    assert r.status_code == 200

    # Session should be ended now; commands should carry disarm for B
    # (and for A too — the server broadcasts to every online device).
    status = client.get("/status").json()
    assert status["session"] is not None
    assert status["session"]["armed"] is False
    assert status["session"]["end_reason"] == "cycle_uploaded"
    assert status["commands"] == {"A": "disarm", "B": "disarm"}


def test_dashboard_renders_control_panel():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Nav brand + the three SSR-hydrated panel containers the JS polls into.
    assert "BALL_TRACKER" in body
    assert 'action="/sessions/arm"' in body
    assert 'action="/sessions/cancel"' in body
    assert 'id="devices-body"' in body
    assert 'id="session-body"' in body
    assert 'id="events-body"' in body
    assert 'id="scene-root"' in body


def test_dashboard_marks_expected_cameras_offline_when_absent():
    client = TestClient(app)
    r = client.get("/")
    # Neither A nor B have heartbeated → both rendered with the "Not seen"
    # caption and the idle/offline chip in the initial server-rendered
    # device list. `in` (not `count`) so incidental matches inside the
    # inline JS template don't throw off the assertion.
    body = r.text
    assert '<div class="id">A</div>' in body
    assert '<div class="id">B</div>' in body
    assert body.count('<div class="meta">Not seen</div>') >= 2


# --- Helpers ---------------------------------------------------------------


def _minimal_pitch(camera_id: str, session_id: str) -> main.PitchPayload:
    """A minimal server-internal PitchPayload with no calibration — enough
    for `State.record` to run, not enough for triangulation."""
    return main.PitchPayload(
        camera_id=camera_id,
        session_id=session_id,
        sync_anchor_frame_index=0,
        sync_anchor_timestamp_s=0.0,
        frames=[
            main.FramePayload(
                frame_index=0,
                timestamp_s=0.0,
                theta_x_rad=0.0,
                theta_z_rad=0.0,
                ball_detected=True,
            ),
        ],
    )


def _minimal_pitch_body(camera_id: str, session_id: str) -> dict:
    """Dict version for /pitch multipart form."""
    return _minimal_pitch(camera_id, session_id).model_dump()
