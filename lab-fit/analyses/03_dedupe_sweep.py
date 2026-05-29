"""A/B compare legacy vs chord-based dedupe across every session.

Runs frozen segmenter twice per session×path with `dedupe_rank_by` set
to "legacy" (rank by -n then +rmse, requires cos≥0.95) and "chord"
(rank by 3D head-to-tail chord, no cos gate). Diffs the resulting
segment sets and reports:

  - segments dropped by chord but kept by legacy ("newly killed")
  - segments dropped by legacy but kept by chord ("newly resurrected")
  - segments where the winner changed identity

For each newly-killed segment, also reports the chord-winner that
displaced it so a human can eyeball "yes that ghost deserved to die"
or "no, you killed a real one".

Output:
  stdout — per-session summary; full diff for affected sessions
  reports/03_dedupe_sweep/diff.csv — one row per dropped segment

Usage:
  uv run --with numpy --with matplotlib python analyses/03_dedupe_sweep.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_loader import (  # noqa: E402
    LIVE_ALGORITHM_ID,
    algorithm_id_for_path,
    iter_results,
)
from runner import run_for_result  # noqa: E402


REPORT_DIR = Path(__file__).resolve().parent.parent / "reports" / "03_dedupe_sweep"


def _seg_key(s) -> tuple:
    """Identity for a segment under either dedupe — by index set."""
    return tuple(sorted(s.indices))


def _seg_chord(s, pts: np.ndarray) -> float:
    a = pts[s.indices[0], 1:4]
    b = pts[s.indices[-1], 1:4]
    return float(np.linalg.norm(b - a))


def _seg_summary(s, pts: np.ndarray) -> dict:
    return {
        "n": len(s.indices),
        "dur_ms": round(1000 * (s.t_end - s.t_start), 1),
        "chord_m": round(_seg_chord(s, pts), 3),
        "rmse_m": round(s.rmse_m, 4),
        "v0_mps": round(float(np.linalg.norm(s.v0)), 2),
        "t_start": round(s.t_start, 3),
        "t_end": round(s.t_end, 3),
    }


def _overlap_frac(a, b) -> float:
    ovlp = max(0.0, min(a.t_end, b.t_end) - max(a.t_start, b.t_start))
    short = min(a.t_end - a.t_start, b.t_end - b.t_start)
    return ovlp / short if short > 0 else 0.0


def sweep() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORT_DIR / "diff.csv"

    rows: list[dict] = []
    n_affected = 0
    n_sessions = 0

    for sid, result in iter_results():
        n_sessions += 1
        for path in ("live", "server_post"):
            alg = algorithm_id_for_path(result, path)
            if alg is None:
                continue
            try:
                legacy_segs, pts = run_for_result(
                    result, path=path, dedupe_rank_by="legacy"
                )
                chord_segs, _ = run_for_result(
                    result, path=path, dedupe_rank_by="chord"
                )
            except Exception as exc:
                print(f"[{sid} / {path}] ERROR {exc}")
                continue

            legacy_keys = {_seg_key(s) for s in legacy_segs}
            chord_keys = {_seg_key(s) for s in chord_segs}

            killed_by_chord = [s for s in legacy_segs if _seg_key(s) not in chord_keys]
            resurrected_by_chord = [
                s for s in chord_segs if _seg_key(s) not in legacy_keys
            ]

            if not killed_by_chord and not resurrected_by_chord:
                continue
            n_affected += 1

            print(f"\n── {sid} / {path} ({alg}) ──")
            print(f"  legacy: {len(legacy_segs)} segs   chord: {len(chord_segs)} segs")

            for lost in killed_by_chord:
                lost_info = _seg_summary(lost, pts)
                # Find the chord-kept opponent with the biggest time overlap
                # that has a longer chord — that's why this one died.
                opponents = [
                    (s, _overlap_frac(lost, s), _seg_chord(s, pts))
                    for s in chord_segs
                    if _overlap_frac(lost, s) >= 0.30
                ]
                opponents.sort(key=lambda x: -x[1])
                opp = opponents[0] if opponents else None
                opp_info = _seg_summary(opp[0], pts) if opp else None
                opp_overlap = round(opp[1], 2) if opp else None

                print(
                    f"  KILLED  {lost_info}"
                    + (
                        f"\n   ↳ by  {opp_info}  overlap={opp_overlap}"
                        if opp
                        else "\n   ↳ (no chord-overlap opponent found — odd)"
                    )
                )
                rows.append(
                    {
                        "sid": sid,
                        "path": path,
                        "algorithm": alg,
                        "event": "killed_by_chord",
                        **{f"lost_{k}": v for k, v in lost_info.items()},
                        **(
                            {f"kept_{k}": v for k, v in opp_info.items()}
                            if opp_info
                            else {}
                        ),
                        "overlap_frac": opp_overlap,
                    }
                )

            for gained in resurrected_by_chord:
                info = _seg_summary(gained, pts)
                print(f"  RESURRECTED  {info}")
                rows.append(
                    {
                        "sid": sid,
                        "path": path,
                        "algorithm": alg,
                        "event": "resurrected_by_chord",
                        **{f"lost_{k}": v for k, v in info.items()},
                        "overlap_frac": None,
                    }
                )

    print(f"\n══ summary ══")
    print(f"  sessions scanned:  {n_sessions}")
    print(f"  sessions affected: {n_affected}")
    print(f"  diff rows:         {len(rows)}")

    if rows:
        all_keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in all_keys:
                    all_keys.append(k)
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=all_keys)
            w.writeheader()
            w.writerows(rows)
        print(f"  wrote {csv_path}")


if __name__ == "__main__":
    sweep()
