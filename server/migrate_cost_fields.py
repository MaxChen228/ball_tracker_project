"""One-shot migration: add cost_a/cost_b: null to legacy TriangulatedPoint
JSONs so they validate against the post-PR schema (cost_a / cost_b are
required fields, no default).

Run once after pulling the PR that introduces the fields:

    cd server
    uv run python migrate_cost_fields.py

Subsequent live sessions populate cost_a/cost_b from candidate.cost at
emit time. To regenerate cost from existing candidates on legacy sessions,
run `reprocess_sessions.py --all` instead — that re-pairs from
`frames_server_post` candidates and stamps real cost values, not the
null placeholder this script writes.

Idempotent. Safe to run multiple times.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
RESULT_DIR = DATA_DIR / "results"


def _patch_pts(pts: list[dict]) -> int:
    n = 0
    for p in pts:
        if "cost_a" not in p:
            p["cost_a"] = None
            p["cost_b"] = None
            n += 1
    return n


def main() -> int:
    if not RESULT_DIR.is_dir():
        print(f"no results dir at {RESULT_DIR}; nothing to migrate")
        return 0
    files = sorted(RESULT_DIR.glob("session_*.json"))
    total_patched = 0
    files_changed = 0
    for f in files:
        obj = json.loads(f.read_text())
        n = 0
        for key in ("triangulated", "points"):
            n += _patch_pts(obj.get(key, []))
        for path_pts in obj.get("triangulated_by_path", {}).values():
            n += _patch_pts(path_pts)
        if n:
            f.write_text(json.dumps(obj))
            files_changed += 1
            total_patched += n
            print(f"  {f.name}: patched {n} points")
    print(f"done: {files_changed}/{len(files)} files changed, {total_patched} points patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
