"""Regression guard: state_events.build_events pitch-mtime cache.

`state_events._latest_pitch_mtime` used to disk-stat() every pitch JSON
on every dashboard /events tick (5 s). At ~100 sessions × 2 cams that
was ~40k stat()/min. PR introduces a write-through cache populated by
`State.record()` and invalidated by `delete_session()` / `reset()`.

These tests pin:
  1. After the first build_events read, repeated rebuilds do NOT stat
     the disk again — value comes from the cache.
  2. delete_session() invalidates so a subsequent build_events for a
     fresh session of the same id stats once (cold) again.
  3. reset() invalidates the entire cache.
"""
from __future__ import annotations

from unittest.mock import patch

from state import State
from state_events import build_events, _latest_pitch_mtime
from schemas import PitchPayload


def _sid(n: int) -> str:
    return f"s_{n:08x}"


def _pitch(cam: str, session_id: str) -> PitchPayload:
    return PitchPayload(
        camera_id=cam,
        session_id=session_id,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=[],
    )


def _stat_count(s: State, sids_and_cams: list[tuple[str, str]]) -> int:
    """Run _latest_pitch_mtime once per (sid, cams) and count stat() calls."""
    real_pitch_path = s._pitch_path
    calls = {"n": 0}

    class _CountingPath:
        def __init__(self, p):
            self._p = p

        def stat(self):
            calls["n"] += 1
            return self._p.stat()

    def wrapped(cam, sid):
        return _CountingPath(real_pitch_path(cam, sid))

    with patch.object(s, "_pitch_path", side_effect=wrapped):
        for sid, cam in sids_and_cams:
            _latest_pitch_mtime(s, [cam], sid)
    return calls["n"]


def test_record_populates_cache_so_repeat_build_events_skips_stat(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(1)
    s.record(_pitch("A", sid))
    s.record(_pitch("B", sid))

    # Cache populated by record() — no stat() should happen now.
    n = _stat_count(s, [(sid, "A"), (sid, "B")])
    assert n == 0, f"expected 0 stat() calls (cache hits), got {n}"

    # build_events end-to-end should also not stat.
    real_pitch_path = s._pitch_path
    calls = {"n": 0}

    class _CountingPath:
        def __init__(self, p):
            self._p = p

        def stat(self):
            calls["n"] += 1
            return self._p.stat()

    def wrapped(cam, sid_):
        return _CountingPath(real_pitch_path(cam, sid_))

    with patch.object(s, "_pitch_path", side_effect=wrapped):
        for _ in range(5):
            build_events(s)
    assert calls["n"] == 0, (
        f"5x build_events should hit cache only, got {calls['n']} stat()"
    )


def test_delete_session_invalidates_cache(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(2)
    s.record(_pitch("A", sid))
    assert ("A", sid) in s._pitch_mtime_cache

    s.delete_session(sid)
    assert ("A", sid) not in s._pitch_mtime_cache
    assert ("B", sid) not in s._pitch_mtime_cache


def test_cold_load_falls_through_to_stat_then_caches(tmp_path):
    """Boot path: pitch file exists on disk (loaded by _load_from_disk)
    but record() hasn't run this process. First _latest_pitch_mtime
    must stat (cache miss); subsequent reads must hit the cache."""
    s = State(data_dir=tmp_path)
    sid = _sid(3)
    s.record(_pitch("A", sid))
    # Simulate post-boot state: pitch on disk + in pitches dict but
    # no cache entry yet.
    s._pitch_mtime_cache.clear()

    n_first = _stat_count(s, [(sid, "A")])
    assert n_first == 1, f"cold miss should stat once, got {n_first}"
    assert ("A", sid) in s._pitch_mtime_cache, "miss must backfill cache"

    n_second = _stat_count(s, [(sid, "A")])
    assert n_second == 0, f"warm read should hit cache, got {n_second}"


def test_reset_clears_cache(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(4)
    s.record(_pitch("A", sid))
    s.record(_pitch("B", sid))
    assert s._pitch_mtime_cache

    s.reset()
    assert s._pitch_mtime_cache == {}
