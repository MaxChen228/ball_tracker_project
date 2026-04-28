"""Tests for state_gt_index.GTIndex.

Covers: cache key (mtime+size), invalidation, fallback for heatmap
source, skip list, corrupt skip list, missing files, missing video.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from state_gt_index import GTIndex


def _write_pitch(
    data_dir: Path,
    sid: str,
    cam: str,
    *,
    video_start: float = 100.0,
    frames_live: list[dict] | None = None,
    frames_server_post: list[dict] | None = None,
    sync_anchor: float | None = None,
) -> Path:
    pitch_dir = data_dir / "pitches"
    pitch_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "camera_id": cam,
        "video_start_pts_s": video_start,
        "video_fps": 240.0,
        "sync_anchor_timestamp_s": sync_anchor,
        "frames_live": frames_live or [],
        "frames_server_post": frames_server_post or [],
    }
    p = pitch_dir / f"session_{sid}_{cam}.json"
    p.write_text(json.dumps(payload))
    return p


def _write_mov(data_dir: Path, sid: str, cam: str) -> Path:
    video_dir = data_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    p = video_dir / f"session_{sid}_{cam}.mov"
    p.write_bytes(b"\x00" * 16)
    return p


def _write_gt(data_dir: Path, sid: str, cam: str) -> Path:
    sam_dir = data_dir / "gt" / "sam3"
    sam_dir.mkdir(parents=True, exist_ok=True)
    p = sam_dir / f"session_{sid}_{cam}.json"
    p.write_text("{}")
    return p


# ----- discovery + basic build ----------------------------------------


def test_discover_finds_sessions_with_pitch_json(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    _write_pitch(tmp_path, "s_aaa", "B")
    _write_pitch(tmp_path, "s_bbb", "A")
    idx = GTIndex(data_dir=tmp_path)
    states = idx.get_all()
    sids = sorted(s.session_id for s in states)
    assert sids == ["s_aaa", "s_bbb"]


def test_per_cam_presence_and_mov(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    _write_pitch(tmp_path, "s_aaa", "B")
    _write_mov(tmp_path, "s_aaa", "A")  # only A has MOV
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    assert s.cams_present == {"A": True, "B": True}
    assert s.has_mov == {"A": True, "B": False}
    assert s.has_gt == {"A": False, "B": False}


def test_n_live_dets_filters_px_none(tmp_path: Path):
    frames = [
        {"frame_index": 0, "timestamp_s": 100.10, "px": None, "py": None},
        {"frame_index": 1, "timestamp_s": 100.20, "px": 100.0, "py": 50.0},
        {"frame_index": 2, "timestamp_s": 100.30, "px": 110.0, "py": 60.0},
        {"frame_index": 3, "timestamp_s": 100.40, "px": None},
    ]
    _write_pitch(tmp_path, "s_aaa", "A", video_start=100.0, frames_live=frames)
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    assert s.n_live_dets["A"] == 2
    assert s.t_first_video_rel["A"] == pytest.approx(0.20, abs=1e-6)
    assert s.t_last_video_rel["A"] == pytest.approx(0.30, abs=1e-6)


def test_uses_video_relative_clock_not_anchor_relative(tmp_path: Path):
    """Regression: anchor can be 357 s before video_start (real session
    s_4b23a195). Must derive `t_video = timestamp_s - video_start_pts_s`,
    NOT `timestamp_s - sync_anchor_timestamp_s`."""
    frames = [
        {"frame_index": 0, "timestamp_s": 35033.10, "px": 100.0, "py": 50.0},
        {"frame_index": 1, "timestamp_s": 35033.50, "px": 110.0, "py": 50.0},
    ]
    _write_pitch(
        tmp_path, "s_aaa", "A",
        video_start=35032.67,
        sync_anchor=34675.23,  # 357 s before video_start
        frames_live=frames,
    )
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    # video-relative: ~0.43 to ~0.83
    assert s.t_first_video_rel["A"] == pytest.approx(0.43, abs=0.01)
    assert s.t_last_video_rel["A"] == pytest.approx(0.83, abs=0.01)


def test_fallback_to_server_post_when_live_empty(tmp_path: Path):
    frames_srv = [
        {"frame_index": 0, "timestamp_s": 100.10, "px": 100.0, "py": 50.0},
        {"frame_index": 1, "timestamp_s": 100.50, "px": 110.0, "py": 50.0},
    ]
    _write_pitch(
        tmp_path, "s_aaa", "A",
        video_start=100.0,
        frames_live=[],
        frames_server_post=frames_srv,
    )
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    assert s.n_live_dets["A"] == 2
    assert s.t_first_video_rel["A"] == pytest.approx(0.10)


def test_no_detections_returns_none_times(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A", video_start=100.0)
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    assert s.n_live_dets["A"] == 0
    assert s.t_first_video_rel["A"] is None
    assert s.t_last_video_rel["A"] is None


# ----- cache key (mtime + size) ---------------------------------------


def test_cache_hit_when_files_unchanged(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    s1 = idx.get("s_aaa")
    s2 = idx.get("s_aaa")
    # Same instance returned (shallow identity not guaranteed for dataclass
    # but we can test via mutation in cache rebuild later).
    assert s1 == s2


def test_cache_invalidates_when_pitch_rewritten(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    s1 = idx.get("s_aaa")
    assert s1.has_gt["A"] is False
    # Add GT JSON; cache key for sam3 file changes from None → tuple
    time.sleep(0.01)  # nudge mtime forward in case of truly identical timestamp
    _write_gt(tmp_path, "s_aaa", "A")
    s2 = idx.get("s_aaa")
    assert s2.has_gt["A"] is True


def test_explicit_invalidate_forces_rebuild(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    idx.get("s_aaa")
    # Sneak in a GT file via a separate path then invalidate
    _write_gt(tmp_path, "s_aaa", "A")
    idx.invalidate("s_aaa")
    s = idx.get("s_aaa")
    assert s.has_gt["A"] is True


# ----- skip list ------------------------------------------------------


def test_add_skip_persists_to_disk(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    idx.add_skip("s_aaa")
    assert (tmp_path / "gt" / "skip_list.json").is_file()
    assert idx.get("s_aaa").is_skipped is True


def test_remove_skip(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    idx.add_skip("s_aaa")
    idx.remove_skip("s_aaa")
    assert idx.get("s_aaa").is_skipped is False


def test_corrupt_skip_list_treated_as_empty(tmp_path: Path):
    skip_path = tmp_path / "gt" / "skip_list.json"
    skip_path.parent.mkdir(parents=True, exist_ok=True)
    skip_path.write_text("{not valid")
    _write_pitch(tmp_path, "s_aaa", "A")
    # Should NOT crash on construction
    idx = GTIndex(data_dir=tmp_path)
    assert idx.get("s_aaa").is_skipped is False


def test_skip_idempotent(tmp_path: Path):
    _write_pitch(tmp_path, "s_aaa", "A")
    idx = GTIndex(data_dir=tmp_path)
    idx.add_skip("s_aaa")
    idx.add_skip("s_aaa")  # no-op
    raw = json.loads((tmp_path / "gt" / "skip_list.json").read_text())
    assert raw == {"sids": ["s_aaa"]}


# ----- recency sort ---------------------------------------------------


def test_get_all_sorts_by_recency_desc(tmp_path: Path):
    _write_pitch(tmp_path, "s_old", "A")
    p_old = tmp_path / "pitches" / "session_s_old_A.json"
    # Force older mtime via os.utime
    os.utime(p_old, (1000, 1000))
    _write_pitch(tmp_path, "s_new", "A")
    p_new = tmp_path / "pitches" / "session_s_new_A.json"
    os.utime(p_new, (5000, 5000))
    idx = GTIndex(data_dir=tmp_path)
    sids = [s.session_id for s in idx.get_all()]
    assert sids[0] == "s_new"
    assert sids[1] == "s_old"


# ----- video duration -------------------------------------------------


def test_video_duration_from_last_server_post_frame(tmp_path: Path):
    frames_srv = [
        {"frame_index": 0, "timestamp_s": 100.0, "px": 1.0, "py": 1.0},
        {"frame_index": 100, "timestamp_s": 102.5, "px": 1.0, "py": 1.0},
    ]
    _write_pitch(
        tmp_path, "s_aaa", "A",
        video_start=100.0,
        frames_server_post=frames_srv,
    )
    idx = GTIndex(data_dir=tmp_path)
    s = idx.get("s_aaa")
    assert s.video_duration_s["A"] == pytest.approx(2.5)
