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
