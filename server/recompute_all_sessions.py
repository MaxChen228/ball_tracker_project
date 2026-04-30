"""One-shot: re-run pairing fan-out + segmenter on every session under
`data/results/` using a uniform `cost_threshold` / `gap_threshold_m`.
Faster than `reprocess_sessions.py` (no MOV decode, no HSV) — just
re-applies operator-tunable filters to the already-emitted point set.

Usage:
    cd server && uv run python recompute_all_sessions.py
    cd server && uv run python recompute_all_sessions.py --cost 0.5 --gap-m 0.20

Stop the server first — the offline State and the live State both write
to the same SessionResult JSONs, and the live process won't notice the
disk change until restart anyway.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("recompute-all")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cost", type=float, default=None,
                    help="cost_threshold (default: PairingTuning.default())")
    ap.add_argument("--gap-m", type=float, default=None,
                    help="gap_threshold_m in metres (default: PairingTuning.default())")
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sys.path.insert(0, str(Path(__file__).parent))
    from pairing_tuning import PairingTuning
    from session_results import recompute_result_for_session
    import main as _main

    default_pt = PairingTuning.default()
    cost = default_pt.cost_threshold if args.cost is None else float(args.cost)
    gap_m = default_pt.gap_threshold_m if args.gap_m is None else float(args.gap_m)

    logger.info("recompute-all cost=%.3f gap=%.3fm data_dir=%s", cost, gap_m, args.data_dir)

    state = _main.State(data_dir=args.data_dir)
    sids = sorted(state.results.keys())
    logger.info("loaded %d sessions", len(sids))

    n_ok = 0
    n_skip = 0
    n_fail = 0
    for sid in sids:
        try:
            new_result = recompute_result_for_session(
                state, sid, cost_threshold=cost, gap_threshold_m=gap_m,
            )
        except Exception as exc:
            logger.error("FAIL %s: %s", sid, exc)
            n_fail += 1
            continue
        if new_result is None:
            n_skip += 1
            continue
        state.store_result(new_result)
        n_segs = sum(len(v) for v in new_result.segments_by_path.values())
        n_pts = sum(len(v) for v in new_result.triangulated_by_path.values())
        logger.info("OK   %s segs=%d pts=%d cost=%.2f gap=%.3f",
                    sid, n_segs, n_pts, new_result.cost_threshold or 0,
                    new_result.gap_threshold_m or 0)
        n_ok += 1

    logger.info("done ok=%d skip=%d fail=%d", n_ok, n_skip, n_fail)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
