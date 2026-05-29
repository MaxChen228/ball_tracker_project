"""Read sessions from server/data into in-memory structures.

Single source of truth for "where is the data" — everything else under
lab-fit/ goes through here.

Usage:
    from data_loader import DATA_DIR, load_result, list_sessions

    for sid in list_sessions():
        result = load_result(sid)
        ...

A `Result` is the dict from `data/results/session_<sid>.json` with these
top-level keys (current schema, post `triangulated_by_algorithm`
migration):

    triangulated                    : authority-path flat list (legacy
                                      mirror)
    points                          : alias of triangulated
    triangulated_by_algorithm       : {alg_id: [TriPoint dicts]}, keys
                                      include LIVE_ALGORITHM_ID
                                      ("ios_capture_time") and the active
                                      server-post algo id
                                      (e.g. "v11_hsv_cc")
    segments                        : authority-path segments (legacy)
    segments_by_algorithm           : {alg_id: [segment dicts]}
    frame_counts_by_algorithm       : {alg_id: {"A": int, "B": int}}
    active_server_post_algorithm_id : the SVR algo id currently active
    algorithms_completed            : list of alg ids that have data
    gap_threshold_m                 : residual filter cutoff at solve time
    cost_threshold                  : candidate cost cutoff (may be None)

A `PitchPayload` is `data/pitches/session_<sid>_<cam>.json`. Has live
and server_post candidate frames per camera (frames_live,
frames_server_post).
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

# Algorithm id that backs the LIVE path. Mirrors server-side naming.
LIVE_ALGORITHM_ID = "ios_capture_time"

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


def algorithm_id_for_path(result: dict, path: str) -> str | None:
    """Resolve a logical path ("live"/"server_post") to its algorithm id
    in `triangulated_by_algorithm` / `segments_by_algorithm`.

    Returns None when the result has no points for that path."""
    if path == "live":
        if LIVE_ALGORITHM_ID in result.get("triangulated_by_algorithm", {}):
            return LIVE_ALGORITHM_ID
        return None
    if path == "server_post":
        alg = result.get("active_server_post_algorithm_id")
        if alg and alg in result.get("triangulated_by_algorithm", {}):
            return alg
        return None
    raise ValueError(f"unknown path: {path!r}")
