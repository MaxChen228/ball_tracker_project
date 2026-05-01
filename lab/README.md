# Lab — ball labeling workspace

Standalone labeling tool. Human points the seed; SAM 2 turns the point into a
mask in real time; SAM 2 video predictor propagates that mask outward and
streams each frame's result back to the UI as it lands.

## Run

```bash
lab/.venvs/sam2_probe/bin/python -m lab.labeller
# open http://127.0.0.1:8876
```

`LABELLER_PORT=9000` env var overrides the port.

## Pipeline

```
drop MOV into lab/standalone_workspace/source_videos/
    → reload UI → item appears
    → scrub video, [ to mark in, ] to mark out
    → S to mark seed frame, click ball → mask appears (<1s on M-series MPS)
    → Enter to propagate → masks fill timeline outward from seed via SSE
    → masks live under items/<slug>/masks/<source_frame:05d>.png
```

Compose an overlay video from the masks:

```bash
lab/cli.py overlay --slug <slug> --out overlay.mp4
```

Quick LLM/agent video skim (not part of the main flow):

```bash
lab/contact_sheet.py --video X.mov --mode macro --tiles 25 --out sheet.jpg
lab/contact_sheet.py --video X.mov --mode micro --anchor 408 --out micro.jpg
```

## Files

- `labeller.py` — HTTP server (`ThreadingHTTPServer`), manifest, endpoints, SSE bus.
- `seeder.py` — wraps SAM 2 image predictor; one positive-point click → PNG mask.
- `propagator.py` — wraps SAM 2 video predictor; generator yields `(frame, mask)`.
- `static/` — vanilla JS SPA (HTML5 video + canvas overlay + SSE).
- `cli.py` — stdlib HTTP client; every UI action has a CLI parity.
- `contact_sheet.py` — independent CLI for LLM-friendly contact sheets.

## Workspace

```
lab/standalone_workspace/
├── manifest.json
├── source_videos/<slug>.<ext>     # drop MOV / mp4 here
└── items/<slug>/
    ├── frames/00000.jpg ...        # extracted on /propagate over [in,out]
    ├── seed_mask.png               # most recent seed click result
    └── masks/<source_idx:05d>.png  # propagation outputs
```

## SAM 2 model

`facebook/sam2-hiera-tiny`, loaded once on first `/seed` or `/propagate`.
On Apple Silicon: ~1.5s load, ~1s per seed click, ~1.2 frames/sec propagate.
