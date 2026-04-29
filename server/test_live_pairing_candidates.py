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
    return []


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


# ---------- multi-candidate fan-out + dedupe tests ----------

from schemas import TriangulatedPoint


def _make_point(t_rel: float, ca_idx: int, cb_idx: int,
                gap: float = 0.05) -> TriangulatedPoint:
    return TriangulatedPoint(
        t_rel_s=t_rel, x_m=0.0, y_m=0.0, z_m=0.0,
        residual_m=gap,
        source_a_cand_idx=ca_idx,
        source_b_cand_idx=cb_idx,
    )


def test_dedupe_key_extended_with_candidate_idx():
    """Two ingests of the same frame-pair but different candidate-pair
    indices should both land — old 2-tuple key would have collapsed
    them into one. (a,b,0,1) and (a,b,1,0) are different physical
    candidate pairs, both should survive dedupe."""
    sess = LivePairingSession("s_test")
    cands = [BlobCandidate(px=10.0, py=10.0, area=100, area_score=1.0,
                            aspect=1.0, fill=0.68)]
    # Seed cam B's buffer first (same timestamp, single candidate).
    sess.ingest("B", _frame(0, 0.0, candidates=cands), _no_triangulate)
    # Now A ingest triggers triangulation. Closure emits two points
    # with different candidate-index pairs.
    def _two_points(_cam, _a, _b):
        return [_make_point(0.0, 0, 1), _make_point(0.0, 1, 0)]
    new_pts = sess.ingest("A", _frame(0, 0.0, candidates=cands), _two_points)
    assert len(new_pts) == 2
    # Same call again should dedupe to zero (same keys).
    new_pts = sess.ingest("A", _frame(0, 0.0, candidates=cands), _two_points)
    # frame_index repeats are filtered earlier (peer windowing) but the
    # dedupe set should also block re-emission via key match.
    # A repeated A frame at frame_index=0 will re-pair against the
    # cam-B frame with its existing key.
    assert len(new_pts) == 0


def test_dedupe_key_canonicalized_across_cam_directions():
    """Same physical candidate pair, ingest via cam A vs ingest via
    cam B → same dedupe key. Without canonicalization, B-triggered
    ingest would produce a different key from A-triggered, double-
    counting the pair."""
    cands = [BlobCandidate(px=10.0, py=10.0, area=100, area_score=1.0,
                            aspect=1.0, fill=0.68)]

    # Direction 1: B arrives first, A triggers.
    sess1 = LivePairingSession("s_test")
    sess1.ingest("B", _frame(5, 0.0, candidates=cands), _no_triangulate)
    sess1.ingest("A", _frame(7, 0.0, candidates=cands),
                 lambda _cam, _a, _b: [_make_point(0.0, 0, 0)])
    keys1 = set(sess1.paired_frame_ids)

    # Direction 2: A arrives first, B triggers.
    sess2 = LivePairingSession("s_test")
    sess2.ingest("A", _frame(7, 0.0, candidates=cands), _no_triangulate)
    sess2.ingest("B", _frame(5, 0.0, candidates=cands),
                 lambda _cam, _a, _b: [_make_point(0.0, 0, 0)])
    keys2 = set(sess2.paired_frame_ids)

    # Same canonical key under both ingest directions: A frame_idx 7,
    # B frame_idx 5, both candidate indices 0.
    assert keys1 == {(7, 5, 0, 0)}
    assert keys2 == {(7, 5, 0, 0)}


def test_fan_out_emits_all_passing_pairs():
    """Closure emits 6 points (2A × 3B), session keeps all 6 + records
    each under a distinct dedupe key. Zero points dropped."""
    sess = LivePairingSession("s_test")
    cands = [BlobCandidate(px=10.0, py=10.0, area=100, area_score=1.0,
                            aspect=1.0, fill=0.68)]
    sess.ingest("B", _frame(0, 0.0, candidates=cands), _no_triangulate)

    def _six_points(_cam, _a, _b):
        return [
            _make_point(0.0, ca, cb)
            for ca in range(2) for cb in range(3)
        ]
    pts = sess.ingest("A", _frame(0, 0.0, candidates=cands), _six_points)
    assert len(pts) == 6
    # 6 unique dedupe keys recorded.
    assert len(sess.paired_frame_ids) == 6


def test_fan_out_skips_within_pair_dedupe():
    """Closure returns two points with the SAME (ca, cb) index pair
    (e.g. via redundant triangulation). The second one is deduped out."""
    sess = LivePairingSession("s_test")
    cands = [BlobCandidate(px=10.0, py=10.0, area=100, area_score=1.0,
                            aspect=1.0, fill=0.68)]
    sess.ingest("B", _frame(0, 0.0, candidates=cands), _no_triangulate)

    def _duplicate_keys(_cam, _a, _b):
        return [_make_point(0.0, 0, 0), _make_point(0.0, 0, 0)]
    pts = sess.ingest("A", _frame(0, 0.0, candidates=cands), _duplicate_keys)
    assert len(pts) == 1
    assert len(sess.paired_frame_ids) == 1
