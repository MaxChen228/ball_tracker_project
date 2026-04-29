"""Unit tests for `candidate_selector.select_best_candidate` and the
underlying `score_candidates` helper. Cost is shape-prior (track-
independent): size + aspect + fill, no temporal input."""
from __future__ import annotations

import cv2
import numpy as np

from candidate_selector import (
    Candidate,
    CandidateSelectorTuning,
    score_candidates,
    select_best_candidate,
)
from detection import HSVRange, detect_ball, detect_ball_with_candidates


_T = CandidateSelectorTuning.default()


def _yg() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def test_empty_returns_none():
    assert select_best_candidate([], _T) is None


def test_score_candidates_empty_returns_empty():
    assert score_candidates([], _T) == []


def test_size_prior_picks_expected_radius():
    """expected_area = π·12² ≈ 452. A blob right at expected wins over
    one an octave too small or too large, holding aspect/fill equal."""
    expected = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    too_small = Candidate(cx=0, cy=0, area=100, aspect=1.0, fill=0.68)
    too_big = Candidate(cx=0, cy=0, area=2000, aspect=1.0, fill=0.68)
    assert select_best_candidate([expected, too_small, too_big], _T) is expected


def test_aspect_prior_prefers_round():
    """Equal area+fill, perfectly round beats oblong."""
    round_ = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    oblong = Candidate(cx=0, cy=0, area=452, aspect=0.6, fill=0.68)
    assert select_best_candidate([round_, oblong], _T) is round_


def test_fill_prior_prefers_typical():
    """Equal area+aspect, fill at empirical median (0.68) wins over
    fill far from it."""
    typical = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    too_dense = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=1.0)
    assert select_best_candidate([typical, too_dense], _T) is typical


def test_unknown_aspect_fill_is_neutral():
    """`aspect=None` / `fill=None` (iOS-sourced legacy candidates)
    contribute 0 to their respective penalties — explicit design,
    documented in module docstring. Effectively reduces cost to size-only."""
    a = Candidate(cx=0, cy=0, area=452, aspect=None, fill=None)
    b = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68)
    # Both are at expected_area + neutral on the missing axes for `a`,
    # known-good on the present axes for `b`. Costs equal under the
    # neutral-default contract.
    costs = score_candidates([a, b], _T)
    # Both reduce to size_pen + 0 (neutral on aspect/fill). area=452
    # ≈ π·12² so size_pen ≈ 0; floating-point slop allowed.
    assert abs(costs[0] - costs[1]) < 1e-9
    assert costs[0] < 1e-3


def test_costs_in_unit_interval():
    cands = [
        Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68),
        Candidate(cx=0, cy=0, area=20, aspect=0.5, fill=0.0),
        Candidate(cx=0, cy=0, area=8000, aspect=0.6, fill=1.0),
    ]
    for c in score_candidates(cands, _T):
        assert 0.0 <= c <= 1.0


def test_select_best_equals_argmin_score():
    """White-box invariant: `select_best_candidate` is the argmin
    indexed back into the input list."""
    cands = [
        Candidate(cx=0, cy=0, area=300, aspect=0.85, fill=0.55),
        Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=0.68),
        Candidate(cx=0, cy=0, area=50, aspect=0.7, fill=0.45),
    ]
    costs = score_candidates(cands, _T)
    argmin = min(range(len(costs)), key=lambda i: costs[i])
    assert select_best_candidate(cands, _T) is cands[argmin]


# --- end-to-end on a synthetic frame ---

def test_detect_ball_picks_correctly_sized_circle():
    """Two yellow circles in frame: one near expected radius, one
    way too big. detect_ball must pick the near-expected one."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 12, _yg(), thickness=-1)  # ~452 px², spot on
    cv2.circle(img, (500, 100), 30, _yg(), thickness=-1)  # ~2800 px², way too big
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
    cv2.circle(img, (220, 240), 12, _yg(), thickness=-1)
    cv2.circle(img, (500, 100), 30, _yg(), thickness=-1)
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
        cv2.circle(img, (220, 240), 12, _yg(), thickness=-1)
        cv2.circle(img, (500, 100), 30, _yg(), thickness=-1)
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
