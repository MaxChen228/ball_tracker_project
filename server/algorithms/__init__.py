"""Algorithm registry — single source of truth for which detection
algorithms the server knows how to run. Disk records, wire payloads,
and persisted detection results stamp an `algorithm_id` from this
registry. Slug rule: lowercase alphanumerics + underscore, ≤32 chars.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

V11_HSV_CC = "v11_hsv_cc"

DEFAULT_ALGORITHM_ID = V11_HSV_CC


@dataclass(frozen=True)
class AlgorithmEntry:
    algorithm_id: str
    label: str
    description: str


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
    if algorithm_id not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise ValueError(
            f"unknown algorithm_id {algorithm_id!r} (known: {known})"
        )
