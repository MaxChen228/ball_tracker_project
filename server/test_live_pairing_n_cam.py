"""Phase 2a live-path N-camera pairing: the incoming frame pairs against
EVERY peer cam independently (pair-as-atom), and a cam with a missing /
partial anchor only drops ITS pairs — the others keep emitting.

The callback records which (cam_lo, cam_hi) pairs it was invoked for, so
we can assert the fan-out and the failure isolation without real geometry.
"""
from __future__ import annotations

from live_pairing import LivePairingSession
from schemas import BlobCandidate, FramePayload, TriangulatedPoint


def _frame(idx: int, t: float) -> FramePayload:
    return FramePayload(
        frame_index=idx, timestamp_s=t, ball_detected=True,
        candidates=[BlobCandidate(px=10.0 + idx, py=20.0, area=100,
                                  area_score=1.0, aspect=1.0, fill=0.68)],
    )


def _point() -> TriangulatedPoint:
    return TriangulatedPoint(
        t_rel_s=0.0, x_m=0.0, y_m=0.0, z_m=0.0, residual_m=0.0,
        source_a_cand_idx=0, source_b_cand_idx=0, cost_a=0.1, cost_b=0.1,
    )


def test_incoming_frame_pairs_against_all_peers():
    """A and B already have a frame; C arrives → C pairs with BOTH A and B.
    The callback fires once per (cam_lo, cam_hi) pair."""
    sess = LivePairingSession("s_n3")
    anchors = {"A": 0.0, "B": 0.0, "C": 0.0}
    seen_pairs: list[tuple[str, str]] = []

    def _cb(cam_lo, cam_hi, _flo, _fhi):
        seen_pairs.append((cam_lo, cam_hi))
        return [_point()]

    sess.ingest("A", _frame(0, 0.0), _cb, anchors=anchors)
    sess.ingest("B", _frame(0, 0.0), _cb, anchors=anchors)
    seen_pairs.clear()
    created = sess.ingest("C", _frame(0, 0.0), _cb, anchors=anchors)

    assert set(seen_pairs) == {("A", "C"), ("B", "C")}
    assert len(created) == 2  # one point per pair


def test_partial_anchor_isolates_only_that_pair():
    """C has no anchor (never synced). A and B are synced. When C arrives,
    its pairs (A|C, B|C) are skipped — but a subsequent A frame still pairs
    with B. Failure is per-pair, not whole-session."""
    sess = LivePairingSession("s_iso")
    anchors = {"A": 0.0, "B": 0.0, "C": None}
    fired: list[tuple[str, str]] = []

    def _cb(cam_lo, cam_hi, _flo, _fhi):
        fired.append((cam_lo, cam_hi))
        return [_point()]

    sess.ingest("A", _frame(0, 0.0), _cb, anchors=anchors)
    sess.ingest("B", _frame(1, 0.0), _cb, anchors=anchors)
    # C arrives but is unsynced → both its pairs skip.
    fired.clear()
    c_created = sess.ingest("C", _frame(2, 0.0), _cb, anchors=anchors)
    assert fired == []
    assert c_created == []
    # A arrives again → still pairs with B (and would skip C).
    fired.clear()
    sess.ingest("A", _frame(3, 0.0), _cb, anchors=anchors)
    assert ("A", "B") in fired
    assert ("A", "C") not in fired
    assert ("B", "C") not in fired


def test_dedup_is_per_pair():
    """The same A frame paired against B and against C must NOT collide in
    the dedup set — distinct cam-pair prefixes keep them separate."""
    sess = LivePairingSession("s_dedup")
    anchors = {"A": 0.0, "B": 0.0, "C": 0.0}

    def _cb(cam_lo, cam_hi, _flo, _fhi):
        return [_point()]

    sess.ingest("B", _frame(0, 0.0), _cb, anchors=anchors)
    sess.ingest("C", _frame(0, 0.0), _cb, anchors=anchors)  # B↔C pairs here
    created = sess.ingest("A", _frame(0, 0.0), _cb, anchors=anchors)
    # A's ingest fans out to A|B and A|C → 2 fresh points.
    assert len(created) == 2
    # All three pairs are represented in the dedup set, each under its own
    # cam-pair prefix (no collision between A|B and A|C on the shared A frame).
    keys = {k[:2] for k in sess.paired_frame_ids}
    assert keys == {("A", "B"), ("A", "C"), ("B", "C")}
