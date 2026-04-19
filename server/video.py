"""MOV / MP4 decode + absolute-PTS reconstruction.

The iPhone records via `AVAssetWriter.startSession(atSourceTime: firstPTS)`,
which causes the MOV container to store sample PTS that are either
absolute (session clock) or relative (starting at 0) depending on
codec / OS version. To keep the server deterministic across iOS builds,
the iPhone ships `video_start_pts_s` (the absolute session-clock PTS of
the first appended sample) in the upload JSON. We add
`(frame.pts * time_base - first_container_pts_s)` to that start value so
every decoded frame's `absolute_pts_s` sits on the same clock as
`sync_anchor_timestamp_s` and A/B pairing works.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import av  # type: ignore[import]
import numpy as np

logger = logging.getLogger(__name__)


def iter_frames(
    video_path: Path,
    video_start_pts_s: float,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield `(absolute_pts_s, bgr_frame)` for every decoded video sample.

    `bgr_frame` is a contiguous BGR uint8 numpy array — the format
    `cv2.cvtColor` / `cv2.inRange` expect. `absolute_pts_s` is the iOS
    session-clock PTS reconstructed from the container.
    """
    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        time_base = stream.time_base
        first_container_pts: int | None = None

        for frame in container.decode(stream):
            if frame.pts is None:
                # Can happen on very first B-frame in some codecs; skip.
                continue
            if first_container_pts is None:
                first_container_pts = frame.pts
            container_pts_s = float((frame.pts - first_container_pts) * time_base)
            absolute_pts_s = video_start_pts_s + container_pts_s
            bgr = frame.to_ndarray(format="bgr24")
            yield absolute_pts_s, bgr
    finally:
        container.close()


def probe_dims(video_path: Path) -> tuple[int, int] | None:
    """Return `(width, height)` of the MOV's decoded pixel grid, or None
    if the container can't be opened / has no video stream. Lightweight —
    opens the container, reads stream metadata, closes; no frame decode."""
    try:
        container = av.open(str(video_path))
    except Exception as e:
        logger.warning("probe_dims failed for %s: %s", video_path, e)
        return None
    try:
        stream = container.streams.video[0]
        w = int(getattr(stream.codec_context, "width", 0) or getattr(stream, "width", 0) or 0)
        h = int(getattr(stream.codec_context, "height", 0) or getattr(stream, "height", 0) or 0)
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    finally:
        container.close()


def count_frames(video_path: Path) -> int:
    """Cheap second-pass frame count (used by tests / sanity logs)."""
    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        n = 0
        for _ in container.decode(stream):
            n += 1
        return n
    finally:
        container.close()
