# 12 — Ensemble Distillation of Orthogonal Detection Cues (design)

## Research thesis

We have empirical evidence (notes 03_frst_eval / 08_yplane_diff /
11_cue_independence) that three classical detectors fail on **disjoint**
frame sets:

| Cue                   | Alone R | Union w/ V11 | Attacks                     | FP/frame |
|-----------------------|---------|--------------|-----------------------------|----------|
| V11 (HSV color)       | 0.9049  | —            | normal-lit, color-saturated | low      |
| Y-plane diff (luma Δ) | ~0.55   | 0.9702       | M1 specular (75.6%)         | ~1       |
| FRST (radial sym)     | 0.9521  | 0.9961       | M1 specular (97.8%)         | ~9789    |

`V11 ∪ Y-diff ∪ FRST` ≈ 0.998 oracle ceiling. The cues are **physically
orthogonal**: chrominance vs temporal luminance gradient vs radial
intensity symmetry. FRST cannot ship alone (FP rate kills any tracker)
but its recall on M1 (specular desaturation) is the highest of the three.

**Question.** Can a single tiny CNN (<500K params, standard conv only,
192–256² input — see [09_a15_detection_benchmarks.md](09_a15_detection_benchmarks.md))
absorb all three mechanisms via ensemble distillation, hitting the
~0.97-0.98 union ceiling **without** the FRST FP cost at deploy time?

The contribution is methodology + ablation evidence even if the student
doesn't beat the union baseline at this data scale (1073 frames).

---

## 1. Teacher design

**Choice: Option C — cue-decomposable 3-channel teacher.**

Options A (hard OR label) and B (single fused soft heatmap) **collapse
cue identity** before the supervision signal reaches the student. The
cue-consistency auxiliary loss requires per-cue supervision so the
student's internal feature map can be linearly probed for each cue
individually. Only C provides that.

### Teacher tensor

For each frame (256² resized), produce `T ∈ R^{3×256×256}` where each
channel is a normalized soft heatmap in [0, 1]:

- **Channel 0 (V11)**: NMS peaks of V11 candidates → 2-D Gaussian splat
  (σ=4 px) at each peak.
- **Channel 1 (Y-diff)**: NMS peaks of Y-diff candidates (thr=15 — best
  union per 11_cue_independence) → Gaussian splat. Frame 0 of each
  session has no t-1; channel = zeros (explicit, no fallback).
- **Channel 2 (FRST)**: top-K (K=8) NMS peaks of FRST symmetry map after
  threshold tuned in 19_frst (best_t≈0.8) → Gaussian splat.

Why splat instead of raw normalized FRST score map: FRST FP rate is
9789/frame; the raw map is mostly noise. Splatting the post-NMS top-K
gives a teacher that mirrors the actual detector output and stays
consistent in form across all three cues. **Sanity-check a few frames
visually** before launching the full LOSO run.

### Teacher cache

Compute all teacher heatmaps **once** at startup (~1073 frames × 3 cues),
serialize to `outputs/23_teacher_heatmaps.npz` keyed by `(slug, src)`.
FRST at 1080p Python is ~hundreds of ms/frame — caching is critical
since 9 folds × 30 epochs would otherwise re-pay FRST cost ~18×.

### Why not the GT mask itself

The student already gets the GT Gaussian as primary loss. Teacher
captures **what each cue knows**, including that cue's failure pattern
(FRST teacher will have huge background noise on backgrounds with high
local symmetry — that's the cue's identity, not a bug). The student is
forced to learn the union *without* inheriting per-cue FP burdens because
it ultimately optimises the GT primary loss; the per-cue auxiliary heads
exist purely as feature regularisers.

---

## 2. Student architecture

Reuse `TinyFCN` from `22_dl_upper_bound.py` (verified params <500K, no
depthwise, encoder-decoder w/ skip):

- Input: **256×256 RGB** (anisotropic resize from 1920×1080). Justification:
  ball area 60–3000 px² @ 1080p → ~10–50 px @ 256². 192² would push
  smallest balls to ~7 px which is borderline for a σ=4 Gaussian target.
- Backbone: 3→16→32→64→64 encoder, mirrored decoder, skip cat. ~135K
  params (measured in 22_dl_upper_bound).
- **Primary head**: 1×1 conv, 1-channel heatmap logit @ 256².
- **NEW — cue head** (only used for aux loss): 1×1 conv on bottleneck
  feature `b` (16×16, 64ch) → 3-channel logits → bilinear upsample to
  256². Total extra params: 64×3 + 3 = 195. Negligible.
- **Ball presence head**: kept for regularisation as in 22.

Cue head is **bottleneck-only** so the auxiliary signal forces the
information bottleneck to retain cue identity, not late decoder layers
which can short-circuit from the primary heatmap target.

---

## 3. Loss design

```
L = L_primary  +  λ_cue * L_cue  +  λ_ball * L_ball
```

- `L_primary` = focal-MSE(σ(hm_logit), GT_gaussian) — same as script 22.
- `L_cue` = mean over 3 channels of BCE-with-logits(cue_logit_c,
  teacher_c). Per-cue independent (no softmax across channels — they
  can co-fire on same frame).
- `L_ball` = BCE on ball-present logit. λ_ball = 0.1 (kept from 22).

**λ_cue = 0.3** chosen so primary heatmap remains dominant. Ablation
runs `λ_cue = 0` (cue head still exists but not back-propagated) to
isolate the auxiliary signal effect.

---

## 4. Evaluation protocol

LOSO 9-fold over 9 items (same as script 22). Per fold: 8 train, 1 test.

Recall metric: `argmax(student heatmap) within max(10, 0.5·r) px of GT
centroid`, in original 1080p coords.

### Conditions

To attribute gains cleanly:

| Condition | Teacher | Aux loss | What it isolates                        |
|-----------|---------|----------|-----------------------------------------|
| **A** GT-only        | none    | off      | "Just a CNN" baseline (= script 22)     |
| **B** Distill no-aux | 3 cues  | off      | Teacher data augments primary only      |
| **C** Distill + aux  | 3 cues  | on       | Cue-consistency forces feature identity |

Ablation A vs C answers: does ensemble distillation help over from-scratch?
Ablation B vs C answers: does the cue-consistency loss matter, or is it
just having a teacher signal?

### Compute budget guard

9 folds × 30 epochs × 3 conditions = 27 trainings. If wall-clock per fold
exceeds 3 min on MPS, **drop conditions to {A, C}** for the full 9 folds
and run B on 3 representative folds only (one cam per unique pitch). The
key contrast is A↔C; B vs C is the second-tier ablation. Decide at the
start of the run, not mid-way.

### Per-mode breakdown

Same proxy classifier (M1 gt_s<80, M3 gt_h<100, else M2). Report
per-mode student recovery vs V11 / V11∪Y-diff / V11∪Y-diff∪FRST union.

### Error decomposition — "what did the student learn?"

For each student miss in test fold:

1. Did V11 hit?  → student should be learning V11 but isn't.
2. Did Y-diff hit but V11 didn't?  → student didn't absorb Y-diff cue.
3. Did FRST hit but V11+Y-diff didn't?  → student didn't absorb FRST cue.
4. Did all three miss?  → cue ceiling, can't blame student.

Tabulate counts per condition. If condition C reduces (3) more than B,
the auxiliary loss is doing work.

### Saturation analysis

`gap = R_oracle_union(V11∪Ydiff∪FRST) - R_student_C`. If gap < 1pp,
student is essentially at ceiling. If gap > 5pp, distillation is leaving
information on the table.

---

## 5. Honest-reporting clause

Per CLAUDE.md (no silent fallback, no fabricated numbers):

- 1073 frames ≪ typical CNN saturation (~10K). Student R may not beat
  V11∪Y-diff (0.970). Report the negative truthfully.
- If λ_cue = 0 vs 0.3 gives Δ < 0.005 macro R, write "auxiliary loss
  has no measurable effect at this scale". Don't claim it helps.
- Student R is upper-bounded by the **GT** (heatmap target), not by
  the teacher union. If we ever see student R > 0.998 (oracle union
  ceiling), it's a tolerance / metric bug; investigate before reporting.
- Cam A/B item-level LOSO has shared-pitch leakage (5 unique pitches
  across 9 items). Report this as a limitation, same as script 22.

---

## 6. Implementation plan

`lab-research/scripts/23_ensemble_distillation.py`:

1. Copy script 22 as skeleton (TinyFCN, LOSO loop, V11+Y-diff baselines,
   hit_check, FrameRecord).
2. Import FRST from `19_frst.py` (sys.path insert, reuse `frst()` and
   `frst_candidates()`).
3. **Phase 0**: build 3-channel teacher tensor for every frame, cache to
   npz. Visual sanity-check 4 frames (save PNG triptych: image / GT /
   teacher channels).
4. **Phase 1**: extend `TinyFCN` with cue-head (subclass or flag).
5. **Phase 2**: extend loss to 3-term combined.
6. **Phase 3**: run condition A (skip teacher loading), C (full),
   optionally B (ablation).
7. **Phase 4**: aggregate, write `13_ensemble_distillation_results.md`.

Outputs:

- `lab-research/outputs/23_teacher_heatmaps.npz`
- `lab-research/outputs/23_ensemble_distillation_results.json`
- `lab-research/notes/13_ensemble_distillation_results.md`
- `lab-research/outputs/23_teacher_sanity_*.png` (4 sanity triptychs)
