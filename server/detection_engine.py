"""Server-side detection engine abstraction.

Wraps the per-frame detector behind a `Protocol` so the codebase can host
multiple implementations (today: HSV; planned: distilled ML model) without
the upstream pipeline / live ingest paths needing to know which one ran.

The engine identity (`name`) is stamped onto every produced `FramePayload`
so historical pitches stay reproducible — five years from now you can still
tell whether a session's centroids came from `hsv@1.0` or `ml@<sha>`.

Versioning convention: `<family>@<version-or-sha>`. Server-side HSV is
`hsv@1.0`; iOS-side HSV (run inside `BallDetector.mm`, frames arriving over
WS) is `hsv@ios.1.0` — same algorithm, different implementation language
and input domain (BGRA sensor direct vs H.264-decoded BGR), so a separate
identifier keeps the asymmetry legible in archived data.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np

from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate, detect_ball_with_candidates
from schemas import BlobCandidate


HSV_SERVER_ENGINE_NAME = "hsv@1.0"
HSV_IOS_ENGINE_NAME = "hsv@ios.1.0"


class DetectionEngine(Protocol):
    """Per-frame ball detector. Stateless w.r.t. frames — temporal prior
    is supplied by the caller (pipeline / live_pairing) so the engine
    object can be reused across sessions / pitches without per-instance
    reset bookkeeping."""

    @property
    def name(self) -> str:
        """Engine identity stamped onto produced FramePayloads. MUST be
        a stable string; downstream callers use it as a dict key / log
        partition / on-disk identifier. Bumping algorithm or weights ⇒
        bump the version suffix."""
        ...

    def detect(
        self,
        frame_bgr: np.ndarray,
        *,
        prev_position: tuple[float, float] | None = None,
        prev_velocity: tuple[float, float] | None = None,
        dt: float | None = None,
    ) -> tuple[BlobCandidate | None, list[BlobCandidate]]:
        """Detect ball candidates in a BGR frame.

        Returns `(winner, blobs)` where `blobs` is every candidate that
        passed the engine's gates with `area_score` + selector `cost`
        stamped, and `winner` is the lowest-cost one (or `None` if no
        candidates survived). Same shape as
        `detection.detect_ball_with_candidates` — the pipeline + viewer
        depend on this contract."""
        ...


class HSVDetectionEngine:
    """Server-side HSV pipeline behind the `DetectionEngine` Protocol.

    Binds HSV range + shape gate + selector tuning at construction so
    the pipeline can pass a single engine object instead of plumbing
    three knobs through every call. Mutating any of these in-flight is
    not supported — build a fresh engine when settings change."""

    def __init__(
        self,
        hsv_range: HSVRange,
        shape_gate: ShapeGate | None = None,
        selector_tuning: CandidateSelectorTuning | None = None,
    ) -> None:
        self._hsv_range = hsv_range
        self._shape_gate = shape_gate
        self._selector_tuning = selector_tuning

    @property
    def name(self) -> str:
        return HSV_SERVER_ENGINE_NAME

    def detect(
        self,
        frame_bgr: np.ndarray,
        *,
        prev_position: tuple[float, float] | None = None,
        prev_velocity: tuple[float, float] | None = None,
        dt: float | None = None,
    ) -> tuple[BlobCandidate | None, list[BlobCandidate]]:
        return detect_ball_with_candidates(
            frame_bgr,
            self._hsv_range,
            prev_position=prev_position,
            prev_velocity=prev_velocity,
            dt=dt,
            shape_gate=self._shape_gate,
            selector_tuning=self._selector_tuning,
        )
