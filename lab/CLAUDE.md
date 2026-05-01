# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Scope

`lab/` is a standalone labeling workspace, not mounted into the main `server/`
FastAPI app. Cross-project rules live in [`../CLAUDE.md`](../CLAUDE.md) and
apply here too (no silent fallback, lockstep deploy, OpenCV hue 0-179).

## Pipeline (the one that matters)

```
trim (in / out marker) → seed (human click → SAM 2 image predictor → mask <1s)
                       → propagate (SAM 2 video predictor generator → SSE stream)
```

There is **no** Grounding DINO / auto-seed path. Seed is always human-driven.
There is **no** "approve preview" gate — the mask appears immediately on click;
unhappy with it, click again, costs <1s.

## Run

```bash
lab/.venvs/sam2_probe/bin/python -m lab.labeller    # http://127.0.0.1:8876
```

The labeller process must run inside `lab/.venvs/sam2_probe` (sam2 + torch
deps), not the main project's uv venv. Model loads lazily on first `/seed` or
`/propagate` request (~1.5s on M-series MPS).

## File ownership

- `labeller.py` — single HTTP entrypoint. Owns manifest store, endpoint
  dispatch, SSE bus, frame extraction, propagation thread.
- `seeder.py` — `Seeder.seed_at(bgr_array, x, y) → png_bytes`. Holds image
  predictor; threadsafe.
- `propagator.py` — `Propagator.propagate(frames_dir, seed_local_idx, point) →
  Iterator[(local_idx, png_bytes)]`. Does forward then reverse pass.
- `static/` — pure vanilla JS, no build step. Talks to labeller via REST + SSE.
- `cli.py` — stdlib HTTP client, parity with UI actions for headless / agent
  use.
- `contact_sheet.py` — independent CLI; not part of labeller flow.

## Coordinates and indexing

Frontend always sends **source-video frame indices** (0-based, range
`[0, total_frames-1]`). Labeller translates to local (extracted-frames) index
when calling SAM 2: `local = source - in_frame`. Masks on disk are keyed by
**source** index (`masks/<source_idx:05d>.png`).

When changing this convention: search for `local_idx` and `source_idx` in
`labeller.py` and `propagator.py`; both must move together.

## SSE bus contract

`BUS.publish(slug, event, data)` → all subscribers of that slug get the event.
Events emitted by `run_propagate`:

- `event: mask` `data: {"frame": <source_idx>, "mask_url": "/mask/<slug>/<NNNNN>.png"}`
- `event: done` `data: {}`
- `event: error` `data: {"msg": <str>}`

Frontend `app.js` listens on these names; CLI `--watch` parses the same
stream. Adding new events → update both consumers same commit.

## What to never do

- Don't bring back Grounding DINO / auto-seed. The whole point of the rewrite
  is that human-pick beats DINO on small ball.
- Don't add a "review mask" approve/reject UI step. Re-clicking is the review.
- Don't pre-extract all frames at import. Frame extraction is on-demand at
  `/propagate` (range `[in, out]` only).
- Don't write `lab/.venvs/sam2_probe` paths into committed code as `python` —
  always `lab/.venvs/sam2_probe/bin/python`. Worktrees don't carry that venv;
  for tests in worktrees, point `PYTHONPATH` at the worktree and use the main
  repo's venv binary.
- Don't modify `manifest.json` from anywhere except `ManifestStore` (lock
  invariant). Don't introduce a second writer.
