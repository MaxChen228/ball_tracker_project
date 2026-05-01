"""Algorithm registry — single source of truth for which detection
algorithms the server knows how to run.

Today (phase 1 of the multi-version refactor) the registry contains
exactly one entry: `v11_hsv_cc` — the HSV + connected-components +
shape-gate pipeline currently shipped in `detection.py`. The registry
exists so disk records, wire payloads, and persisted detection results
carry an explicit `algorithm_id` discriminator from day one. Future
versions (Y-diff fusion, trajectory gap-fill, etc.) plug in here without
touching the schemas that already reference `algorithm_id`.

What the registry does NOT do (yet):
  - It does not dispatch detection. `pipeline.detect_pitch` and
    `live_pairing` still call `detect_ball_with_candidates` directly.
    Adding a `detect()` callable to each entry is phase 2+ work.
  - It does not own params dataclasses. v11's params (`HSVRange` /
    `ShapeGate`) still live in `detection.py`. A future entry that
    needs different params will declare them under
    `server/algorithms/<algorithm_id>/`.

The id slug rule mirrors `presets.validate_slug`: lowercase
alphanumerics + underscore, ≤32 chars. This keeps it usable as a
filename / URL component without escaping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Sole entry today. When phase 2 lands a second algorithm, it appears
# here with its own id and (eventually) a `detect` factory.
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
    """Raise `ValueError` if the id isn't a registered algorithm. The
    slug-shape check happens too (defence-in-depth) but the meaningful
    failure is "not in registry"."""
    if not isinstance(algorithm_id, str) or not _ID_RE.match(algorithm_id):
        raise ValueError(
            f"invalid algorithm_id {algorithm_id!r}: must match [a-z0-9_]{{1,32}}"
        )
    if algorithm_id not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise ValueError(
            f"unknown algorithm_id {algorithm_id!r} (known: {known})"
        )
