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
from schemas import SyncReport, TrackingExposureCapMode


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
    """A malformed client hammering state.heartbeat() with random camera_ids
    must not grow the device registry without bound — entries older than the
    GC window get dropped on the next heartbeat write. (Post-/heartbeat-
    endpoint retirement the same invariant applies to WS connect / `hello`
    writes.)"""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    for i in range(10):
        s.heartbeat(f"ghost_{i}")
    # Advance well past the GC window (main._DEVICE_GC_AFTER_S = 60 s).
    clock["now"] = 1000.0 + main._DEVICE_GC_AFTER_S + 1.0
    # One fresh heartbeat should sweep the 10 stale entries.
    s.heartbeat("A")
    assert set(s._device_registry.devices.keys()) == {"A"}


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
    assert len(s._device_registry.devices) == cap
    # The 5 oldest (dev_000..dev_004) should have been evicted, newest kept.
    assert "dev_000" not in s._device_registry.devices
    assert f"dev_{cap + 4:03d}" in s._device_registry.devices


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


def test_chirp_wav_has_dual_burst_shape():
    """The reference waveform is two 100 ms sweeps separated by 50 ms of
    silence. Detector-side Doppler cancellation depends on exactly that gap
    geometry — if the WAV layout drifts, every phone in the field would need
    a matching re-flash, so pin the shape here."""
    chirp.chirp_wav_bytes.cache_clear()
    wav = chirp.chirp_wav_bytes()
    # Skip the 44-byte RIFF header and parse as int16 PCM.
    pcm = np.frombuffer(wav[44:], dtype=np.int16).astype(np.float32) / 32768.0
    sr = 44100

    # Envelope: per-10-ms energy.
    win = sr // 100
    energy = np.array(
        [np.sum(pcm[i : i + win] ** 2) for i in range(0, len(pcm) - win, win)]
    )
    active = energy > (energy.max() * 0.05)

    # Find contiguous active regions.
    regions: list[tuple[int, int]] = []
    i = 0
    while i < len(active):
        if active[i]:
            j = i
            while j < len(active) and active[j]:
                j += 1
            regions.append((i, j))
            i = j
        else:
            i += 1

    assert len(regions) == 2, f"expected exactly 2 chirp bursts, got {len(regions)}"
    # Each burst ≈ 100 ms = 10 windows; tolerate ±3 windows for Hann taper.
    for start, end in regions:
        assert 7 <= (end - start) <= 13
    # Gap between bursts ≈ 50 ms = 5 windows; tolerate ±3.
    gap = regions[1][0] - regions[0][1]
    assert 2 <= gap <= 8


# --- Session state machine -------------------------------------------------


def test_arm_session_creates_session_and_is_idempotent(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_a = s.arm_session()
    assert session_a.armed is True
    assert session_a.id.startswith("s_")
    # Double-click: second arm returns the same session (no double-arm bug).
    session_b = s.arm_session()
    assert session_b.id == session_a.id


def test_arm_session_snapshots_common_time_sync_id(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef")
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef")
    session = s.arm_session()
    assert session.sync_id == "sy_deadbeef"


def test_arm_session_drops_mismatched_time_sync_ids(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_aaaaaaaa")
    s.heartbeat("B", time_synced=True, time_sync_id="sy_bbbbbbbb")
    session = s.arm_session()
    assert session.sync_id is None


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

    # The recently-ended sessions ring should have the timed-out session
    # so commands_for_devices can emit disarm during the echo window.
    assert len(s._recently_ended_sessions) == 1
    assert s._recently_ended_sessions[0].ended_at is not None


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


# HTTP /heartbeat endpoint retired — registration + shape coverage now
# lives in WS tests (ws_device connect handler) and /status tests. The
# old two tests (endpoint_registers_device_and_returns_status,
# rejects_path_traversal_in_camera_id) are dropped; path-traversal is
# enforced by the WS route via _validate_camera_id_or_422.


def test_status_surfaces_session_and_commands_during_arm():
    client = TestClient(app)
    # Post-retirement: register devices via state.heartbeat directly (the
    # HTTP endpoint is gone; WS carries the live-path equivalent). /status
    # still derives `commands` from the device registry + session state,
    # so the dashboard path is unchanged.
    main.state.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    main.state.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    _seed_minimal_calibration("A")
    _seed_minimal_calibration("B")

    assert client.post("/sessions/arm", headers={"Accept": "application/json"}).status_code == 200

    status = client.get("/status").json()
    assert status["session"] is not None
    assert status["session"]["armed"] is True
    assert status["commands"] == {"A": "arm", "B": "arm"}


def test_sessions_arm_stop_json_api():
    client = TestClient(app)
    main.state.heartbeat("A")
    _seed_minimal_calibration("A")
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
    main.state.heartbeat("A")
    _seed_minimal_calibration("A")
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


def test_sessions_stop_html_form_broadcasts_disarm_before_redirect(monkeypatch):
    client = TestClient(app)
    main.state.heartbeat("A")
    session = main.state.arm_session()
    broadcasts: list[dict[str, dict[str, object]]] = []
    events: list[tuple[str, dict[str, object]]] = []

    class _CaptureDeviceWS:
        async def broadcast(self, message_by_camera: dict[str, dict[str, object]]) -> None:
            broadcasts.append(message_by_camera)

        def snapshot(self) -> dict[str, object]:
            return {}

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict[str, object]) -> None:
            events.append((event, data))

    monkeypatch.setattr(main, "device_ws", _CaptureDeviceWS())
    monkeypatch.setattr(main, "sse_hub", _CaptureHub())

    r = client.post(
        "/sessions/stop",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert broadcasts == [{"A": {"type": "disarm", "sid": session.id}}]
    assert events and events[0][0] == "session_ended"


def test_pitch_upload_keeps_session_armed_until_stop():
    """Post-pivot, an upload does NOT end the session — only an
    explicit Stop (or the server-side timeout) does. The phone only
    flushes after receiving disarm, so in practice uploads always land
    on already-ended sessions; this test covers the rare in-flight
    case and asserts the session stays armed."""
    client = TestClient(app)
    main.state.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    main.state.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    _seed_minimal_calibration("A")
    _seed_minimal_calibration("B")
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


# --- Capture mode (legacy CaptureMode enum) --------------------------------


def test_status_does_not_include_capture_mode():
    """CaptureMode was retired and the wire-compat string was removed
    — /status no longer exposes `capture_mode` and the dashboard JS
    no longer reads it."""
    client = TestClient(app)
    status = client.get("/status").json()
    assert "capture_mode" not in status


def test_set_mode_endpoint_removed():
    """The legacy /sessions/set_mode toggle was retired alongside
    CaptureMode — only one value ever shipped and the dashboard no
    longer surfaces a picker."""
    client = TestClient(app)
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "camera_only"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_default_tracking_exposure_cap_is_frame_duration(tmp_path):
    s = main.State(data_dir=tmp_path)
    assert s.tracking_exposure_cap() == TrackingExposureCapMode.frame_duration


def test_arm_session_snapshots_tracking_exposure_cap(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.set_tracking_exposure_cap(TrackingExposureCapMode.shutter_1000)
    session = s.arm_session()
    assert session.tracking_exposure_cap == TrackingExposureCapMode.shutter_1000


def test_tracking_exposure_change_after_arm_does_not_affect_armed_session(tmp_path):
    s = main.State(data_dir=tmp_path)
    session = s.arm_session()
    s.set_tracking_exposure_cap(TrackingExposureCapMode.shutter_500)
    assert session.tracking_exposure_cap == TrackingExposureCapMode.frame_duration
    assert s.tracking_exposure_cap() == TrackingExposureCapMode.shutter_500


def test_status_surfaces_tracking_exposure_cap():
    # /heartbeat retirement: the field now lives on /status (and on the
    # WS settings message broadcast to each device). Previous two-way
    # coverage collapses to one-way.
    client = TestClient(app)
    assert client.get("/status").json()["tracking_exposure_cap"] == "frame_duration"


def test_events_tags_mode_one_when_video_on_disk(tmp_path):
    """Mode-one session: a MOV file under data/videos/ flips the event's
    `mode` tag to `camera_only`, so the dashboard chip reads correctly."""
    s = main.State(data_dir=tmp_path)
    pitch = _minimal_pitch("A", session_id=sid(700))
    s.record(pitch)
    # Pretend the video-write path ran (main.py normally does this during
    # /pitch; here we just drop a dummy MOV into the right dir).
    (tmp_path / "videos").mkdir(exist_ok=True)
    (tmp_path / "videos" / f"session_{sid(700)}_A.mov").write_bytes(b"fake")

    events = s.events()
    match = [e for e in events if e["session_id"] == sid(700)]
    assert match, events
    assert match[0]["mode"] == "camera_only"


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


def test_lookup_session_locked_finds_through_recently_ended_ring(tmp_path):
    """Late frames from cam A draining its detection backlog after disarm
    must still resolve back to session A even when the operator has already
    armed (and ended) session B in the meantime. The single-slot
    `_last_ended_session` predecessor would lose A's reference here."""
    s = main.State(data_dir=tmp_path)

    a_session = s.arm_session()
    s.stop_session()
    assert s._lookup_session_locked(a_session.id) is a_session

    b_session = s.arm_session()
    # While B is armed, lookups for either session work — A is in the ring,
    # B is current.
    assert s._lookup_session_locked(a_session.id) is a_session
    assert s._lookup_session_locked(b_session.id) is b_session

    s.stop_session()
    # Both sessions ended → ring holds both, current is None. A's drain
    # frames can still find their session even though B has displaced A as
    # "most recent".
    assert s._lookup_session_locked(a_session.id) is a_session
    assert s._lookup_session_locked(b_session.id) is b_session
    # Most recent still surfaces as B.
    assert s._most_recent_ended_session_locked() is b_session


def test_recently_ended_sessions_ring_evicts_oldest_at_maxlen(tmp_path):
    """The ring's maxlen=4 caps memory; the oldest session is silently
    dropped. Beyond the ring, a stale lookup returns None and the caller
    is expected to fall back to disk (pitches / results)."""
    s = main.State(data_dir=tmp_path)
    sessions = []
    # Each `stop_session` synchronously frees `_current_session` before
    # the next `arm_session` runs, so this tight loop deterministically
    # walks the ring without overlapping armed-window guards.
    for _ in range(5):
        sess = s.arm_session()
        s.stop_session()
        sessions.append(sess)

    # Oldest (sessions[0]) was evicted; the next four remain.
    assert s._lookup_session_locked(sessions[0].id) is None
    for sess in sessions[1:]:
        assert s._lookup_session_locked(sess.id) is sess


def test_delete_session_removes_target_from_ring_only(tmp_path):
    """Deleting one session must not collapse the rest of the ring — its
    purpose is to keep multi-session lookup working through deletes."""
    s = main.State(data_dir=tmp_path)
    a = s.arm_session()
    s.stop_session()
    b = s.arm_session()
    s.stop_session()
    # `delete_session` only returns True when there are pitches/results to
    # remove; we pre-record a pitch for A so the call exercises the ring
    # cleanup branch on a real removal, mirroring
    # `test_delete_clears_last_ended_session_pointer`.
    s.record(_minimal_pitch("A", session_id=a.id))

    assert s.delete_session(a.id) is True
    assert s._lookup_session_locked(a.id) is None
    assert s._lookup_session_locked(b.id) is b
    assert s._most_recent_ended_session_locked() is b
    # Ring rebuild preserved B without accidentally collapsing both.
    assert len(s._recently_ended_sessions) == 1


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


def test_record_does_not_resurrect_deleted_session(tmp_path, monkeypatch):
    """A `delete_session` racing with the middle of a `record` call must
    NOT come back to life via the result publish.

    record() releases the lock between CS1 (mutate pitches) and CS2
    (publish result). If delete_session lands in that window,
    `_atomic_write` of the result JSON + the CS2 republish would
    otherwise resurrect the session. We simulate the race by patching
    rebuild_result_for_session to call delete_session() mid-call;
    record() must observe the deletion via its re-check guards and
    discard the result publish."""
    s = main.State(data_dir=tmp_path)
    pitch = _minimal_pitch("A", session_id=sid(900))

    import session_results as _sr
    real_rebuild = _sr.rebuild_result_for_session

    def rebuild_then_delete(state, session_id):
        result = real_rebuild(state, session_id)
        state.delete_session(session_id)
        return result

    monkeypatch.setattr(_sr, "rebuild_result_for_session", rebuild_then_delete)

    s.record(pitch)

    # delete_session ran mid-record; record's guards must have aborted
    # the publish. Both in-memory maps and on-disk artefacts should
    # reflect the deletion, not a resurrected session.
    assert ("A", sid(900)) not in s.pitches
    assert sid(900) not in s.results
    assert not (tmp_path / "results" / f"session_{sid(900)}.json").exists()
    assert not (tmp_path / "pitches" / f"session_{sid(900)}_A.json").exists()


def test_store_result_skips_unknown_session(tmp_path):
    """`store_result` on a session that isn't in pitches OR results must
    not write the result JSON or republish into memory — the session
    has been deleted (or never recorded), and resurrecting it would
    surface a ghost in /events."""
    s = main.State(data_dir=tmp_path)
    pitch = _minimal_pitch("A", session_id=sid(901))
    s.record(pitch)
    result = s.results[sid(901)]
    assert s.delete_session(sid(901)) is True

    # Session is gone from both maps + disk. store_result must be a no-op.
    s.store_result(result)
    assert sid(901) not in s.results
    assert not (tmp_path / "results" / f"session_{sid(901)}.json").exists()


def test_store_result_skips_live_only_session_after_delete(tmp_path):
    """`store_result` guard includes `_live_pairings`; for the guard
    to actually fire on a live-only WS session that was deleted,
    `delete_session` must also purge `_live_pairings`. Otherwise a
    live-only ghost would slip past the guard."""
    from schemas import SessionResult
    from live_pairing import LivePairingSession

    s = main.State(data_dir=tmp_path)
    # Synthesise a live-only session by directly populating
    # `_live_pairings` — same shape as ingest_live_frame would produce,
    # no /pitch upload, no entries in `pitches` or `results`.
    s._live_pairings[sid(902)] = LivePairingSession(sid(902))
    assert sid(902) in s._live_pairings
    assert s.delete_session(sid(902)) is True
    assert sid(902) not in s._live_pairings, (
        "delete_session must purge _live_pairings or the store_result "
        "guard's live-path check is ineffective"
    )

    # store_result on the dead session must noop — guard sees no
    # pitches, no results, no live pairing.
    fake_result = SessionResult(
        session_id=sid(902),
        camera_a_received=False,
        camera_b_received=False,
    )
    s.store_result(fake_result)
    assert sid(902) not in s.results
    assert not (tmp_path / "results" / f"session_{sid(902)}.json").exists()


def test_state_get_rebuilds_lazily_for_live_only_session(tmp_path):
    """Per-frame rebuild was retired from the WS frame loop; mid-stream
    GET /results/{sid} (which calls state.get) must still return a
    fresh result by rebuilding against the current live snapshot
    instead of a 404."""
    from live_pairing import LivePairingSession
    s = main.State(data_dir=tmp_path)
    # Live-only session: in `_live_pairings`, NOT yet in `results`.
    s._live_pairings[sid(950)] = LivePairingSession(sid(950))
    assert sid(950) not in s.results

    r = s.get(sid(950))
    assert r is not None, (
        "state.get must lazy-rebuild for an active live session — otherwise "
        "viewer mid-stream sees stale 404 once per-frame publish is gone"
    )
    assert r.session_id == sid(950)


def test_state_get_returns_none_for_unknown_session(tmp_path):
    """state.get must still return None for sessions with no live data
    AND no published result — the lazy rebuild path is gated on
    `_live_pairings` membership, so it doesn't resurrect ghosts."""
    s = main.State(data_dir=tmp_path)
    assert s.get(sid(951)) is None


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


def test_trash_session_hides_from_active_events_and_restore_brings_it_back(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.record(_minimal_pitch("A", session_id=sid(6)))

    assert [e["session_id"] for e in s.events(bucket="active")] == [sid(6)]
    assert s.trash_session(sid(6)) is True
    assert s.events(bucket="active") == []
    trash = s.events(bucket="trash")
    assert [e["session_id"] for e in trash] == [sid(6)]
    assert trash[0]["trashed"] is True

    assert s.restore_session(sid(6)) is True
    assert [e["session_id"] for e in s.events(bucket="active")] == [sid(6)]


def test_cancel_and_resume_processing_summary(tmp_path):
    s = main.State(data_dir=tmp_path)
    pitch = _minimal_pitch("A", session_id=sid(7)).model_copy(deep=True)
    pitch.frames_server_post = []
    pitch.paths = [main.DetectionPath.server_post.value]
    s.record(pitch)
    (tmp_path / "videos" / f"session_{sid(7)}_A.mov").write_bytes(b"fake mov")

    s._processing.mark_server_post_queued(sid(7), "A")
    status, resumable = s._processing.session_summary(sid(7))
    assert status == "queued"
    assert resumable is True

    assert s._processing.cancel_processing(sid(7)) is True
    status, resumable = s._processing.session_summary(sid(7))
    assert status == "canceled"
    assert resumable is True

    queued = s._processing.resume_processing(sid(7))
    assert len(queued) == 1
    status, resumable = s._processing.session_summary(sid(7))
    assert status == "queued"
    assert resumable is True


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


def test_sessions_trash_and_restore_json_api():
    client = TestClient(app)
    main.state.record(_minimal_pitch("A", session_id=sid(33)))

    trash = client.post(
        f"/sessions/{sid(33)}/trash",
        headers={"Accept": "application/json"},
    )
    assert trash.status_code == 200
    assert trash.json() == {"ok": True, "session_id": sid(33)}
    assert [e["session_id"] for e in main.state.events(bucket="trash")] == [sid(33)]

    restore = client.post(
        f"/sessions/{sid(33)}/restore",
        headers={"Accept": "application/json"},
    )
    assert restore.status_code == 200
    assert restore.json() == {"ok": True, "session_id": sid(33)}
    assert [e["session_id"] for e in main.state.events(bucket="active")] == [sid(33)]


def test_sessions_cancel_and_run_server_post_json_api(tmp_path):
    client = TestClient(app)
    pitch = _minimal_pitch("A", session_id=sid(34)).model_copy(deep=True)
    pitch.frames_server_post = []
    pitch.paths = [main.DetectionPath.server_post.value]
    main.state.record(pitch)
    (main.state.video_dir / f"session_{sid(34)}_A.mov").write_bytes(b"fake mov")
    main.state.processing.mark_server_post_queued(sid(34), "A")

    cancel = client.post(
        f"/sessions/{sid(34)}/cancel_processing",
        headers={"Accept": "application/json"},
    )
    assert cancel.status_code == 200
    assert cancel.json() == {"ok": True, "session_id": sid(34)}

    resume = client.post(
        f"/sessions/{sid(34)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "live"},
    )
    assert resume.status_code == 200
    assert resume.json()["ok"] is True
    assert resume.json()["queued"] == 1
    assert resume.json()["source"] == "live"


def _arm_minimal_session_for_run_server_post(session_id: str) -> None:
    """Shared fixture for the /run_server_post body-validation tests below.
    Records one server_post-pending pitch with a fake MOV so the endpoint
    has something to enqueue (or, for the negative tests, gets past the
    `_SESSION_ID_RE` check before failing on the body)."""
    pitch = _minimal_pitch("A", session_id=session_id).model_copy(deep=True)
    pitch.frames_server_post = []
    pitch.paths = [main.DetectionPath.server_post.value]
    main.state.record(pitch)
    (main.state.video_dir / f"session_{session_id}_A.mov").write_bytes(b"fake mov")
    main.state.processing.mark_server_post_queued(session_id, "A")


def test_run_server_post_missing_source_field_returns_400(tmp_path):
    """Per CLAUDE.md no-silent-fallback: empty body must NOT default to
    'live'. Caller must specify source explicitly so research provenance
    is auditable from the request log alone."""
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(35))
    r = client.post(
        f"/sessions/{sid(35)}/run_server_post",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert "source" in r.json()["detail"]


def test_run_server_post_unknown_source_returns_400(tmp_path):
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(36))
    r = client.post(
        f"/sessions/{sid(36)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "magical_default"},
    )
    assert r.status_code == 400, r.text
    assert "magical_default" in r.json()["detail"]


def test_run_server_post_unknown_preset_returns_400(tmp_path):
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(37))
    r = client.post(
        f"/sessions/{sid(37)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "preset:no_such_preset"},
    )
    assert r.status_code == 400, r.text
    assert "no_such_preset" in r.json()["detail"]


def test_run_server_post_frozen_without_snapshot_returns_409(tmp_path):
    """`source=frozen` requires per-pitch `*_used` snapshots from PR #93
    stamping. _minimal_pitch is a pre-stamping fixture (no snapshots),
    so the route must reject — not silently fall back to live."""
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(38))
    r = client.post(
        f"/sessions/{sid(38)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "frozen"},
    )
    assert r.status_code == 409, r.text
    assert "frozen" in r.json()["detail"]


def test_run_server_post_frozen_failure_does_not_zombie_job_state(tmp_path):
    """Regression for the pre-flight ordering bug caught in phase 2
    review: if `_resolve_detection_config` raised AFTER
    `resume_processing` had already mutated job states to "queued",
    the failed cams stayed stuck in "queued" forever (no
    BackgroundTask backing them, no operator path to clear). The fix
    is to validate config resolution against `session_candidates`
    BEFORE `resume_processing`. This test pins that ordering: the 409
    must leave the job in its pre-request state so a subsequent
    `source=live` retry works without `cancel_processing` first."""
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(40))
    # First: source=frozen must 409 because _minimal_pitch has no
    # *_used snapshot.
    r1 = client.post(
        f"/sessions/{sid(40)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "frozen"},
    )
    assert r1.status_code == 409, r1.text
    # Second: a follow-up `source=live` request must succeed without
    # any intervening cancel. If pre-flight ordering is wrong, the cam
    # is stuck in "queued" and resume_processing returns nothing → 409
    # "no resumable processing" instead of 200.
    r2 = client.post(
        f"/sessions/{sid(40)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "live"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["queued"] == 1
    assert r2.json()["source"] == "live"


def test_run_server_post_preset_blue_ball_enqueues(tmp_path):
    """`source=preset:blue_ball` should enqueue without mutating disk
    HSV / shape_gate — the operator's live dashboard config is
    untouched, satisfying the research-compare workflow this redesign
    exists for."""
    client = TestClient(app)
    _arm_minimal_session_for_run_server_post(sid(39))
    hsv_before = main.state.hsv_range()
    sg_before = main.state.shape_gate()
    r = client.post(
        f"/sessions/{sid(39)}/run_server_post",
        headers={"Accept": "application/json"},
        json={"source": "preset:blue_ball"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 1
    assert r.json()["source"] == "preset:blue_ball"
    # Sub-knobs untouched — research compare must not drift the
    # operator's dashboard state on any axis.
    assert main.state.hsv_range() == hsv_before
    assert main.state.shape_gate() == sg_before


def test_resolve_detection_config_preset_uses_preset_shape():
    """A preset carries (HSV + shape_gate). Resolving `source=preset:NAME`
    must return the preset's own shape_gate, **not** the state's current
    value. Earlier the preset only carried HSV and shape_gate leaked
    from state, so a concurrent dashboard slider edit on shape_gate
    would silently shift the cost basis of an in-flight research-compare
    reprocess. (Selector cost weights are now `_W_ASPECT` / `_W_FILL`
    constants — no leak path remains.)"""
    from detection import ShapeGate
    from presets import PRESETS
    from routes.sessions import _resolve_detection_config

    # Build a fake state whose shape_gate deliberately disagrees with
    # every preset, so a leak from state would be observable as the
    # wrong value coming back.
    class _FakeState:
        def hsv_range(self):
            return PRESETS["tennis"].hsv  # irrelevant for this test
        def shape_gate(self):
            return ShapeGate(aspect_min=0.01, fill_min=0.01)

    fake_pitch = _minimal_pitch("A", session_id=sid(41))

    for preset_name in ("tennis", "blue_ball"):
        hsv, gate, _tuning, label = _resolve_detection_config(
            f"preset:{preset_name}", fake_pitch, _FakeState()
        )
        preset = PRESETS[preset_name]
        assert hsv == preset.hsv
        assert gate == preset.shape_gate, (
            f"preset:{preset_name} leaked shape_gate from state instead of "
            f"using the preset's own ({preset.shape_gate})"
        )
        assert label == f"preset:{preset_name}"


def test_sessions_delete_json_returns_404_for_unknown():
    client = TestClient(app)
    r = client.post(
        f"/sessions/{sid(4)}/delete",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_sessions_delete_json_returns_409_when_armed():
    client = TestClient(app)
    main.state.heartbeat("A")
    _seed_minimal_calibration("A")
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


def test_dashboard_events_list_renders_trash_form():
    client = TestClient(app)
    main.state.record(_minimal_pitch("A", session_id=sid(5)))
    body = client.get("/").text
    assert f'action="/sessions/{sid(5)}/trash"' in body
    assert 'data-events-bucket="trash"' in body


def test_dashboard_renders_control_panel():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # `/` is operational-only now: Session + Events + 3D canvas. Devices,
    # calibration, extended markers, and tuning all live on /setup.
    assert "BALL_TRACKER" in body
    assert 'action="/sessions/arm"' in body
    assert 'action="/sessions/stop"' in body
    assert "/sessions/cancel" not in body
    assert 'id="session-body"' in body
    assert 'id="events-body"' in body
    assert 'id="scene-root"' in body
    assert 'href="/setup"' in body
    assert 'href="/markers"' in body


def test_dashboard_no_longer_renders_detection_path_picker():
    """Detection Paths picker was removed — live is always on, server_post
    is now an on-demand action on the events list. The Session Monitor
    card was also retired; during streaming the operator only watches
    the 3D canvas on the right."""
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="active-body"' not in body
    assert 'action="/detection/paths"' not in body
    assert 'id="paths-form"' not in body


def test_dashboard_renders_unified_detection_config_card():
    """Phase 3 of unified-config redesign: single form, single Apply,
    identity header. The legacy three Apply buttons are gone — the JS
    handler in `static/dashboard/15_hsv_controls.js` POSTs the full
    triple to `/detection/config` on a single submit."""
    client = TestClient(app)
    body = client.get("/").text
    assert 'id="hsv-body"' in body
    assert 'id="detection-config-form"' in body
    # Identity header element + the new preset-button data attrs
    # (which now also carry shape_gate / selector values for one-click
    # full-triple loads).
    assert 'class="detection-identity"' in body
    assert 'data-hsv-preset="tennis"' in body
    assert 'data-hsv-preset="blue_ball"' in body
    assert 'data-aspect-min=' in body
    assert 'data-w-aspect=' in body
    # Legacy per-section Apply forms must be absent.
    assert 'action="/detection/hsv"' not in body
    assert 'action="/detection/shape_gate"' not in body
    assert 'action="/detection/candidate_selector"' not in body
    # Single Apply button.
    assert 'data-detection-apply' in body


def _seed_minimal_calibration(camera_id: str) -> None:
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id=camera_id,
            intrinsics=main.IntrinsicsPayload(fx=1000.0, fy=1000.0, cx=500.0, cy=500.0),
            homography=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            image_width_px=1000,
            image_height_px=1000,
        )
    )


def test_dashboard_allows_single_calibrated_camera_without_time_sync():
    client = TestClient(app)
    main.state.heartbeat("A")
    _seed_minimal_calibration("A")

    body = client.get("/").text
    assert "single-camera session (A); no triangulation" in body
    assert '<button class="btn" type="submit" title="single-camera session (A); no triangulation">Arm session</button>' in body

    arm = client.post("/sessions/arm", headers={"Accept": "application/json"})
    assert arm.status_code == 200
    assert arm.json()["session"]["id"].startswith("s_")


def test_sessions_arm_blocks_two_calibrated_cameras_without_time_sync():
    client = TestClient(app)
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    _seed_minimal_calibration("A")
    _seed_minimal_calibration("B")

    body = client.get("/").text
    assert "A not time-synced" in body
    assert "B not time-synced" in body
    assert "disabled" in body

    arm = client.post("/sessions/arm", headers={"Accept": "application/json"})
    assert arm.status_code == 409
    assert arm.json()["detail"]["error"] == "not_ready_to_arm"


def test_sessions_arm_blocks_online_uncalibrated_peer():
    client = TestClient(app)
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    _seed_minimal_calibration("A")

    body = client.get("/").text
    assert "B not calibrated" in body

    arm = client.post("/sessions/arm", headers={"Accept": "application/json"})
    assert arm.status_code == 409
    assert arm.json()["detail"]["blockers"] == ["B not calibrated"]


def test_state_marks_single_camera_server_post_path_completed(tmp_path):
    s = main.State(data_dir=tmp_path)
    pitch = _minimal_pitch("A", session_id=sid(90))
    pitch.paths = [main.DetectionPath.server_post.value]
    pitch.frames_server_post = [
        main.FramePayload(frame_index=0, timestamp_s=0.0, px=100.0, py=100.0, ball_detected=True),
    ]

    result = s.record(pitch)

    assert result.frame_counts_by_path["server_post"] == {"A": 1}
    assert "server_post" in result.paths_completed


def test_record_merges_live_frames_into_single_camera_pitch(tmp_path):
    s = main.State(data_dir=tmp_path)
    from schemas import BlobCandidate
    live_frame = main.FramePayload(
        frame_index=3,
        timestamp_s=0.125,
        ball_detected=True,
        candidates=[BlobCandidate(px=123.0, py=456.0, area=100, area_score=1.0)],
    )
    s.ingest_live_frame("A", sid(92), live_frame)
    s.mark_live_path_ended("A", sid(92), "disarmed")

    pitch = _minimal_pitch("A", session_id=sid(92))
    pitch.paths = [main.DetectionPath.live.value]
    pitch.frames_server_post = []

    s.record(pitch)
    stored = s.pitches_for_session(sid(92))["A"]
    assert len(stored.frames_live) == 1
    assert stored.frames_live[0].frame_index == 3
    assert stored.frames_live[0].px == 123.0


def test_session_stop_flushes_live_frames_without_pitch_upload(tmp_path):
    """iOS dies mid-session before sending /pitch — operator presses Stop.
    With a sync anchor on file the buffered live frames are persisted as a
    synthetic pitch so they survive a server restart. Without the anchor the
    flush refuses to synthesise (would peg t_rel_s onto the wrong clock)."""
    from schemas import BlobCandidate
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.arm_session(paths={main.DetectionPath.live})
    session_id = s.current_session().id

    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=1, timestamp_s=0.1, ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    ))
    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=2, timestamp_s=0.2, ball_detected=True,
        candidates=[BlobCandidate(px=11.0, py=21.0, area=100, area_score=1.0)],
    ))

    # No /pitch upload arrives. Operator presses Stop.
    s.stop_session()

    stored = s.pitches_for_session(session_id)
    assert "A" in stored, "synthetic pitch must be written for cam that never uploaded"
    assert len(stored["A"].frames_live) == 2
    assert stored["A"].frames_live[0].frame_index == 1
    assert stored["A"].frames_live[1].px == 11.0

    # Restart simulation: a fresh State reads the synthetic pitch from disk.
    s2 = main.State(data_dir=tmp_path)
    reloaded = s2.pitches_for_session(session_id)
    assert "A" in reloaded
    assert len(reloaded["A"].frames_live) == 2


def test_session_stop_drops_live_frames_without_anchor(tmp_path):
    """No sync anchor on file → flush refuses to synthesise rather than
    pegging t_rel_s onto a fake 0.0 clock."""
    from schemas import BlobCandidate
    s = main.State(data_dir=tmp_path)
    s.arm_session(paths={main.DetectionPath.live})
    session_id = s.current_session().id

    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=1, timestamp_s=0.1, ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    ))
    s.stop_session()

    assert s.pitches_for_session(session_id) == {}


def test_session_timeout_flushes_live_frames(tmp_path):
    """Same as above but the session ends via lazy timeout (operator
    forgot Stop) instead of explicit stop_session. The flush must still
    fire on the next current_session() poll."""
    from schemas import BlobCandidate
    clock = [1000.0]
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock[0])
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.arm_session(max_duration_s=5.0, paths={main.DetectionPath.live})
    session_id = s.current_session().id

    s.ingest_live_frame("B", session_id, main.FramePayload(
        frame_index=7, timestamp_s=0.7, ball_detected=True,
        candidates=[BlobCandidate(px=99.0, py=88.0, area=100, area_score=1.0)],
    ))

    # Push past timeout, then poll — first poll triggers timeout + flush.
    clock[0] += 10.0
    assert s.current_session() is None

    stored = s.pitches_for_session(session_id)
    assert "B" in stored
    assert len(stored["B"].frames_live) == 1
    assert stored["B"].frames_live[0].frame_index == 7


def test_set_calibration_invalidates_cached_live_camera_pose(tmp_path):
    """Re-running auto-calibration must invalidate any cached `CameraPose`
    on active live sessions. Pre-canonical-storage the dim change served as
    the natural invalidation signal; now that snapshots are always stored
    at canonical 1920×1080, snap_dims won't change across recals so the
    cache hit (`existing.image_wh == live_dims`) would silently keep using
    the stale K. Regression test for the live-pose cache-staleness bug."""
    from schemas import BlobCandidate
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.arm_session(paths={main.DetectionPath.live})
    session_id = s.current_session().id

    initial_K = main.IntrinsicsPayload(fx=1500.0, fy=1500.0, cx=960.0, cy=540.0)
    s.set_calibration(main.CalibrationSnapshot(
        camera_id="A",
        intrinsics=initial_K,
        homography=[1.0, 0.0, 100.0, 0.0, 1.0, 200.0, 0.0, 0.0, 1.0],
        image_width_px=1920, image_height_px=1080,
    ))

    # First ingest populates the per-cam cached pose with the initial K.
    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=1, timestamp_s=0.0, ball_detected=True,
        candidates=[BlobCandidate(px=100.0, py=100.0, area=50, area_score=1.0)],
    ))
    pairing_a = s._live_pairings[session_id]
    pose_initial = pairing_a.camera_pose("A")
    assert pose_initial is not None
    assert pose_initial.K[0, 0] == 1500.0  # fx of initial calibration

    # Operator re-runs auto-cal → new K, same canonical 1920×1080 dims.
    new_K = main.IntrinsicsPayload(fx=2000.0, fy=2000.0, cx=960.0, cy=540.0)
    s.set_calibration(main.CalibrationSnapshot(
        camera_id="A",
        intrinsics=new_K,
        homography=[1.0, 0.0, 100.0, 0.0, 1.0, 200.0, 0.0, 0.0, 1.0],
        image_width_px=1920, image_height_px=1080,
    ))

    # Next ingest must rebuild the pose with the new K — pre-fix this
    # silently kept the stale K because cached.image_wh == live_dims.
    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=2, timestamp_s=0.005, ball_detected=True,
        candidates=[BlobCandidate(px=100.0, py=100.0, area=50, area_score=1.0)],
    ))
    pose_after = pairing_a.camera_pose("A")
    assert pose_after is not None
    assert pose_after.K[0, 0] == 2000.0, (
        f"live CameraPose K not refreshed after set_calibration: "
        f"got fx={pose_after.K[0, 0]}, expected 2000.0"
    )


def test_ingest_live_frame_scales_K_to_720p_when_runtime_capture_height_is_720(tmp_path):
    """Operator switches capture_height_px to 720p (state_runtime allows it).
    Snapshot stays at canonical 1920×1080 but live BGRA frames arrive at
    1280×720. ingest_live_frame must scale K + H down to 720p basis before
    building CameraPose; without this, live triangulation rays diverge by
    1.5× from where the ball actually is. This is the direct Bug C target."""
    from schemas import BlobCandidate
    s = main.State(data_dir=tmp_path)
    s.set_capture_height_px(720)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    s.arm_session(paths={main.DetectionPath.live})
    session_id = s.current_session().id

    snap_K = main.IntrinsicsPayload(fx=1500.0, fy=1500.0, cx=960.0, cy=540.0)
    s.set_calibration(main.CalibrationSnapshot(
        camera_id="A",
        intrinsics=snap_K,
        homography=[1.0, 0.0, 100.0, 0.0, 1.0, 200.0, 0.0, 0.0, 1.0],
        image_width_px=1920, image_height_px=1080,
    ))

    s.ingest_live_frame("A", session_id, main.FramePayload(
        frame_index=1, timestamp_s=0.0, ball_detected=True,
        candidates=[BlobCandidate(px=100.0, py=100.0, area=50, area_score=1.0)],
    ))
    pose = s._live_pairings[session_id].camera_pose("A")
    assert pose is not None
    # 1920×1080 → 1280×720 is pure 0.667× linear scale (16:9→16:9).
    # Pre-fix the cached pose used snap K verbatim → fx=1500. Post-fix
    # fx is scaled to 1500*(1280/1920)=1000.
    assert pose.image_wh == (1280, 720)
    assert abs(pose.K[0, 0] - 1000.0) < 1e-6, f"fx not scaled to 720p basis: {pose.K[0, 0]}"
    assert abs(pose.K[1, 1] - 1000.0) < 1e-6, f"fy not scaled to 720p basis: {pose.K[1, 1]}"
    assert abs(pose.K[0, 2] - 640.0) < 1e-6, f"cx not scaled to 720p basis: {pose.K[0, 2]}"
    assert abs(pose.K[1, 2] - 360.0) < 1e-6, f"cy not scaled to 720p basis: {pose.K[1, 2]}"


def test_setup_page_wires_auto_calibration_status_into_device_renders():
    client = TestClient(app)
    r = client.get("/setup")
    assert r.status_code == 200
    body = r.text
    assert "currentAutoCalibration = s.auto_calibration || { active: {}, last: {} };" in body
    assert "auto_calibration: currentAutoCalibration" in body


def test_setup_page_renders_all_config_surfaces():
    client = TestClient(app)
    r = client.get("/setup")
    assert r.status_code == 200
    body = r.text
    # `/setup` is now geometry-only.
    assert 'id="devices-body"' in body
    assert 'href="/markers"' in body
    assert 'Camera Position Setup' in body
    assert 'href="/sync"' in body
    # Phase 3 migrated /setup to the merged single-pane cam-view.
    # Each cam now renders one [data-cam-view] block instead of two
    # side-by-side [preview-panel] + [virt-cell] panels.
    assert 'data-cam-view="A"' in body
    assert 'data-cam-view="B"' in body
    # `<img data-cam-img>` only renders when preview is enabled — `/setup`
    # paints with preview off by default (operator opts in via PREVIEW
    # button) so we assert the canvas overlay is present instead.
    assert 'data-cam-canvas="A"' in body
    # SSR `data-cam-id` lets renderDevices's first JS tick adopt the
    # existing rows and go straight to surgical patch — without it,
    # every page load flashes (SSR paint → JS innerHTML rebuild).
    assert 'data-cam-id="A"' in body
    assert 'data-cam-id="B"' in body
    assert 'data-preview-cam="A"' in body  # PREVIEW button still present
    assert 'data-preview-cam="B"' in body
    assert 'BallTrackerCamView' in body


def test_sync_page_renders_time_sync_and_tuning_surfaces():
    client = TestClient(app)
    r = client.get("/sync")
    assert r.status_code == 200
    body = r.text
    assert 'id="sync-body"' in body
    assert 'id="sync-log"' in body
    assert 'id="sync-report-copy"' in body
    assert 'id="tuning-status"' in body
    assert 'id="burst-params-body"' in body
    assert 'id="per-cam-sync"' in body
    assert 'action="/settings/sync_params"' in body
    assert 'action="/sync/trigger"' in body
    assert 'action="/sync/start"' in body


def test_markers_page_renders_workspace():
    client = TestClient(app)
    r = client.get("/markers")
    assert r.status_code == 200
    body = r.text
    assert 'id="markers-plot"' in body
    assert 'id="compare-root"' in body
    # Phase 4: markers migrated to merged single-pane cam-view; the
    # marker_footprints layer draws ArUco quads + IDs over the live
    # preview, click hit-tests via BallTrackerCamView.onCanvasClick.
    assert 'data-cam-view="A"' in body
    assert 'data-cam-view="B"' in body
    # Markers page also paints with preview off by default — assert canvas
    # overlay only; `<img data-cam-img>` only when PREVIEW is on.
    assert 'data-cam-canvas="A"' in body
    # marker_footprints must be one of the toggleable layers + on by default.
    assert 'data-layer="marker_footprints"' in body
    assert 'BallTrackerCamView' in body
    # Negative: legacy 2-pane shape gone from markers page.
    assert 'data-preview-img="A"' not in body
    assert 'data-markers-virt-canvas' not in body
    assert 'data-preview-overlay="A"' not in body
    assert 'id="candidate-body"' in body
    assert 'id="stored-body"' in body
    assert 'id="details-body"' in body
    assert 'id="show-aruco-ids"' in body


def test_dashboard_no_longer_renders_telemetry_overlay():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="telemetry-panel"' not in body
    assert 'id="telemetry-body"' not in body


def test_settings_message_includes_server_authoritative_sync_status():
    main.state.heartbeat(
        "A",
        time_synced=True,
        time_sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=12.34,
    )
    msg = main._settings_message_for("A")
    assert msg["device_time_synced"] is True
    assert msg["device_time_sync_id"] == "sy_deadbeef"


def test_setup_page_no_longer_renders_preview_marker_count_chip():
    client = TestClient(app)
    r = client.get("/setup")
    assert r.status_code == 200
    assert "data-marker-chip=" not in r.text


def test_sync_page_no_longer_redirects_to_setup():
    client = TestClient(app)
    r = client.get("/sync")
    assert r.status_code == 200
    assert 'Time Sync' in r.text


def test_dashboard_marks_expected_cameras_offline_when_absent():
    client = TestClient(app)
    # Device status is now surfaced on /setup, not /.
    r = client.get("/setup")
    body = r.text
    assert '<div class="id">A</div>' in body
    assert '<div class="id">B</div>' in body
    # Each cam row renders its own "offline" chip + "offline" sub-labels;
    # count ≥ 2 rows present.
    assert body.count('<span class="chip idle">offline</span>') >= 2


# --- Mutual chirp sync -----------------------------------------------------


def _heartbeat_both(client: TestClient) -> None:
    # HTTP /heartbeat has been retired (live transport is WS-only). For
    # tests that only need "pretend A and B are online", reach into the
    # state directly rather than spinning up a WS in every call — the WS
    # handler for each connect/disconnect is covered elsewhere.
    main.state.heartbeat("A")
    main.state.heartbeat("B")


def _build_sync_report(
    *, role: str, sync_id: str, delta_s: float, distance_m: float,
    e_a: float = 100.0, e_b: float = 100.0, c: float = 343.0,
) -> dict:
    """Produce the JSON body for POST /sync/report matching a given
    ground-truth (Δ, D) — same construction as test_sync_solver's helper
    but returns the wire dict so the TestClient can pass it as JSON."""
    tof = distance_m / c
    if role == "A":
        return {
            "camera_id": "A", "sync_id": sync_id, "role": "A",
            "t_self_s": e_a, "t_from_other_s": e_b + delta_s + tof,
            "emitted_band": "A",
        }
    return {
        "camera_id": "B", "sync_id": sync_id, "role": "B",
        "t_self_s": e_b, "t_from_other_s": e_a - delta_s + tof,
        "emitted_band": "B",
    }


def test_sync_start_requires_two_devices():
    client = TestClient(app)
    main.state.heartbeat("A")

    r = client.post("/sync/start")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "devices_missing"


def test_sync_start_conflicts_with_armed_session():
    client = TestClient(app)
    main.state.heartbeat("A")
    _seed_minimal_calibration("A")
    client.post("/sessions/arm", headers={"Accept": "application/json"})

    r = client.post("/sync/start")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "session_armed"


def test_sync_start_rejects_reentrant_run():
    client = TestClient(app)
    _heartbeat_both(client)

    r1 = client.post("/sync/start")
    assert r1.status_code == 200
    r2 = client.post("/sync/start")
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "sync_in_progress"


def test_sync_end_to_end_solves_delta_and_distance():
    client = TestClient(app)
    _heartbeat_both(client)

    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]

    # After /heartbeat retirement, /sync/state is the canonical inspection
    # surface (previously heartbeat reply mirrored it). Semantics otherwise
    # unchanged: the armed sync run exposes its id + the reports received
    # so far, and cooldown / last_sync take over after completion.
    st = client.get("/sync/state").json()
    assert st["sync"]["id"] == sync_id
    assert st["sync"]["reports_received"] == []

    delta_truth = 0.007
    distance_truth = 2.8
    a_report = _build_sync_report(
        role="A", sync_id=sync_id, delta_s=delta_truth, distance_m=distance_truth,
    )
    r_a = client.post("/sync/report", json=a_report)
    assert r_a.status_code == 200
    assert r_a.json()["solved"] is False

    st2 = client.get("/sync/state").json()
    assert st2["sync"]["reports_received"] == ["A"]

    b_report = _build_sync_report(
        role="B", sync_id=sync_id, delta_s=delta_truth, distance_m=distance_truth,
    )
    r_b = client.post("/sync/report", json=b_report)
    assert r_b.status_code == 200
    body = r_b.json()
    assert body["solved"] is True
    assert body["result"]["delta_s"] == pytest.approx(delta_truth, abs=1e-9)
    assert body["result"]["distance_m"] == pytest.approx(distance_truth, abs=1e-5)

    st3 = client.get("/sync/state").json()
    assert st3["sync"] is None
    assert st3["last_sync"]["id"] == sync_id
    assert st3["cooldown_remaining_s"] > 0.0


def test_sync_stale_report_is_rejected(tmp_path):
    """A report whose sync_id doesn't match the current run's id is a leftover
    from a timed-out run and must be refused rather than overwriting the
    fresh run's partial state."""
    client = TestClient(app)
    _heartbeat_both(client)
    client.post("/sync/start")

    report = _build_sync_report(
        role="A", sync_id="sy_deadbeef", delta_s=0.0, distance_m=1.0,
    )
    r = client.post("/sync/report", json=report)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "stale_sync_id"


def test_sync_report_without_active_run_is_rejected():
    client = TestClient(app)
    _heartbeat_both(client)

    r = client.post("/sync/report", json=_build_sync_report(
        role="A", sync_id="sy_deadbeef", delta_s=0.0, distance_m=1.0,
    ))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_sync"


def test_sync_timeout_drops_run_and_triggers_cooldown(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")
    run, reason = s.start_sync()
    assert reason is None and run is not None

    # Advance past timeout without either phone reporting. Re-heartbeat so
    # devices stay online through the long wait — matches the real flow
    # where phones beat at 1 Hz regardless of sync state.
    clock["now"] = 1000.0 + main._SYNC_TIMEOUT_S + 0.5
    s.heartbeat("A")
    s.heartbeat("B")
    assert s._sync.current_sync() is None
    # Fresh /sync/start must wait for cooldown.
    _, reason2 = s.start_sync()
    assert reason2 == "cooldown"

    # After cooldown, next start succeeds.
    clock["now"] += main._SYNC_COOLDOWN_S + 0.1
    s.heartbeat("A")
    s.heartbeat("B")
    run2, reason3 = s.start_sync()
    assert reason3 is None and run2 is not None


def test_sync_cooldown_blocks_immediate_restart(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")

    run, _ = s.start_sync()
    assert run is not None
    a = SyncReport(
        camera_id="A", sync_id=run.id, role="A",
        t_self_s=0.0, t_from_other_s=0.01, emitted_band="A",
    )
    b = SyncReport(
        camera_id="B", sync_id=run.id, role="B",
        t_self_s=0.0, t_from_other_s=0.01, emitted_band="B",
    )
    s._sync.record_sync_report(a)
    _, result, _ = s._sync.record_sync_report(b)
    assert result is not None

    # Still in cooldown.
    _, reason = s.start_sync()
    assert reason == "cooldown"

    clock["now"] += main._SYNC_COOLDOWN_S + 0.1
    s.heartbeat("A")
    s.heartbeat("B")
    _, reason2 = s.start_sync()
    assert reason2 is None


def test_sync_run_ids_are_unique_across_runs(tmp_path):
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")

    run1, _ = s.start_sync()
    assert run1 is not None
    # Simulate completion to clear state and escape cooldown.
    a1 = SyncReport(camera_id="A", sync_id=run1.id, role="A",
                    t_self_s=0.0, t_from_other_s=0.0, emitted_band="A")
    b1 = SyncReport(camera_id="B", sync_id=run1.id, role="B",
                    t_self_s=0.0, t_from_other_s=0.0, emitted_band="B")
    s._sync.record_sync_report(a1)
    s._sync.record_sync_report(b1)
    clock["now"] += main._SYNC_COOLDOWN_S + 0.1
    s.heartbeat("A")
    s.heartbeat("B")

    run2, _ = s.start_sync()
    assert run2 is not None
    assert run2.id != run1.id
    assert run2.id.startswith("sy_")


def test_sync_state_endpoint_reflects_run_and_last():
    client = TestClient(app)
    _heartbeat_both(client)

    assert client.get("/sync/state").json() == {
        "sync": None, "last_sync": None,
        "cooldown_remaining_s": 0.0, "logs": [], "telemetry": {},
    }

    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]
    state_during = client.get("/sync/state").json()
    assert state_during["sync"]["id"] == sync_id
    assert state_during["last_sync"] is None


# --- Helpers ---------------------------------------------------------------


def _minimal_pitch(camera_id: str, session_id: str) -> main.PitchPayload:
    """A minimal server-internal PitchPayload with no calibration — enough
    for `State.record` to run, not enough for triangulation. Frames are
    populated to keep `events()` counts meaningful without going through
    the /pitch detection path."""
    return main.PitchPayload(
        camera_id=camera_id,
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=[
            main.FramePayload(
                frame_index=0,
                timestamp_s=0.0,
                px=100.0, py=100.0,
                ball_detected=True,
            ),
        ],
    )
