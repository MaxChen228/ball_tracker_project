"""Base abstractions for the algorithm registry.

A `Detector` is the per-algorithm contract: given a video + algorithm-
specific params, produce a list of `FramePayload` candidates. The
contract is intentionally narrow — every detector accepts the same
environmental knobs (`should_cancel`, `progress`, `frame_iter`) so
the calling code (server_post background task, reprocess CLI) is
algorithm-agnostic.

V11 is currently the only registered algorithm. V12+ (Y-diff, SAM2
propagator, FCN distillation) will plug in here without touching the
caller paths.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel

if TYPE_CHECKING:
    from schemas import FramePayload


FrameIteratorFactory = Callable[[Path, float], Iterator[tuple[float, np.ndarray]]]
CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[int], None]


class Detector(ABC):
    """Per-algorithm runner. Each algorithm subclasses this once and
    registers itself in `algorithms.__init__._REGISTRY`.

    The detector owns the per-algorithm params schema (`params_schema`)
    so callers can validate untrusted dicts (CLI `--params` JSON,
    persisted preset payloads) into a typed Pydantic model before
    handing it off."""

    params_schema: type[BaseModel]
    """The Pydantic model that this detector's `params` argument must
    instantiate. Set as a class attribute on the subclass."""

    @abstractmethod
    def detect(
        self,
        video_path: Path,
        video_start_pts_s: float,
        params: BaseModel,
        *,
        frame_iter: FrameIteratorFactory | None = None,
        should_cancel: CancelCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> list["FramePayload"]:
        """Run detection over every frame and return one `FramePayload`
        per decoded sample. `params` MUST be an instance of
        `self.params_schema` — caller is responsible for validating
        the dict."""
