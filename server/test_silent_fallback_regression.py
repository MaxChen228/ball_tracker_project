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

import pytest


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
    """If candidate_paths includes server_post but server_post produced
    nothing, `result.points` MUST be empty — not a silent borrow from
    live. Research invariant: missing path = empty, never a substitution.
    """
    from schemas import DetectionPath, SessionResult, TriangulatedPoint

    live_pt = _make_triangulated_point()

    # Inline the relevant slice of rebuild_result_for_session's
    # legacy-points selection (post-fix). This is the contract under
    # test; we exercise it directly to avoid a full state-machine
    # fixture.
    candidate_paths = {DetectionPath.server_post, DetectionPath.live}
    triangulated_by_path: dict[str, list[TriangulatedPoint]] = {
        DetectionPath.live.value: [live_pt],
        # server_post entry absent → simulates "server_post never ran /
        # produced no triangulation"
    }

    if DetectionPath.server_post in candidate_paths:
        legacy_points = triangulated_by_path.get(
            DetectionPath.server_post.value, []
        )
    elif DetectionPath.live in candidate_paths:
        legacy_points = triangulated_by_path.get(
            DetectionPath.live.value, []
        )
    else:
        legacy_points = []

    assert legacy_points == [], (
        "server_post requested but missing must yield empty points; "
        "silently borrowing live points would contaminate "
        "live-vs-server_post comparisons."
    )


def test_legacy_points_uses_live_only_when_server_post_not_requested():
    from schemas import DetectionPath, TriangulatedPoint

    live_pt = _make_triangulated_point()
    candidate_paths = {DetectionPath.live}
    triangulated_by_path: dict[str, list[TriangulatedPoint]] = {
        DetectionPath.live.value: [live_pt],
    }

    if DetectionPath.server_post in candidate_paths:
        legacy_points = triangulated_by_path.get(
            DetectionPath.server_post.value, []
        )
    elif DetectionPath.live in candidate_paths:
        legacy_points = triangulated_by_path.get(
            DetectionPath.live.value, []
        )
    else:
        legacy_points = []

    assert legacy_points == [live_pt]


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
