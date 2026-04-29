"""Contract tests for the arm-then-ingest alignment freeze.

R7 caught that the original first-ingest freeze branch was dead on the
dashboard-armed flow because `state.arm_session` pre-creates the
`LivePairingSession` in `_live_pairings`, so the `if new_session:` guard
inside `ingest_live_frame` evaluated False and the whole freeze block
got skipped. These tests pin the post-fix invariant: arm + mid-cycle
slider drag + first ingest → frozen values are the ones in effect at
arm time, NOT the post-drag values.

Covers all four frozen fields exposed via `LivePairingSession`:
  - hsv_range_used
  - shape_gate_used
  - tuning            (CandidateSelectorTuning, dashboard selector slider)
  - pairing_tuning    (cd87995 cost/gap freeze contract)
"""
from __future__ import annotations

from dataclasses import replace

import main
from candidate_selector import CandidateSelectorTuning
from conftest import sid
from detection import HSVRange, ShapeGate
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
    hsv_frozen, _gate_frozen, _tuning_frozen = frozen
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
    hsv_frozen2, _, _ = frozen2
    assert hsv_frozen2 == hsv_x, "stamp must be idempotent — never re-stamped"


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
    _hsv, gate_frozen, _tuning = frozen
    assert gate_frozen == gate_x


def test_arm_then_slider_drag_then_ingest_freezes_arm_time_selector_tuning(tmp_path):
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    sel_x = CandidateSelectorTuning(w_aspect=0.71, w_fill=0.29)
    s.set_candidate_selector_tuning(sel_x)
    session = s.arm_session(paths={main.DetectionPath.live})

    sel_y = CandidateSelectorTuning(w_aspect=0.10, w_fill=0.90)
    s.set_candidate_selector_tuning(sel_y)
    s.ingest_live_frame("A", session.id, _make_frame(1))

    frozen = s.live_session_frozen_config(session.id)
    assert frozen is not None
    _hsv, _gate, sel_frozen = frozen
    assert sel_frozen == sel_x


def test_arm_then_slider_drag_then_ingest_freezes_arm_time_pairing_tuning(tmp_path):
    """The cd87995 PairingTuning freeze contract — regression hook for the
    original BLOCK that left LivePairingSession.pairing_tuning at default."""
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    pt_x = PairingTuning(cost_threshold=0.42, gap_threshold_m=0.42)
    s.set_pairing_tuning(pt_x)
    session = s.arm_session(paths={main.DetectionPath.live})

    pt_y = PairingTuning(cost_threshold=0.99, gap_threshold_m=0.99)
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
    Accessor returns None so the caller falls back to current state — that's
    how test_pitch_endpoints + replay paths stay green."""
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
    hsv_frozen, _, _ = frozen
    assert hsv_frozen == hsv_x
