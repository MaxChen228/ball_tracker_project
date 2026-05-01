# 07 Epipolar Rescue — Feasibility Study

**Date**: 2026-05-01  
**Status**: Experiment complete — NOT recommended for production integration.

---

## 1. Problem statement

cam A frame t: V11 HIT (ball detected at px_a, py_a).  
cam B frame t: V11 MISS (0 candidates or all background).  
Goal: use A's detection → epipolar line in B → widened HSV search in ±30 px strip → recover B's missed ball.

Same analysis for the symmetric case: B hits, A misses.

---

## 2. Data

4 sessions with GT labels on both cameras (SAM3):

| session | A frames | A recall | B frames | B recall |
|---|---|---|---|---|
| s_170a6a89 | 133 | 76.7% | 320 | 88.8% |
| s_21af9a82 | 80  | 78.8% | 54  | 96.3% |
| s_22d1835e | 57  | 96.5% | 180 | 92.2% |
| s_2546618f | 107 | 71.0% | 116 | 85.3% |

Synchronized frame pairs (±6 ms temporal match via chirp anchors): **653 total**.  
Rescue-eligible pairs (one cam hit, one miss, both with GT masks): **79** (12%).

---

## 3. Fundamental matrix — critical blocker

### 3a. Homography-derived F

Per camera: `K, dist, H → R, t` via `recover_extrinsics()` (existing `triangulate.py`).  
Relative pose `R_rel = Rb @ Ra.T`, `t_rel = tb - R_rel @ ta`.  
F = K_b^{-T} [t_rel]× R_rel K_a^{-1}.

**Result: completely unusable.**

| session | median epipolar dist (hit-hit pairs) |
|---|---|
| s_170a6a89 | 131 px |
| s_21af9a82 | 359 px |
| s_22d1835e | 480 px |
| s_2546618f | 342 px |
| overall | **372 px** |

Root cause: single-camera homography calibrates each cam independently against the 2D plate plane (Z=0). The relative extrinsics accumulate per-camera error and have no simultaneous 3D constraint. A proper `cv2.stereoCalibrate()` with a charuco board captured simultaneously by both cameras is required for F to be useful.

### 3b. RANSAC-estimated F from ball detections

`cv2.findFundamentalMat(pts_a_hit, pts_b_hit, FM_RANSAC, 3.0px)`.

| session | hit-hit pairs | RANSAC inliers | inlier epi dist (mean) |
|---|---|---|---|
| s_170a6a89 | 102 | 35 (34%) | 0.33 px |
| s_21af9a82 | 43  | 16 (37%) | 0.42 px |
| s_22d1835e | 55  | 23 (42%) | 0.47 px |
| s_2546618f | 61  | 24 (39%) | 1.11 px |

Low inlier rate (24–45%) is a fundamental degeneracy problem: all 3D ball positions lie on a near-parabolic trajectory (1D manifold in 3D). RANSAC with 7-DoF F has far more DoF than the trajectory provides. The "inlier set" is an arbitrary subset consistent with one of many equally valid F estimates. The estimated F does not reliably generalize to miss frames.

Epipolar distance from GT to estimated-F lines on rescue-eligible frames:

- B-hit/A-miss (n=68): mean=74 px, median=28 px, **p95=334 px**
- A-hit/B-miss (n=14): mean=42 px, median=37 px

Even the "best case" (per-session RANSAC F) gives a ±30 px band that only covers ~53% of GT positions.

---

## 4. Rescue experiment

Two conditions run on the same 79 GT-eligible rescue frames:

**Condition A — Band rescue**: relaxed HSV (h=95–130, s=30–255, v=30–255, aspect≥0.3, fill≥0.3, area≥5) restricted to ±30 px epipolar band (using RANSAC-estimated F).

**Condition B — No-band control**: same relaxed HSV applied to full frame (no spatial constraint).

### Results

| session | direction | n | band recall | band FP/f | noband recall | noband FP/f |
|---|---|---|---|---|---|---|
| s_170a6a89 | B→A | 26–31 | 31% | 7.5  | 39%  | 105 |
| s_21af9a82 | B→A | 13–15 | 23% | 20.2 | 67%  | 117 |
| s_21af9a82 | A→B | 2     | 0%  | 2.0  | 100% | 98  |
| s_22d1835e | B→A | 1–2   | 0%  | 0.0  | 0%   | 100 |
| s_2546618f | B→A | 19    | 47% | 4.8  | 100% | 33  |
| s_2546618f | A→B | 17    | 29% | 2.5  | 47%  | 92  |
| **Total**  |     | **79–85** | **31.6%** | **7.5** | **60.0%** | **100** |

V11 standalone recall on these frames: **0%** (by definition — miss frames).

### Key finding: the band *hurts* recall

No-band relaxed HSV recovers **60% overall**; the band drops this to **31.6%**. The degenerate F causes the ±30 px strip to exclude the true ball position in a large fraction of cases (consistent with p95 epipolar distance = 334 px).

The band's only benefit is FP suppression: **100 FP/frame → 7.5 FP/frame**, a 13× reduction. But the recall penalty (60% → 32%) makes this a bad tradeoff.

**Bottleneck decomposition**:

| axis | contribution |
|---|---|
| HSV detector (recoverable misses) | 60% upper bound (no-band) |
| Spatial accuracy of F | band excludes GT in ~40% of frames → recall drops to 32% |
| Irreducible detector misses | ~40% of GT-labeled frames are unrecoverable even with relaxed HSV |

The bottleneck is **geometry, not HSV**: the concept is sound but requires calibrated F.

---

## 5. Why the rescue rate is limited

1. **F estimation degeneracy**: ball-only correspondences don't constrain F reliably. The ±30 px band covers the true ball position in only 53% of cases (median epipolar distance 28 px, but p95 = 334 px). The band crops out the ball in 7 of 25 recovered cases.

2. **Relaxed HSV already recovers 60% unaided**: the full-frame relaxed HSV already recovers most rescuable misses. Adding a broken spatial constraint makes things worse.

3. **Irreducible misses (~40%)**: roughly 40% of miss frames have GT regions that fail even relaxed HSV (shadow, specular glare, strong motion blur) and cannot be recovered by any detection approach.

---

## 6. What would be needed

To make epipolar rescue work end-to-end:

1. **Proper stereo calibration**: `cv2.stereoCalibrate()` from simultaneous charuco captures by both cameras → reduces epipolar error from 370 px to <3 px. This is the **hard prerequisite**.

2. **With good F**: the ±30 px band (reducible to ±10–15 px with accurate calibration) would preserve the 60% no-band recall ceiling while cutting FP from 100 to ~5–10/frame.

3. **Net gain**: stereo-calibrated epipolar rescue = 60% recall at ~5–10 FP/frame, vs. no-band = 60% recall at 100 FP/frame, vs. current V11 = 0% recall on these frames.

---

## 7. Production integration path

Conditional on stereo calibration being available:

- Per-pitch, after V11 server_post detection, identify frames where cam A hit / cam B miss.
- Project A's detected centroid through F → epipolar line in B.
- Re-run V11 with h=(95,130), s=(50,255), v=(30,255) (slightly relaxed vs current s=120) in the ±15 px band only.
- If exactly 1 cand passes shape gate: accept as B detection; append to B's frame candidates.
- Symmetric for B→A.

**Not recommended** until `stereoCalibrate()` data is available.

---

## 8. Conclusion

**Not viable with current calibration data. Concept is sound but requires stereo calibration.**

| | recall | FP/frame |
|---|---|---|
| V11 standalone (on miss frames) | 0% | 0 |
| No-band relaxed HSV (h=95–130, s≥30) | **60%** | 100 |
| Epipolar rescue (RANSAC F, ±30 px band) | 32% | 7.5 |
| Epipolar rescue (ideal stereo F, ±15 px band) | ~60% (est.) | ~5–10 |

The degenerate F from ball-only correspondences actively degrades recall below the no-band baseline. The band's spatial exclusion cuts out the ball in ~40% of cases.

Recommended next step: capture a charuco calibration board **simultaneously** with both cameras (10–20 frames from different angles), run `cv2.stereoCalibrate()`, store the resulting F/E matrices alongside existing per-camera calibrations. This is the sole prerequisite for epipolar rescue to be useful and is straightforwardly achievable with the existing hardware.
