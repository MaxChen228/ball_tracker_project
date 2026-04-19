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


def test_stop_session_transitions_to_ended(tmp_path):
    s = main.State(data_dir=tmp_path)
    session = s.arm_session()
    ended = s.stop_session()
    assert ended is not None
    assert ended.id == session.id
    assert ended.armed is False
    assert s.current_session() is None


def test_stop_without_armed_session_returns_none(tmp_path):
    s = main.State(data_dir=tmp_path)
    assert s.stop_session() is None


def test_session_times_out_automatically(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.arm_session(max_duration_s=5.0)
    assert s.current_session() is not None

    clock["now"] = 1004.9
    assert s.current_session() is not None  # still within window

    clock["now"] = 1006.0   # past max_duration
    assert s.current_session() is None

    # `_last_ended_session` should be set so commands_for_devices can
    # emit disarm during the echo window.
    assert s._last_ended_session is not None
    assert s._last_ended_session.ended_at is not None


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
    s.stop_session()

    # Immediately after cancel: both phones should see disarm.
    assert s.commands_for_devices() == {"A": "disarm", "B": "disarm"}

    # After _DISARM_ECHO_S (5 s) the command drops off — steady state, no cmd.
    clock["now"] = 1006.0
    assert s.commands_for_devices() == {}


def test_upload_records_camera_in_armed_session(tmp_path):
    """An upload tagged with the current session adds that camera to
    `uploads_received`. It does NOT end the session — in the server-side
    detection pivot the phone only flushes after receiving disarm, so
    the session is always already ended by Stop / timeout before an
    upload arrives. This test exercises the rare case where the upload
    lands while the session is still nominally armed (e.g. a background
    race) and confirms the session stays armed."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    s.heartbeat("B")
    session = s.arm_session()

    pitch_a = _minimal_pitch("A", session_id=session.id)
    s.record(pitch_a)

    current = s.current_session()
    assert current is not None
    assert current.id == session.id
    assert current.armed is True
    assert set(current.uploads_received) == {"A"}


def test_upload_from_stale_session_does_not_disarm_current(tmp_path):
    """An upload arriving while a newer session is armed, but tagged with
    the previous session's id, must not end the newer session."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    first = s.arm_session()
    s.stop_session()
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


def test_sessions_arm_stop_json_api():
    client = TestClient(app)
    r = client.post(
        "/sessions/arm",
        headers={"Accept": "application/json"},
        params={"max_duration_s": 30.0},
    )
    assert r.status_code == 200
    session_id = r.json()["session"]["id"]

    r2 = client.post("/sessions/stop", headers={"Accept": "application/json"})
    assert r2.status_code == 200
    assert r2.json()["session"]["id"] == session_id
    assert r2.json()["session"]["armed"] is False


def test_sessions_stop_returns_409_when_nothing_armed():
    client = TestClient(app)
    r = client.post("/sessions/stop", headers={"Accept": "application/json"})
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


def test_sessions_stop_html_form_redirects_even_if_not_armed():
    """The dashboard Stop button should never look broken, even when
    pressed on an idle session — 303 redirect, not 409."""
    client = TestClient(app)
    r = client.post(
        "/sessions/stop",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_pitch_upload_keeps_session_armed_until_stop():
    """Post-pivot, an upload does NOT end the session — only an
    explicit Stop (or the server-side timeout) does. The phone only
    flushes after receiving disarm, so in practice uploads always land
    on already-ended sessions; this test covers the rare in-flight
    case and asserts the session stays armed."""
    client = TestClient(app)
    client.post("/heartbeat", json={"camera_id": "A"})
    client.post("/heartbeat", json={"camera_id": "B"})
    arm_reply = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm_reply["session"]["id"]

    main.state.record(_minimal_pitch("A", session_id=session_id))

    status = client.get("/status").json()
    assert status["session"] is not None
    assert status["session"]["armed"] is True
    assert status["session"]["ended_at"] is None
    assert status["commands"] == {"A": "arm", "B": "arm"}
    assert set(status["session"]["uploads_received"]) == {"A"}


# --- Capture mode (mode-one / mode-two dashboard toggle) -------------------


def test_default_mode_is_camera_only(tmp_path):
    s = main.State(data_dir=tmp_path)
    assert s.current_mode().value == "camera_only"


def test_set_mode_changes_current_mode(tmp_path):
    from schemas import CaptureMode
    s = main.State(data_dir=tmp_path)
    s.set_mode(CaptureMode.on_device)
    assert s.current_mode() == CaptureMode.on_device


def test_arm_session_snapshots_current_mode(tmp_path):
    from schemas import CaptureMode
    s = main.State(data_dir=tmp_path)
    s.set_mode(CaptureMode.on_device)
    session = s.arm_session()
    assert session.mode == CaptureMode.on_device


def test_mode_change_after_arm_does_not_affect_armed_session(tmp_path):
    from schemas import CaptureMode
    s = main.State(data_dir=tmp_path)
    session = s.arm_session()
    # Dashboard operator flips the toggle mid-session.
    s.set_mode(CaptureMode.on_device)
    assert session.mode == CaptureMode.camera_only  # snapshot frozen
    assert s.current_mode() == CaptureMode.on_device  # global flipped for next arm


def test_status_includes_capture_mode():
    client = TestClient(app)
    status = client.get("/status").json()
    assert status["capture_mode"] == "camera_only"


def test_heartbeat_reply_includes_capture_mode():
    client = TestClient(app)
    r = client.post("/heartbeat", json={"camera_id": "A"})
    assert r.json()["capture_mode"] == "camera_only"


def test_set_mode_endpoint_persists_on_device_choice():
    client = TestClient(app)
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "on_device"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["capture_mode"] == "on_device"
    # Round-trips via status.
    status = client.get("/status").json()
    assert status["capture_mode"] == "on_device"


def test_set_mode_endpoint_rejects_invalid_value():
    client = TestClient(app)
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "lightning_fast"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400


def test_set_mode_endpoint_html_form_redirects():
    client = TestClient(app)
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "on_device"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_session_to_dict_includes_mode(tmp_path):
    from schemas import CaptureMode
    s = main.State(data_dir=tmp_path)
    s.set_mode(CaptureMode.on_device)
    session = s.arm_session()
    assert session.to_dict()["mode"] == "on_device"


def test_delete_session_removes_memory_and_disk_artefacts(tmp_path):
    s = main.State(data_dir=tmp_path)
    pitch_a = _minimal_pitch("A", session_id=sid(1))
    pitch_b = _minimal_pitch("B", session_id=sid(1))
    s.record(pitch_a)
    s.record(pitch_b)
    # Drop a fake video clip into the expected location so the cleanup
    # also exercises the video glob.
    video_path = tmp_path / "videos" / f"session_{sid(1)}_A.mov"
    video_path.write_bytes(b"fake mov")

    assert (tmp_path / "pitches" / f"session_{sid(1)}_A.json").exists()
    assert (tmp_path / "results" / f"session_{sid(1)}.json").exists()

    assert s.delete_session(sid(1)) is True

    assert ("A", sid(1)) not in s.pitches
    assert ("B", sid(1)) not in s.pitches
    assert sid(1) not in s.results
    assert not (tmp_path / "pitches" / f"session_{sid(1)}_A.json").exists()
    assert not (tmp_path / "pitches" / f"session_{sid(1)}_B.json").exists()
    assert not (tmp_path / "results" / f"session_{sid(1)}.json").exists()
    assert not video_path.exists()


def test_delete_unknown_session_returns_false(tmp_path):
    s = main.State(data_dir=tmp_path)
    assert s.delete_session(sid(99)) is False


def test_delete_clears_last_ended_session_pointer(tmp_path):
    """`session_snapshot` surfaces the last-ended session even after it's
    been deleted; clearing the pointer ensures the dashboard doesn't show
    a ghost session chip for a session whose files are gone."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    armed = s.arm_session()
    s.stop_session()

    pitch = _minimal_pitch("A", session_id=armed.id)
    s.record(pitch)

    assert s.delete_session(armed.id) is True
    assert s.session_snapshot() is None


def test_clear_last_ended_session(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.arm_session()
    s.stop_session()
    assert s.session_snapshot() is not None

    assert s.clear_last_ended_session() is True
    assert s.session_snapshot() is None
    # Second call is a no-op.
    assert s.clear_last_ended_session() is False


def test_clear_refuses_while_armed(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.arm_session()
    assert s.clear_last_ended_session() is False
    assert s.session_snapshot() is not None


def test_sessions_clear_html_redirect():
    main.state.reset()
    main.state.arm_session()
    main.state.stop_session()

    client = TestClient(app)
    r = client.post(
        "/sessions/clear",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert main.state.session_snapshot() is None


def test_sessions_clear_json_409_when_nothing_to_clear():
    main.state.reset()
    client = TestClient(app)
    r = client.post(
        "/sessions/clear",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 409


def test_delete_refuses_armed_session(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A")
    armed = s.arm_session()
    with pytest.raises(RuntimeError, match="armed"):
        s.delete_session(armed.id)


def test_sessions_delete_html_form_redirects():
    client = TestClient(app)
    main.state.record(_minimal_pitch("A", session_id=sid(2)))
    assert sid(2) in main.state.results

    r = client.post(
        f"/sessions/{sid(2)}/delete",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert sid(2) not in main.state.results


def test_sessions_delete_json_api():
    client = TestClient(app)
    main.state.record(_minimal_pitch("A", session_id=sid(3)))

    r = client.post(
        f"/sessions/{sid(3)}/delete",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "session_id": sid(3)}
    assert sid(3) not in main.state.results


def test_sessions_delete_json_returns_404_for_unknown():
    client = TestClient(app)
    r = client.post(
        f"/sessions/{sid(4)}/delete",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_sessions_delete_json_returns_409_when_armed():
    client = TestClient(app)
    client.post("/heartbeat", json={"camera_id": "A"})
    armed = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()["session"]

    r = client.post(
        f"/sessions/{armed['id']}/delete",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 409


def test_sessions_delete_rejects_malformed_id_json():
    client = TestClient(app)
    r = client.post(
        "/sessions/bad..id/delete",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 422


def test_sessions_delete_malformed_html_redirects():
    """Dashboard never sends a malformed id, but if a hand-edited URL
    lands one, redirect instead of surfacing a 422 page."""
    client = TestClient(app)
    r = client.post(
        "/sessions/bad..id/delete",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_dashboard_events_list_renders_delete_form():
    client = TestClient(app)
    main.state.record(_minimal_pitch("A", session_id=sid(5)))
    body = client.get("/").text
    assert f'action="/sessions/{sid(5)}/delete"' in body
    assert 'class="event-delete"' in body


def test_dashboard_renders_control_panel():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Nav brand + the three SSR-hydrated panel containers the JS polls into.
    assert "BALL_TRACKER" in body
    assert 'action="/sessions/arm"' in body
    assert 'action="/sessions/stop"' in body
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
    for `State.record` to run, not enough for triangulation. Frames are
    populated to keep `events()` counts meaningful without going through
    the /pitch detection path."""
    return main.PitchPayload(
        camera_id=camera_id,
        session_id=session_id,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=[
            main.FramePayload(
                frame_index=0,
                timestamp_s=0.0,
                px=100.0, py=100.0,
                ball_detected=True,
            ),
        ],
    )
