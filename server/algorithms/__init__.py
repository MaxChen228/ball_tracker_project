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
from algorithms.hybrid_28d import Hybrid28dDetector
from algorithms.v11_hsv_cc import V11Detector

if TYPE_CHECKING:
    from schemas import FramePayload

V11_HSV_CC = "v11_hsv_cc"
HYBRID_28D = "hybrid_28d"

DEFAULT_ALGORITHM_ID = V11_HSV_CC

# iOS-side capture-time detection. Stamped on `frames_by_algorithm` and
# `config_used_by_algorithm` for the `live` data source. NOT runnable
# server-side (source pixels live only in the iOS capture buffer; no
# MOV-equivalent to feed back), so it bypasses `_REGISTRY` and lives in
# `NON_RUNNABLE_IDS` instead. `validate_id` accepts it (it's a real id
# seen on disk + wire); `run_detection` rejects it via the registry
# lookup.
IOS_CAPTURE_TIME = "ios_capture_time"

# Public set of non-runnable data-source ids — callers that need to
# dispatch on "is this id a non-runnable data source" (e.g. the wire-
# schema validator skipping `Detector.params_schema` round-trip because
# there's no Detector) read this directly.
NON_RUNNABLE_IDS: frozenset[str] = frozenset({IOS_CAPTURE_TIME})

# Cost threshold for the iOS capture-time data source. The live pipeline
# uses the same `score_candidates` cost function as v11_hsv_cc (both
# read aspect/fill from HSV+CC mask candidates), so the threshold also
# matches v11's. Held as a separate constant rather than synthesized
# from `_REGISTRY` so the lookup helper stays a single dict read for
# the runnable case and a typed branch for IOS_CAPTURE_TIME.
IOS_CAPTURE_TIME_COST_THRESHOLD = 0.5


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
    # Algorithm-owned cost gate, applied as `max(cost_a, cost_b) ≤
    # cost_threshold` on every triangulated point before the segmenter
    # consumes it (`session_results._passes_stamped_filter`). The cost
    # values themselves come from `candidate_selector.score_candidates`,
    # which reads detection-specific features (aspect, fill) — so the
    # "right" threshold is a property of the detector + its feature
    # distribution, not an operator preference. v11_hsv_cc baseline 0.5
    # was the previous global default; future algorithms set their own.
    cost_threshold: float


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
        cost_threshold=0.5,
    ),
    HYBRID_28D: AlgorithmEntry(
        algorithm_id=HYBRID_28D,
        label="Hybrid PROD + V11 motion-rescue (28d)",
        description=(
            "Per frame: if PROD (tight HSV+shape gate) emits ≥1 cand → "
            "rank by shape cost; else fall back to V11 (loose HSV + "
            "morphology CLOSE + loose shape gate) ranked by motion-"
            "novelty (persistence in ±neigh_half window) then shape "
            "cost. Lab eval (PR #112): R_top1=0.660 on 1956 GT frames, "
            "+0.045 over PROD baseline 0.615; 0/15 session regressions."
        ),
        detector=Hybrid28dDetector(),
        # Same gate as v11 — both detectors emit through the shared
        # `candidate_selector.score_candidates` cost function on
        # aspect+fill, so the threshold envelope is identical.
        cost_threshold=0.5,
    ),
}


# Drift guard #2: `schemas._LEGACY_PRE_SNAPSHOT_ALGORITHM_ID` names the
# bucket pre-Phase-2 server_post frames mirror under (the only detector
# that ever shipped at the time was v11_hsv_cc). If someone removes v11
# from the registry without updating that constant, the legacy fallback
# would point at a dangling id and 6b readers would surface nothing for
# pre-snapshot pitches. Catch it at boot, after `_REGISTRY` is defined.
def _check_legacy_bucket_in_registry() -> None:
    from schemas import _LEGACY_PRE_SNAPSHOT_ALGORITHM_ID as _BUCKET
    if _BUCKET not in _REGISTRY:
        raise RuntimeError(
            f"schemas._LEGACY_PRE_SNAPSHOT_ALGORITHM_ID is "
            f"{_BUCKET!r} but that id is no longer in the algorithm "
            f"registry (have: {sorted(_REGISTRY)}). Either restore the "
            "id or pick a new historical bucket — pre-snapshot pitches "
            "still need somewhere to file their server_post frames."
        )


_check_legacy_bucket_in_registry()


def is_valid_id_format(s: str) -> bool:
    """Pure regex check on the slug shape — no registry lookup. Lets
    HTTP handlers split "this string is structurally not an
    algorithm_id" (400 — malformed input, like a typo with capitals or
    a dash) from "this string is shaped like an id but I don't know
    what it names" (422 — semantically invalid). Both cases otherwise
    funnel through `validate_id`'s ValueError and the route layer
    can't tell them apart."""
    return bool(_ID_RE.match(s))


def is_known(algorithm_id: str) -> bool:
    return algorithm_id in _REGISTRY


def get(algorithm_id: str) -> AlgorithmEntry:
    """Strict lookup. KeyError surface for callers at the system
    boundary (HTTP routes, disk loaders) so they can translate to
    HTTP 400 / boot failure with the offending id in the message."""
    return _REGISTRY[algorithm_id]


def cost_threshold_for_algorithm(algorithm_id: str) -> float:
    """Strict cost-threshold lookup for any wire / disk algorithm id.

    Accepts both runnable algorithms (`_REGISTRY`) and the non-runnable
    iOS capture-time data source — both have a cost gate applied on
    triangulated points. Raises `ValueError` on unknown ids; no silent
    fallback (CLAUDE.md: experimental phase, no backcompat shims)."""
    entry = _REGISTRY.get(algorithm_id)
    if entry is not None:
        return entry.cost_threshold
    if algorithm_id == IOS_CAPTURE_TIME:
        return IOS_CAPTURE_TIME_COST_THRESHOLD
    known = sorted(set(_REGISTRY) | NON_RUNNABLE_IDS)
    raise ValueError(
        f"unknown algorithm_id {algorithm_id!r} (known: {known})"
    )


def list_all() -> list[AlgorithmEntry]:
    """Sorted by id for deterministic UI / log output."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def validate_id(algorithm_id: str) -> None:
    """Raise `ValueError` if `algorithm_id` is not a known wire / disk
    identity. Accepts both server-runnable algorithms (`_REGISTRY`) and
    non-runnable data-source ids (`NON_RUNNABLE_IDS`, e.g.
    `ios_capture_time`).

    Use this when you only need to validate that a string is a real
    algorithm identity — e.g. when stamping
    `DetectionConfigSnapshotPayload.algorithm_id` from disk or wire.

    For callsites that will later pass `algorithm_id` to
    `run_detection`, use `validate_runnable_id` instead so a typo like
    `ios_capture_time` is caught at set time, not at run time."""
    if not _ID_RE.match(algorithm_id):
        raise ValueError(
            f"invalid algorithm_id {algorithm_id!r}: must match [a-z0-9_]{{1,32}}"
        )
    if algorithm_id in _REGISTRY or algorithm_id in NON_RUNNABLE_IDS:
        return
    known = sorted(set(_REGISTRY) | NON_RUNNABLE_IDS)
    raise ValueError(
        f"unknown algorithm_id {algorithm_id!r} (known: {known})"
    )


def validate_runnable_id(algorithm_id: str) -> None:
    """Raise `ValueError` unless `algorithm_id` names an algorithm that
    `run_detection` can actually execute. Stricter than `validate_id`:
    rejects non-runnable data sources (`ios_capture_time`).

    Use at set-time boundaries where the caller's contract is "this id
    will get passed to run_detection later" — preset writes, dashboard
    active-config edits, reprocess `--algorithm-id` overrides. Pushes
    typo errors forward to set time instead of run time."""
    validate_id(algorithm_id)
    if algorithm_id not in _REGISTRY:
        raise ValueError(
            f"algorithm_id {algorithm_id!r} is a non-runnable data "
            f"source — cannot be used where run_detection is the eventual "
            f"target (runnable: {sorted(_REGISTRY)})"
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
    validate_runnable_id(algorithm_id)
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
