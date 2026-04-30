"""One-shot migration — re-stamp `segments` onto every persisted SessionResult.

Phase 2 of the 3D refactor introduced `SessionResult.segments` and
dropped `extra="ignore"` from the schema, so old result JSONs that
carry legacy `ballistic_*` / `peak_z_m` / `ballistic_speed_mph` fields
will fail to load.

Run once after pulling phase 2:

    cd server
    uv run python migrate_results_segments.py

Behaviour:
  - For each `data/results/session_*.json`, drop legacy keys, run the
    segmenter on `triangulated`, write back atomically.
  - Stops loudly if any file fails to parse — no silent skip. CLAUDE.md
    forbids silent fallback.

This is a migration, not a recurring tool. Once the directory is clean
the script can be deleted; the SessionResult schema change is permanent.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from session_results import stamp_segments_on_result
from schemas import SessionResult

logger = logging.getLogger("migrate_results")
logging.basicConfig(level=logging.INFO, format="%(message)s")


_LEGACY_KEYS = (
    "ballistic_by_path",
    "ballistic_live",
    "ballistic_server_post",
    "peak_z_m",
    "ballistic_speed_mph",
)


def main() -> int:
    here = Path(__file__).resolve().parent
    results_dir = here / "data" / "results"
    if not results_dir.is_dir():
        logger.warning("no results dir at %s — nothing to migrate", results_dir)
        return 0
    files = sorted(results_dir.glob("session_*.json"))
    if not files:
        logger.info("no result files; nothing to migrate")
        return 0

    fails: list[tuple[Path, str]] = []
    rewrote = 0
    for path in files:
        try:
            obj = json.loads(path.read_text())
        except Exception as e:
            fails.append((path, f"unreadable: {e}"))
            continue
        for k in _LEGACY_KEYS:
            obj.pop(k, None)
        try:
            result = SessionResult.model_validate(obj)
        except Exception as e:
            fails.append((path, f"schema: {e}"))
            continue
        stamp_segments_on_result(result)
        path.write_text(result.model_dump_json())
        rewrote += 1
        logger.info("rewrote %s — %d segments", path.name, len(result.segments))

    logger.info("migration done: rewrote=%d failed=%d", rewrote, len(fails))
    if fails:
        for p, why in fails:
            logger.error("FAIL %s — %s", p.name, why)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
