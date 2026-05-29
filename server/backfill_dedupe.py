"""One-shot backfill: re-run pairing fan-out + segmenter on every
persisted session under the new chord-based dedupe.

WHY: `_dedupe_segments` in `segmenter.py` switched ranking from
`(-n, +rmse, cos≥0.95)` to "longer 3D chord wins, no other rule". The
on-disk `SessionResult.segments_by_algorithm` for every existing
session still reflects the old rule. This script rebuilds every
session's result in place at its current `gap_threshold_m`.

SAFETY: The HTTP server must NOT be running — concurrent writes to
`data/results/session_*.json` will corrupt them. Script aborts if it
detects a listener on 8765.

AUDIT: every run (including --dry-run) writes a JSON diff log to
`data/migrations/dedupe-backfill-<epoch>.json` capturing per-session
before/after segment counts + each segment's `original_indices`, so the
exact set of dropped segments is recoverable for review. Run --dry-run
first to inspect the change distribution before committing the rewrite.

Usage:
    cd server && uv run python backfill_dedupe.py --dry-run  # preview + log
    cd server && uv run python backfill_dedupe.py            # write + log
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

# Allow `python backfill_dedupe.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _server_listening(port: int = 8765) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report per-session before/after segment count; don't write")
    ap.add_argument("--port", type=int, default=8765,
                    help="port the server uses (safety check)")
    args = ap.parse_args()

    if not args.dry_run and _server_listening(args.port):
        print(f"ERROR: a process is listening on :{args.port}. "
              "Stop the server before running backfill (concurrent writes "
              "would corrupt result JSONs). Re-run with --dry-run to "
              "preview without stopping the server.", file=sys.stderr)
        return 2

    # Lazy import so the safety check above runs first.
    from state import State
    from session_results import recompute_result_for_session

    state = State()
    sids = sorted(state.results.keys())
    print(f"hydrated {len(sids)} sessions from disk")

    changed = 0
    unchanged = 0
    failed = 0
    # Per-session audit record. For changed sessions we keep both the
    # count delta AND each segment's original_indices before/after, so a
    # reviewer can see exactly which segments the new dedupe dropped
    # (recoverable by re-running detection / reverting the segmenter).
    audit: dict[str, object] = {}
    for sid in sids:
        try:
            old = state.results[sid]
            old_counts = {
                alg: len(segs)
                for alg, segs in old.segments_by_algorithm.items()
            }
            new = recompute_result_for_session(
                state, sid, gap_threshold_m=old.gap_threshold_m,
            )
            new_counts = {
                alg: len(segs)
                for alg, segs in new.segments_by_algorithm.items()
            }
            if new_counts != old_counts:
                deltas = []
                for alg in sorted(set(old_counts) | set(new_counts)):
                    o, n = old_counts.get(alg, 0), new_counts.get(alg, 0)
                    if o != n:
                        deltas.append(f"{alg}: {o}→{n}")
                print(f"  {sid}  Δ  {'  '.join(deltas)}")
                changed += 1
                audit[sid] = {
                    "before": {
                        alg: [list(s.original_indices) for s in segs]
                        for alg, segs in old.segments_by_algorithm.items()
                    },
                    "after": {
                        alg: [list(s.original_indices) for s in segs]
                        for alg, segs in new.segments_by_algorithm.items()
                    },
                }
            else:
                unchanged += 1
            if not args.dry_run:
                state.store_result(new)
        except Exception as exc:
            print(f"  {sid}  FAIL  {exc}")
            failed += 1

    # Always emit the audit log (even dry-run) so the change distribution
    # can be inspected before committing the rewrite.
    migrations_dir = state._data_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    suffix = "dry-run" if args.dry_run else "applied"
    log_path = migrations_dir / f"dedupe-backfill-{stamp}-{suffix}.json"
    log_path.write_text(json.dumps({
        "epoch": stamp,
        "dry_run": args.dry_run,
        "summary": {"changed": changed, "unchanged": unchanged, "failed": failed},
        "sessions": audit,
    }, indent=2))

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}done — "
          f"changed={changed}  unchanged={unchanged}  failed={failed}")
    print(f"audit log: {log_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
