"""Regression tests for server-side silent-fallback removals.

Two paths historically masked invariant violations with `or` shims:

1. `session_results.py`: legacy `result.points` used to fall back from
   server_post to live (`result.triangulated_by_path.get(...) or []`).
   Research-mode invariant: a missing path's points must read empty,
   not silently substitute another path's points. Cross-path
   substitution corrupts live-vs-server_post comparisons.

2. `pipeline.py`: server-side per-frame detection wrote
   `candidates=blobs if winner else (blobs or None)`. Empty list
   ("detector ran, found 0 candidates") collapsed to None ("no
   detection attempted"). These two states must remain distinguishable.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. session_results.py — explicit per-path selection, no cross-path fallback
# ---------------------------------------------------------------------------


def _make_triangulated_point():
    """Minimal TriangulatedPoint stub with required fields."""
    from schemas import TriangulatedPoint

    return TriangulatedPoint(
        t_rel_s=0.0,
        x_m=1.0,
        y_m=2.0,
        z_m=3.0,
        residual_m=0.01,
        cost_a=None,
        cost_b=None,
    )


def test_legacy_points_no_silent_fallback_to_live_when_server_post_requested():
    """If the caller selected server_post for the legacy surface but
    server_post produced nothing, `result.points` MUST stay empty even
    after segment stamping sees live points.
    """
    from schemas import DetectionPath, SessionResult
    from session_results import stamp_segments_on_result

    live_pt = _make_triangulated_point()
    result = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True,
        camera_b_received=True,
    )
    result.triangulated_by_path = {
        DetectionPath.live.value: [live_pt],
        # server_post entry absent → simulates "server_post never ran /
        # produced no triangulation"
    }

    stamp_segments_on_result(
        result,
        legacy_points_path=DetectionPath.server_post,
    )

    assert result.triangulated == [live_pt]
    assert result.points == [], (
        "server_post requested but missing must yield empty points; "
        "silently borrowing live points would contaminate "
        "live-vs-server_post comparisons."
    )
    assert result.segments == []


def test_legacy_points_uses_live_only_when_server_post_not_requested():
    from schemas import DetectionPath, SessionResult
    from session_results import stamp_segments_on_result

    live_pt = _make_triangulated_point()
    result = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True,
        camera_b_received=True,
    )
    result.triangulated_by_path = {
        DetectionPath.live.value: [live_pt],
    }

    stamp_segments_on_result(result, legacy_points_path=DetectionPath.live)

    assert result.points == [live_pt]


def test_session_results_module_uses_explicit_branches():
    """Source-level guard: confirm the silent `or []` pattern is gone
    from the legacy-points selection so future edits don't reintroduce
    it without flipping this test."""
    from pathlib import Path

    src = Path(__file__).parent / "session_results.py"
    text = src.read_text()
    # The exact removed shim:
    assert "result.triangulated_by_path.get(DetectionPath.live.value)\n            or []" not in text, (
        "silent fallback `... .get(live) or []` reintroduced — see "
        "CLAUDE.md 'Experimental phase — 禁止 silent fallback'."
    )
    assert "or result.triangulated_by_path.get(DetectionPath.live.value" not in text, (
        "silent server_post→live fallback reintroduced in the recompute "
        "or segment-stamping path."
    )


# ---------------------------------------------------------------------------
# 2. pipeline.py — empty list ≠ None
# ---------------------------------------------------------------------------


def test_pipeline_pass_through_empty_blobs_list_not_none():
    """Source-level invariant: pipeline.py must NOT collapse an empty
    blobs list to None. Reader code distinguishes 'detector ran with
    0 candidates' (empty list) from 'detector did not run' (None);
    `blobs or None` confused these and obscured detection failures."""
    from pathlib import Path

    src = Path(__file__).parent / "pipeline.py"
    text = src.read_text()
    assert "blobs or None" not in text, (
        "Reintroduced `blobs or None` silent fallback in pipeline.py. "
        "Empty blobs list must propagate as []; use `candidates=blobs`."
    )


def test_framepayload_accepts_empty_candidates_list():
    """Schema must permit candidates=[] (the post-fix default for
    detector-ran-but-nothing-found). Regression guard against schema
    drift that would force pipeline.py back into a fallback shim."""
    from schemas import FramePayload

    fp = FramePayload(
        frame_index=0,
        timestamp_s=0.0,
        px=None,
        py=None,
        ball_detected=False,
        candidates=[],
    )
    assert fp.candidates == []
    assert fp.candidates is not None
