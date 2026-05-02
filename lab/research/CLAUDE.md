# lab/research — agent mandate

## What I'm doing

Research detection algorithms that **beat production** on the SAM2-GT
labelled dataset under `lab/standalone_workspace/items/<slug>/`.

- Goal: per-frame detection R > production. Single number, single
  benchmark. PROD baseline `R_top1 = 0.615` (= "production
  cost-ranked winner within 10 px of GT centroid"). Beat it.
- Constraint: **generalizable methods, not overfitting**. Validate per-
  session, not just aggregate. If aggregate +0.05 but tanks 3/15
  sessions, it's overfit — reject.
- Down-stream is not my concern. Pairing / triangulation / physics gate
  is owned by the user. Do not engineer for n_cand budgets, do not
  optimize for downstream pipeline costs.

## Out of scope (do not touch)

- Triangulation, pairing, ballistic fit, residual filter — user owns
- Production deployment, cost-ranking redesign, candidate budgets
- iOS / server FastAPI changes — research stays in `lab/research/`

## Working data

- 15 confirmed deep-blue sessions + 1 special. SAM2 GT masks at
  `lab/standalone_workspace/items/<slug>/masks/<seg>/<src>.png`
- Cleaned GT centroid (HSV-intersect masks): `masks_hsv/<seg>/`
  via `scripts/_materialize_clean_gt.py`. Drop frames with `hsv_area<5`
  (mask drifted off ball).
- `session_s_373bbf6e_b`: skip entirely (whole-session GT drift).

## What "beat production" means concretely

Single benchmark, single tolerance:

```
R_top1 = mean(any of top-1 cand within TOL_PX of GT centroid)
TOL_PX = 10
top-1  = first cand under production cost ranker (server/candidate_selector.py:
         0.6·aspect_pen + 0.4·fill_pen, lower=better). If your method
         emits with shape stats, this comparison is automatic.
```

PROD baseline: see `outputs/27c_R_topK.json`. Don't move the goalposts.

## Anti-overfit checklist (must answer "yes" to all before claiming win)

1. Per-session R reported, **min session R ≥ PROD's min session R**?
2. No tuning on a held-out subset (declare train/test up front)?
3. Method describable in 1 sentence of physics / vision principle, not
   "thresh=A, kernel=B, dedup=C tuned for our 15 sessions"?
4. Adding more sessions (when ready) — does the method hold?

If a method needs > 3 hyperparameters tuned per-session it's overfit.

## Failure modes that look like progress

These patterns inflate R_emit while degrading R_top1; reject before
running large experiments:

- **Spray + union**: emit 100s-1000s cands/frame, claim R_emit > 0.97.
  Truth-cand rank under any shape ranker collapses (median rank ≫ 1).
  Spray gap (R_emit − R_top1) > 0.5 is the tell.
- **Single-cue temporal gate alone**: motion-only / diff-only. Ball
  isn't the only moving thing in the scene. Always need it gated by a
  ball-specific cue.
- **Per-session threshold tuning**: looks like a generalisable method,
  isn't. See anti-overfit checklist above.

## What's actually open

Detection signals **uncorrelated with production HSV+shape** that
might add discrimination *at the candidate-emit stage* (not as a
post-hoc reranker on top of spray):

- Trajectory consistency across consecutive frames (RANSAC ballistic
  fit on raw cands → inlier-only emit). Single physics constraint,
  not a tuning knob.
- HSV "core depth" — count of strongly-blue pixels in candidate ROI
  (penalize edge-only HSV matches that drag a green-blue boundary in)
- Specular highlight signature — leather ball has consistent highlight
  spot; field markings don't.

## File ownership inside research/

- `scripts/NN_*.py` — one experiment per file, numbered roughly by
  time. `_paths.py` resolves repo paths.
- `outputs/` — gitignored. Each script writes its own JSON/PNG.
- `notes/` — write-ups. Synthesis doc `00_synthesis_*.md` is the
  current top-of-stack summary.

## Commit / push discipline

When in worktree (e.g. `r-metric-redesign`), commit per experiment.
Don't bundle "added scripts + ran them + drew conclusions" in one
diff — each step is reviewable independently.
