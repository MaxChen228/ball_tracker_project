"""Algorithm registry — single source of truth for which detection
algorithms the server knows how to run. Disk records, wire payloads,
and persisted detection results stamp an `algorithm_id` from this
registry. Slug rule: lowercase alphanumerics + underscore, ≤32 chars.

Each registry entry carries a `Detector` (per-algorithm runner) so
`run_detection(algorithm_id, video, params_dict, ...)` is the only
entry point callers need; v11 / v12+ swap behind it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from algorithms.base import (
    CancelCheck,
    Detector,
    FrameIteratorFactory,
    ProgressCallback,
)
from algorithms.v11_hsv_cc import V11Detector

if TYPE_CHECKING:
    from schemas import FramePayload

V11_HSV_CC = "v11_hsv_cc"

DEFAULT_ALGORITHM_ID = V11_HSV_CC

# iOS-side capture-time detection. Stamped on `frames_by_algorithm` and
# `config_used_by_algorithm` for the `live` data source. NOT runnable
# server-side (source pixels live only in the iOS capture buffer; no
# MOV-equivalent to feed back), so it bypasses `_REGISTRY` and lives in
# `_NON_RUNNABLE_IDS` instead. `validate_id` accepts it (it's a real id
# seen on disk + wire); `run_detection` rejects it via the registry
# lookup.
IOS_CAPTURE_TIME = "ios_capture_time"

_NON_RUNNABLE_IDS: frozenset[str] = frozenset({IOS_CAPTURE_TIME})


# Drift guard: schemas.py duplicates the IOS_CAPTURE_TIME literal to
# avoid a back-import cycle. Anyone editing one but not the other gets
# caught at boot rather than at first wire load.
def _check_schemas_constant_drift() -> None:
    from schemas import IOS_CAPTURE_TIME_ALGORITHM_ID as _SCHEMAS_IOS_ID
    if _SCHEMAS_IOS_ID != IOS_CAPTURE_TIME:
        raise RuntimeError(
            f"algorithms.IOS_CAPTURE_TIME ({IOS_CAPTURE_TIME!r}) and "
            f"schemas.IOS_CAPTURE_TIME_ALGORITHM_ID ({_SCHEMAS_IOS_ID!r}) "
            "have drifted — keep the literal identical in both files."
        )


_check_schemas_constant_drift()


@dataclass(frozen=True)
class AlgorithmEntry:
    algorithm_id: str
    label: str
    description: str
    detector: Detector


_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")


_REGISTRY: dict[str, AlgorithmEntry] = {
    V11_HSV_CC: AlgorithmEntry(
        algorithm_id=V11_HSV_CC,
        label="HSV + connected components",
        description=(
            "BGR→HSV → cv2.inRange → connectedComponentsWithStats → "
            "area / aspect / fill gates → shape-prior cost. The "
            "production pipeline since 2026-04; baseline R≈0.905 on "
            "the 1073-frame GT set."
        ),
        detector=V11Detector(),
    ),
}


def is_known(algorithm_id: str) -> bool:
    return algorithm_id in _REGISTRY


def get(algorithm_id: str) -> AlgorithmEntry:
    """Strict lookup. KeyError surface for callers at the system
    boundary (HTTP routes, disk loaders) so they can translate to
    HTTP 400 / boot failure with the offending id in the message."""
    return _REGISTRY[algorithm_id]


def list_all() -> list[AlgorithmEntry]:
    """Sorted by id for deterministic UI / log output."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def validate_id(algorithm_id: str) -> None:
    """Raise `ValueError` if the id isn't a registered algorithm."""
    if not _ID_RE.match(algorithm_id):
        raise ValueError(
            f"invalid algorithm_id {algorithm_id!r}: must match [a-z0-9_]{{1,32}}"
        )
    if algorithm_id in _REGISTRY or algorithm_id in _NON_RUNNABLE_IDS:
        return
    known = sorted(set(_REGISTRY) | _NON_RUNNABLE_IDS)
    raise ValueError(
        f"unknown algorithm_id {algorithm_id!r} (known: {known})"
    )


def run_detection(
    algorithm_id: str,
    video_path: Path,
    video_start_pts_s: float,
    params: dict[str, Any],
    *,
    frame_iter: FrameIteratorFactory | None = None,
    should_cancel: CancelCheck | None = None,
    progress: ProgressCallback | None = None,
) -> list["FramePayload"]:
    """Validate `algorithm_id`, materialize `params` against the
    registered detector's Pydantic schema, and run detection. Single
    entry point for every detection callsite (server_post background
    job, reprocess CLI). Raises `ValueError` (unknown algorithm_id) or
    `pydantic.ValidationError` (params fail schema) before touching
    the video — caller catches at the system boundary."""
    validate_id(algorithm_id)
    if algorithm_id not in _REGISTRY:
        raise ValueError(
            f"algorithm_id {algorithm_id!r} is not server-runnable "
            f"(non-runnable data sources: {sorted(_NON_RUNNABLE_IDS)})"
        )
    entry = _REGISTRY[algorithm_id]
    typed_params = entry.detector.params_schema.model_validate(params)
    return entry.detector.detect(
        video_path,
        video_start_pts_s,
        typed_params,
        frame_iter=frame_iter,
        should_cancel=should_cancel,
        progress=progress,
    )
