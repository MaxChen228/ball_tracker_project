"""Sync trigger/command TTL/drain/mismatched sync ids."""
from __future__ import annotations

import main


def test_sync_trigger_flags_all_online_cameras():
    """With no camera_ids argument, trigger_sync_command targets every
    currently-online camera and returns them sorted + deduped."""
    import main
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    dispatched = main.state.trigger_sync_command(None)
    assert dispatched == ["A", "B"]
    cmd_a, sync_id_a = main.state._sync.consume_sync_command("A")
    cmd_b, sync_id_b = main.state._sync.consume_sync_command("B")
    assert cmd_a == "start"
    assert cmd_b == "start"
    assert sync_id_a is not None
    assert sync_id_a == sync_id_b
    # Both cams show up in the pending-commands snapshot.
    assert main.state._sync.pending_sync_commands() == {}


def test_sync_trigger_skips_armed_session():
    """Firing CALIBRATE TIME while a session is armed must dispatch to NO
    camera — running a chirp-listen in the middle of a recording would
    disrupt the armed clip."""
    import main
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    main.state.arm_session()
    dispatched = main.state.trigger_sync_command(None)
    assert dispatched == []
    assert main.state._sync.pending_sync_commands() == {}


def test_sync_command_drains_on_heartbeat_consumption():
    """Once a phone consumes the flag via heartbeat, subsequent heartbeats
    don't re-fire (one-shot dispatch)."""
    import main
    main.state.heartbeat("A")
    main.state.trigger_sync_command(["A"])
    # First consume returns the command.
    first = main.state._sync.consume_sync_command("A")
    assert first[0] == "start"
    assert first[1] is not None
    # Second consume is empty — flag drained.
    assert main.state._sync.consume_sync_command("A") == (None, None)
    assert main.state._sync.pending_sync_commands() == {}


def test_sync_command_expires_after_ttl(tmp_path, monkeypatch):
    """Stale flags self-expire after _SYNC_COMMAND_TTL_S so a command
    doesn't fire hours later if the operator gave up on the request."""
    import main
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state.heartbeat("A")
    main.state.trigger_sync_command(["A"])
    assert "A" in main.state._sync.pending_sync_commands()
    # Advance past the TTL.
    clock[0] += main._SYNC_COMMAND_TTL_S + 1.0
    assert main.state._sync.consume_sync_command("A") == (None, None)
    assert main.state._sync.pending_sync_commands() == {}


def test_sync_claim_reuses_live_intent_then_rolls_after_window(tmp_path, monkeypatch):
    import main
    clock = [1000.0]

    def fake_time() -> float:
        return clock[0]

    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    first = main.state._sync.claim_time_sync_intent()
    second = main.state._sync.claim_time_sync_intent()
    assert first.id == second.id

    clock[0] += main._TIME_SYNC_INTENT_WINDOW_S + 0.1
    third = main.state._sync.claim_time_sync_intent()
    assert third.id != first.id


def test_flush_live_frames_synthesises_pitch_with_calibration(tmp_path, monkeypatch):
    """When a cam streams `live` frames over WS but its /pitch upload
    never lands, session-end calls `flush_live_frames_for_session` to
    synthesise a minimal pitch from the buffered frames. That pitch
    MUST carry the cam's current calibration + sync_id, otherwise the
    viewer's persisted pitch JSON has intrinsics=None and renders the
    misleading "Cam X missing calibration" red banner even though the
    operator had calibration on file the whole time."""
    import main
    from schemas import (
        CalibrationSnapshot, FramePayload, IntrinsicsPayload,
    )
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))

    cal_b = CalibrationSnapshot(
        camera_id="B",
        intrinsics=IntrinsicsPayload(fx=1500.0, fy=1500.0, cx=960.0, cy=540.0),
        homography=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        image_width_px=1920,
        image_height_px=1080,
    )
    main.state.set_calibration(cal_b)
    main.state.heartbeat(
        "B", time_synced=True,
        time_sync_id="sy_abcd1234", sync_anchor_timestamp_s=42.0,
    )

    sess = main.state.arm_session()
    sid = sess.id
    # Seed one buffered live frame for B so flush has work to do.
    main.state.ingest_live_frame(
        "B", sid,
        FramePayload(
            frame_index=0, timestamp_s=42.1, ball_detected=True,
            candidates=[{"px": 100.0, "py": 200.0, "area": 50,
                         "area_score": 1.0}],
        ),
    )
    main.state.flush_live_frames_for_session(sid)

    pitch = main.state.pitches.get(("B", sid))
    assert pitch is not None
    assert pitch.intrinsics is not None, "flush must fill intrinsics"
    assert pitch.intrinsics.fx == 1500.0
    assert pitch.homography is not None
    assert len(pitch.homography) == 9
    assert pitch.image_width_px == 1920
    assert pitch.image_height_px == 1080
    assert pitch.sync_id == "sy_abcd1234"
    assert pitch.sync_anchor_timestamp_s == 42.0


def test_quick_chirp_drops_prior_anchor_for_dispatched_cams(tmp_path, monkeypatch):
    """A successful sync from earlier must not survive a fresh
    /sync/trigger: until the phone reports a new anchor matching the
    new expected id, the cam reads as not-synced. Prevents the case
    where one cam misses the chirp and silently sails through readiness
    on a stale anchor while the peer locks onto the new one."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    main.state.heartbeat(
        "A", time_synced=True,
        time_sync_id="sy_old_a", sync_anchor_timestamp_s=10.0,
    )
    main.state.heartbeat(
        "B", time_synced=True,
        time_sync_id="sy_old_b", sync_anchor_timestamp_s=20.0,
    )
    dev_a_before = main.state.device_snapshot("A")
    assert dev_a_before is not None
    assert dev_a_before.time_synced is True
    assert dev_a_before.time_sync_id == "sy_old_a"

    dispatched = main.state.trigger_sync_command(None)
    assert dispatched == ["A", "B"]

    for cam in ("A", "B"):
        snap = main.state.device_snapshot(cam)
        assert snap is not None
        assert snap.time_synced is False, f"{cam} still claims synced"
        assert snap.time_sync_id is None, f"{cam} still carries id"
        assert snap.sync_anchor_timestamp_s is None, f"{cam} still carries anchor"


def test_arm_readiness_blocks_mismatched_sync_ids(tmp_path, monkeypatch):
    """Both cams pass per-cam id_match (each echoed its own expected id)
    but the two ids differ — that means each cam locked onto its own
    chirp event. Triangulation across mismatched anchors is meaningless,
    so readiness must surface a blocker."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    # Pretend both cams are calibrated.
    monkeypatch.setattr(
        main.state, "calibrations", lambda: {"A": object(), "B": object()},
    )
    main.state.heartbeat(
        "A", time_synced=True,
        time_sync_id="sy_first", sync_anchor_timestamp_s=10.0,
    )
    main.state.heartbeat(
        "B", time_synced=True,
        time_sync_id="sy_second", sync_anchor_timestamp_s=20.0,
    )
    # Set per-cam expected ids matching each cam's own report so both
    # pass per-cam id_match — only the pair-check should trip.
    main.state._sync.set_expected_sync_id(["A"], "sy_first")
    main.state._sync.set_expected_sync_id(["B"], "sy_second")

    readiness = main._arm_readiness()
    assert readiness["mode"] == "stereo"
    assert readiness["ready"] is False
    assert any(
        "sync ids mismatch" in b for b in readiness["blockers"]
    ), readiness["blockers"]


def test_paired_payloads_with_mismatched_sync_ids_fail_before_triangulation(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_deadbeef"
    frame = [main.FramePayload(frame_index=0, timestamp_s=0.0, px=100.0, py=100.0, ball_detected=True)]
    pa = main.PitchPayload(
        camera_id="A",
        session_id=sid,
        sync_id="sy_aaaaaaaa",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frame,
    )
    pb = main.PitchPayload(
        camera_id="B",
        session_id=sid,
        sync_id="sy_bbbbbbbb",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frame,
    )
    main.state.record(pa)
    result = main.state.record(pb)
    assert result.error == "sync id mismatch"
    assert result.points == []
