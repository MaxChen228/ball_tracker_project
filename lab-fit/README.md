# lab-fit/ — segmenter analysis sandbox

Decoupled from `server/`. Reads from `server/data/` (read-only),
writes to `lab-fit/reports/` and `lab-fit/out/`.

## Layout

```
algo/                   frozen algorithm snapshots (do not edit prod)
  segmenter.py          frozen from server/segmenter.py @ <sha>
  __init__.py           records the snapshot commit
data_loader.py          single source of truth for "where is the data"
metrics.py              per-segment quality metrics (rmse ratios, LOO,
                        density, max inner gap, ...)
runner.py               run frozen segmenter on a result file
analyses/               one analysis per script, each prints to stdout
                        and saves CSV/PNG to reports/
notebooks/              .py with `# %%` cells for interactive exploration
reports/                analysis outputs (gitignored)
out/                    scratch/ad-hoc outputs (gitignored)
```

## Conventions

- Every analysis script is independently runnable: `python analyses/foo.py`
- Use `from data_loader import ...`, `from algo.segmenter import ...`,
  `from metrics import ...`, `from runner import ...`
- Print useful summary to stdout. Save full results to
  `reports/<analysis_name>/...`
- One analysis per question. Don't try to be a framework.

## Running

From `lab-fit/`:

```bash
uv run python analyses/01_diagnose_session.py s_c26df506
```

(Or `python` if matplotlib/numpy are already available globally.)
