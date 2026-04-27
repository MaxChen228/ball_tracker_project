"""Rewrite legacy `fz` keys as `fy` across persisted intrinsics JSON.

`IntrinsicsPayload` originally accepted both `fy` and `fz` on the wire via
Pydantic AliasChoices — a historical collision from early iOS code. That
alias has been retired; any surviving `fz` key would now fail validation
on load. This script walks `data/calibrations/*.json` and
`data/pitches/*.json`, finds every object containing an `fz` key alongside
`fx`/`cx`/`cy` (the unambiguous intrinsics shape), renames `fz` → `fy`,
and writes back atomically (tmp + rename).

Safe to re-run: if `fy` already exists on the same object, `fz` is
dropped without overwriting (both were equal by construction anyway —
the alias only populated `fy`). Prints a per-file summary.

Usage (from repo root):
  uv run python server/scripts/migrate_fz_to_fy.py [--data-dir PATH] [--dry-run]

Defaults `data-dir` to `server/data` (relative to repo root).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _rewrite_fz_in_place(obj: Any) -> int:
    """Recursively walk a JSON value, renaming `fz` → `fy` on any dict
    that also has `fx`/`cx`/`cy` (i.e. an intrinsics-shaped object).
    Returns the number of rewrites applied."""
    count = 0
    if isinstance(obj, dict):
        is_intrinsics = "fx" in obj and "cx" in obj and "cy" in obj and "fz" in obj
        if is_intrinsics:
            fz = obj.pop("fz")
            if "fy" not in obj:
                obj["fy"] = fz
            count += 1
        for v in obj.values():
            count += _rewrite_fz_in_place(v)
    elif isinstance(obj, list):
        for v in obj:
            count += _rewrite_fz_in_place(v)
    return count


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def migrate(data_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Walk data_dir's `calibrations/`, `pitches/`, and `intrinsics/`
    subdirs. Returns {"files_scanned", "files_rewritten", "total_replacements"}.

    The original script only covered `calibrations/` + `pitches/`, but
    `data/intrinsics/<device_id>.json` (per-device ChArUco K, written by
    the dashboard intrinsics upload) follows the same shape and was
    silently skipped — any pre-migration intrinsics file still loads
    today via `DeviceIntrinsics` schema validation, but fails because
    `IntrinsicsPayload` no longer accepts `fz` as an alias. Adding the
    third subdir here covers that case."""
    stats = {"files_scanned": 0, "files_rewritten": 0, "total_replacements": 0}
    for sub in ("calibrations", "pitches", "intrinsics"):
        root = data_dir / sub
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            stats["files_scanned"] += 1
            try:
                obj = json.loads(path.read_text())
            except Exception as e:
                print(f"skip {path}: {e}", file=sys.stderr)
                continue
            n = _rewrite_fz_in_place(obj)
            if n == 0:
                continue
            stats["files_rewritten"] += 1
            stats["total_replacements"] += n
            if dry_run:
                print(f"[dry-run] would rewrite {n}x fz→fy in {path}")
            else:
                _atomic_write(path, json.dumps(obj, indent=2))
                print(f"rewrote {n}x fz→fy in {path}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename fz→fy in persisted intrinsics JSON.")
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "server" / "data",
        help="Root data/ dir (expects calibrations/ + pitches/ subdirs).",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.data_dir.is_dir():
        print(f"data dir not found: {args.data_dir}", file=sys.stderr)
        return 2
    stats = migrate(args.data_dir, dry_run=args.dry_run)
    print(
        f"done: scanned={stats['files_scanned']} "
        f"rewritten={stats['files_rewritten']} "
        f"replacements={stats['total_replacements']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
