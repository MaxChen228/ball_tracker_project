"""One-shot migration: rewrite `data/results/*.json` from the pre-flip
flat-keyed shape to the post-flip dict-canonical shape.

Pre-flip disk JSON carried both `triangulated_by_path` /
`segments_by_path` / `frame_counts_by_path` / `paths_completed` /
`live_config_used` / `server_post_config_used` AND the dict-keyed
mirrors. This script collapses the flat keys into the dicts (with
`setdefault` semantics — pre-existing dict entries win), stamps
`active_server_post_algorithm_id` from the snapshot when present, and
writes the file back without the flat keys.

Idempotent: running on an already-migrated file (no flat keys present)
is a no-op. Atomic per-file (tmp + rename).

Run:

    cd server
    uv run python migrate_disk_results.py --dry-run   # report only
    uv run python migrate_disk_results.py             # rewrite

Schema-free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULT_DIR = Path(__file__).parent / "data" / "results"

_IOS_CAPTURE_TIME = "ios_capture_time"
_LEGACY_PRE_SNAPSHOT = "v11_hsv_cc"

_FLAT_KEYS = (
    "triangulated_by_path",
    "segments_by_path",
    "frame_counts_by_path",
    "paths_completed",
    "live_config_used",
    "server_post_config_used",
)


def _route_path_dict(d: dict, legacy: dict | None, target_key: str, srv_bucket: str) -> bool:
    """Move `legacy["live"]` / `legacy["server_post"]` into
    `d[target_key][ios_capture_time]` / `d[target_key][srv_bucket]`.
    Returns True if `legacy["server_post"]` was non-empty so the caller
    can stamp the active pointer."""
    saw_srv = False
    if not legacy:
        return saw_srv
    target = d.setdefault(target_key, {})
    live_val = legacy.get("live")
    if live_val:
        target.setdefault(_IOS_CAPTURE_TIME, live_val)
    server_val = legacy.get("server_post")
    if server_val:
        target.setdefault(srv_bucket, server_val)
        saw_srv = True
    return saw_srv


def collapse_result_dict(d: dict) -> tuple[dict, bool]:
    """Return (new_dict, changed). Pop legacy flat keys, populate
    `*_by_algorithm` mirrors + `active_server_post_algorithm_id`."""
    has_any_flat = any(k in d for k in _FLAT_KEYS)
    if not has_any_flat:
        return d, False

    flat_tri = d.pop("triangulated_by_path", None)
    flat_segs = d.pop("segments_by_path", None)
    flat_counts = d.pop("frame_counts_by_path", None)
    flat_paths = d.pop("paths_completed", None)
    flat_live_cfg = d.pop("live_config_used", None)
    flat_server_cfg = d.pop("server_post_config_used", None)

    srv_alg = (
        flat_server_cfg.get("algorithm_id")
        if isinstance(flat_server_cfg, dict)
        else None
    )
    srv_bucket = srv_alg if srv_alg is not None else _LEGACY_PRE_SNAPSHOT

    server_post_observed = False
    if _route_path_dict(d, flat_tri, "triangulated_by_algorithm", srv_bucket):
        server_post_observed = True
    if _route_path_dict(d, flat_segs, "segments_by_algorithm", srv_bucket):
        server_post_observed = True
    if _route_path_dict(d, flat_counts, "frame_counts_by_algorithm", srv_bucket):
        server_post_observed = True

    if flat_paths:
        completed = d.setdefault("algorithms_completed", [])
        completed_set = set(completed) if isinstance(completed, list) else set(completed)
        if "live" in flat_paths:
            completed_set.add(_IOS_CAPTURE_TIME)
        if "server_post" in flat_paths:
            completed_set.add(srv_bucket)
            server_post_observed = True
        d["algorithms_completed"] = sorted(completed_set)

    if flat_live_cfg is not None:
        d.setdefault("config_used_by_algorithm", {}).setdefault(_IOS_CAPTURE_TIME, flat_live_cfg)
    if flat_server_cfg is not None and srv_alg is not None:
        d.setdefault("config_used_by_algorithm", {}).setdefault(srv_alg, flat_server_cfg)
        d.setdefault("active_server_post_algorithm_id", srv_alg)

    if server_post_observed:
        d.setdefault("active_server_post_algorithm_id", srv_bucket)

    return d, True


def migrate_file(path: Path, dry_run: bool) -> str:
    try:
        original = path.read_text()
        d = json.loads(original)
    except Exception as exc:
        return f"error:read({exc})"
    d, changed = collapse_result_dict(d)
    if not changed:
        return "noop"
    if dry_run:
        return "changed"
    payload = json.dumps(d)
    tmp = path.with_suffix(path.suffix + ".migrate.tmp")
    try:
        tmp.write_text(payload)
        tmp.replace(path)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return f"error:write({exc})"
    return "changed"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    p.add_argument("--path", type=Path, default=RESULT_DIR,
                   help="result directory (default: server/data/results)")
    args = p.parse_args()

    if not args.path.is_dir():
        print(f"error: {args.path} is not a directory", file=sys.stderr)
        return 2

    files = sorted(args.path.glob("session_*.json"))
    if not files:
        print(f"no result JSONs found in {args.path}")
        return 0

    counts = {"noop": 0, "changed": 0, "error": 0}
    errors: list[tuple[Path, str]] = []
    for f in files:
        outcome = migrate_file(f, dry_run=args.dry_run)
        if outcome.startswith("error"):
            counts["error"] += 1
            errors.append((f, outcome))
        else:
            counts[outcome] += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"{verb}: {counts['changed']}  noop: {counts['noop']}  errors: {counts['error']}")
    for f, reason in errors:
        print(f"  {f.name}: {reason}", file=sys.stderr)
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
