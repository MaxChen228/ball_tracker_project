"""Tests for live_pairing candidate selection (multi-candidate live path).

Selector is shape-prior (track-independent). iOS-sourced BlobCandidates
predate the aspect/fill wire-schema fields, so this test exercises the
neutral-default path: with aspect=fill=None, the cost reduces to
size_pen alone."""
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


def test_legacy_ios_candidates_pick_closest_to_expected_area():
    """iOS sends candidates without aspect/fill (legacy). Cost reduces
    to log-area distance from expected_area = π·12² ≈ 452. The blob
    nearest expected wins."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=50.0, py=50.0, area=80, area_score=0.4),    # too small
        BlobCandidate(px=300.0, py=400.0, area=400, area_score=1.0), # near expected
        BlobCandidate(px=10.0, py=10.0, area=4000, area_score=0.5),  # too big
    ]
    sess.ingest("A", _frame(0, 1.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.px == 300.0 and stored.py == 400.0
    assert stored.ball_detected is True


def test_with_shape_data_aspect_fill_break_size_tie():
    """If two candidates are equally close to expected_area, the one
    with better aspect+fill wins."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=10.0, py=10.0, area=452, area_score=1.0,
                      aspect=0.6, fill=0.40),  # bad shape
        BlobCandidate(px=999.0, py=999.0, area=452, area_score=1.0,
                      aspect=1.0, fill=0.68),  # round + typical
    ]
    sess.ingest("A", _frame(0, 0.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.px == 999.0 and stored.py == 999.0


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
        BlobCandidate(px=100.0, py=100.0, area=452, area_score=1.0),
    ]), _no_triangulate)
    sess.ingest("B", _frame(0, 0.0, candidates=[
        BlobCandidate(px=999.0, py=999.0, area=452, area_score=1.0),
    ]), _no_triangulate)
    assert sess.frames_by_cam["A"][0].px == 100.0
    assert sess.frames_by_cam["B"][0].px == 999.0


def test_resolved_frame_winner_has_min_cost():
    """`_resolve_candidates` stamps `cost` on every BlobCandidate; the
    winner's cost is the minimum. Viewer relies on this for top-K
    rendering — what was used to pick must match what's persisted."""
    sess = LivePairingSession("s_test")
    raw = [
        BlobCandidate(px=10.0, py=10.0, area=80, area_score=0.4),    # too small
        BlobCandidate(px=300.0, py=400.0, area=452, area_score=1.0), # at expected
        BlobCandidate(px=50.0, py=50.0, area=4000, area_score=0.6),  # too big
    ]
    sess.ingest("A", _frame(0, 0.0, candidates=raw), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    cands = stored.candidates
    assert cands is not None and len(cands) == 3
    for c in cands:
        assert c.cost is not None and 0.0 <= c.cost <= 1.0
    min_cost = min(c.cost for c in cands)
    # cands[1] is the at-expected blob — should win.
    assert cands[1].cost == min_cost
    assert (stored.px, stored.py) == (cands[1].px, cands[1].py)
