"""Dry-run: re-score frames_live winners under the *current* selector
tuning, compare against the px/py stamped at ingest time. No writes.

Walks every `data/pitches/session_*.json`, loads `frames_live[]`, and for
each frame with a non-empty `candidates[]`:

  - Identifies the OLD winner by matching the frame's stamped `px/py`
    against the candidates (smallest L2 distance, must be < 0.5 px to
    count as confident match).
  - Runs `score_candidates(cands, tuning)` with the live tuning loaded
    from `data/candidate_selector_tuning.json` (default if missing).
  - Reports whether argmin matches the old winner.

Compares old stamped px/py (chosen at ingest time under the live
tuning that was active then) against argmin under the *current*
tuning. After the size_pen→pure-shape refactor, legacy live frames
where iOS only sent area get aspect=None / fill=None on every
candidate → both penalties zero → all costs tie → first-candidate
wins. That's the expected outcome on legacy data; new live frames
(post iOS aspect/fill wire) carry the stats and re-score honestly.

Usage:
    uv run python server/dry_run_live_rerun.py             # all sessions
    uv run python server/dry_run_live_rerun.py s_f50fd07f  # one session
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from candidate_selector import (
    Candidate,
    CandidateSelectorTuning,
    score_candidates,
)

DATA = Path(__file__).parent / "data"
PITCHES = DATA / "pitches"
TUNING_PATH = DATA / "candidate_selector_tuning.json"

MATCH_TOL_PX = 0.5  # px stamped on FramePayload should match a candidate exactly


def load_tuning() -> CandidateSelectorTuning:
    if not TUNING_PATH.exists():
        return CandidateSelectorTuning.default()
    obj = json.loads(TUNING_PATH.read_text())
    d = CandidateSelectorTuning.default()
    return CandidateSelectorTuning(
        w_aspect=float(obj.get("w_aspect", d.w_aspect)),
        w_fill=float(obj.get("w_fill", d.w_fill)),
    )


def to_cand(b: dict) -> Candidate:
    return Candidate(
        cx=float(b["px"]), cy=float(b["py"]),
        area=int(b["area"]),
        aspect=b.get("aspect"),
        fill=b.get("fill"),
    )


def find_old_winner_idx(cands: list[Candidate], px: float, py: float) -> int | None:
    """Match stamped (px, py) → candidate index. Returns None if no
    candidate within MATCH_TOL_PX (means the winner came from a
    pre-resolution pipeline we don't recognize)."""
    best_i = None
    best_d2 = math.inf
    for i, c in enumerate(cands):
        d2 = (c.cx - px) ** 2 + (c.cy - py) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_i = i
    if best_i is None or best_d2 > MATCH_TOL_PX * MATCH_TOL_PX:
        return None
    return best_i


def process_pitch(path: Path, tuning: CandidateSelectorTuning) -> dict:
    obj = json.loads(path.read_text())
    frames = obj.get("frames_live") or []

    n_frames = len(frames)
    n_with_cands = 0
    n_single = 0      # only one candidate → nothing to re-decide
    n_unmatched = 0   # stamped px/py didn't match any candidate (legacy)
    n_same = 0
    n_changed = 0
    examples: list[dict] = []
    px_shifts: list[float] = []

    for f in frames:
        if not f.get("ball_detected"):
            continue
        cands_raw = f.get("candidates") or []
        if not cands_raw:
            continue
        n_with_cands += 1
        if len(cands_raw) == 1:
            n_single += 1
            continue
        cands = [to_cand(b) for b in cands_raw]
        px = f.get("px")
        py = f.get("py")
        if px is None or py is None:
            continue
        old_idx = find_old_winner_idx(cands, float(px), float(py))
        if old_idx is None:
            n_unmatched += 1
            continue
        costs = score_candidates(cands, tuning)
        new_idx = min(range(len(costs)), key=lambda i: costs[i])
        if new_idx == old_idx:
            n_same += 1
            continue
        n_changed += 1
        old = cands[old_idx]
        new = cands[new_idx]
        shift = math.hypot(new.cx - old.cx, new.cy - old.cy)
        px_shifts.append(shift)
        if len(examples) < 3:
            examples.append({
                "frame": f.get("frame_index"),
                "old": {"px": old.cx, "py": old.cy, "area": old.area, "cost": costs[old_idx]},
                "new": {"px": new.cx, "py": new.cy, "area": new.area, "cost": costs[new_idx]},
                "shift_px": shift,
                "n_cands": len(cands),
            })

    return {
        "n_frames": n_frames,
        "n_with_cands": n_with_cands,
        "n_single": n_single,
        "n_unmatched": n_unmatched,
        "n_same": n_same,
        "n_changed": n_changed,
        "px_shift_med": (sorted(px_shifts)[len(px_shifts) // 2] if px_shifts else 0.0),
        "px_shift_max": max(px_shifts, default=0.0),
        "examples": examples,
    }


def main():
    args = sys.argv[1:]
    tuning = load_tuning()
    print(f"tuning: w_aspect={tuning.w_aspect} w_fill={tuning.w_fill}")
    print()

    pattern = "session_*.json"
    if args:
        files = []
        for sid in args:
            files.extend(sorted(PITCHES.glob(f"session_{sid}_*.json")))
    else:
        files = sorted(PITCHES.glob(pattern))

    print(f"{'pitch':40s} {'frames':>6s} {'multi':>6s} {'unmtc':>6s} "
          f"{'same':>6s} {'CHG':>5s} {'shift_med':>10s} {'shift_max':>10s}")
    print("-" * 100)

    tot = {"n": 0, "multi": 0, "unmatched": 0, "same": 0, "changed": 0}
    changed_pitches: list[tuple[str, dict]] = []

    for path in files:
        try:
            r = process_pitch(path, tuning)
        except Exception as e:
            print(f"{path.stem:40s} ERROR {e!r}")
            continue
        multi = r["n_with_cands"] - r["n_single"]
        if multi == 0 and r["n_with_cands"] == 0:
            continue
        tot["n"] += 1
        tot["multi"] += multi
        tot["unmatched"] += r["n_unmatched"]
        tot["same"] += r["n_same"]
        tot["changed"] += r["n_changed"]
        flag = "***" if r["n_changed"] > 0 else "   "
        print(f"{path.stem:40s} {r['n_frames']:6d} {multi:6d} "
              f"{r['n_unmatched']:6d} {r['n_same']:6d} {r['n_changed']:5d} "
              f"{r['px_shift_med']:10.2f} {r['px_shift_max']:10.2f}  {flag}")
        if r["n_changed"] > 0:
            changed_pitches.append((path.stem, r))

    print("-" * 100)
    print(f"TOTAL pitches scanned: {tot['n']}, multi-cand frames: {tot['multi']}, "
          f"unmatched: {tot['unmatched']}, same: {tot['same']}, "
          f"CHANGED: {tot['changed']}")

    if changed_pitches:
        print(f"\n{len(changed_pitches)} pitch file(s) would shift winners. Examples:\n")
        for name, r in changed_pitches[:10]:
            print(f"  {name}  ({r['n_changed']} frames)")
            for ex in r["examples"]:
                o, n = ex["old"], ex["new"]
                print(f"    frame={ex['frame']:>5}  n_cands={ex['n_cands']}  "
                      f"shift={ex['shift_px']:.1f}px")
                print(f"      OLD: px=({o['px']:.1f},{o['py']:.1f}) area={o['area']} cost={o['cost']:.3f}")
                print(f"      NEW: px=({n['px']:.1f},{n['py']:.1f}) area={n['area']} cost={n['cost']:.3f}")


if __name__ == "__main__":
    main()
