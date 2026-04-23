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
    cmd_a, sync_id_a = main.state.consume_sync_command("A")
    cmd_b, sync_id_b = main.state.consume_sync_command("B")
    assert cmd_a == "start"
    assert cmd_b == "start"
    assert sync_id_a is not None
    assert sync_id_a == sync_id_b
    # Both cams show up in the pending-commands snapshot.
    assert main.state.pending_sync_commands() == {}


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
    assert main.state.pending_sync_commands() == {}


def test_sync_command_drains_on_heartbeat_consumption():
    """Once a phone consumes the flag via heartbeat, subsequent heartbeats
    don't re-fire (one-shot dispatch)."""
    import main
    main.state.heartbeat("A")
    main.state.trigger_sync_command(["A"])
    # First consume returns the command.
    first = main.state.consume_sync_command("A")
    assert first[0] == "start"
    assert first[1] is not None
    # Second consume is empty — flag drained.
    assert main.state.consume_sync_command("A") == (None, None)
    assert main.state.pending_sync_commands() == {}


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
    assert "A" in main.state.pending_sync_commands()
    # Advance past the TTL.
    clock[0] += main._SYNC_COMMAND_TTL_S + 1.0
    assert main.state.consume_sync_command("A") == (None, None)
    assert main.state.pending_sync_commands() == {}


def test_sync_claim_reuses_live_intent_then_rolls_after_window(tmp_path, monkeypatch):
    import main
    clock = [1000.0]

    def fake_time() -> float:
        return clock[0]

    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    first = main.state.claim_time_sync_intent()
    second = main.state.claim_time_sync_intent()
    assert first.id == second.id

    clock[0] += main._TIME_SYNC_INTENT_WINDOW_S + 0.1
    third = main.state.claim_time_sync_intent()
    assert third.id != first.id


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
