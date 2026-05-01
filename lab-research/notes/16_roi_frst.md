# 16 — Stateful ROI-FRST Evaluation

Script: `scripts/24_roi_frst.py`  
Results: `outputs/24_roi_frst_results.json`  
Date: 2026-05-01

## Research question

Can ROI-gated FRST (anchored to prior V11 / Y-diff detections) push V11+Y-diff (R=0.970) closer to V11+full-FRST (R=0.996) without catastrophic FP rates?

---

## Design

Two modes, both explicit stateful:

**Mode 1 (temporal anchor)**: when frame t has any V11 or Y-diff detection (top-1 by area used as anchor), arm a 100×100 px ROI around that centroid for K=5 subsequent frames. FRST runs inside the union of all active ROI boxes.

**Mode 2 (prior frame ROI)**: same as Mode 1, plus if Mode-2 state is empty (TTL expired across all anchors) and `last_known_roi` exists (set on any frame with a detection), propagate that ROI forward indefinitely until a new detection updates it. Implementation note: "prior frame ROI" is interpreted as fallback-when-empty rather than always-included; an alternative reading would make Mode 2 a strict superset of Mode 1 but does not change the headline conclusion.

**Stateless contract broken**: both modes maintain K-frame ROI state across calls. This is an explicit design choice — stateless FRST had 9789 FP/frame (03_frst_eval.md).

### Deviations from spec

The task specified anchoring on "每個 cand 中心" (all candidate centres). This implementation uses **top-1 candidate by area** from V11 ∪ Y-diff instead. Reason: V11 emits 25+ FP candidates/frame; anchoring all of them causes K=5×25=125 active ROIs whose merged bounding box covers the whole frame — defeating the FP-containment purpose. Top-1 is the most likely ball candidate. The +0.37pp headline is robust to this: rescued frames have isolated anchors (single prior-frame hit), not multi-anchor ROIs.

Additional implementation choices:
- Y-diff top-50 limit applied only for dedup_union cost (O(N×M)); full ydiff list used for hit_check (O(N) linear scan) — recall numbers reflect full ydiff coverage
- FRST threshold = 0.10, radii = [3, 5, 8, 12], K = 5, ROI half = 50 px
- NMS = 5-px dilate trick (same as 19_frst.py)

---

## Per-session results

| session | split | n | V11 | Base (V11+Yd) | M1 | M2 | FP_base | FP_m1 | FP_m2 |
|---|---|---|---|---|---|---|---|---|---|
| 16ec069a_b | train | 228 | 0.982 | 0.991 | **0.996** | **0.996** | 76.6 | 181.1 | 181.1 |
| 170a6a89_a | test | 112 | 0.991 | 0.991 | 0.991 | 0.991 | 74.0 | 180.6 | 180.6 |
| **170a6a89_b** | test | **274** | 0.734 | 0.967 | **0.971** | **0.971** | 77.6 | 230.6 | 230.6 |
| 21af9a82_a | test | 76 | 0.803 | 0.816 | 0.816 | 0.816 | 59.0 | 150.0 | 150.0 |
| 21af9a82_b | test | 54 | 1.000 | 1.000 | 1.000 | 1.000 | 0.0 | 0.0 | 0.0 |
| 22d1835e_a | test | 57 | 0.982 | 1.000 | 1.000 | 1.000 | 0.0 | 0.0 | 0.0 |
| 22d1835e_b | test | 77 | 0.987 | 0.987 | 0.987 | 0.987 | 72.3 | 238.3 | 238.3 |
| 2546618f_a | test | 107 | 0.972 | 0.981 | **1.000** | **1.000** | 0.0 | 0.0 | 0.0 |
| 2546618f_b | test | 88 | 0.955 | 0.966 | 0.966 | 0.966 | 89.4 | 322.5 | 322.5 |

## Aggregate (1073 ball-in / 236 no-ball frames)

| detector | R | FP cands/frame (no-ball) |
|---|---|---|
| V11 alone | 0.9049 | — |
| V11 + Y-diff (base) | **0.9702** | 75.9 |
| + ROI-FRST Mode 1 | **0.9739** | 231.9 |
| + ROI-FRST Mode 2 | **0.9739** | 231.9 |

Macro gain from ROI-FRST: **+0.37pp** over the already-strong V11+Y-diff baseline.

---

## V11+Y-diff miss rescue breakdown

32 remaining misses after V11+Y-diff:

| Mode type | n | M1 rescued | M2 rescued |
|---|---|---|---|
| M1 specular (gt_s<80) | 25 | 2 (8%) | 2 (8%) |
| M2 fragmentation | 4 | 1 (25%) | 1 (25%) |
| M3 hue shift | 3 | 1 (33%) | 1 (33%) |

**ROI-FRST rescued 4/32 remaining misses** — both modes identical.

The remaining 25 M1 misses that ROI-FRST could not rescue: these are specular frames in 170a6a89_b and 21af9a82_a. In 170a6a89_b, V11 is near-completely blind (R=0.734), and Y-diff carries most of the 0.967 recall by detecting temporal contrast. The few remaining misses (8 frames in 170a6a89_b) are frames where neither detector fires, so there is no ROI anchor to propagate from — the FRST ROI is empty.

---

## Long miss run: session_s_170a6a89_b

The task hypothesised a "21-frame V11 miss run" at the opening of 170a6a89_b. With Y-diff added, this run collapses to **1 frame** (local=0 only).

**Opening miss run** (base = V11+Y-diff): **1 frame** (local 0)  
**Mode 1 covers**: 0/1  
**Mode 2 covers**: 0/1

Per-frame detail (first 25 ball-in frames):

```
 local  V11  Base   M1   M2  M1c  M2c
     0    .     .    .    .    0    0   ← cold-start: no prior anchor
     1    .     Y    Y    Y   53   53   ← Y-diff hit; Mode 1 armed for frames 2-6
     2    .     Y    Y    Y   55   55   ← Y-diff hit (successive)
     3    .     Y    Y    Y   66   66
    ...  ...   ...  ...  ...  ... ...
    20    .     Y    Y    Y   81   81
    22    Y     Y    Y    Y   69   69   ← first V11 hit (frame 22)
```

**Why the "21-frame run" doesn't appear**: Y-diff rescues frames 1–20 via temporal contrast (ball motion between consecutive frames). The specular-reflection frames that V11 misses are still detected by Y-diff because the ball displacement (~32 px at 240fps) is visible in |Y_t − Y_{t-1}| even when colour is destroyed.

**Cold-start structural constraint** (confirmed): Frame 0 (local=0) has no prior anchor for either mode. Neither Mode 1 nor Mode 2 can cover this frame. This is by design: the ROI propagation requires at least one prior hit. Frame 0 of a session is structurally unrecoverable by any stateful ROI approach unless external seed is provided. Mode 2's `last_known_roi` initialises to None at session start.

**Conclusion on long-run cover**: The stateful design is validated for recovery *within* a miss run that follows a hit — ROI-FRST gets non-zero M1c/M2c scores (50–200 FRST cands inside ROI) on frames 1–20 whenever the prior-frame hit provides an anchor. The cold-start single miss (frame 0) is the residual that no forward-only propagation design can address.

---

## Mode 1 vs Mode 2 comparison

**Results are identical**: R=0.9739 for both modes. This has a clear explanation:

- In most sessions, V11 or Y-diff fires on nearly every ball-in frame (e.g., 170a6a89_b Base=0.967). So `last_known_roi` in Mode 2 is updated almost every frame.
- Mode 2's extra coverage (propagating last-known ROI when m2_state is empty) only triggers on frames with no active TTL anchors AND no current-frame detection. With K=5, the m2_state depletes only if there are 5+ consecutive misses AND no new hits in those 5 frames.
- In the 32 remaining misses, the base detector also misses the preceding frame in many cases, so m2_state is already empty — and `last_known_roi` from the last known hit is often >5 frames stale, meaning the ball has moved out of the ROI.
- **Mode 2 adds zero extra rescue** in this dataset because the miss clusters are too isolated or the ball moves too fast to stay in a 100×100 ROI across >5 stale frames.

**Practical implication**: Mode 2's stateful complexity (persistent `last_known_roi`) offers no measurable benefit over Mode 1's K=5 TTL decay.

---

## FP rate analysis

| mode | FP cands/frame (no-ball) | vs baseline |
|---|---|---|
| V11 + Y-diff base | 75.9 | baseline |
| Mode 1 | 231.9 | 3.1× |
| Mode 2 | 231.9 | 3.1× |

FP increases 3.1× over base. This is because ROI-FRST runs on no-ball frames whenever m1_state has active anchors (from the prior detection at the end of the ball flight). The K=5 window means ROI-FRST fires on up to 5 no-ball frames after each ball-in session end.

**Comparison to full-frame FRST**: full-frame FRST had 9789 FP/frame. ROI-FRST is **42× lower** (231.9 vs 9789).

**Absolute numbers**: 231.9 cands/frame. For the stereo pairing downstream, this is ~232² = 53,824 candidate pairs vs V11's ~25² = 625. Still extremely high for a real-time stereo pipeline, but ~180× fewer than full-frame FRST.

**Sessions with FP=0.0 for Mode 1**: these are sessions where no ROI is active on no-ball frames (the ball-in segment ends cleanly more than K=5 frames before any no-ball frame in the evaluation window). Exact FP depends heavily on session structure.

---

## Bench

| | Python, 1080p, Mac M-class | note |
|---|---|---|
| ROI-FRST 100×100 | **0.43 ms/frame** | single 100×100 patch |
| Full-frame FRST | 51.3 ms/frame | radii [3,5,8,12] |
| Speedup | 120× | |
| iPhone 14 C++ estimate | ~0.04–0.11 ms | 10–25% of Python |

Budget context: V11 budget = 4.16 ms/frame. ROI-FRST 100×100 adds ~0.04–0.11 ms on iPhone — **within budget**. The merged ROI from K=5 anchors (ball displacement ~160 px → ~260×100 box) would be ~2.5× larger → ~0.1–0.28 ms. Still well within budget.

---

## Does ROI-FRST break the V11+Y-diff ceiling?

**No.** V11+Y-diff reaches R=0.970, and ROI-FRST adds only +0.37pp (R=0.9739). The full-frame FRST union reaches R=0.996, so there is +2.6pp of theoretical headroom that ROI-FRST does not recover.

**Why ROI-FRST fails to rescue the specular misses** (M1, 25 frames):

1. **Cold-start**: Frame 0 of 170a6a89_b (the first frame of the session) has no prior anchor. Single cold-start miss is structurally unrecoverable.

2. **Y-diff already rescues most M1 frames**: The "21-frame V11 miss run" in 170a6a89_b collapses to 1 frame when Y-diff is added. Y-diff detects ball displacement in luma, which is independent of whether V11's colour gate fires.

3. **Remaining M1 misses have no anchor from preceding frame**: Of the 25 remaining M1 misses, most are frames where *both* V11 and Y-diff miss the preceding frame too (e.g., isolated frames in 21af9a82_a where desat is extreme). Without a prior-frame detection, m1_state is empty and ROI-FRST has no ROI to search in.

4. **ROI at the right location still can't recover**: FRST rescues 2/25 M1 misses where an anchor did exist. For the other 23, the anchor ROI was either empty (no prior hit) or the ball had moved out of the 100×100 box.

**Root cause**: the 25 residual M1 misses are isolated single frames (not long runs) where the preceding frame also has no V11/Y-diff detection. These require a fundamentally different rescue approach — Kalman-predicted position, or a physics-based trajectory extrapolation.

---

## Stateless contract impact assessment

ROI-FRST requires:
- `m1_state`: list of (roi_box, ttl) — max K entries, each 4 integers
- `last_known_roi`: single (x0, y0, x1, y1) tuple
- `prev_gray`: previous frame grayscale (1920×1080 bytes = 2MB)

**prev_gray** is already required by Y-diff, so its cost is already paid.

The incremental state is `m1_state` (≤5 tuples = 80 bytes) and `last_known_roi` (16 bytes). This is minimal.

**Live path integration**: in the current architecture, each iOS device is stateless at the server (no frame buffer). Adding ROI state would require the server to maintain per-device ROI windows across WebSocket frames — or moving the FRST to iOS-side logic. The per-device state is 80 bytes; the added complexity is the FRST call itself (~0.1ms on iPhone).

**Verdict**: the stateful contract is broken, but the cost is negligible. The real question is whether +0.37pp justifies the integration complexity.

---

## Conclusion

| claim | verdict |
|---|---|
| Can ROI-FRST push V11+Y-diff toward R=0.996 ceiling? | **No** — achieves R=0.974, only +0.37pp gain |
| Long miss run (21-frame) rescued by Mode 2? | **Not applicable** — Y-diff already collapsed the 21-frame run to 1 frame |
| FP rate controllable? | **Partially** — 42× lower than full-frame FRST (232 vs 9789), but still 3.1× over baseline |
| Mode 1 vs Mode 2 difference? | **None** — identical R on this dataset |
| iPhone timing budget impact? | **Negligible** — ~0.1ms vs 4.16ms V11 budget |

**Recommendation**: ROI-FRST is **not worth integrating** at this gain level (+0.37pp). The residual 32 misses after V11+Y-diff are dominated by isolated single-frame M1 specular drops with no prior anchor — a pattern that stateful ROI cannot rescue. Kalman-trajectory prediction (using ballistic physics from `18_miss_run_physics.py`) would address the structural cold-start problem and is a better candidate for the remaining 3% headroom.

If the +0.37pp matters: use Mode 1 only (K=5, top-1 anchor). Mode 2 adds no benefit.

---

## Files

```
lab-research/
├── scripts/24_roi_frst.py           ← implementation + full eval
└── outputs/24_roi_frst_results.json ← machine-readable results
```
