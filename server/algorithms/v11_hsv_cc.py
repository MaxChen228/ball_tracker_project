"""V11 HSV + connected-components detector.

The production pipeline since 2026-04. Wraps `pipeline.detect_pitch`
behind the `Detector` contract so the registry can dispatch to it
the same way it'll dispatch to V12 / V13 detectors.

Params shape (`V11Params`) mirrors what the dashboard HSV slider +
shape-gate slider edit. Wire layer keeps `HSVRangePayload` /
`ShapeGatePayload` because those existed before the registry; this
module bridges them.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from algorithms.base import (
    CancelCheck,
    Detector,
    FrameIteratorFactory,
    ProgressCallback,
)
from schemas import HSVRangePayload, ShapeGatePayload

if TYPE_CHECKING:
    from schemas import FramePayload


class V11Params(BaseModel):
    """Per-call params for the V11 detector. Carries the HSV cube and
    shape gate the per-frame pipeline applies. `DetectionConfigSnapshotPayload`
    already exposes `params: dict[str, Any]` per-algorithm, so this
    schema is what its `params` dict round-trips against when
    `algorithm_id == "v11_hsv_cc"`."""
    model_config = ConfigDict(extra="forbid")
    hsv: HSVRangePayload
    shape_gate: ShapeGatePayload


class V11Detector(Detector):
    """Adapter: registry dispatch → existing `pipeline.detect_pitch`.
    `pipeline.detect_pitch` is imported lazily so this module is cheap
    to import at registry build time (avoids loading pyav/cv2 unless
    detection is actually invoked)."""

    params_schema: type[BaseModel] = V11Params

    def detect(
        self,
        video_path: Path,
        video_start_pts_s: float,
        params: V11Params,
        *,
        frame_iter: FrameIteratorFactory | None = None,
        should_cancel: CancelCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> list["FramePayload"]:
        from detection import HSVRange, ShapeGate
        from pipeline import detect_pitch
        from video import iter_frames

        hsv_range = HSVRange(
            h_min=params.hsv.h_min, h_max=params.hsv.h_max,
            s_min=params.hsv.s_min, s_max=params.hsv.s_max,
            v_min=params.hsv.v_min, v_max=params.hsv.v_max,
        )
        shape_gate = ShapeGate(
            aspect_min=params.shape_gate.aspect_min,
            fill_min=params.shape_gate.fill_min,
        )
        return detect_pitch(
            video_path,
            video_start_pts_s,
            hsv_range=hsv_range,
            shape_gate=shape_gate,
            frame_iter=iter_frames if frame_iter is None else frame_iter,
            should_cancel=should_cancel,
            progress=progress,
        )
