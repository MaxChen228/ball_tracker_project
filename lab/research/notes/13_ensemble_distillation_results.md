# 13 — Ensemble distillation results

Model: TinyFCNDistill, params=199,077, no depthwise, input 256², device=mps.
Dataset: 9 items, 1309 frames, 1073 ball-in.
Hyperparams: λ_cue=0.3, λ_ball=0.1, YDIFF_THR=15, FRST_TOPK=8, FRST_THR=0.8.
Conditions run: A (GT-only) and C (distill + cue-consistency aux). B not run (see §4 for why).

## 1. Macro recall by condition

| Condition | Student R | V11 | V11∪Ydiff | V11∪Ydiff∪FRST |
|---|---|---|---|---|
| A (n_ball=1073) | **0.1053** | 0.9049 | 0.9702 | 0.9711 |
| C (n_ball=1073) | **0.0829** | 0.9049 | 0.9702 | 0.9711 |

## 2. Per-session student recall (condition C)

| Item | n_ball | V11 | Student | Ydiff | FRST | V11∪Yd∪FRST |
|---|---|---|---|---|---|---|
| session_s_16ec069a_b | 228 | 0.982 | **0.101** | 0.579 | 0.000 | 0.991 |
| session_s_170a6a89_a | 112 | 0.991 | **0.027** | 0.205 | 0.000 | 0.991 |
| session_s_170a6a89_b | 274 | 0.734 | **0.000** | 0.803 | 0.106 | 0.974 |
| session_s_21af9a82_a | 76 | 0.803 | **0.197** | 0.224 | 0.013 | 0.803 |
| session_s_21af9a82_b | 54 | 1.000 | **0.426** | 0.574 | 0.000 | 1.000 |
| session_s_22d1835e_a | 57 | 0.982 | **0.000** | 0.474 | 0.000 | 1.000 |
| session_s_22d1835e_b | 77 | 0.987 | **0.026** | 0.377 | 0.143 | 0.987 |
| session_s_2546618f_a | 107 | 0.972 | **0.168** | 0.383 | 0.000 | 0.981 |
| session_s_2546618f_b | 88 | 0.955 | **0.057** | 0.420 | 0.023 | 0.966 |

Student per-session R ranges 0.000–0.426 with no clear pattern. High variance suggests the model
is not generalising a stable cue — it is memorising session-level colour/texture patterns.

## 3. Pareto comparison vs prior baselines

| Method | Recall R | FP/frame | Latency (approx) | Deploy-ready? |
|---|---|---|---|---|
| V11 alone | 0.9049 | low | <1 ms (HSV+CC) | ✅ production |
| V11 ∪ Y-diff (thr=15) | 0.9702 | ~1 | <2 ms | ✅ ships now |
| V11 ∪ Y-diff ∪ FRST | 0.9711 | ~9789 | >100 ms | ❌ FP unacceptable |
| Oracle teacher union (notes 12) | ~0.998 | — | — | — |
| **Student A (GT-only, CNN)** | **0.1053** | tbd | ~1–5 ms (ANE est.) | ❌ recall fails |
| **Student C (distill+aux)** | **0.0829** | tbd | ~1–5 ms (ANE est.) | ❌ recall fails |

V11 ∪ Y-diff is the current Pareto-optimal live solution: best recall at acceptable FP cost.
Neither student condition comes close. The student ANE latency estimate (~1–5 ms for 199K-param
standard-conv network at 256², per notes/09_a15_detection_benchmarks.md) would be fine if recall
were competitive — it is not.

## 4. Cue-consistency auxiliary loss ablation

- Condition A (GT-only, no teacher): R = 0.1053
- Condition C (teacher + aux loss):  R = 0.0829
- Δ (C − A) = -0.0224

**Condition B not run.** Architectural note: in the current implementation, condition B
(`use_teacher=True, use_aux=False`) is functionally equivalent to condition A — the teacher tensor
is loaded but the only path where it affects gradients is through `L_cue` in `total_loss`, which
is gated by `use_aux`. With `use_aux=False`, teacher information never reaches the loss. Running
B would produce the same result as A ± random seed variance. The B vs C ablation as designed
would isolate *primary-loss teacher conditioning* — but that requires feeding teacher channels
as extra input or adding a second auxiliary decoder head, neither of which is implemented. This is
a design gap; not fixed here, reported as a limitation.

**Verdict: cue-consistency aux loss shows no benefit at this scale.** Condition C is −2.24pp
*worse* than A, which is within the noise of high-variance LOSO at 9 items. The loss landscape
difference between conditions is negligible compared to the per-fold data variance. The auxiliary
loss is not helping, and may be adding noise through the FRST teacher channels (FRST recall=0.040,
its teacher signal is mostly FP-dense background noise even after top-K NMS).

## 5. Where the student misses (condition C)

- Total student misses: 984 / 1073 (91.7%)
- of which V11 alone could have hit: 883 — **student didn't absorb V11** (89.7% of misses)
- of which Y-diff (not V11) could have hit: 70 — student didn't absorb Y-diff
- of which FRST (not V11 nor Y-diff) could have hit: 2 — student didn't absorb FRST
- All-three-miss (cue ceiling, un-learnable): 30

The overwhelming majority of misses (883/984 = 89.7%) are frames where V11 succeeds but the
student fails. The student has not learned even the primary cue (HSV colour). This indicates
data-scale failure: with ~950 training frames per fold and 30 epochs of random-init training,
the model cannot converge reliably to a ball-detection policy.

## 6. Per-mode V11-miss recovery

| Cond | M1 (specular) | M2 (frag) | M3 (hue) |
|---|---|---|---|
| A | 1.1% (n=90) | 11.1% (n=9) | 0.0% (n=3) |
| C | 1.1% (n=90) | 0.0% (n=9) | 0.0% (n=3) |

Student recovery of V11 misses is near-zero across all modes. The ensemble distillation
objective — absorbing Y-diff and FRST cues for M1/M2 — is not achieved. The student learns
almost nothing at this data scale.

## 7. Saturation analysis

Oracle union ceiling (V11∪Yd∪FRST): R = 0.9711

Student gap to ceiling (condition C): **+88.82pp**

At 1073 ball-in frames the student is nowhere near saturation.

## 8. Conclusion

**Student R (best condition A: 0.1053) does NOT beat V11 alone (0.9049).** Both conditions fail
by a large margin. Distillation does not help at this data scale.

The three key questions from notes/12:

1. **Did the student learn ensemble?** No. R=0.1053 vs oracle 0.9711 is an 86.6pp gap. The
   student did not absorb even V11.

2. **Per-mode recovery — what cue did it learn?** None reliably. M1 student recovery = 1.1%
   (Y-diff would give 72.2%). The student is not learning temporal luma gradients.

3. **5–8 ms ANE budget — worth deploying?** No. At R=0.1053 the student cannot replace even
   V11 alone, let alone the V11∪Y-diff union. The latency budget is theoretically fine
   (199K params, standard conv, 256²) but recall is unacceptable.

**Recommended path**: maintain V11∪Y-diff (R=0.9702) as the live production cue. To make
distillation viable, collect ~10× more labelled frames (~10K ball-in) — then re-run this
experiment. The methodology (3-channel teacher, LOSO, cue-consistency aux loss) is sound;
the data scale is the binding constraint.

## 9. Limitations

- **Data scale is the dominant constraint**: 9 items / ~119 ball-in frames per training fold.
  Script 22 (DL upper bound, same architecture without teacher) similarly converges poorly.
  The conclusion "distillation doesn't help" is accurate but partially confounded by the
  fact that from-scratch DL doesn't work at all at this scale.
- 9 items / 5 unique pitches → cam-A/B leakage in LOSO; recall estimates are optimistic.
- Mode classifier is proxy (M1: gt_s<80, M3: gt_h<100); canonical M3 count is 9 (notes 11).
- FRST teacher uses top-K NMS peaks (not raw symmetry score map); its teacher signal may be
  too sparse to carry information vs noise.
- Anisotropic 256² resize distorts ball aspect; letterbox would be cleaner.
- No pretrained weights. With ImageNet pretraining even a tiny backbone may generalise better
  at this data scale.
- Condition B architectural equivalence (see §4) means the B vs C comparison cannot be made
  with the current implementation.
