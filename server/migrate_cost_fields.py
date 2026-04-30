"""One-shot migration: add cost_a/cost_b: null to legacy TriangulatedPoint
JSONs so they validate against the post-PR schema (cost_a / cost_b are
required fields, no default).

Run once after pulling the PR that introduces the fields:

    cd server
    uv run python migrate_cost_fields.py            # apply
    uv run python migrate_cost_fields.py --dry-run  # preview only

Subsequent live sessions populate cost_a/cost_b from candidate.cost at
emit time. To regenerate cost from existing candidates on legacy sessions,
run `reprocess_sessions.py --all` instead — that re-pairs from
`frames_server_post` candidates and stamps real cost values, not the
null placeholder this script writes.

Idempotent. Safe to run multiple times.
"""
from __future__ import annotations

import argparse
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


def _detect_indent(raw: str) -> int | None:
    """Best-effort: peek at the second physical line; if it starts with 2+
    spaces before a quote, mirror that indent on write so we don't churn
    whitespace across all session files. Returns None to call json.dumps
    with no indent (compact)."""
    lines = raw.splitlines()
    if len(lines) < 2:
        return None
    second = lines[1]
    stripped = second.lstrip(" ")
    width = len(second) - len(stripped)
    if width >= 2 and stripped.startswith('"'):
        return width
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args()

    if not RESULT_DIR.is_dir():
        print(f"no results dir at {RESULT_DIR}; nothing to migrate")
        return 0
    files = sorted(RESULT_DIR.glob("session_*.json"))
    total_patched = 0
    files_changed = 0
    for f in files:
        raw = f.read_text()
        obj = json.loads(raw)
        n = 0
        for key in ("triangulated", "points"):
            n += _patch_pts(obj.get(key, []))
        for path_pts in obj.get("triangulated_by_path", {}).values():
            n += _patch_pts(path_pts)
        if n:
            files_changed += 1
            total_patched += n
            verb = "would patch" if args.dry_run else "patched"
            print(f"  {f.name}: {verb} {n} points")
            if not args.dry_run:
                indent = _detect_indent(raw)
                f.write_text(json.dumps(obj, indent=indent))
    suffix = " (dry-run)" if args.dry_run else ""
    print(f"done{suffix}: {files_changed}/{len(files)} files changed, {total_patched} points patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
