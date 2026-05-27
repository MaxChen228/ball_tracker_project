"""Glue between the raw MOV upload and the existing triangulation code.

`detect_pitch` decodes the video, runs HSV ball detection per frame, and
synthesises a list of `FramePayload`s on the iOS session clock. The
payload's `sync_anchor_timestamp_s` then makes anchor-relative time
well-defined for A/B pairing, so `pairing.triangulate_cycle` can consume
the post-detection `PitchPayload` with no code changes.

The detector here is identical to the iOS live path:
HSV → connectedComponents → shape gate → shape-prior selector. No
temporal state, no background model — every frame's cost is local, and
`server_post` is a byte-for-byte offline mock of iOS-live.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from detection import HSVRange, ShapeGate, detect_ball_with_candidates
from schemas import FramePayload
from video import iter_frames

logger = logging.getLogger(__name__)


# Type alias for dependency-injected frame iterators. `detect_pitch` defaults
# to the real PyAV decoder; tests substitute a synthetic generator.
FrameIteratorFactory = Callable[[Path, float], Iterator[tuple[float, np.ndarray]]]
CancelCheck = Callable[[], bool]


class ProcessingCanceled(RuntimeError):
    """Raised when an operator cancels a server-side post-processing job."""


def detect_pitch(
    video_path: Path,
    video_start_pts_s: float,
    hsv_range: HSVRange,
    shape_gate: ShapeGate,
    frame_iter: FrameIteratorFactory = iter_frames,
    *,
    should_cancel: CancelCheck | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[FramePayload]:
    """Decode `video_path`, run HSV ball detection on every frame, and
    return one `FramePayload` per decoded sample. `timestamp_s` is the
    absolute iOS session-clock PTS (same space as `sync_anchor_timestamp_s`).
    `px` / `py` are filled when the post-filter blob matches HSV + area +
    shape.

    `hsv_range` and `shape_gate` are REQUIRED — no env-var fallback, no
    None-default. Research-mode invariant (CLAUDE.md): silent fallback
    to a yellow-green tennis-ball HSV when the operator is actually
    running the blue-ball preset would corrupt every comparison. Every
    caller threads its explicit config through.
    """
    logger.info("detect_pitch video=%s", video_path.name)
    out: list[FramePayload] = []
    for idx, (absolute_pts_s, bgr) in enumerate(frame_iter(video_path, video_start_pts_s)):
        if should_cancel is not None and should_cancel():
            raise ProcessingCanceled(f"detection canceled for {video_path.name}")
        if progress is not None:
            # The caller is responsible for throttling — `progress` may
            # bridge across threads (e.g. asyncio.run_coroutine_threadsafe
            # from a to_thread worker). Per-frame call cost is negligible
            # so the cheap thing is to fire every frame and let the
            # caller decide what to coalesce.
            progress(idx)
        winner, blobs = detect_ball_with_candidates(
            bgr, hsv_range,
            shape_gate=shape_gate,
        )
        out.append(
            FramePayload(
                frame_index=idx,
                timestamp_s=absolute_pts_s,
                px=winner.px if winner is not None else None,
                py=winner.py if winner is not None else None,
                ball_detected=winner is not None,
                # Always pass through the actual blobs list. Empty list
                # ("detector ran, found 0 candidates") must not collapse
                # to None ("no detection attempted") — research-mode
                # invariant: callers distinguish these cases.
                candidates=blobs,
            )
        )
    ball_frames = sum(1 for f in out if f.ball_detected)
    logger.info(
        "detection video=%s frames=%d ball=%d hsv=h[%d-%d]s[%d-%d]v[%d-%d]",
        video_path.name, len(out), ball_frames,
        hsv_range.h_min, hsv_range.h_max, hsv_range.s_min, hsv_range.s_max, hsv_range.v_min, hsv_range.v_max,
    )
    return out
