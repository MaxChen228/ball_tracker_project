"""Glue between the raw MOV upload and the existing triangulation code.

`detect_pitch` decodes the video, runs HSV ball detection per frame, and
synthesises a list of `FramePayload`s on the iOS session clock. The
payload's `sync_anchor_timestamp_s` then makes anchor-relative time
well-defined for A/B pairing, so `pairing.triangulate_cycle` can consume
the post-detection `PitchPayload` with no code changes.

`annotate_video` re-encodes the raw MOV with a green circle drawn on
every ball-detected frame. That's the clip the viewer page shows so
operators can eyeball detection quality at a glance; the raw MOV stays
on disk for forensics.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

from chain_filter import ChainFilterParams, annotate as chain_filter_annotate
from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate, detect_ball
from schemas import FramePayload
from video import iter_frames

logger = logging.getLogger(__name__)


# Type alias for dependency-injected frame iterators. `detect_pitch` defaults
# to the real PyAV decoder; tests substitute a synthetic generator.
FrameIteratorFactory = Callable[[Path, float], Iterator[tuple[float, np.ndarray]]]
CancelCheck = Callable[[], bool]


class ProcessingCanceled(RuntimeError):
    """Raised when an operator cancels a server-side post-processing job."""


# MOG2 background subtractor warm-up: the first few frames MOG2 emits a
# mostly-all-foreground mask while it builds per-pixel Gaussian models.
# Skip detection for this window so static yellow-green clutter doesn't
# sneak through as "moving". 30 frames @ 240 fps ≈ 125 ms — well under
# any realistic pitch windup.
_BG_SUBTRACTOR_WARMUP_FRAMES = 30

# 3x3 CLOSE kernel applied to the MOG2 foreground mask before AND-ing with
# the HSV mask. MOG2's raw mask has 1-2 px edge breakage and pinholes —
# confirmed on s_fcf73afa, where 14 in-flight frames missed purely because
# the combined mask's bbox fill fell to 0.61-0.70 (just under the 0.70
# gate) despite aspect ≈ 0.95. Closing heals those holes so fill returns
# to the theoretical π/4 ≈ 0.785. Kernel is intentionally tiny — 3x3 is
# big enough to bridge single-pixel gaps, small enough to not bleed the
# ball outline into adjacent motion.
_BG_CLOSE_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

# MOG2 tuning: OpenCV defaults (history=500, varThreshold=16) bake a
# background model over ~2 s @ 240 fps. A ball that sits still mid-
# windup for half a second gets learned into the background and then
# disappears from the foreground mask once it starts moving again —
# false-negatives on the first few frames of flight.
#
# history=120 ≈ 0.5 s @ 240 fps — short enough that the model tracks
# operator movement without baking in a stationary ball, long enough to
# stabilize against single-frame flicker. varThreshold kept at OpenCV
# default (16) because lowering it produces more edge noise on the
# indoor rig's dim background.
_BG_HISTORY = 120
_BG_VAR_THRESHOLD = 16

# Explicit low learning rate instead of MOG2's auto-compute
# (`-1` → 1/min(2*frameCount, history)). auto-compute is ~1/240 early on,
# fast enough to learn the ball into the background during the windup
# standstill. 0.0005 ≈ 1/2000 — the model adapts to genuine lighting
# drift over seconds but stays blind to brief stationary objects like
# a held ball. This pairs with the shorter `history` above so the
# two knobs reinforce rather than cancel.
_BG_LEARNING_RATE = 0.0005


def detect_pitch(
    video_path: Path,
    video_start_pts_s: float,
    hsv_range: HSVRange | None = None,
    frame_iter: FrameIteratorFactory = iter_frames,
    *,
    enable_bg_subtraction: bool = True,
    should_cancel: CancelCheck | None = None,
    expected_radius_px: float | None = None,
    shape_gate: ShapeGate | None = None,
    selector_tuning: "CandidateSelectorTuning | None" = None,
    chain_filter_params: ChainFilterParams | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[FramePayload]:
    """Decode `video_path`, run HSV + (optional) MOG2 background
    subtraction on every frame, and return one `FramePayload` per decoded
    sample. `timestamp_s` is the absolute iOS session-clock PTS (same
    space as `sync_anchor_timestamp_s`). `px` / `py` are filled when the
    post-filter blob matches HSV + area + shape + (if enabled) foreground.

    `enable_bg_subtraction=True` (default) prepends an MOG2 subtractor:
    only pixels changing across frames can match HSV, so static yellow-
    green clutter (dehumidifier buttons, door handles, hanger reflections)
    is ignored regardless of colour. Warm-up (`_BG_SUBTRACTOR_WARMUP_FRAMES`)
    is skipped because the subtractor's first-frames mask is unreliable.
    """
    hsv = hsv_range if hsv_range is not None else HSVRange.from_env()
    subtractor = (
        cv2.createBackgroundSubtractorMOG2(
            history=_BG_HISTORY,
            varThreshold=_BG_VAR_THRESHOLD,
            detectShadows=False,
        )
        if enable_bg_subtraction
        else None
    )
    # Explicit per-session log so the mode is never ambiguous in field
    # logs. `no radius prior` means fallback bounds [20, 150_000] px —
    # not a silent degraded mode, just a different code path.
    if expected_radius_px is None:
        logger.info(
            "detect_pitch video=%s mode=no-radius-prior (area∈[20,150000])",
            video_path.name,
        )
    else:
        logger.info(
            "detect_pitch video=%s expected_radius_px=%.1f",
            video_path.name, expected_radius_px,
        )
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
        fg_mask = None
        if subtractor is not None:
            fg_mask_raw = subtractor.apply(bgr, learningRate=_BG_LEARNING_RATE)
            # Skip detection during warm-up; still feed the subtractor so
            # the model keeps building across the whole clip.
            if idx >= _BG_SUBTRACTOR_WARMUP_FRAMES:
                fg_mask = cv2.morphologyEx(
                    fg_mask_raw, cv2.MORPH_CLOSE, _BG_CLOSE_KERNEL
                )
        if subtractor is not None and idx < _BG_SUBTRACTOR_WARMUP_FRAMES:
            centroid = None  # warm-up → force no-detection
        else:
            dt = (
                absolute_pts_s - prev_timestamp_s
                if prev_timestamp_s is not None else None
            )
            centroid = detect_ball(
                bgr, hsv,
                fg_mask=fg_mask,
                expected_radius_px=expected_radius_px,
                prev_position=prev_position,
                prev_velocity=prev_velocity,
                dt=dt,
                shape_gate=shape_gate,
                selector_tuning=selector_tuning,
            )
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
            # Temporal prior resets on miss — extrapolating from a stale
            # point can snap onto clutter on the next frame.
            prev_position = None
            prev_velocity = None
            prev_timestamp_s = None
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
        "detection video=%s frames=%d ball=%d hsv=h[%d-%d]s[%d-%d]v[%d-%d] bg_sub=%s",
        video_path.name, len(out), ball_frames,
        hsv.h_min, hsv.h_max, hsv.s_min, hsv.s_max, hsv.v_min, hsv.v_max,
        enable_bg_subtraction,
    )
    return out


# Annotation overlay style. Green circle so it reads against both the
# blue ball and typical indoor backgrounds; thickness = 3 so the outline
# survives the H.264 re-encode. Radius is in pixels.
_ANNOTATE_RADIUS_PX = 24
_ANNOTATE_COLOR_BGR: tuple[int, int, int] = (60, 220, 60)
_ANNOTATE_THICKNESS = 3


def annotate_video(
    input_path: Path,
    output_path: Path,
    frames: list[FramePayload],
    *,
    should_cancel: CancelCheck | None = None,
) -> None:
    """Re-encode `input_path` to `output_path` with a green circle drawn
    at each detected ball centroid. Frames without a detection pass
    through unmodified.

    `frames` must be in decoded order — exactly as `detect_pitch` emitted
    them — so each iteration pairs a FramePayload with the corresponding
    decoded picture (iter_frames and this function both skip None-PTS
    frames the same way, keeping indices aligned without extra
    bookkeeping).
    """
    import av  # type: ignore[import]

    in_container = av.open(str(input_path))
    try:
        in_stream = in_container.streams.video[0]
        rate = in_stream.average_rate or in_stream.base_rate or Fraction(30)
        out_container = av.open(str(output_path), mode="w")
        try:
            out_stream = out_container.add_stream("h264", rate=rate)
            out_stream.width = in_stream.width
            out_stream.height = in_stream.height
            out_stream.pix_fmt = "yuv420p"

            frames_iter = iter(frames)
            annotated = 0
            for decoded in in_container.decode(in_stream):
                if should_cancel is not None and should_cancel():
                    raise ProcessingCanceled(f"annotation canceled for {input_path.name}")
                if decoded.pts is None:
                    continue
                try:
                    fp = next(frames_iter)
                except StopIteration:
                    # Decoded more pictures than detection saw — shouldn't
                    # happen in practice because both passes share the
                    # skip-None-PTS rule, but stay safe.
                    break
                bgr = decoded.to_ndarray(format="bgr24")
                if fp.ball_detected and fp.px is not None and fp.py is not None:
                    cv2.circle(
                        bgr,
                        (int(round(fp.px)), int(round(fp.py))),
                        _ANNOTATE_RADIUS_PX,
                        _ANNOTATE_COLOR_BGR,
                        _ANNOTATE_THICKNESS,
                    )
                    annotated += 1
                out_frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
                for packet in out_stream.encode(out_frame):
                    out_container.mux(packet)
            # Flush encoder.
            for packet in out_stream.encode():
                out_container.mux(packet)
            logger.info(
                "annotated video input=%s output=%s circles_drawn=%d",
                input_path.name, output_path.name, annotated,
            )
        finally:
            out_container.close()
    finally:
        in_container.close()
