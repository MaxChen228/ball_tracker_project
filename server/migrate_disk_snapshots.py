"""One-shot migration: collapse v11-shaped DetectionConfigSnapshotPayload
flat keys into the new `params` dict.

Pre-flip disk shape (per snapshot bucket inside
`config_used_by_algorithm`):

    {"algorithm_id": "v11_hsv_cc",
     "hsv": {...},
     "shape_gate": {...},
     "preset_name": "..."}

Post-flip:

    {"algorithm_id": "v11_hsv_cc",
     "params": {"hsv": {...}, "shape_gate": {...}},
     "preset_name": "..."}

Same idempotent shape as `migrate_disk_pitches.py` /
`migrate_disk_results.py`: load → transform → atomic write. Run once
after pulling Phase 1. Re-running after migration is no-op.

Touches both `data/pitches/*.json` (PitchPayload's
`config_used_by_algorithm`) and `data/results/*.json` (SessionResult's
`config_used_by_algorithm`)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
RESULT_DIR = DATA_DIR / "results"


def _migrate_snapshot_dict(snap: dict) -> bool:
    """Mutate `snap` in place; return True if changed."""
    if "params" in snap:
        return False  # already migrated
    if "hsv" not in snap and "shape_gate" not in snap:
        # legacy entry without recognisable v11 keys — leave alone,
        # let schema validation catch it on load. Don't manufacture
        # an empty params dict (silent fallback).
        return False
    params: dict = {}
    if "hsv" in snap:
        params["hsv"] = snap.pop("hsv")
    if "shape_gate" in snap:
        params["shape_gate"] = snap.pop("shape_gate")
    snap["params"] = params
    return True


def _migrate_record(obj: dict) -> bool:
    """Walk `config_used_by_algorithm` buckets in a pitch / result
    record and migrate each snapshot dict. Returns True if anything
    changed."""
    bucket = obj.get("config_used_by_algorithm")
    if not isinstance(bucket, dict):
        return False
    changed = False
    for _alg_id, snap in bucket.items():
        if isinstance(snap, dict) and _migrate_snapshot_dict(snap):
            changed = True
    return changed


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _migrate_dir(directory: Path, dry_run: bool) -> tuple[int, int]:
    """Returns (touched, total)."""
    if not directory.exists():
        return (0, 0)
    files = sorted(directory.glob("*.json"))
    touched = 0
    for f in files:
        try:
            obj = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            raise SystemExit(f"{f}: malformed JSON — {e}") from None
        if _migrate_record(obj):
            touched += 1
            if not dry_run:
                _atomic_write(f, json.dumps(obj, indent=2, ensure_ascii=False))
    return (touched, len(files))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report counts without writing.")
    args = ap.parse_args()

    p_touched, p_total = _migrate_dir(PITCH_DIR, args.dry_run)
    r_touched, r_total = _migrate_dir(RESULT_DIR, args.dry_run)
    verb = "would migrate" if args.dry_run else "migrated"
    print(f"pitches:  {verb} {p_touched}/{p_total}")
    print(f"results:  {verb} {r_touched}/{r_total}")


if __name__ == "__main__":
    main()
