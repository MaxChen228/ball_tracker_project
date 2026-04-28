"""In-memory index for the /gt page session list.

The /gt page lists every session with detection metadata + GT existence
flags + skip status. A naive SSR would stat 4-7 files per session × 162
sessions = ~800 syscalls per page load + JSON parse for each pitch JSON
to count detections. That makes TTFB unacceptable (multi-hundred ms on
cold cache).

This module caches a `SessionGTState` per session, keyed by `(mtime_ns,
size)` of every file we depend on. On read, we check the cache key
matches the on-disk state cheaply via os.stat; only re-parse the
underlying JSON when the key drifts. Routes hit `invalidate(sid)` after
mutating any GT-related file so the next read sees fresh state.

Concurrency: single `threading.Lock` covers the cache dict; per-sid
rebuild also holds the lock. This serialises concurrent /gt/sessions
requests (which are cheap once cache is warm) but avoids the read-rebuild
race that would otherwise produce a torn dict.

Time clock: `t_first_video_rel` and `t_last_video_rel` are in
**video-relative seconds** (`absolute_pts − video_start_pts_s`), the
same UI clock the editor uses. We deliberately ignore
`sync_anchor_timestamp_s` because it can be 357s before video_start
on real sessions (verified on s_4b23a195) — anchor-relative would
silently filter out empty sets.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state import State

logger = logging.getLogger(__name__)

CAMS = ("A", "B")
_VIDEO_EXTS = (".mov", ".mp4", ".m4v")


@dataclass
class SessionGTState:
    """Per-session GT metadata for the /gt page.

    All time fields are video-relative seconds.
    """
    session_id: str
    cams_present: dict[str, bool]
    has_mov: dict[str, bool]
    has_gt: dict[str, bool]
    n_live_dets: dict[str, int]
    t_first_video_rel: dict[str, float | None]
    t_last_video_rel: dict[str, float | None]
    video_duration_s: dict[str, float | None]
    is_skipped: bool
    recency: float  # max mtime across pitch/video files; sort key

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cams_present": self.cams_present,
            "has_mov": self.has_mov,
            "has_gt": self.has_gt,
            "n_live_dets": self.n_live_dets,
            "t_first_video_rel": self.t_first_video_rel,
            "t_last_video_rel": self.t_last_video_rel,
            "video_duration_s": self.video_duration_s,
            "is_skipped": self.is_skipped,
            "recency": self.recency,
        }


@dataclass
class _CachedEntry:
    state: SessionGTState
    # cache key tuples per file path: (mtime_ns, size). On any mismatch
    # we rebuild this entry from disk.
    deps: dict[str, tuple[int, int] | None] = field(default_factory=dict)


class GTIndex:
    """Lazy, mtime-keyed cache of SessionGTState per session_id."""

    def __init__(self, data_dir: Path) -> None:
        self._lock = Lock()
        self._data_dir = data_dir
        self._pitch_dir = data_dir / "pitches"
        self._video_dir = data_dir / "videos"
        self._sam3_dir = data_dir / "gt" / "sam3"
        self._skip_path = data_dir / "gt" / "skip_list.json"
        self._cache: dict[str, _CachedEntry] = {}

    # ----- public API -------------------------------------------------

    def get_all(self) -> list[SessionGTState]:
        """Return every session with at least one pitch JSON, sorted
        by recency (newest first)."""
        sids = self._discover_session_ids()
        out: list[SessionGTState] = []
        for sid in sids:
            try:
                out.append(self.get(sid))
            except Exception as e:
                logger.warning("GTIndex.get(%s) failed: %s", sid, e)
        out.sort(key=lambda s: s.recency, reverse=True)
        return out

    def get(self, sid: str) -> SessionGTState:
        """Return cached state for `sid`, rebuilding if any dep file
        changed mtime/size since the cache entry was minted."""
        with self._lock:
            entry = self._cache.get(sid)
            if entry is not None and self._deps_unchanged_locked(entry):
                return entry.state
            new_entry = self._build_locked(sid)
            self._cache[sid] = new_entry
            return new_entry.state

    def invalidate(self, sid: str) -> None:
        with self._lock:
            self._cache.pop(sid, None)

    # ----- internals --------------------------------------------------

    def _discover_session_ids(self) -> list[str]:
        sids: set[str] = set()
        if self._pitch_dir.is_dir():
            for path in self._pitch_dir.glob("session_*.json"):
                stem = path.stem  # session_s_xxxxxxxx_A
                parts = stem.split("_")
                if len(parts) < 4 or parts[0] != "session":
                    continue
                # session_<sid>_<cam>; sid may contain underscores
                cam = parts[-1]
                if cam not in CAMS:
                    continue
                sid = "_".join(parts[1:-1])
                sids.add(sid)
        return sorted(sids)

    def _deps_unchanged_locked(self, entry: _CachedEntry) -> bool:
        for path_str, expected in entry.deps.items():
            actual = self._stat_key(Path(path_str))
            if actual != expected:
                return False
        return True

    def _stat_key(self, path: Path) -> tuple[int, int] | None:
        """Return (mtime_ns, size) for `path`, or None if missing.

        mtime_ns avoids same-second collision on fast operations;
        size doubles up so a same-mtime overwrite with different
        content is still detected.
        """
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    # ----- per-session builder ----------------------------------------

    def _build_locked(self, sid: str) -> _CachedEntry:
        deps: dict[str, tuple[int, int] | None] = {}

        # Skip list (one file, shared across sessions — but cheap to
        # re-read; we cache its parsed content only via the deps map).
        is_skipped = sid in self._load_skip_list()
        deps[str(self._skip_path)] = self._stat_key(self._skip_path)

        cams_present: dict[str, bool] = {}
        has_mov: dict[str, bool] = {}
        has_gt: dict[str, bool] = {}
        n_live_dets: dict[str, int] = {}
        t_first: dict[str, float | None] = {}
        t_last: dict[str, float | None] = {}
        durations: dict[str, float | None] = {}
        recency = 0.0

        for cam in CAMS:
            pitch_path = self._pitch_dir / f"session_{sid}_{cam}.json"
            sam3_path = self._sam3_dir / f"session_{sid}_{cam}.json"
            video_path = self._find_video(sid, cam)

            deps[str(pitch_path)] = self._stat_key(pitch_path)
            deps[str(sam3_path)] = self._stat_key(sam3_path)
            if video_path is not None:
                deps[str(video_path)] = self._stat_key(video_path)

            cams_present[cam] = pitch_path.is_file()
            has_gt[cam] = sam3_path.is_file()
            has_mov[cam] = video_path is not None and video_path.is_file()

            n = 0
            tf: float | None = None
            tl: float | None = None
            duration: float | None = None
            if cams_present[cam]:
                try:
                    payload = json.loads(pitch_path.read_text())
                except Exception as e:
                    logger.warning("pitch JSON %s parse failed: %s", pitch_path, e)
                    payload = {}
                video_start = payload.get("video_start_pts_s")
                if isinstance(video_start, (int, float)):
                    fps = payload.get("video_fps") or 0.0
                    # Prefer frames_live (px non-null); fallback frames_server_post.
                    n, tf, tl = _detection_stats(
                        payload.get("frames_live") or [],
                        float(video_start),
                    )
                    if n == 0:
                        n2, tf2, tl2 = _detection_stats(
                            payload.get("frames_server_post") or [],
                            float(video_start),
                        )
                        if n2 > 0:
                            n, tf, tl = n2, tf2, tl2
                    duration = _video_duration(
                        payload.get("frames_live") or [],
                        payload.get("frames_server_post") or [],
                        float(video_start),
                        float(fps) if fps else None,
                    )
                # mtime for sort
                try:
                    recency = max(recency, pitch_path.stat().st_mtime)
                except FileNotFoundError:
                    pass
            n_live_dets[cam] = n
            t_first[cam] = tf
            t_last[cam] = tl
            durations[cam] = duration

        state_obj = SessionGTState(
            session_id=sid,
            cams_present=cams_present,
            has_mov=has_mov,
            has_gt=has_gt,
            n_live_dets=n_live_dets,
            t_first_video_rel=t_first,
            t_last_video_rel=t_last,
            video_duration_s=durations,
            is_skipped=is_skipped,
            recency=recency,
        )
        return _CachedEntry(state=state_obj, deps=deps)

    def _find_video(self, sid: str, cam: str) -> Path | None:
        if not self._video_dir.is_dir():
            return None
        for ext in _VIDEO_EXTS:
            p = self._video_dir / f"session_{sid}_{cam}{ext}"
            if p.is_file():
                return p
        return None

    # ----- skip list --------------------------------------------------

    def _load_skip_list(self) -> set[str]:
        """`data/gt/skip_list.json` is `{"sids": ["s_xxx", ...]}`. Corrupt
        / missing returns empty set; never raises (boot tolerance)."""
        if not self._skip_path.is_file():
            return set()
        try:
            raw = json.loads(self._skip_path.read_text())
            sids = raw.get("sids", [])
            return {str(s) for s in sids if isinstance(s, str)}
        except Exception as e:
            logger.warning("skip_list.json corrupt — treating empty (%s)", e)
            return set()

    def add_skip(self, sid: str) -> None:
        with self._lock:
            self._skip_path.parent.mkdir(parents=True, exist_ok=True)
            current = self._load_skip_list()
            if sid in current:
                return
            current.add(sid)
            self._write_skip_locked(current)
            self._cache.pop(sid, None)

    def remove_skip(self, sid: str) -> None:
        with self._lock:
            current = self._load_skip_list()
            if sid not in current:
                return
            current.discard(sid)
            self._write_skip_locked(current)
            self._cache.pop(sid, None)

    def _write_skip_locked(self, sids: set[str]) -> None:
        tmp = self._skip_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sids": sorted(sids)}, indent=2))
        import os
        os.replace(tmp, self._skip_path)


# ----- pure helpers ---------------------------------------------------


def _detection_stats(
    frames: list, video_start_pts_s: float
) -> tuple[int, float | None, float | None]:
    """Count frames with px non-null + return (t_first, t_last) in
    video-relative seconds. Empty / all-px-null returns (0, None, None)."""
    if not frames:
        return 0, None, None
    valid_ts: list[float] = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        if f.get("px") is None:
            continue
        ts = f.get("timestamp_s")
        if not isinstance(ts, (int, float)):
            continue
        valid_ts.append(float(ts) - video_start_pts_s)
    if not valid_ts:
        return 0, None, None
    return len(valid_ts), min(valid_ts), max(valid_ts)


def _video_duration(
    frames_live: list,
    frames_server_post: list,
    video_start_pts_s: float,
    video_fps: float | None,
) -> float | None:
    """Best-effort video duration.

    Source preference: frames_server_post (full decode) → frames_live →
    None. We use last frame's `timestamp_s − video_start_pts_s` because
    that's the duration the operator's `<video>` element sees on disk.
    """
    for frames in (frames_server_post, frames_live):
        if not frames:
            continue
        last = frames[-1]
        if isinstance(last, dict) and isinstance(last.get("timestamp_s"), (int, float)):
            return float(last["timestamp_s"]) - video_start_pts_s
    return None


def session_gt_state_to_json(s: SessionGTState) -> dict:
    """Single serializer for SSR + GET /gt/sessions JSON. Used by both
    `render_gt_page.py` and `routes/gt.py` to avoid wire-format drift."""
    return s.to_dict()
