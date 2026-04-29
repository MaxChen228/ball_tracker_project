"""Unit tests for `candidate_selector.select_best_candidate` and the
underlying `score_candidates` helper. Cost is shape-prior (track-
independent) and scale-invariant: aspect + fill only, no size term,
no temporal input."""
from __future__ import annotations

import cv2
import numpy as np

from candidate_selector import (
    Candidate,
    score_candidates,
    select_best_candidate,
)
from detection import HSVRange, detect_ball, detect_ball_with_candidates


def _yg() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def test_empty_returns_none():
    assert select_best_candidate([]) is None


def test_score_candidates_empty_returns_empty():
    assert score_candidates([]) == []


def test_ideal_candidate_has_zero_cost():
    """A blob at aspect=1.0 (perfectly square bbox) and fill=0.68
    (project-ball median) is the ideal-shape point — both penalties
    evaluate to zero, so total cost is exactly 0. Locks the
    ideal-point invariant directly."""
    ideal = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    assert score_candidates([ideal]) == [0.0]


def test_size_does_not_affect_cost():
    """Selector is scale-invariant: holding aspect/fill equal, area
    has no effect. A blob 10× larger costs exactly the same."""
    small = Candidate(cx=0, cy=0, area=20, aspect=1.0, fill=0.68)
    big = Candidate(cx=0, cy=0, area=10000, aspect=1.0, fill=0.68)
    costs = score_candidates([small, big])
    assert costs[0] == costs[1]


def test_aspect_prior_prefers_round():
    """Equal fill, perfectly round beats oblong."""
    round_ = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    oblong = Candidate(cx=0, cy=0, area=452, aspect=0.6, fill=0.68)
    assert select_best_candidate([round_, oblong]) is round_


def test_fill_prior_prefers_typical():
    """Equal aspect, fill at empirical median (0.68) wins over fill
    far from it."""
    typical = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    too_dense = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=1.0)
    assert select_best_candidate([typical, too_dense]) is typical


def test_unknown_aspect_fill_is_neutral():
    """`aspect=None` / `fill=None` (legacy persisted JSONs predating
    aspect/fill capture) contribute 0 to both penalties — explicit
    neutral-default per module docstring. Effectively all-zero cost,
    so argmin tie-break (first index) decides."""
    a = Candidate(cx=0, cy=0, area=452, aspect=None, fill=None)
    b = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    costs = score_candidates([a, b])
    # `a` reduces to 0 + 0 (both axes neutral). `b` is also at the
    # ideal aspect=1 / fill=0.68, so its cost is also 0. Equal.
    assert abs(costs[0] - costs[1]) < 1e-9
    assert costs[0] < 1e-9


def test_costs_in_unit_interval():
    cands = [
        Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68),
        Candidate(cx=0, cy=0, area=20, aspect=0.5, fill=0.0),
        Candidate(cx=0, cy=0, area=8000, aspect=0.6, fill=1.0),
    ]
    for c in score_candidates(cands):
        assert 0.0 <= c <= 1.0


def test_select_best_equals_argmin_score():
    """White-box invariant: `select_best_candidate` is the argmin
    indexed back into the input list."""
    cands = [
        Candidate(cx=0, cy=0, area=300, aspect=0.85, fill=0.55),
        Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68),
        Candidate(cx=0, cy=0, area=50, aspect=0.7, fill=0.45),
    ]
    costs = score_candidates(cands)
    argmin = min(range(len(costs)), key=lambda i: costs[i])
    assert select_best_candidate(cands) is cands[argmin]


# --- end-to-end on a synthetic frame ---

def test_detect_ball_prefers_round_over_oblong():
    """A round circle and an oblong ellipse both pass the shape gate,
    but the round one has lower aspect_pen → wins. Locks the contract:
    detect_ball runs the scale-invariant selector under the hood."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 15, _yg(), thickness=-1)               # round
    cv2.ellipse(img, (500, 100), (28, 22), 0, 0, 360, _yg(), -1)        # oblong
    out = detect_ball(img, HSVRange.default())
    assert out is not None
    cx, cy = out
    assert abs(cx - 220) < 3 and abs(cy - 240) < 3


def test_detect_ball_with_candidates_returns_winner_and_scored_blobs():
    """Returns both the winner (lowest-cost BlobCandidate) and the full
    scored list with aspect/fill/area_score/cost stamped, so
    `pipeline.detect_pitch` can populate `FramePayload.candidates` for
    the viewer's BLOBS overlay on the server_post path."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 15, _yg(), thickness=-1)
    cv2.ellipse(img, (500, 100), (28, 22), 0, 0, 360, _yg(), -1)
    winner, blobs = detect_ball_with_candidates(img, HSVRange.default())
    assert winner is not None
    assert len(blobs) >= 2
    for b in blobs:
        assert b.cost is not None
        assert 0.0 <= b.cost <= 1.0
        assert 0.0 <= b.area_score <= 1.0
        # Producer-side aspect/fill must be populated (Phase 2 contract).
        assert b.aspect is not None and 0.0 <= b.aspect <= 1.0
        assert b.fill is not None and 0.0 <= b.fill <= 1.0
    assert min(blobs, key=lambda b: b.cost) is winner
    assert abs(winner.px - 220) < 3 and abs(winner.py - 240) < 3


def test_detect_ball_with_candidates_empty_returns_none_and_empty():
    img = np.zeros((100, 100, 3), dtype=np.uint8)  # no yellow → no survivors
    winner, blobs = detect_ball_with_candidates(img, HSVRange.default())
    assert winner is None
    assert blobs == []


def test_pipeline_detect_pitch_stamps_candidates_with_cost():
    """End-to-end: synthetic frame iterator → detect_pitch → emitted
    FramePayload carries `candidates` with cost+aspect+fill stamped."""
    from pathlib import Path
    from pipeline import detect_pitch

    def _frame_iter(path, video_start_pts_s):
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.circle(img, (220, 240), 15, _yg(), thickness=-1)
        cv2.ellipse(img, (500, 100), (28, 22), 0, 0, 360, _yg(), -1)
        yield (0.0, img)

    out = detect_pitch(
        Path("/tmp/fake.mov"),
        video_start_pts_s=0.0,
        frame_iter=_frame_iter,
    )
    assert len(out) == 1
    f = out[0]
    assert f.ball_detected is True
    assert f.candidates is not None and len(f.candidates) >= 2
    for c in f.candidates:
        assert c.cost is not None
        assert 0.0 <= c.cost <= 1.0
        assert c.aspect is not None
        assert c.fill is not None
