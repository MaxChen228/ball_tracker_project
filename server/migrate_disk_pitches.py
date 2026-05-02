"""One-shot migration: rewrite `data/pitches/*.json` from the pre-flip
flat-keyed shape to the post-flip dict-canonical shape.

Pre-flip disk JSON carried both `frames_live` / `frames_server_post` /
`live_config_used` / `server_post_config_used` AND `frames_by_algorithm`
/ `config_used_by_algorithm` (dual-write Phase 6a/6b mirror). This
script collapses the flat keys into the dicts (with `setdefault`
semantics — pre-existing dict entries win), stamps
`active_server_post_algorithm_id` from the snapshot, and writes the
file back without the flat keys.

Idempotent: running on an already-migrated file (no flat keys present)
is a no-op. Atomic per-file: write to `<file>.tmp` then rename, so a
crash mid-migration leaves untouched originals.

Run:

    cd server
    uv run python migrate_disk_pitches.py --dry-run   # report only
    uv run python migrate_disk_pitches.py             # rewrite

Schema-free: dict-only manipulation so this script works regardless of
schema version. Does NOT import `schemas`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PITCH_DIR = Path(__file__).parent / "data" / "pitches"

# String literals duplicated from `schemas` to avoid a schema import —
# this script is meant to run during transition, including under
# branches where the schema may differ.
_IOS_CAPTURE_TIME = "ios_capture_time"
_LEGACY_PRE_SNAPSHOT = "v11_hsv_cc"

_FLAT_KEYS = (
    "frames_live",
    "frames_server_post",
    "live_config_used",
    "server_post_config_used",
)


def collapse_pitch_dict(d: dict) -> tuple[dict, bool]:
    """Return (new_dict, changed). Pop flat keys, populate
    `frames_by_algorithm` / `config_used_by_algorithm` /
    `active_server_post_algorithm_id` if not already present."""
    has_any_flat = any(k in d for k in _FLAT_KEYS)
    if not has_any_flat:
        return d, False

    flat_live = d.pop("frames_live", None)
    flat_server = d.pop("frames_server_post", None)
    flat_live_cfg = d.pop("live_config_used", None)
    flat_server_cfg = d.pop("server_post_config_used", None)

    srv_alg = (
        flat_server_cfg.get("algorithm_id")
        if isinstance(flat_server_cfg, dict)
        else None
    )

    if flat_live:
        d.setdefault("frames_by_algorithm", {}).setdefault(_IOS_CAPTURE_TIME, flat_live)
    if flat_server:
        bucket = srv_alg if srv_alg is not None else _LEGACY_PRE_SNAPSHOT
        d.setdefault("frames_by_algorithm", {}).setdefault(bucket, flat_server)
        # Stamp pointer eagerly so the post-flip schema's
        # `frames_server_post` computed_field projection (which has
        # NO silent legacy fallback per CLAUDE.md) surfaces these
        # frames. Without this, pre-snapshot legacy records would
        # land on disk with frames in the v11 bucket but no pointer
        # → projection returns [].
        d.setdefault("active_server_post_algorithm_id", bucket)
    if flat_live_cfg is not None:
        d.setdefault("config_used_by_algorithm", {}).setdefault(_IOS_CAPTURE_TIME, flat_live_cfg)
    if flat_server_cfg is not None and srv_alg is not None:
        d.setdefault("config_used_by_algorithm", {}).setdefault(srv_alg, flat_server_cfg)
        d.setdefault("active_server_post_algorithm_id", srv_alg)

    return d, True


def migrate_file(path: Path, dry_run: bool) -> str:
    """Returns one of: 'noop', 'changed', 'error:<reason>'."""
    try:
        original = path.read_text()
        d = json.loads(original)
    except Exception as exc:
        return f"error:read({exc})"
    d, changed = collapse_pitch_dict(d)
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
    p.add_argument("--path", type=Path, default=PITCH_DIR,
                   help="pitch directory (default: server/data/pitches)")
    args = p.parse_args()

    if not args.path.is_dir():
        print(f"error: {args.path} is not a directory", file=sys.stderr)
        return 2

    files = sorted(args.path.glob("session_*.json"))
    if not files:
        print(f"no pitch JSONs found in {args.path}")
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
