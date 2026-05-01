"""Read sessions from server/data into in-memory structures.

Single source of truth for "where is the data" — everything else under
lab-fit/ goes through here.

Usage:
    from data_loader import DATA_DIR, load_result, load_pitches, list_sessions

    for sid in list_sessions():
        result = load_result(sid)
        ...

A `Result` is the dict from `data/results/session_<sid>.json` with these
top-level keys (Python-side names match disk schema):

    triangulated         : list of dicts with t_rel_s, x_m, y_m, z_m,
                           residual_m, source_a_cand_idx, source_b_cand_idx,
                           cost_a, cost_b
    triangulated_by_path : {"live": [...], "server_post": [...]}
    segments             : flattened segments list (already dedupe+merge)
    segments_by_path     : {"live": [...], "server_post": [...]}
    frame_counts_by_path : {"live": {"A": int, "B": int}, ...}
    gap_threshold_m      : residual filter cutoff used at solve time
    cost_threshold       : candidate cost cutoff
    hsv_range_used       : frozen HSV used by server_post
    shape_gate_used      : frozen shape gate used by server_post

A `PitchPayload` is `data/pitches/session_<sid>_<cam>.json`. Has live
and server_post candidate frames per camera (frames_live, frames_server_post).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).parent.parent / "server" / "data"
RESULTS_DIR = DATA_DIR / "results"
PITCHES_DIR = DATA_DIR / "pitches"
VIDEOS_DIR = DATA_DIR / "videos"

_RESULT_RE = re.compile(r"^session_(s_[a-f0-9]+)\.json$")
_PITCH_RE = re.compile(r"^session_(s_[a-f0-9]+)_([AB])\.json$")


def list_sessions() -> list[str]:
    """All session ids that have a results JSON. Sorted by mtime descending."""
    pairs = []
    for p in RESULTS_DIR.iterdir():
        m = _RESULT_RE.match(p.name)
        if m:
            pairs.append((p.stat().st_mtime, m.group(1)))
    return [sid for _, sid in sorted(pairs, reverse=True)]


def load_result(sid: str) -> dict:
    return json.loads((RESULTS_DIR / f"session_{sid}.json").read_text())


def load_pitch(sid: str, cam: str) -> dict:
    """cam ∈ {"A", "B"}."""
    return json.loads((PITCHES_DIR / f"session_{sid}_{cam}.json").read_text())


def session_video_path(sid: str, cam: str) -> Path:
    return VIDEOS_DIR / f"session_{sid}_{cam}.mov"


def session_has_pitch(sid: str, cam: str) -> bool:
    return (PITCHES_DIR / f"session_{sid}_{cam}.json").exists()


def iter_results() -> Iterator[tuple[str, dict]]:
    for sid in list_sessions():
        yield sid, load_result(sid)
