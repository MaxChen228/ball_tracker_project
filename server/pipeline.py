"""Glue between the raw MOV upload and the existing triangulation code.

`detect_pitch` decodes the video, runs HSV ball detection per frame, and
synthesises a list of `FramePayload`s on the iOS session clock. The
payload's `sync_anchor_timestamp_s` then makes anchor-relative time
well-defined for A/B pairing, so `pairing.triangulate_cycle` can consume
the post-detection `PitchPayload` with no code changes.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from detection import HSVRange, detect_ball
from schemas import FramePayload
from video import iter_frames

logger = logging.getLogger(__name__)


# Type alias for dependency-injected frame iterators. `detect_pitch` defaults
# to the real PyAV decoder; tests substitute a synthetic generator.
FrameIteratorFactory = Callable[[Path, float], Iterator[tuple[float, np.ndarray]]]


def detect_pitch(
    video_path: Path,
    video_start_pts_s: float,
    hsv_range: HSVRange | None = None,
    frame_iter: FrameIteratorFactory = iter_frames,
) -> list[FramePayload]:
    """Decode `video_path`, run HSV detection on every frame, and return
    one `FramePayload` per decoded sample. `timestamp_s` is the absolute
    iOS session-clock PTS (i.e. the same space `sync_anchor_timestamp_s`
    lives in). `px` / `py` are filled when a blob matches the HSV + area
    filter, else `ball_detected=False`."""
    hsv = hsv_range if hsv_range is not None else HSVRange.from_env()
    out: list[FramePayload] = []
    for idx, (absolute_pts_s, bgr) in enumerate(frame_iter(video_path, video_start_pts_s)):
        centroid = detect_ball(bgr, hsv)
        if centroid is None:
            out.append(
                FramePayload(
                    frame_index=idx,
                    timestamp_s=absolute_pts_s,
                    px=None,
                    py=None,
                    ball_detected=False,
                )
            )
        else:
            px, py = centroid
            out.append(
                FramePayload(
                    frame_index=idx,
                    timestamp_s=absolute_pts_s,
                    px=px,
                    py=py,
                    ball_detected=True,
                )
            )
    ball_frames = sum(1 for f in out if f.ball_detected)
    logger.info(
        "detection video=%s frames=%d ball=%d hsv=h[%d-%d]s[%d-%d]v[%d-%d]",
        video_path.name, len(out), ball_frames,
        hsv.h_min, hsv.h_max, hsv.s_min, hsv.s_max, hsv.v_min, hsv.v_max,
    )
    return out
