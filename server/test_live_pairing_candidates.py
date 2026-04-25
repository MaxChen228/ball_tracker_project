"""Tests for live_pairing candidate-selection (multi-candidate live path)."""
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


def test_first_frame_with_candidates_picks_largest_area():
    """No prior → explicit largest-area fallback (matches candidate_selector)."""
    sess = LivePairingSession("s_test")
    cands = [
        BlobCandidate(px=50.0, py=50.0, area=80, area_score=0.4),
        BlobCandidate(px=300.0, py=400.0, area=200, area_score=1.0),
    ]
    sess.ingest("A", _frame(0, 1.0, candidates=cands), _no_triangulate)
    stored = sess.frames_by_cam["A"][0]
    assert stored.px == 300.0 and stored.py == 400.0
    assert stored.ball_detected is True


def test_temporal_prior_beats_largest_area():
    """After two hits build a velocity, an off-trajectory bigger blob loses
    to a smaller blob sitting on the predicted next position."""
    sess = LivePairingSession("s_test")
    # Frame 0 — single candidate at (100, 100)
    sess.ingest("A", _frame(0, 0.0, candidates=[
        BlobCandidate(px=100.0, py=100.0, area=100, area_score=1.0),
    ]), _no_triangulate)
    # Frame 1 — single candidate at (110, 100). Velocity = (10, 0) px/s
    sess.ingest("A", _frame(1, 1.0, candidates=[
        BlobCandidate(px=110.0, py=100.0, area=100, area_score=1.0),
    ]), _no_triangulate)
    # Frame 2 — small blob near predicted (120, 100); huge clutter at (500, 500)
    sess.ingest("A", _frame(2, 2.0, candidates=[
        BlobCandidate(px=120.0, py=100.0, area=80, area_score=0.4),
        BlobCandidate(px=500.0, py=500.0, area=200, area_score=1.0),
    ]), _no_triangulate)
    stored = sess.frames_by_cam["A"][2]
    assert stored.px == 120.0 and stored.py == 100.0


def test_miss_resets_temporal_prior():
    """A frame with empty candidates → ball_detected False, prior cleared."""
    sess = LivePairingSession("s_test")
    sess.ingest("A", _frame(0, 0.0, candidates=[
        BlobCandidate(px=100.0, py=100.0, area=100, area_score=1.0),
    ]), _no_triangulate)
    sess.ingest("A", _frame(1, 1.0, candidates=[]), _no_triangulate)
    assert "A" not in sess.last_position
    assert "A" not in sess.last_velocity


def test_per_camera_state_isolated():
    """Cam A's prior must not influence cam B's selection."""
    sess = LivePairingSession("s_test")
    sess.ingest("A", _frame(0, 0.0, candidates=[
        BlobCandidate(px=100.0, py=100.0, area=100, area_score=1.0),
    ]), _no_triangulate)
    # Cam B first frame — no prior on B → falls back to largest area
    sess.ingest("B", _frame(0, 0.0, candidates=[
        BlobCandidate(px=10.0, py=10.0, area=50, area_score=0.5),
        BlobCandidate(px=999.0, py=999.0, area=200, area_score=1.0),
    ]), _no_triangulate)
    stored = sess.frames_by_cam["B"][0]
    assert stored.px == 999.0
