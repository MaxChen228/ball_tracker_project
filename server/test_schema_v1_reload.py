"""Phase 4-1 regression: a pre-v2 SessionResult on disk must NOT crash
boot and must NOT silently load with missing pair_key — it must trigger
the self-healing rebuild path in `_load_from_disk`.

Phase 3a made TriangulatedPoint.pair_key required. Any persisted result
written before 3a has triangulated points without that field, so Pydantic
will raise on `SessionResult.model_validate(json.loads(...))`. The
`_load_cached_result_for_session` catch returns
`(None, "invalid:<truncated err>")`, which the boot loop converts into a
`rebuild_result_for_session` call. This test locks that behavior down so
a future change can't quietly turn it into a 500 or, worse, a load that
succeeds with malformed point data."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import main
from conftest import sid


def _write_v1_result(data_dir: Path, session_id: str) -> Path:
    """Construct a hand-rolled v1-shaped SessionResult JSON: no
    schema_version, no pair_key on triangulated points. Mirrors the
    shape that landed on disk before Phase 3a."""
    rdir = data_dir / "results"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"session_{session_id}.json"
    path.write_text(json.dumps({
        # NB: schema_version intentionally absent — Phase 3a's Literal[2]
        # has a default of 2 so this still parses past that field, but the
        # point-level pair_key requirement catches the missing label.
        "session_id": session_id,
        "cameras_received": {"A": True, "B": True},
        "solved_at": 0.0,
        "triangulated": [
            {
                "t_rel_s": 0.0,
                "x_m": 0.1, "y_m": 0.2, "z_m": 0.3,
                "residual_m": 1e-6,
                "source_a_cand_idx": None, "source_b_cand_idx": None,
                "cost_a": None, "cost_b": None,
                # pair_key MISSING — this is the v1 shape
            }
        ],
        "aborted": False,
        "abort_reasons": {},
        "points": [],
        "error": None,
        "segments": [],
        "triangulated_by_algorithm": {},
        "segments_by_algorithm": {},
        "frame_counts_by_algorithm": {},
        "algorithms_completed": [],
        "config_used_by_algorithm": {},
        "active_server_post_algorithm_id": None,
    }))
    return path


def _matching_pitch(data_dir: Path, session_id: str, cam: str) -> Path:
    """Write a minimal valid PitchPayload pair so the result-rebuild path
    has something to iterate. Use the existing schema (which is what
    State._load_from_disk reads back)."""
    pdir = data_dir / "pitches"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"session_{session_id}_{cam}.json"
    path.write_text(json.dumps({
        "camera_id": cam,
        "session_id": session_id,
        "sync_id": "sy_deadbeef",
        "sync_anchor_timestamp_s": 0.0,
        "video_start_pts_s": 0.0,
        "video_fps": 240.0,
        # No frames → rebuild yields an empty result (which IS v2 shape).
        "frames_by_algorithm": {},
        "active_server_post_algorithm_id": None,
    }))
    return path


def test_v1_result_with_stale_pitch_triggers_validation_failure(tmp_path):
    """Result mtime newer than pitch mtime → loader walks the try/except
    load path. Pydantic rejects the v1 point shape (no pair_key) → loader
    returns (None, 'invalid:...') and the boot loop calls
    `rebuild_result_for_session`, which produces a fresh v2 result.
    Critical: State() construction MUST NOT raise."""
    session_id = sid(42)
    result_path = _write_v1_result(tmp_path, session_id)
    pitch_path = _matching_pitch(tmp_path, session_id, "A")
    _matching_pitch(tmp_path, session_id, "B")
    # Force result newer than pitch so the cache path is taken (not "stale").
    now_ns = result_path.stat().st_mtime_ns
    os.utime(pitch_path, ns=(now_ns - 1_000_000, now_ns - 1_000_000))

    s = main.State(data_dir=tmp_path)  # MUST NOT raise

    # Rebuild produced a fresh v2 result (empty triangulated because no frames),
    # which is what's now in the cache.
    rebuilt = s.results.get(session_id)
    assert rebuilt is not None
    assert rebuilt.schema_version == 2
    assert rebuilt.triangulated == []


def test_v1_result_load_path_explicitly_fails_validation(tmp_path):
    """Direct unit on `_load_cached_result_for_session`: a v1 JSON with
    a pair_key-less triangulated point fails Pydantic, the loader
    returns (None, 'invalid:...'). Locks the self-healing contract."""
    session_id = sid(43)
    result_path = _write_v1_result(tmp_path, session_id)
    pitch_path = _matching_pitch(tmp_path, session_id, "A")
    now_ns = result_path.stat().st_mtime_ns
    os.utime(pitch_path, ns=(now_ns - 1_000_000, now_ns - 1_000_000))

    s = main.State(data_dir=tmp_path)
    cached, reason = s._load_cached_result_for_session(session_id, [pitch_path])
    # `s` already rebuilt at construction time → the on-disk file IS now
    # v2-shaped. Re-write the v1 file so we can probe the load path itself.
    _write_v1_result(tmp_path, session_id)
    os.utime(pitch_path, ns=(now_ns - 1_000_000, now_ns - 1_000_000))
    cached, reason = s._load_cached_result_for_session(session_id, [pitch_path])
    assert cached is None
    assert reason.startswith("invalid:"), reason
