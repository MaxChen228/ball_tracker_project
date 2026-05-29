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

Usage:
    cd server && uv run python backfill_dedupe.py
    # add --dry-run to just count what would change
"""
from __future__ import annotations

import argparse
import socket
import sys
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
            else:
                unchanged += 1
            if not args.dry_run:
                state.store_result(new)
        except Exception as exc:
            print(f"  {sid}  FAIL  {exc}")
            failed += 1

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}done — "
          f"changed={changed}  unchanged={unchanged}  failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
