#!/usr/bin/env python3
# One-shot ops migration: run once after the hybrid_28d removal commit
# to strip the algorithm from this machine's local persisted state.
# Safe to delete this file after the migration has been run on every
# machine that holds a server/data/ tree with hybrid_28d residue.
#
# Behaviour:
#   1. cp -r each scanned dir to <dir>.bak.<unix_ts> first
#   2. load + validate all hits into memory (any error → abort, no writes)
#   3. atomic write back (tmp file + os.replace)
#
# Scans:
#   - server/data/pitches/*.json (PitchPayload-shaped)
#   - server/data/results/*.json (SessionResult-shaped)
#   - server/data/*.json (top-level state files)
"""Strip hybrid_28d from persisted ball_tracker state."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

HYBRID = "hybrid_28d"
V11 = "v11_hsv_cc"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "server" / "data"
SCAN_DIRS = [DATA_DIR, DATA_DIR / "pitches", DATA_DIR / "results"]


def mutate(obj):
    """Return (new_obj, changed). Strips hybrid_28d buckets from
    frames_by_algorithm / config_used_by_algorithm and rewrites
    active_server_post_algorithm_id if it points at hybrid_28d."""
    if not isinstance(obj, dict):
        return obj, False
    changed = False

    for key in ("frames_by_algorithm", "config_used_by_algorithm"):
        bucket = obj.get(key)
        if isinstance(bucket, dict) and HYBRID in bucket:
            del bucket[HYBRID]
            changed = True

    if obj.get("active_server_post_algorithm_id") == HYBRID:
        obj["active_server_post_algorithm_id"] = V11
        changed = True

    return obj, changed


def main() -> int:
    if not DATA_DIR.exists():
        print(f"no {DATA_DIR}, nothing to do", file=sys.stderr)
        return 0

    ts = int(time.time())
    backups: list[tuple[Path, Path]] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            bak = d.parent / f"{d.name}.bak.{ts}"
            shutil.copytree(d, bak)
            backups.append((d, bak))
    print(f"backups: {[str(b) for _, b in backups]}")

    # Phase 1 — load + validate all candidates into memory
    plan: list[tuple[Path, dict]] = []
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                print(f"ABORT: {p} is not valid JSON: {e}", file=sys.stderr)
                return 1
            new, changed = mutate(data)
            if changed:
                plan.append((p, new))

    if not plan:
        print("no files contain hybrid_28d, nothing to migrate")
        for _, bak in backups:
            shutil.rmtree(bak)
        return 0

    print(f"will rewrite {len(plan)} file(s):")
    for p, _ in plan:
        print(f"  - {p}")

    # Phase 2 — atomic write
    for p, new in plan:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False))
        os.replace(tmp, p)

    print(
        f"done. backups kept at {[str(b) for _, b in backups]}; "
        "verify viewer renders correctly, then `rm -rf` them."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
