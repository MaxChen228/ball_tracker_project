"""Contract tests for the arm-then-ingest alignment freeze.

R7 caught that the original first-ingest freeze branch was dead on the
dashboard-armed flow because `state.arm_session` pre-creates the
`LivePairingSession` in `_live_pairings`, so the `if new_session:` guard
inside `ingest_live_frame` evaluated False and the whole freeze block
got skipped. These tests pin the post-fix invariant: arm + mid-cycle
slider drag + first ingest → frozen values are the ones in effect at
arm time, NOT the post-drag values.

Covers the frozen fields exposed via `LivePairingSession`:
  - live_config_used.hsv
  - live_config_used.shape_gate
  - pairing_tuning    (cd87995 cost/gap freeze contract)
"""
from __future__ import annotations

from dataclasses import replace

import main
from conftest import sid
from detection import HSVRange
from pairing_tuning import PairingTuning
from schemas import BlobCandidate


def _make_frame(idx: int = 1) -> main.FramePayload:
    return main.FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    )


def test_arm_then_slider_drag_then_ingest_freezes_arm_time_hsv(tmp_path):
    """Slider edit between arm and first ingest must NOT poison the
    session: ingest stamps the value that was active at arm time.
    Idempotent — second ingest does NOT re-stamp under post-drag value."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)

    # 1. Set baseline HSV = X.
    hsv_x = HSVRange(h_min=10, h_max=20, s_min=30, s_max=200,
                    v_min=40, v_max=210)
    s.set_hsv_range(hsv_x)

    # 2. Arm — pre-creates the LivePairingSession in _live_pairings.
    session = s.arm_session(paths={main.DetectionPath.live})

    # 3. Operator drags the HSV slider mid-cycle to Y.
    hsv_y = HSVRange(h_min=99, h_max=109, s_min=0, s_max=255,
                    v_min=0, v_max=255)
    s.set_hsv_range(hsv_y)
    assert s.hsv_range() == hsv_y, "slider edit must apply to global state"

    # 4. First live frame arrives — must stamp X (arm-time), not Y.
    s.ingest_live_frame("A", session.id, _make_frame(1))

    frozen = s.live_session_frozen_config(session.id)
    assert frozen is not None, "first ingest must have stamped"
    hsv_frozen = HSVRange(
        h_min=frozen.hsv.h_min, h_max=frozen.hsv.h_max,
        s_min=frozen.hsv.s_min, s_max=frozen.hsv.s_max,
        v_min=frozen.hsv.v_min, v_max=frozen.hsv.v_max,
    )
    assert hsv_frozen == hsv_x, (
        f"frozen hsv should be arm-time X, got {hsv_frozen}; "
        "slider drag between arm and first ingest poisoned the session"
    )

    # 5. Drag again to Z, ingest a second frame — frozen value MUST stay X.
    hsv_z = HSVRange(h_min=1, h_max=2, s_min=3, s_max=4, v_min=5, v_max=6)
    s.set_hsv_range(hsv_z)
    s.ingest_live_frame("A", session.id, _make_frame(2))
    frozen2 = s.live_session_frozen_config(session.id)
    assert frozen2 is not None
    assert (frozen2.hsv.h_min, frozen2.hsv.h_max) == (hsv_x.h_min, hsv_x.h_max), \
        "stamp must be idempotent — never re-stamped"


def test_arm_then_slider_drag_then_ingest_freezes_arm_time_shape_gate(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    gate_x = replace(s.shape_gate(), aspect_min=0.61, fill_min=0.62)
    s.set_shape_gate(gate_x)
    session = s.arm_session(paths={main.DetectionPath.live})

    gate_y = replace(s.shape_gate(), aspect_min=0.99, fill_min=0.99)
    s.set_shape_gate(gate_y)
    s.ingest_live_frame("A", session.id, _make_frame(1))

    frozen = s.live_session_frozen_config(session.id)
    assert frozen is not None
    from detection import ShapeGate
    gate_frozen = ShapeGate(
        aspect_min=frozen.shape_gate.aspect_min,
        fill_min=frozen.shape_gate.fill_min,
    )
    assert gate_frozen == gate_x


def test_arm_then_slider_drag_then_ingest_freezes_arm_time_pairing_tuning(tmp_path):
    """The cd87995 PairingTuning freeze contract — regression hook for the
    original BLOCK that left LivePairingSession.pairing_tuning at default."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    pt_x = PairingTuning(gap_threshold_m=0.42)
    s.set_pairing_tuning(pt_x)
    session = s.arm_session(paths={main.DetectionPath.live})

    pt_y = PairingTuning(gap_threshold_m=0.99)
    s.set_pairing_tuning(pt_y)
    s.ingest_live_frame("A", session.id, _make_frame(1))

    # pairing_tuning isn't returned by live_session_frozen_config (which
    # only exposes the iOS-detector-side stamp); reach into _live_pairings
    # directly here because this test IS the freeze invariant for that
    # field.
    with s._lock:
        live = s._live_pairings[session.id]
        assert live.pairing_tuning == pt_x


def test_no_live_session_returns_none(tmp_path):
    """Test fixtures that POST /pitch without arming see no LivePairingSession.
    Accessor returns None and the caller propagates None to
    `live_config_used` (no fabrication from disk) — viewer / events
    renderers display a dash for sessions that never armed."""
    s = main.State(data_dir=tmp_path)
    assert s.live_session_frozen_config(sid(999)) is None


def test_arm_alone_stamps_without_waiting_for_ingest(tmp_path):
    """Server_post-only flow: arm_session must stamp the frozen config
    immediately so /pitch can read it even when no live WS frame ever
    streams. Without this stamp /pitch sees an unstamped LivePairingSession
    and silently falls back to current state — defeating the freeze."""
    s = main.State(data_dir=tmp_path)
    hsv_x = HSVRange(h_min=11, h_max=22, s_min=33, s_max=44,
                    v_min=55, v_max=66)
    s.set_hsv_range(hsv_x)
    session = s.arm_session(paths={main.DetectionPath.live})

    # Slider drag — must NOT affect the just-armed session.
    s.set_hsv_range(HSVRange(h_min=1, h_max=2, s_min=3, s_max=4,
                              v_min=5, v_max=6))

    frozen = s.live_session_frozen_config(session.id)
    assert frozen is not None, "arm_session must stamp synchronously"
    hsv_frozen = HSVRange(
        h_min=frozen.hsv.h_min, h_max=frozen.hsv.h_max,
        s_min=frozen.hsv.s_min, s_max=frozen.hsv.s_max,
        v_min=frozen.hsv.v_min, v_max=frozen.hsv.v_max,
    )
    assert hsv_frozen == hsv_x


def test_pitch_ingest_does_not_fabricate_live_config_when_never_armed(tmp_path):
    """Phase-2 contract: when no arm ever happened for this session_id,
    `state.live_session_frozen_config` returns None and `/pitch` MUST
    propagate None to `pitch.live_config_used`. Pre-fix the route
    fabricated a snapshot from `state.detection_config()` (whatever
    disk currently held — boot defaults or operator-edited),
    silently claiming a live config that no detection ever actually
    used. The viewer CFG chip would then surface this fabricated
    name, biasing post-hoc live-vs-server_post delta investigations."""
    from fastapi.testclient import TestClient
    from main import app
    from _test_helpers import _base_payload, _make_scene, _post_pitch

    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    session_id = sid(770)

    client = TestClient(app)

    # Bypass arm. POST a frames-only pitch (mode-two, no MOV).
    payload = _base_payload("A", session_id, K, H_a)
    payload["frames_live"] = [{
        "frame_index": 0,
        "timestamp_s": 0.0,
        "ball_detected": False,
        "candidates": [],
    }]
    r = _post_pitch(client, payload, None)
    assert r.status_code == 200, r.text

    persisted = main.state.pitches[("A", session_id)]
    assert persisted.live_config_used is None, (
        f"live_config_used must be None when no arm preceded /pitch; "
        f"got {persisted.live_config_used!r}"
    )
