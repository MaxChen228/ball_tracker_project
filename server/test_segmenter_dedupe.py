"""Pin the chord-based `_dedupe_segments` behaviour: when two ballistic
segments overlap in time, the one with the longer 3D chord wins, no
other tiebreaks."""
from __future__ import annotations

import numpy as np
import pytest

from schemas import TriangulatedPoint
from segmenter import find_segments, _dedupe_segments, Segment


def _ball(t0: float, p0: tuple[float, float, float],
          v0: tuple[float, float, float], n: int = 12,
          dt: float = 1 / 240, residual_m: float = 0.005,
          jitter: float = 0.0, seed: int = 0) -> list[TriangulatedPoint]:
    """Generate ballistic trajectory points with optional Gaussian jitter."""
    rng = np.random.default_rng(seed)
    g = np.array([0.0, 0.0, -9.81])
    out: list[TriangulatedPoint] = []
    for i in range(n):
        t = t0 + i * dt
        tau = i * dt
        x = p0[0] + v0[0] * tau + 0.5 * g[0] * tau * tau
        y = p0[1] + v0[1] * tau + 0.5 * g[1] * tau * tau
        z = p0[2] + v0[2] * tau + 0.5 * g[2] * tau * tau
        if jitter > 0:
            x += float(rng.normal(0, jitter))
            y += float(rng.normal(0, jitter))
            z += float(rng.normal(0, jitter))
        out.append(TriangulatedPoint(
            t_rel_s=t, x_m=x, y_m=y, z_m=z, residual_m=residual_m,
            source_a_cand_idx=None, source_b_cand_idx=None,
            cost_a=None, cost_b=None,
            pair_key=("A","B"),
        ))
    return out


def _segment_from(indices: list[int], pts: np.ndarray, v0=(10.0, 0.0, 0.0)) -> Segment:
    """Hand-build a Segment for direct _dedupe_segments unit tests."""
    return Segment(
        indices=sorted(indices),
        original_indices=sorted(indices),
        p0=np.array([pts[indices[0], 1], pts[indices[0], 2], pts[indices[0], 3]]),
        v0=np.array(v0, dtype=float),
        t_anchor=float(pts[indices[0], 0]),
        t_start=float(pts[min(indices), 0]),
        t_end=float(pts[max(indices), 0]),
        rmse_m=0.005,
    )


def test_overlap_longer_chord_wins_via_find_segments():
    """Real pitch (long chord) + stereo ghost in the same time window
    (short chord, low velocity) → ghost dropped."""
    pitch = _ball(t0=0.0, p0=(0.0, 0.0, 1.3), v0=(0.0, 22.0, -0.5), n=14)
    # Ghost: same window, much shorter chord. Use a different start
    # position so the segmenter sees two distinct fits.
    ghost = _ball(t0=0.005, p0=(-0.8, -0.1, 1.25), v0=(-6.0, 10.0, 0.0), n=9)
    points = pitch + ghost
    segments, _ = find_segments(points, v_min_mps=2.0)
    # Pitch chord ≈ |v|·dur ≈ 22 · 14/240 ≈ 1.28m; ghost ≈ 11.7 · 9/240 ≈ 0.44m
    assert len(segments) == 1, [
        (len(s.indices), s.t_start, s.t_end, float(np.linalg.norm(s.v0)))
        for s in segments
    ]
    assert float(np.linalg.norm(segments[0].v0)) > 15.0


def test_overlap_near_equal_chord_deterministic():
    """Two segments with overlap ≥ 30% and near-equal chord. The tiebreak
    must be deterministic (sort stability) and ONE survives — that's
    the explicit single-rule contract."""
    pts_a = _ball(t0=0.0, p0=(0.0, 0.0, 1.3), v0=(0.0, 22.0, -0.5), n=12, seed=1)
    pts_b = _ball(t0=0.002, p0=(0.01, 0.01, 1.3), v0=(0.0, 22.0, -0.5), n=12, seed=2,
                  jitter=0.002)
    # Build the (M,5) pts array the way segmenter would: t,x,y,z,residual
    pts_arr = np.array([
        [p.t_rel_s, p.x_m, p.y_m, p.z_m, p.residual_m]
        for p in pts_a + pts_b
    ])
    seg_a = _segment_from(list(range(0, 12)), pts_arr, v0=(0.0, 22.0, -0.5))
    seg_b = _segment_from(list(range(12, 24)), pts_arr, v0=(0.0, 22.0, -0.5))
    kept = _dedupe_segments([seg_a, seg_b], pts_arr)
    assert len(kept) == 1
    # Re-running must give the same survivor (deterministic).
    kept2 = _dedupe_segments([seg_a, seg_b], pts_arr)
    assert kept[0].indices == kept2[0].indices


def test_bounce_pair_both_survive():
    """Bounce: two ballistic arcs sharing a small time window at the
    apex/landing instant. Overlap < 30% of shorter span → both kept."""
    pre = _ball(t0=0.0, p0=(0.0, 0.0, 1.0), v0=(0.0, 3.0, 3.0), n=24)
    # Post-bounce: starts right at end of pre with z-velocity reversed and
    # damped. Make a small overlap of one frame to mimic a leakage point.
    post = _ball(t0=pre[-1].t_rel_s, p0=(pre[-1].x_m, pre[-1].y_m, pre[-1].z_m),
                 v0=(0.0, 2.5, 2.2), n=20)
    pts_arr = np.array([
        [p.t_rel_s, p.x_m, p.y_m, p.z_m, p.residual_m]
        for p in pre + post
    ])
    seg_pre = _segment_from(list(range(0, 24)), pts_arr, v0=(0.0, 3.0, 3.0))
    seg_post = _segment_from(list(range(24, 44)), pts_arr, v0=(0.0, 2.5, 2.2))
    # Single shared instant → ovlp=0; even if we force a 1-frame stretch
    # the overlap fraction is well below 0.30.
    kept = _dedupe_segments([seg_pre, seg_post], pts_arr)
    assert len(kept) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
