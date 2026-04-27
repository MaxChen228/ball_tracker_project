"""Pin the three algorithms cam_view_math.py mirrors to its JS siblings.

The JS sources of truth are listed in cam_view_math.py's module docstring;
when one of these tests breaks, both halves must move together.
"""
from __future__ import annotations

import pytest

from cam_view_math import (
    compare_rows_collapse,
    find_detection_index,
    hit_test_nearest,
)


# --- compare_rows_collapse -------------------------------------------------


def test_collapse_known_only():
    rows = compare_rows_collapse([{"marker_id": 1}, {"marker_id": 2}], [], [])
    assert {r["marker_id"] for r in rows} == {1, 2}
    assert {r["origin"] for r in rows} == {"known"}


def test_collapse_stored_overrides_known_on_collision():
    """Phase 1b regression: known→stored collisions used to drop the
    stored row, leaving the marker stuck as known with no edit path."""
    rows = compare_rows_collapse(
        [{"marker_id": 1, "label": "from-known"}],
        [{"marker_id": 1, "label": "from-stored"}],
        [],
    )
    assert len(rows) == 1
    [row] = rows
    assert row["origin"] == "stored"
    assert row["kind"] == "stored"
    assert row["label"] == "from-stored"


def test_collapse_candidate_overrides_stored_on_collision():
    """Phase 4 reviewer fix: refresh-action candidate must beat stored
    so the operator can review the proposed update."""
    rows = compare_rows_collapse(
        [],
        [{"marker_id": 7, "label": "stored"}],
        [{"marker_id": 7, "label": "candidate"}],
    )
    assert len(rows) == 1
    [row] = rows
    assert row["origin"] == "candidate"
    assert row["kind"] == "candidate"
    assert row["label"] == "candidate"


def test_collapse_candidate_beats_known_directly():
    rows = compare_rows_collapse(
        [{"marker_id": 9}],
        [],
        [{"marker_id": 9, "label": "cand"}],
    )
    [row] = rows
    assert row["origin"] == "candidate"


def test_collapse_keeps_distinct_ids():
    rows = compare_rows_collapse(
        [{"marker_id": 1}],
        [{"marker_id": 2}],
        [{"marker_id": 3}],
    )
    assert {r["marker_id"] for r in rows} == {1, 2, 3}


def test_collapse_strips_internal_priority_field():
    rows = compare_rows_collapse([{"marker_id": 1}], [], [])
    assert "_priority" not in rows[0]


def test_collapse_coerces_id_for_collision_check():
    """JS does `Number(row.marker_id)` before compare. Mirror that so a
    stringified id from one source still collides with an int id from
    another — otherwise the collapse would emit duplicates."""
    rows = compare_rows_collapse(
        [{"marker_id": "5"}],
        [{"marker_id": 5}],
        [],
    )
    assert len(rows) == 1
    assert rows[0]["origin"] == "stored"


# --- find_detection_index --------------------------------------------------


def _frames(*, t_step: float = 1 / 240, n: int = 8) -> list[float]:
    return [i * t_step for i in range(n)]


def test_find_detection_returns_exact_match():
    ts = _frames()
    det = [True] * len(ts)
    idx = find_detection_index(ts, det, ts[3], tol=0.005)
    assert idx == 3


def test_find_detection_left_scan_walks_back_through_gap():
    """Phase 1a regression: chain_filter rejected_jump leaves a run of
    det=False between two detected frames; binary search lands inside
    the gap, but the dot should stick to the prior detected sample
    rather than blank out."""
    ts = _frames(n=10)
    det = [True, True, True, False, False, False, False, True, True, True]
    # currentT lands in the middle of the gap (frame 5).
    idx = find_detection_index(ts, det, ts[5], tol=0.020)
    # Walk back to frame 2 (last detected before the gap).
    assert idx == 2


def test_find_detection_returns_none_when_gap_exceeds_tol():
    """Tighter tol must stop the walk-back at the boundary so the dot
    blanks rather than glomming onto a stale sample beyond tol."""
    ts = _frames(n=10)
    det = [True, False, False, False, False, False, False, False, False, False]
    # currentT at frame 6, tol of 1 frame — left scan can't reach frame 0.
    idx = find_detection_index(ts, det, ts[6], tol=1.5 / 240)
    assert idx is None


def test_find_detection_returns_none_on_all_false():
    ts = _frames()
    det = [False] * len(ts)
    assert find_detection_index(ts, det, ts[2], tol=0.020) is None


def test_find_detection_returns_none_on_empty():
    assert find_detection_index([], [], 0.0, tol=0.020) is None


def test_find_detection_handles_single_sample():
    assert find_detection_index([1.0], [True], 1.0, tol=0.001) == 0
    assert find_detection_index([1.0], [True], 2.0, tol=0.001) is None
    assert find_detection_index([1.0], [False], 1.0, tol=0.001) is None


def test_find_detection_picks_closer_neighbour_at_boundary():
    """Binary search lands between two samples; algorithm picks the
    nearer one before doing the left-scan."""
    ts = [0.0, 0.010, 0.020]
    det = [True, True, True]
    # currentT closer to ts[1]
    assert find_detection_index(ts, det, 0.011, tol=0.005) == 1


# --- hit_test_nearest ------------------------------------------------------


def _identity_proj(row: dict) -> tuple[float, float] | None:
    if row.get("u") is None:
        return None
    return (row["u"], row["v"])


def test_hit_test_returns_nearest_within_tol():
    rows = [
        {"marker_id": 1, "u": 100, "v": 100, "origin": "stored"},
        {"marker_id": 2, "u": 200, "v": 200, "origin": "stored"},
    ]
    hit = hit_test_nearest(rows, (105, 105), _identity_proj, tol_px=20)
    assert hit is not None
    assert hit["marker_id"] == 1


def test_hit_test_candidate_wins_tie_break():
    """Phase 4 reviewer fix: when a candidate sits on top of a stored
    row at the same xyz (refresh-action), candidate must win the
    hit-test so the operator can review the proposed update."""
    rows = [
        {"marker_id": 1, "u": 100, "v": 100, "origin": "stored"},
        {"marker_id": 1, "u": 100, "v": 100, "origin": "candidate"},
    ]
    hit = hit_test_nearest(rows, (100, 100), _identity_proj, tol_px=10)
    assert hit is not None
    assert hit["origin"] == "candidate"


def test_hit_test_returns_none_outside_tol():
    rows = [{"marker_id": 1, "u": 100, "v": 100, "origin": "stored"}]
    assert hit_test_nearest(rows, (300, 300), _identity_proj, tol_px=20) is None


def test_hit_test_skips_rows_that_fail_to_project():
    """Out-of-frame markers have no projected (u, v); the projector
    returns None and they should be skipped, not crash the hit-test."""
    rows = [
        {"marker_id": 1, "u": None, "v": None, "origin": "stored"},
        {"marker_id": 2, "u": 100, "v": 100, "origin": "stored"},
    ]
    hit = hit_test_nearest(rows, (100, 100), _identity_proj, tol_px=10)
    assert hit is not None
    assert hit["marker_id"] == 2


def test_hit_test_breaks_ties_at_same_rank_by_distance():
    rows = [
        {"marker_id": 1, "u": 100, "v": 100, "origin": "stored"},
        {"marker_id": 2, "u": 110, "v": 110, "origin": "stored"},
    ]
    hit = hit_test_nearest(rows, (100, 100), _identity_proj, tol_px=20)
    assert hit is not None
    assert hit["marker_id"] == 1
