"""Follow-up coverage for PR #81: AliasChoices legacy fz, live-ray
missing-calibration dedupe/surface, server_post error clear-on-retry,
and reset/delete cleanup of the new observability dicts."""
from __future__ import annotations

import logging

from schemas import FramePayload, IntrinsicsPayload
from state import State


def test_intrinsics_accepts_legacy_fz_alias():
    """Historical calibration JSONs stored vertical focal length as `fz`.
    The rename preserves load compatibility via AliasChoices; dump must
    always emit the canonical `fy` so re-persisted files drift forward."""
    legacy = {"fx": 1000.0, "fz": 1100.0, "cx": 960.0, "cy": 540.0}
    intr = IntrinsicsPayload.model_validate(legacy)
    assert intr.fy == 1100.0

    dumped = intr.model_dump()
    assert "fy" in dumped
    assert "fz" not in dumped
    assert dumped["fy"] == 1100.0

    # New constructor path (field name) stays ergonomic.
    intr2 = IntrinsicsPayload(fx=1.0, fy=2.0, cx=3.0, cy=4.0)
    assert intr2.fy == 2.0


def _frame(i: int) -> FramePayload:
    return FramePayload(
        frame_index=i, timestamp_s=float(i) / 240.0, ball_detected=True, px=100.0, py=100.0
    )


def test_live_ray_missing_calibration_dedup_logs(tmp_path, caplog):
    """live_ray_for_frame should record the missing cam in session state
    every frame (for /events surfacing) but log the warning only once
    per (session, cam) pair — no log flood at 60 Hz."""
    state = State(data_dir=tmp_path)
    sid = "s_deadbeef"

    with caplog.at_level(logging.WARNING, logger="state"):
        ray1 = state.live_ray_for_frame("A", sid, _frame(0))
        ray2 = state.live_ray_for_frame("A", sid, _frame(1))
        ray3 = state.live_ray_for_frame("A", sid, _frame(2))

    assert ray1 is None and ray2 is None and ray3 is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "no calibration" in r.getMessage()]
    assert len(warnings) == 1, f"expected single warn-once, got {len(warnings)}"
    assert state.live_missing_calibration_for(sid) == ["A"]

    # A second cam on the same session triggers its own single warning.
    with caplog.at_level(logging.WARNING, logger="state"):
        caplog.clear()
        state.live_ray_for_frame("B", sid, _frame(3))
        state.live_ray_for_frame("B", sid, _frame(4))
    warnings_b = [r for r in caplog.records if r.levelno == logging.WARNING and "no calibration" in r.getMessage()]
    assert len(warnings_b) == 1
    assert state.live_missing_calibration_for(sid) == ["A", "B"]


def test_server_post_error_cleared_on_retry(tmp_path):
    """Retry semantics: a successful re-run must clear the stale error so
    /events doesn't keep a red pill after the operator fixed the input."""
    state = State(data_dir=tmp_path)
    sid = "s_cafebabe"

    state.record_server_post_error(sid, "A", "detect: PyAV decode failed")
    state.record_server_post_error(sid, "B", "annotate: cv2 draw failed")
    assert state.server_post_errors_for(sid) == {
        "A": "detect: PyAV decode failed",
        "B": "annotate: cv2 draw failed",
    }

    state.clear_server_post_error(sid, "A")
    assert state.server_post_errors_for(sid) == {"B": "annotate: cv2 draw failed"}

    # Clearing the last cam collapses the session entry entirely.
    state.clear_server_post_error(sid, "B")
    assert state.server_post_errors_for(sid) == {}

    # Idempotent on unknown keys.
    state.clear_server_post_error(sid, "A")
    state.clear_server_post_error("s_nonexistent", "A")


def test_reset_and_delete_clear_observability_state(tmp_path):
    """Without explicit cleanup, _live_missing_cal / _server_post_errors
    leak across sessions and across resets — the dashboard would keep
    showing pills for sessions that no longer exist."""
    state = State(data_dir=tmp_path)
    sid1 = "s_11111111"
    sid2 = "s_22222222"

    state.live_ray_for_frame("A", sid1, _frame(0))
    state.live_ray_for_frame("B", sid2, _frame(0))
    state.record_server_post_error(sid1, "A", "boom")
    state.record_server_post_error(sid2, "B", "kaboom")

    # delete_session only purges the targeted sid.
    state.delete_session(sid1)
    assert state.live_missing_calibration_for(sid1) == []
    assert state.server_post_errors_for(sid1) == {}
    assert state.live_missing_calibration_for(sid2) == ["B"]
    assert state.server_post_errors_for(sid2) == {"B": "kaboom"}

    # A second live_ray call for sid1's cam should re-populate the set —
    # the per-(sid,cam) dedupe key must have been dropped too, so the
    # cam shows up again on /events if the operator re-arms the sid.
    state.live_ray_for_frame("A", sid1, _frame(0))
    assert state.live_missing_calibration_for(sid1) == ["A"]

    # reset() wipes everything.
    state.reset()
    assert state.live_missing_calibration_for(sid1) == []
    assert state.live_missing_calibration_for(sid2) == []
    assert state.server_post_errors_for(sid1) == {}
    assert state.server_post_errors_for(sid2) == {}
