"""Tests for live_pairing candidate selection (multi-candidate live path).

Selector is shape-prior (track-independent) and scale-invariant. iOS
ships aspect/fill on every candidate as of the wire-schema update;
None only appears on legacy persisted frames reloaded for offline
analysis (covered by `test_legacy_no_shape_data_zero_cost`)."""
from __future__ import annotations

from live_pairing import LivePairingSession
from schemas import BlobCandidate, FramePayload


def _frame(idx: int, t: float, *, candidates: list[BlobCandidate]) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=t,
        candidates=candidates,
        ball_detected=bool(candidates),
    )


def _no_triangulate(_cam, _a, _b):
    return None


def test_aspect_fill_pick_round_typical():
    """Two candidates with valid wire shape stats. The round, typical-
    fill one wins; the oblong, atypical-fill one loses."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=10.0, py=10.0, area=452, area_score=1.0,
                      aspect=0.6, fill=0.40),   # bad shape
        BlobCandidate(px=999.0, py=999.0, area=452, area_score=1.0,
                      aspect=1.0, fill=0.68),   # round + typical
    ]
    sess.ingest("A", _frame(0, 0.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.px == 999.0 and stored.py == 999.0


def test_size_does_not_decide_winner():
    """Two candidates with identical aspect+fill but very different
    areas → tie under the scale-invariant cost. Argmin tie-break picks
    the first; the winner is NOT determined by size."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=100.0, py=100.0, area=80, area_score=0.4,
                      aspect=1.0, fill=0.68),
        BlobCandidate(px=900.0, py=900.0, area=4000, area_score=1.0,
                      aspect=1.0, fill=0.68),
    ]
    sess.ingest("A", _frame(0, 0.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.px == 100.0


def test_legacy_no_shape_data_zero_cost():
    """Legacy persisted frames (aspect=fill=None) score zero on both
    axes → all candidates tie at cost=0. Argmin tie-break picks the
    first. Not a useful selection in practice; this just locks the
    neutral-default contract."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=10.0, py=10.0, area=80, area_score=0.4),
        BlobCandidate(px=300.0, py=400.0, area=400, area_score=1.0),
        BlobCandidate(px=999.0, py=999.0, area=4000, area_score=0.5),
    ]
    sess.ingest("A", _frame(0, 1.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    # First candidate wins by argmin tie-break (all costs equal 0).
    assert stored.px == 10.0 and stored.py == 10.0
    assert stored.ball_detected is True
    for c in stored.candidates:
        assert c.cost == 0.0


def test_empty_candidates_marks_no_detection():
    sess = LivePairingSession("s_test")
    sess.ingest("A", _frame(0, 0.0, candidates=[]), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.ball_detected is False
    assert stored.px is None and stored.py is None


def test_per_camera_independence():
    """Cam A and Cam B selection runs are independent (each frame is
    scored on its own candidates with no cross-cam state)."""
    sess = LivePairingSession("s_test")
    sess.ingest("A", _frame(0, 0.0, candidates=[
        BlobCandidate(px=100.0, py=100.0, area=452, area_score=1.0,
                      aspect=1.0, fill=0.68),
    ]), _no_triangulate)
    sess.ingest("B", _frame(0, 0.0, candidates=[
        BlobCandidate(px=999.0, py=999.0, area=452, area_score=1.0,
                      aspect=1.0, fill=0.68),
    ]), _no_triangulate)
    assert sess.frames_by_cam["A"][0].px == 100.0
    assert sess.frames_by_cam["B"][0].px == 999.0


def test_resolved_frame_winner_has_min_cost():
    """`_resolve_candidates` stamps `cost` on every BlobCandidate; the
    winner's cost is the minimum. Viewer relies on this for top-K
    rendering — what was used to pick must match what's persisted."""
    sess = LivePairingSession("s_test")
    raw = [
        BlobCandidate(px=10.0, py=10.0, area=80, area_score=0.4,
                      aspect=0.6, fill=0.30),    # bad shape
        BlobCandidate(px=300.0, py=400.0, area=452, area_score=1.0,
                      aspect=1.0, fill=0.68),    # round + typical
        BlobCandidate(px=50.0, py=50.0, area=4000, area_score=0.6,
                      aspect=0.7, fill=0.50),    # mid
    ]
    sess.ingest("A", _frame(0, 0.0, candidates=raw), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    cands = stored.candidates
    assert cands is not None and len(cands) == 3
    for c in cands:
        assert c.cost is not None and 0.0 <= c.cost <= 1.0
    min_cost = min(c.cost for c in cands)
    # cands[1] has best shape — should win.
    assert cands[1].cost == min_cost
    assert (stored.px, stored.py) == (cands[1].px, cands[1].py)
