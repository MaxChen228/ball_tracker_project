# tools/ — Offline GT labelling + distillation venv

Separate uv-managed venv for offline SAM 3 GT labelling and parameter
distillation. Kept out of `server/pyproject.toml` so production server
imports never trigger torch / transformers loading.

## Why HuggingFace transformers, not facebookresearch/sam3

The official SAM 3 repo (https://github.com/facebookresearch/sam3) hard-requires Triton + CUDA
12.6 for Euclidean Distance Transform calculations and **fails to load
on Apple Silicon**. The HuggingFace transformers port (added Nov 2025
on main, not yet in a stable release) drops both Triton and flash-attn
dependencies and works on MPS / CUDA / CPU.

## First-time setup

```bash
cd tools
uv sync                    # creates tools/.venv, installs torch + transformers main + deps
uv run huggingface-cli login   # SAM 3 weights are gated; needs HF token
```

The first `label_with_sam3.py` run downloads the SAM 3 weights
(~5 GB) into the HuggingFace cache (`~/.cache/huggingface/hub/`).
That cache is shared across projects — only one copy on disk.

## Apple Silicon (MPS) caveat

There's one upstream bug in `transformers/models/sam3_video/processing_sam3_video.py`
(`pin_memory()` is called on a tensor before `.to()`, which silently
breaks on MPS — see https://huggingface.co/facebook/sam3/discussions/11).
`server/sam3_runtime.py` applies the patch via runtime monkey-patching
on import, so no manual edits to the transformers package are needed.

## Usage from server scripts

The CLIs live under `server/scripts/`. Run them via the `tools` venv:

```bash
cd server
uv run --project ../tools python scripts/label_with_sam3.py \
    --session s_xxxxxxxx --cam A --prompt "blue ball"
```

Or from the repo root:

```bash
uv run --project tools python server/scripts/label_with_sam3.py ...
```

`server/sam3_runtime.py` is on the `server/` package import path (the
script does `sys.path.insert` like other scripts), so the same
codebase is used by both the tools venv (heavy, has torch) and the
server venv (light, no torch — `sam3_runtime` import is gated behind
the script entry).

## Memory notes (M4 Mac Air, 16 GB)

- SAM 3 model in bfloat16: ~5 GB
- 1080p × 1200-frame video pre-loaded: ~7 GB
- Total active: well within 16 GB; no swap needed
- Use `--limit-frames N` if you hit memory pressure on longer clips
- Use `--image-size 560` (default 1008) for ~3× speedup at minor accuracy cost
