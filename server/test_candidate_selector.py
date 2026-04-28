"""Unit tests for `candidate_selector.select_best_candidate` and the
underlying `score_candidates` helper."""
from __future__ import annotations

import cv2
import numpy as np

from candidate_selector import (
    Candidate,
    CandidateSelectorTuning,
    score_candidates,
    select_best_candidate,
)
from detection import HSVRange, detect_ball


_DEFAULT_T = CandidateSelectorTuning.default()
_DEFAULTS = dict(
    w_area=_DEFAULT_T.w_area,
    w_dist=_DEFAULT_T.w_dist,
    dist_cost_sat_radii=_DEFAULT_T.dist_cost_sat_radii,
)


def _yg() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def test_empty_returns_none():
    assert select_best_candidate([], **_DEFAULTS) is None


def test_fallback_largest_without_prior():
    small = Candidate(cx=10, cy=10, area=100, area_score=0.5)
    big = Candidate(cx=200, cy=200, area=200, area_score=1.0)
    assert select_best_candidate([small, big], **_DEFAULTS) is big


def test_temporal_prior_beats_largest():
    """Big distracter far from prediction loses to small candidate
    near the predicted trajectory point."""
    prev_pos = (100.0, 100.0)
    prev_vel = (50.0, 0.0)  # 50 px/s in x
    dt = 1.0 / 240  # one frame @ 240 fps
    r = 12.0
    # predicted = (100 + 50/240, 100) ≈ (100.2, 100)
    near_small = Candidate(cx=102.0, cy=100.0, area=400, area_score=0.4)
    far_big = Candidate(cx=500.0, cy=500.0, area=1000, area_score=1.0)
    winner = select_best_candidate(
        [near_small, far_big],
        prev_position=prev_pos,
        prev_velocity=prev_vel,
        dt=dt,
        r_px_expected=r,
        **_DEFAULTS,
    )
    assert winner is near_small


def test_temporal_prior_fallback_on_non_finite_dt():
    """dt=0 or inf → fallback to largest (explicit behavior, not silent)."""
    c_small = Candidate(cx=0, cy=0, area=10, area_score=0.1)
    c_big = Candidate(cx=9999, cy=9999, area=9999, area_score=1.0)
    out = select_best_candidate(
        [c_small, c_big],
        prev_position=(0, 0),
        prev_velocity=(0, 0),
        dt=0.0,
        r_px_expected=10.0,
        **_DEFAULTS,
    )
    assert out is c_big


def test_detect_ball_end_to_end_temporal_prefers_near_prediction():
    """Put the ball near a predicted point and a larger distracter far
    away; the selector must pick the ball despite being smaller."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)  # small, near-pred
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)  # big distracter
    prev_pos = (200.0, 240.0)
    prev_vel = (4800.0, 0.0)  # 4800 px/s => +20 px/frame @ 240 fps
    dt = 1.0 / 240
    out = detect_ball(
        img, HSVRange.default(),
        prev_position=prev_pos,
        prev_velocity=prev_vel,
        dt=dt,
    )
    assert out is not None
    cx, cy = out
    # ball sits at (220, 240), not at distracter (500, 100).
    assert abs(cx - 220) < 3 and abs(cy - 240) < 3


def test_detect_ball_no_prior_picks_largest():
    """Without prev state, detect_ball must still pick the biggest
    candidate — backward-compat with the old behavior."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)
    out = detect_ball(img, HSVRange.default())
    assert out is not None
    cx, cy = out
    # bigger distracter at (500, 100).
    assert abs(cx - 500) < 3 and abs(cy - 100) < 3


# --- score_candidates tests (Phase: cost persistence) ---

def test_score_candidates_returns_costs_in_order():
    """Output length / order matches input; every cost ∈ [0,1] under
    default tuning."""
    cands = [
        Candidate(cx=100.0, cy=100.0, area=100, area_score=0.5),
        Candidate(cx=200.0, cy=200.0, area=200, area_score=1.0),
        Candidate(cx=50.0, cy=50.0, area=80, area_score=0.4),
    ]
    costs = score_candidates(
        cands,
        prev_position=(110.0, 110.0),
        prev_velocity=(0.0, 0.0),
        dt=1.0 / 240,
        r_px_expected=12.0,
        **_DEFAULTS,
    )
    assert len(costs) == 3
    for c in costs:
        assert 0.0 <= c <= 1.0
    # Empty input → empty output (not None).
    assert score_candidates([], **_DEFAULTS) == []


def test_score_candidates_no_prior_pure_area():
    """No temporal prior → cost == 1 - area_score, exact equality. Pins
    the fallback contract that the viewer relies on for legacy JSONs."""
    cands = [
        Candidate(cx=0, cy=0, area=80, area_score=0.4),
        Candidate(cx=0, cy=0, area=200, area_score=1.0),
        Candidate(cx=0, cy=0, area=120, area_score=0.6),
    ]
    costs = score_candidates(cands, **_DEFAULTS)
    assert costs == [1.0 - 0.4, 1.0 - 1.0, 1.0 - 0.6]


def test_select_best_equivalent_to_argmin_score_candidates():
    """White-box wrapper invariant: `select_best_candidate(...)` MUST be
    `cands[argmin(score_candidates(...))]` for the same args. Catches
    silent score_candidates regressions where the winner happens to
    still be right by luck on the existing black-box tests."""
    cands = [
        Candidate(cx=100.0, cy=100.0, area=100, area_score=0.5),
        Candidate(cx=200.0, cy=200.0, area=200, area_score=1.0),
        Candidate(cx=50.0, cy=50.0, area=80, area_score=0.4),
        Candidate(cx=300.0, cy=300.0, area=150, area_score=0.75),
    ]
    # Run across several prior configurations to widen the surface.
    configs = [
        # No prior — fallback path.
        dict(prev_position=None, prev_velocity=None, dt=None, r_px_expected=None),
        # Strong temporal prior near cands[0].
        dict(prev_position=(100.0, 100.0), prev_velocity=(0.0, 0.0),
             dt=1.0 / 240, r_px_expected=12.0),
        # Strong temporal prior near cands[2].
        dict(prev_position=(50.0, 50.0), prev_velocity=(0.0, 0.0),
             dt=1.0 / 240, r_px_expected=12.0),
        # Velocity drift.
        dict(prev_position=(100.0, 100.0), prev_velocity=(2400.0, 0.0),
             dt=1.0 / 240, r_px_expected=12.0),
    ]
    for cfg in configs:
        kw = {**cfg, **_DEFAULTS}
        winner = select_best_candidate(cands, **kw)
        costs = score_candidates(cands, **kw)
        argmin = min(range(len(costs)), key=lambda i: costs[i])
        assert winner is cands[argmin], (
            f"wrapper drift: cfg={cfg} winner_idx={cands.index(winner)} "
            f"argmin={argmin} costs={costs}"
        )


# --- detect_ball_with_candidates: server_post BLOBS feed -----------------

def test_detect_ball_with_candidates_returns_winner_and_scored_blobs():
    """`detect_ball_with_candidates` returns both the BlobCandidate winner
    (px/py/area/area_score/cost) and the full scored list, so
    `pipeline.detect_pitch` can stamp `FramePayload.candidates` for the
    viewer's BLOBS overlay on the server_post path. The winner is the
    lowest-cost element of the returned list."""
    from detection import detect_ball_with_candidates
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)  # ball near prior
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)  # bigger distractor
    winner, blobs = detect_ball_with_candidates(
        img, HSVRange.default(),
        prev_position=(200.0, 240.0),
        prev_velocity=(4800.0, 0.0),
        dt=1.0 / 240,
    )
    assert winner is not None
    assert len(blobs) >= 2
    for b in blobs:
        assert b.cost is not None
        assert 0.0 <= b.cost <= 1.0
        assert 0.0 <= b.area_score <= 1.0
    assert min(blobs, key=lambda b: b.cost) is winner
    assert abs(winner.px - 220) < 3 and abs(winner.py - 240) < 3


def test_detect_ball_with_candidates_empty_returns_none_and_empty():
    img = np.zeros((100, 100, 3), dtype=np.uint8)  # no yellow → no survivors
    from detection import detect_ball_with_candidates
    winner, blobs = detect_ball_with_candidates(img, HSVRange.default())
    assert winner is None
    assert blobs == []


def test_pipeline_detect_pitch_stamps_candidates_with_cost():
    """End-to-end: a synthetic frame iterator feeds detect_pitch; emitted
    FramePayload carries `candidates` with cost stamped on every blob.
    Mirrors live_pairing._resolve_candidates' contract so the viewer's
    BLOBS-svr layer reads the same shape as BLOBS-live."""
    from pathlib import Path
    from pipeline import detect_pitch

    def _frame_iter(path, video_start_pts_s):
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)
        cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)
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
