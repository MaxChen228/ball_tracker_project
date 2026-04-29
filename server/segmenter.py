"""Multi-segment ballistic event extractor (lab prototype).

First principles:
- Free flight obeys p(t) = p0 + v0·τ + 0.5·g·τ²,  g = (0,0,-9.81).
- az is HARD-PINNED, not a free parameter (under short windows it's
  unobservable so leaving it free turns noise into "gravity").
- residual_m on TriangulatedPoint is the 3D gap between the two camera
  rays' closest points. gap > threshold ⇒ the two cameras did not see
  the same physical 3D object ⇒ cannot be a ball. Hard physics gate.
- Bounces split a real event into multiple ballistic segments with
  continuous position but discontinuous velocity. Greedy multi-segment
  with az-pinned refit naturally recovers them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

G = np.array([0.0, 0.0, -9.81])
MPS_TO_KPH = 3.6


@dataclass
class Segment:
    indices: list[int]            # indices into the FILTERED+SORTED pts array
    original_indices: list[int]   # indices into the ORIGINAL caller-side list
    p0: np.ndarray                # (3,) world-frame release point at t_anchor
    v0: np.ndarray                # (3,) world-frame velocity at t_anchor
    t_anchor: float               # original-clock time the (p0,v0) is anchored at
    t_start: float
    t_end: float
    rmse_m: float

    @property
    def speed_kph(self) -> float:
        return float(np.linalg.norm(self.v0)) * MPS_TO_KPH

    @property
    def speed_mps(self) -> float:
        return float(np.linalg.norm(self.v0))

    def sample_curve(self, n: int = 80) -> np.ndarray:
        ts = np.linspace(self.t_start, self.t_end, n)
        tau = ts - self.t_anchor
        out = np.empty((n, 4))
        out[:, 0] = ts
        for axis in range(3):
            out[:, 1 + axis] = (
                self.p0[axis]
                + self.v0[axis] * tau
                + 0.5 * G[axis] * tau * tau
            )
        return out


def _refit_pinned(pts: np.ndarray, idx: list[int]) -> tuple[np.ndarray, np.ndarray, float, float]:
    """LSQ on (p0, v0), az pinned to G[axis]. pts is (N, 4): [t, x, y, z].
    Returns (p0, v0, rmse_m, t_anchor)."""
    sub = pts[idx]
    t_anchor = float(sub[0, 0])
    tau = sub[:, 0] - t_anchor
    A = np.column_stack([np.ones_like(tau), tau])
    p0 = np.zeros(3)
    v0 = np.zeros(3)
    res_sq = np.zeros(sub.shape[0])
    for axis in range(3):
        rhs = sub[:, 1 + axis] - 0.5 * G[axis] * tau * tau
        coef, *_ = np.linalg.lstsq(A, rhs, rcond=None)
        p0[axis] = coef[0]
        v0[axis] = coef[1]
        pred = coef[0] + coef[1] * tau + 0.5 * G[axis] * tau * tau
        res_sq += (sub[:, 1 + axis] - pred) ** 2
    rmse = float(np.sqrt(res_sq.mean()))
    return p0, v0, rmse, t_anchor


def _predict(p0: np.ndarray, v0: np.ndarray, t_anchor: float, t: float) -> np.ndarray:
    tau = t - t_anchor
    return p0 + v0 * tau + 0.5 * G * tau * tau


def find_segments(
    points: list,
    *,
    min_seg_len: int = 5,
    v_min_mps: float = 5.0,
    v_max_mps: float = 60.0,
    seed_dt_max_factor: float = 3.0,
    grow_dt_max_factor: float = 8.0,
    gate_r0_m: float = 0.05,
    gate_b: float = 0.10,
    max_consec_misses: int = 3,
    min_displacement_m: float = 0.30,
) -> tuple[list[Segment], np.ndarray]:
    """Run multi-segment ballistic extraction.

    Args:
        points: iterable of objects with attributes
            (t_rel_s, x_m, y_m, z_m, residual_m). Order doesn't matter.
            Caller is responsible for residual filtering — pairing's
            `gap_threshold_m` gate (per-session via PairingTuning) is
            the single source of truth for skew-line residual culling;
            segmenter trusts everything it receives.

    Returns:
        (segments, pts_sorted)
            pts_sorted: (M, 5) [t, x, y, z, residual] of the time-sorted
              input set used internally.
    """
    raw_full = np.array(
        [[p.t_rel_s, p.x_m, p.y_m, p.z_m, p.residual_m] for p in points],
        dtype=float,
    )
    n_in = raw_full.shape[0]
    if n_in == 0:
        return [], np.zeros((0, 5))

    # No timestamp-twin collapse. Multi-candidate fan-out introduces
    # twins that are different physical objects (real ball + distractor
    # that happened to clear pairing's gap gate); averaging contaminates
    # position. The grow loop's RMSE gate (`gate_r0_m`, `gate_b`) is the
    # correct outlier filter in the fan-out world — it walks ballistic
    # predictions point-by-point and skips candidates that don't fit.
    survivor_idx = np.arange(n_in)
    if n_in < min_seg_len:
        return [], np.zeros((0, 5))

    survivor = raw_full[survivor_idx]
    sort_perm = np.argsort(survivor[:, 0], kind="stable")
    pts = survivor[sort_perm]
    # Each working-index row corresponds 1-to-1 with an original input
    # row (no collapse), so `back_to_orig[k] = original input index`.
    back_to_orig = survivor_idx[sort_perm]
    n = pts.shape[0]

    # Median frame interval (over positive Δt only).
    dts_all = np.diff(pts[:, 0])
    dts_pos = dts_all[dts_all > 0]
    if dts_pos.size == 0:
        return [], pts
    frame_interval = float(np.median(dts_pos))
    seed_dt_max = frame_interval * seed_dt_max_factor
    grow_dt_max = frame_interval * grow_dt_max_factor

    used = np.zeros(n, dtype=bool)
    segments: list[Segment] = []

    while True:
        seed = _find_best_seed(
            pts, used, seed_dt_max, v_min_mps, v_max_mps
        )
        if seed is None:
            break
        seg_idx = _grow_segment(
            pts, used, seed,
            grow_dt_max=grow_dt_max,
            gate_r0=gate_r0_m,
            gate_b=gate_b,
            max_consec_misses=max_consec_misses,
        )
        if len(seg_idx) >= min_seg_len:
            seg_idx_sorted = sorted(seg_idx)
            p0, v0, rmse, t_anchor = _refit_pinned(pts, seg_idx_sorted)
            # Fill-in pass: greedy grow's "best residual in window" leap-
            # frogs noisier-but-real points (4ms candidate at 6cm fails,
            # 8ms candidate at 2cm wins → 4ms permanently skipped). Now
            # that the segment has matured, predict every unused point in
            # its time range with the stable fit and absorb anything that
            # passes a 2·rmse gate.
            seg_idx_sorted = _fill_in_segment(
                pts, used, seg_idx_sorted, p0, v0, t_anchor, rmse, gate_r0_m
            )
            p0, v0, rmse, t_anchor = _refit_pinned(pts, seg_idx_sorted)
            # Displacement gate AFTER fill-in (now reflects the full set).
            disp = float(np.linalg.norm(pts[seg_idx_sorted[-1], 1:4] - pts[seg_idx_sorted[0], 1:4]))
            if disp < min_displacement_m:
                used[seg_idx_sorted] = True
                continue
            used[seg_idx_sorted] = True
            segments.append(Segment(
                indices=seg_idx_sorted,
                original_indices=[int(back_to_orig[k]) for k in seg_idx_sorted],
                p0=p0, v0=v0,
                t_anchor=t_anchor,
                t_start=float(pts[seg_idx_sorted[0], 0]),
                t_end=float(pts[seg_idx_sorted[-1], 0]),
                rmse_m=rmse,
            ))
        else:
            # Burn the seed pair so we don't re-pick.
            used[list(seed)] = True

    segments = _dedupe_segments(segments)
    segments = _merge_compatible_segments(
        segments, pts, back_to_orig=back_to_orig, gate_r0=gate_r0_m,
    )
    return segments, pts


def _merge_compatible_segments(
    segments: list[Segment],
    pts: np.ndarray,
    *,
    back_to_orig: np.ndarray,
    gate_r0: float,
    max_gap_s: float = 0.05,
    cos_threshold: float = 0.92,
    pos_threshold_m: float = 0.10,
    rmse_blowup_factor: float = 2.0,
) -> list[Segment]:
    """Merge segment pairs that are temporally continuous and ballistically
    consistent. Catches both:
      - flight with detection gap larger than grow_dt_max (gap > 0)
      - overlap pairs that dedup's threshold barely missed (gap < 0)

    Bounces are NOT merged because their v0 direction differs (z reverses)
    — fails the cos check by construction.
    """
    if len(segments) < 2:
        return segments
    while True:
        best: tuple[float, int, int, Segment] | None = None
        for i in range(len(segments)):
            for j in range(i + 1, len(segments)):
                merged = _try_merge_pair(
                    segments[i], segments[j], pts, back_to_orig,
                    max_gap_s=max_gap_s,
                    cos_threshold=cos_threshold,
                    pos_threshold_m=pos_threshold_m,
                    rmse_blowup_factor=rmse_blowup_factor,
                    gate_r0=gate_r0,
                )
                if merged is None:
                    continue
                # Pick the merge that produces the lowest-RMSE result.
                if best is None or merged.rmse_m < best[0]:
                    best = (merged.rmse_m, i, j, merged)
        if best is None:
            break
        _, i, j, merged = best
        segments = [s for k, s in enumerate(segments) if k != i and k != j]
        segments.append(merged)
    return sorted(segments, key=lambda s: s.t_start)


def _try_merge_pair(
    a: Segment,
    b: Segment,
    pts: np.ndarray,
    back_to_orig: np.ndarray,
    *,
    max_gap_s: float,
    cos_threshold: float,
    pos_threshold_m: float,
    rmse_blowup_factor: float,
    gate_r0: float,
) -> Segment | None:
    """Test merge feasibility; return merged Segment or None."""
    # Order earlier-first by t_anchor so velocity comparison is well-defined.
    if a.t_anchor > b.t_anchor:
        a, b = b, a
    # Time-gap gate (negative if overlap).
    gap = b.t_start - a.t_end
    if gap > max_gap_s or -gap > max(b.t_end - b.t_start, a.t_end - a.t_start):
        return None
    # Direction gate — adjust a.v0 to b's anchor under gravity then compare.
    v_a_at_b = a.v0 + G * (b.t_anchor - a.t_anchor)
    denom = float(np.linalg.norm(v_a_at_b) * np.linalg.norm(b.v0)) + 1e-9
    cos = float(np.dot(v_a_at_b, b.v0) / denom)
    if cos < cos_threshold:
        return None
    # Position continuity gate — predict a's curve at b's first point.
    b_first_idx = b.indices[0]
    b_first_t = pts[b_first_idx, 0]
    tau = b_first_t - a.t_anchor
    pred = a.p0 + a.v0 * tau + 0.5 * G * tau * tau
    pos_resid = float(np.linalg.norm(pts[b_first_idx, 1:4] - pred))
    if pos_resid > pos_threshold_m:
        return None
    # Try the actual merge.
    merged_indices = sorted(set(a.indices) | set(b.indices))
    p0, v0, rmse, t_anchor = _refit_pinned(pts, merged_indices)
    rmse_cap = max(rmse_blowup_factor * max(a.rmse_m, b.rmse_m), gate_r0)
    if rmse > rmse_cap:
        return None
    return Segment(
        indices=merged_indices,
        original_indices=sorted(set(a.original_indices) | set(b.original_indices)),
        p0=p0, v0=v0,
        t_anchor=t_anchor,
        t_start=float(pts[merged_indices[0], 0]),
        t_end=float(pts[merged_indices[-1], 0]),
        rmse_m=rmse,
    )


def _dedupe_segments(
    segments: list[Segment],
    *,
    cos_threshold: float = 0.95,
    overlap_frac_threshold: float = 0.30,
) -> list[Segment]:
    """Drop segments that overlap in time AND share velocity direction
    with a longer / lower-RMSE segment — they are the same physical
    event captured by parallel live triangulation pairs."""
    if len(segments) <= 1:
        return segments
    # Score: longer first, ties broken by lower RMSE.
    ordered = sorted(segments, key=lambda s: (-len(s.indices), s.rmse_m))
    keep: list[Segment] = []
    for s in ordered:
        is_dup = False
        for k in keep:
            ovlp_lo = max(s.t_start, k.t_start)
            ovlp_hi = min(s.t_end, k.t_end)
            if ovlp_hi <= ovlp_lo:
                continue
            ovlp = ovlp_hi - ovlp_lo
            short_span = min(s.t_end - s.t_start, k.t_end - k.t_start)
            if short_span <= 0:
                continue
            if ovlp / short_span < overlap_frac_threshold:
                continue
            cos = float(
                np.dot(s.v0, k.v0)
                / (np.linalg.norm(s.v0) * np.linalg.norm(k.v0) + 1e-9)
            )
            if cos >= cos_threshold:
                is_dup = True
                break
        if not is_dup:
            keep.append(s)
    # Re-sort kept segments by t_start so consumer sees chronological order.
    return sorted(keep, key=lambda s: s.t_start)


def _fill_in_segment(
    pts: np.ndarray,
    used: np.ndarray,
    idx: list[int],
    p0: np.ndarray,
    v0: np.ndarray,
    t_anchor: float,
    rmse: float,
    gate_r0: float,
) -> list[int]:
    """Add unused points inside the segment's time range that the mature
    fit predicts within max(2·rmse, gate_r0). Recovers leapfrog skips."""
    if not idx:
        return idx
    sorted_idx = sorted(idx)
    t_lo = pts[sorted_idx[0], 0]
    t_hi = pts[sorted_idx[-1], 0]
    gate = max(2.0 * rmse, gate_r0)
    in_seg = set(sorted_idx)
    extended = list(sorted_idx)
    for k in range(pts.shape[0]):
        if used[k] or k in in_seg:
            continue
        t_k = pts[k, 0]
        if t_k < t_lo or t_k > t_hi:
            continue
        tau = t_k - t_anchor
        pred = p0 + v0 * tau + 0.5 * G * tau * tau
        if float(np.linalg.norm(pts[k, 1:4] - pred)) < gate:
            extended.append(k)
    return sorted(extended)


def _claim_consistent_points(
    pts: np.ndarray,
    used: np.ndarray,
    p0: np.ndarray,
    v0: np.ndarray,
    t_anchor: float,
    *,
    t_start: float,
    t_end: float,
    rmse: float,
    gate_r0: float,
    grow_dt_max: float,
) -> None:
    """Claim every unused point whose 3D position the segment's ballistic
    fit can predict to within `max(2·rmse, gate_r0)`, restricted to a
    time window slightly larger than the segment's span. These are
    same-event noise duplicates emitted by live multi-pair triangulation."""
    consistency_gate = max(2.0 * rmse, gate_r0)
    t_lo = t_start - grow_dt_max
    t_hi = t_end + grow_dt_max
    for k in range(pts.shape[0]):
        if used[k]:
            continue
        t_k = pts[k, 0]
        if t_k < t_lo or t_k > t_hi:
            continue
        tau = t_k - t_anchor
        pred = p0 + v0 * tau + 0.5 * G * tau * tau
        if float(np.linalg.norm(pts[k, 1:4] - pred)) < consistency_gate:
            used[k] = True


def _mark_timestamps_used(used: np.ndarray, pts: np.ndarray, indices: list[int]) -> None:
    """Mark every point sharing a timestamp with one of the given indices
    as used. Live triangulation often emits multiple noisy 3D estimates
    at the same t_rel_s — once one is claimed by a segment, its twins
    must not seed another (otherwise we get parallel duplicate segments)."""
    if not indices:
        return
    target_ts = pts[indices, 0]
    mask = np.isin(pts[:, 0], target_ts)
    used |= mask


def _find_best_seed(
    pts: np.ndarray,
    used: np.ndarray,
    seed_dt_max: float,
    v_min: float,
    v_max: float,
) -> tuple[int, int] | None:
    """Pick the seed pair (i, j) j>i with smallest Δt that gives an
    implied speed in [v_min, v_max] (gravity-corrected). Returns None
    when no such pair exists."""
    n = pts.shape[0]
    best: tuple[int, int] | None = None
    best_dt = np.inf
    for i in range(n):
        if used[i]:
            continue
        ti = pts[i, 0]
        # advance j until either we exceed dt or run off the end
        for j in range(i + 1, n):
            if used[j]:
                continue
            dt = pts[j, 0] - ti
            if dt <= 0:
                continue
            if dt > seed_dt_max:
                break
            disp = pts[j, 1:4] - pts[i, 1:4]
            v_implied = (disp - 0.5 * G * dt * dt) / dt
            speed = float(np.linalg.norm(v_implied))
            if speed < v_min or speed > v_max:
                continue
            if dt < best_dt:
                best_dt = dt
                best = (i, j)
    return best


def _grow_segment(
    pts: np.ndarray,
    used: np.ndarray,
    seed: tuple[int, int],
    *,
    grow_dt_max: float,
    gate_r0: float,
    gate_b: float,
    max_consec_misses: int,
) -> list[int]:
    """Bidirectional grow from seed: forward to t_max, then backward to
    t_min. Refit (p0, v0) az-pinned after every accepted point.

    Gate is `k_sigma · running_rmse` once n>=3 (3-sigma inlier band on
    the segment's own noise scale — auto-adapts to motion-blur jitter
    on fast balls). For n<3 we have no rmse yet, so use `gate_r0` as
    a bootstrap floor."""
    K_SIGMA = 3.0
    BOOTSTRAP_GATE = 0.15  # generous m during the first few points so
    # zigzag jitter on fast balls can enter — once segment matures the
    # 3·rmse rule clamps back. This deliberately admits some noise during
    # bootstrap; the post-grow validation + dedup catches false segments.
    BOOTSTRAP_LEN = 5
    n = pts.shape[0]
    idx: list[int] = list(seed)

    def current_gate(rmse: float) -> float:
        if len(idx) < BOOTSTRAP_LEN:
            return BOOTSTRAP_GATE
        return max(K_SIGMA * rmse, gate_r0)

    # ---- Forward grow ----
    p0, v0, rmse, t_anchor = _refit_pinned(pts, idx)
    last_t = pts[idx[-1], 0]
    while True:
        best_k = -1
        best_resid = np.inf
        gate = current_gate(rmse)
        for scan in range(n):
            if used[scan] or scan in idx:
                continue
            t_k = pts[scan, 0]
            if t_k <= last_t or t_k - last_t > grow_dt_max:
                continue
            pred = _predict(p0, v0, t_anchor, t_k)
            resid = float(np.linalg.norm(pts[scan, 1:4] - pred))
            if resid < gate and resid < best_resid:
                best_resid = resid
                best_k = scan
        if best_k < 0:
            break
        idx.append(best_k)
        last_t = pts[best_k, 0]
        p0, v0, rmse, t_anchor = _refit_pinned(pts, idx)

    # ---- Backward grow ----
    p0, v0, rmse, t_anchor = _refit_pinned(pts, sorted(idx))
    first_t = pts[min(idx, key=lambda k: pts[k, 0]), 0]
    while True:
        best_k = -1
        best_resid = np.inf
        gate = current_gate(rmse)
        for scan in range(n):
            if used[scan] or scan in idx:
                continue
            t_k = pts[scan, 0]
            if t_k >= first_t or first_t - t_k > grow_dt_max:
                continue
            pred = _predict(p0, v0, t_anchor, t_k)
            resid = float(np.linalg.norm(pts[scan, 1:4] - pred))
            if resid < gate and resid < best_resid:
                best_resid = resid
                best_k = scan
        if best_k < 0:
            break
        idx.append(best_k)
        first_t = pts[best_k, 0]
        p0, v0, rmse, t_anchor = _refit_pinned(pts, sorted(idx))

    # Trim trailing endpoint(s) whose post-final-refit residual blows the
    # gate (catches a noisy point that pulled the fit off-axis).
    if len(idx) >= 3:
        p0, v0, _, t_anchor = _refit_pinned(pts, idx)
        while len(idx) > 2:
            last = idx[-1]
            t_k = pts[last, 0]
            pred = _predict(p0, v0, t_anchor, t_k)
            resid = float(np.linalg.norm(pts[last, 1:4] - pred))
            gate = gate_r0 + gate_b * float(np.linalg.norm(v0)) * max(t_k - pts[idx[-2], 0], 1e-3)
            if resid > 2.0 * gate:
                idx.pop()
                p0, v0, _, t_anchor = _refit_pinned(pts, idx)
            else:
                break

    return idx
