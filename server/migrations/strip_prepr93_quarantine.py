"""One-shot migration (2026-05-13).

PR #93 made BlobCandidate.aspect/fill required. 60 sessions / 117 pitches
recorded before PR #93 carry `aspect=null, fill=null` on their
candidates and pydantic refuses to load them — they were parked in
`server/data/_quarantine_2026-05-13/`.

This script rehydrates them:

- Live bucket (`ios_capture_time`): candidates are unrecoverable (no
  source frames retained), so each frame's `candidates` list is set to
  None. Frame-level `px/py/ball_detected` survive — viewer still has a
  trajectory; the multi-candidate BLOBS overlay just degrades to the
  legacy single-pick.
- Server bucket (`v11_hsv_cc`): dropped entirely from
  `frames_by_algorithm` + `config_used_by_algorithm`, plus
  `active_server_post_algorithm_id` cleared and `server_post_ran_at`
  zeroed. `reprocess_sessions.py` regenerates these from the matching
  MOVs in `data/videos/`.

After migration, run:

    uv run python reprocess_sessions.py --session <ids...> \
        --force-preset blue_ball

(use blue_ball or tennis depending on the session — operator must
remember which colour was live for each session; `--use-frozen-snapshot`
is intentionally unavailable here because the frozen snapshot referred
to a v11_hsv_cc bucket we just deleted.)

Then the quarantine folder can be removed entirely.

Usage:
    cd server
    uv run python migrations/strip_prepr93_quarantine.py            # dry run
    uv run python migrations/strip_prepr93_quarantine.py --apply    # write
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

# Make `import schemas` work whether run from server/ or from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import IOS_CAPTURE_TIME_ALGORITHM_ID, PitchPayload, persist_pitch_json

logger = logging.getLogger("migrate.prepr93")

QUARANTINE_DIR = Path(__file__).resolve().parent.parent / "data" / "_quarantine_2026-05-13"
PITCH_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "pitches"


def strip(raw: dict) -> dict:
    """Return a new dict with PR #93-incompatible substructure removed.

    Mutations are documented in the module docstring."""
    out = dict(raw)

    by_algo = dict(out.get("frames_by_algorithm") or {})
    # Drop every non-ios bucket: those are server detections whose
    # cost basis depends on aspect/fill we no longer have.
    kept_buckets = {k: v for k, v in by_algo.items() if k == IOS_CAPTURE_TIME_ALGORITHM_ID}
    # In the surviving live bucket, blank out every candidate list.
    blanked: list[dict] = []
    for frame in kept_buckets.get(IOS_CAPTURE_TIME_ALGORITHM_ID, []):
        f = dict(frame)
        f["candidates"] = None
        blanked.append(f)
    if IOS_CAPTURE_TIME_ALGORITHM_ID in kept_buckets:
        kept_buckets[IOS_CAPTURE_TIME_ALGORITHM_ID] = blanked
    out["frames_by_algorithm"] = kept_buckets

    cfg = dict(out.get("config_used_by_algorithm") or {})
    out["config_used_by_algorithm"] = {
        k: v for k, v in cfg.items() if k == IOS_CAPTURE_TIME_ALGORITHM_ID
    }

    out["active_server_post_algorithm_id"] = None
    out["server_post_ran_at"] = None
    return out


def migrate_pitch(src: Path, *, apply: bool) -> tuple[bool, str]:
    raw = json.loads(src.read_text())
    stripped = strip(raw)
    try:
        pitch = PitchPayload.model_validate(stripped)
    except Exception as e:  # pydantic ValidationError or anything else
        return False, f"{src.name}: validate fail — {e}"

    dest = PITCH_OUT_DIR / src.name
    if dest.exists():
        return False, f"{src.name}: destination already exists at {dest}; refuse to clobber"

    if apply:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(persist_pitch_json(pitch))
        tmp.replace(dest)
    return True, f"{src.name}: ok ({len(pitch.frames_live)} live frames preserved)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    ap.add_argument(
        "--purge-quarantine",
        action="store_true",
        help="after successful --apply, rm -rf the quarantine folder",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    pitch_dir = QUARANTINE_DIR / "pitches"
    if not pitch_dir.is_dir():
        logger.error("no quarantine pitch dir at %s", pitch_dir)
        return 2

    PITCH_OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(pitch_dir.glob("session_*.json"))
    if not files:
        logger.info("no pitches under %s — nothing to do", pitch_dir)
        return 0

    ok = 0
    fail = 0
    for f in files:
        success, msg = migrate_pitch(f, apply=args.apply)
        if success:
            ok += 1
            logger.info("  ✓ %s", msg)
        else:
            fail += 1
            logger.warning("  ✗ %s", msg)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    logger.info("---")
    logger.info("%s — ok=%d fail=%d total=%d", mode, ok, fail, len(files))

    if fail:
        logger.warning("non-zero fails; quarantine not purged")
        return 1

    if args.apply and args.purge_quarantine:
        logger.info("purging %s", QUARANTINE_DIR)
        shutil.rmtree(QUARANTINE_DIR)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
