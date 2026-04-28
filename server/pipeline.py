"""Glue between the raw MOV upload and the existing triangulation code.

`detect_pitch` decodes the video, runs HSV ball detection per frame, and
synthesises a list of `FramePayload`s on the iOS session clock. The
payload's `sync_anchor_timestamp_s` then makes anchor-relative time
well-defined for A/B pairing, so `pairing.triangulate_cycle` can consume
the post-detection `PitchPayload` with no code changes.

The detector here is intentionally identical to the iOS `live` path
(HSV + connectedComponents + shape gate + temporal selector). Earlier
versions prepended an MOG2 background subtractor + 3x3 CLOSE morphology;
that asymmetry made `server_post` unable to act as an offline mock for
iOS-live. Distillation against SAM 3 GT replaces the role MOG2 used to
play (filtering static yellow-green clutter) by tightening HSV / shape
gate parameters, applied uniformly to both paths.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from chain_filter import ChainFilterParams, annotate as chain_filter_annotate
from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate
from detection_engine import DetectionEngine, HSVDetectionEngine
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
    hsv_range: HSVRange | None = None,
    frame_iter: FrameIteratorFactory = iter_frames,
    *,
    should_cancel: CancelCheck | None = None,
    shape_gate: ShapeGate | None = None,
    selector_tuning: "CandidateSelectorTuning | None" = None,
    chain_filter_params: ChainFilterParams | None = None,
    progress: Callable[[int], None] | None = None,
    engine: DetectionEngine | None = None,
) -> list[FramePayload]:
    """Decode `video_path`, run ball detection via `engine` on every frame,
    and return one `FramePayload` per decoded sample. `timestamp_s` is the
    absolute iOS session-clock PTS (same space as `sync_anchor_timestamp_s`).
    `px` / `py` are filled when the post-filter blob matches the engine's
    gates.

    Default engine is `HSVDetectionEngine` constructed from the supplied
    `hsv_range` + `shape_gate` + `selector_tuning`; pass an explicit
    `engine` to override (e.g. ML model). When `engine` is provided the
    three legacy knobs are ignored — the engine owns its own configuration.

    Algorithm of the default HSV engine is byte-for-byte aligned with the
    iOS `live` HSV path so a diff between the two reflects the H.264 vs
    BGRA input asymmetry (chroma 4:2:0 + DCT quantization), not the
    algorithm itself.
    """
    if engine is None:
        hsv = hsv_range if hsv_range is not None else HSVRange.from_env()
        engine = HSVDetectionEngine(
            hsv_range=hsv,
            shape_gate=shape_gate,
            selector_tuning=selector_tuning,
        )
    elif (hsv_range is not None or shape_gate is not None
          or selector_tuning is not None):
        # An engine was supplied AND HSV-flavoured knobs — these don't
        # plumb through to a generic engine, so silently ignoring them
        # would lie about which config actually ran. Caller picks one.
        raise ValueError(
            "detect_pitch: pass either `engine` or "
            "`hsv_range`/`shape_gate`/`selector_tuning`, not both"
        )
    logger.info("detect_pitch video=%s engine=%s", video_path.name, engine.name)
    out: list[FramePayload] = []
    # Temporal prior state — equal-velocity straight-line model that
    # carries the ball's last known (position, velocity). Reset to None
    # whenever detection fails so we don't extrapolate off a stale point.
    # No persistence, no Kalman — keep it simple.
    prev_position: tuple[float, float] | None = None
    prev_velocity: tuple[float, float] | None = None
    prev_timestamp_s: float | None = None
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
        dt = (
            absolute_pts_s - prev_timestamp_s
            if prev_timestamp_s is not None else None
        )
        winner, blobs = engine.detect(
            bgr,
            prev_position=prev_position,
            prev_velocity=prev_velocity,
            dt=dt,
        )
        if winner is None:
            out.append(
                FramePayload(
                    frame_index=idx,
                    timestamp_s=absolute_pts_s,
                    px=None,
                    py=None,
                    ball_detected=False,
                    candidates=blobs or None,
                    detection_engine=engine.name,
                )
            )
            # Temporal prior resets on miss — extrapolating from a stale
            # point can snap onto clutter on the next frame.
            prev_position = None
            prev_velocity = None
            prev_timestamp_s = None
        else:
            px, py = winner.px, winner.py
            out.append(
                FramePayload(
                    frame_index=idx,
                    timestamp_s=absolute_pts_s,
                    px=px,
                    py=py,
                    ball_detected=True,
                    candidates=blobs,
                    detection_engine=engine.name,
                )
            )
            # Update velocity from the previous hit if we have one; on
            # the first hit we only have position — velocity stays None
            # so next frame's selector falls back to area-only.
            if prev_position is not None and prev_timestamp_s is not None:
                dt_seen = absolute_pts_s - prev_timestamp_s
                if dt_seen > 0:
                    prev_velocity = (
                        (px - prev_position[0]) / dt_seen,
                        (py - prev_position[1]) / dt_seen,
                    )
            prev_position = (px, py)
            prev_timestamp_s = absolute_pts_s
    ball_frames = sum(1 for f in out if f.ball_detected)
    chain_filter_annotate(out, chain_filter_params or ChainFilterParams())
    logger.info(
        "detection video=%s engine=%s frames=%d ball=%d",
        video_path.name, engine.name, len(out), ball_frames,
    )
    return out
