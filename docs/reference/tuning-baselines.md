# Tuning baselines — SoT

This is the **single source of truth** for empirical HSV / fill / aspect /
residual baselines on the project's deep-blue ball. Conflict with other
docs → this file wins.

When changing any threshold here, change it here in the same commit that
ships the code change. The values are calibrated on real sessions, not
theory.

## Field ball

- **Deep-blue hard ball** (NOT yellow-green tennis).
- Dashboard preset is `tennis` or `blue_ball` (**not** `baseball`).
- `server/detection.py` docstring saying "default is yellow-green tennis"
  is a historical fallback (used only for headless tests / first boot).
  Do not edit it.

## HSV — OpenCV 0-179 hue space

Live config (`data/detection_config.json`) `blue_ball` values:

| field   | value        |
|---------|--------------|
| h_min   | **105**      |
| h_max   | **112**      |
| s_min   | **140**      |
| s_max   | 255          |
| v_min   | **40**       |
| v_max   | 255          |

Seed source: `data/presets/blue_ball.json`. Operator can rewrite via
dashboard `Manage…`.

### History / rationale

- **2026-04-29**: h tightened from 100-130 → 105-112 to filter background
  blue.
- **`v_min` must be ≥ 40.** The ball's lower hemisphere goes into shadow,
  V drops below 80. Raising v_min causes near-camera balls to show only
  a high-light ring → mask becomes flat → aspect gate kills it.
  Evidence: `s_cc0dcaa5` reprocess comparison.

### Preset library mechanics

- Disk-backed: `data/presets/<slug>.json`, one file per preset.
- `server/presets.py` holds `_BUILTIN_SEEDS` (`tennis`, `blue_ball`).
  Boot writes them only if the file does not exist. Existing files
  are **never overwritten**. Restore canonical seed → `rm` + restart.
- Each preset binds to exactly one `algorithm_id`
  ([algorithms.md](algorithms.md)) — not interchangeable across
  algorithms.
- Operator-created presets go through dashboard Apply
  (algorithm + preset picker → `POST /presets`); form pulls schema from
  `GET /algorithms`, do not edit source.
- CRUD: `GET /presets`, `GET /presets/{name}`,
  `POST /presets {name, label, algorithm_id, params}`,
  `POST /presets/active {name, target}`,
  `DELETE /presets/{name}`. Full error matrix in
  [protocols.md](protocols.md).

## Fill ratio

Combined mask `hsv_mask AND fg_mask`. Empirical: **0.63 – 0.70**, median
**0.68** (s_fcf73afa + s_03d533c4, 26 fill_fail frames). Theoretical
perfect circle fill = π/4 ≈ 0.785; ball-side shadow + seams + HSV edge
failures bite 10–15%.

- `_MIN_FILL = 0.55` in code (`server/detection.py:98`). Empirical lower edge 0.63 → margin OK.
- **Do not** use morphology CLOSE to push fill up to 0.7+ — that masks
  calibration problems with complexity.
- Selector cost uses 0.68 as the fill-penalty target
  (`fill_pen = |fill - 0.68| / 0.68`); change this median → update the
  constant in `candidate_selector.py` too.

## Shape gate (aspect)

At 240 fps a flying ball is near-perfect circular (mild ellipse + boundary
curvature at most).

- Current `_MIN_ASPECT = 0.70` (`server/detection.py:86`).
- 0.75 is plausibly fine, but **quantify before bumping** (backlog).
- If switching to `4πA/P²` circularity → start at ≥ 0.8.
- Do **not** loosen to swallow motion blur — blur is not this game's
  problem.
- Do **not** introduce HoughCircles.

## How to apply (changing detection thresholds)

- Use this distribution as the baseline, not theory.
- Detection-rate pain is usually **iOS sending too few candidates**
  (cam B observed 92% frames with 0 candidates), not HSV. Investigate
  that first.
- After changing HSV → rerun `server/reprocess_sessions.py` on affected
  sessions ([../operations.md](../operations.md) for syntax).
- Operator does **not** switch ball colour — don't suggest "try tennis
  preset".

## Residual filter floor ≈ 20 cm

The viewer's residual filter (ray-midpoint gap filter) plateaus at
~20 cm. Below that, marginal benefit drops and real trajectory points
start getting cut.

- server_post residual median is 3–5 cm, but mid-flight blur + edge
  pixel jitter naturally puts some real points at 10–30 cm. They are
  real, not noise.
- 20 cm is clean enough to remove outliers without self-harm.
- For stricter outlier rejection use **fit-residual** instead (ballistic
  LSQ → cut >3σ → re-fit). Do **not** lower the residual cap further.

## Selector weights (locked)

`candidate_selector.py` module constants — no runtime tunable, no disk
file, no dashboard card:

| weight       | value | meaning                                                |
|--------------|-------|--------------------------------------------------------|
| `_W_ASPECT`  | 0.6   | weight on `(1 - aspect)` normalised to floor 0.5       |
| `_W_FILL`    | 0.4   | weight on `|fill - 0.68| / 0.68`                       |
| `MIN_AREA`   | 20 px | area entry gate (not a cost input — distance-dependent)|

`area` is gated for entry only; absolute pixel area is a function of
distance (240 fps pitch sweeps r ≈ 4 → 25 px), so it cannot be a
property of "ball-likeness".

## When to revisit (chroma cross-link)

Hue values here assume the current iOS BT.601 vs server BT.709
unalignment is acceptable (per
[hue-and-color.md](hue-and-color.md)). Re-quantify with
`server/chroma_alignment_check.py` whenever:

- A new preset's hue width is < 6 OpenCV units (e.g. fluorescent yellow
  25-30 is too narrow).
- iOS chip / OS major upgrade.
- libswscale upgrade.
