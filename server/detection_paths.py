"""Frame-bucket and detection-path selection helpers.

Every pitch's frames live in `frames_by_algorithm` keyed by algorithm
id (`ios_capture_time` for iOS-side capture-time detection, runnable
algorithm ids like `v11_hsv_cc` for server-side detection). These
helpers map between the legacy `(pitch, DetectionPath)` interface
that callers still want to read and the canonical dict storage:

- `algorithm_id_for_path` resolves a path → algorithm id (live →
  `ios_capture_time`, server_post → `pitch.active_server_post_algorithm_id`).
  Raises ValueError when server_post is requested without a pointer —
  no legacy fallback per CLAUDE.md silent-fallback rule.
- `get_algorithm_frames` / `set_algorithm_frames` are the low-level
  dict accessors.
- `stamp_server_post_run` is the atomic writer for a server-side
  detection result (snapshot + frames + active pointer).
- `pitch_with_path_frames` / `pitch_with_algorithm_frames` clone a
  pitch with the chosen surface promoted into the active server_post
  slot so downstream consumers (`reconstruct.build_scene`, ray
  builders) can read a single field regardless of source.

The pure helpers (`normalize_paths`, `has_server_frames`,
`get_path_frames`, the `pitch_with_*` cloners) depend on nothing
beyond the schema and are safe to call anywhere. The state-dependent
helper (`paths_for_pitch`) reads via the public State accessors
`session_paths_for` / `default_detection_paths`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schemas import (
    DetectionPath,
    FramePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
)

if TYPE_CHECKING:
    from state import State


def normalize_paths(
    raw_paths: list[str] | set[DetectionPath] | None,
) -> set[DetectionPath]:
    if raw_paths is None:
        return set()
    parsed: set[DetectionPath] = set()
    for item in raw_paths:
        try:
            parsed.add(item if isinstance(item, DetectionPath) else DetectionPath(str(item)))
        except ValueError:
            continue
    return parsed


def has_server_frames(pitch: PitchPayload) -> bool:
    """True once the server-side MOV detection has populated
    `pitch.frames_server_post`. Used to gate `triangulate_pair(source="server")`
    so the early-surface path (record runs before detection finishes, with
    `frames_server_post=[]`) doesn't flag a spurious error — it just leaves
    `result.points=[]` until the background detect task updates the pitch
    and we re-record."""
    return bool(pitch and pitch.frames_server_post)


def paths_for_pitch(state: "State", pitch: PitchPayload) -> set[DetectionPath]:
    explicit = normalize_paths(pitch.paths)
    if explicit:
        return explicit
    sess_paths = state.session_paths_for(pitch.session_id)
    if sess_paths is not None:
        return sess_paths
    return state.default_detection_paths()


def get_path_frames(pitch: PitchPayload, path: DetectionPath) -> list[FramePayload]:
    if path == DetectionPath.live:
        return list(pitch.frames_live)
    return list(pitch.frames_server_post)


def pitch_with_path_frames(
    pitch: PitchPayload,
    path: DetectionPath,
) -> PitchPayload:
    """Clone the pitch with the chosen path's frames promoted into
    the active server_post slot so downstream consumers
    (reconstruct.build_scene, ray builders) can read a single source.
    Implemented by routing the chosen path's algorithm id through
    `active_server_post_algorithm_id` on the clone — the
    `frames_server_post` computed field then projects from the matching
    `frames_by_algorithm` bucket."""
    alg_id = algorithm_id_for_path(pitch, path)
    clone = pitch.model_copy(deep=True)
    clone.active_server_post_algorithm_id = alg_id
    return clone


# --- algorithm-id-keyed accessors -------------------------------------------
#
# These read from / write to `pitch.frames_by_algorithm` directly.
# Path-keyed helpers above are convenience views — internally they
# resolve the path's algorithm id (`live → ios_capture_time`,
# `server_post → <pitch.active_server_post_algorithm_id>`) and read
# from the same dict.


def algorithm_id_for_path(pitch: PitchPayload, path: DetectionPath) -> str:
    """Resolve the algorithm id a path's frames live under in
    `frames_by_algorithm`. `live` always maps to `ios_capture_time`
    (the iOS-side capture-time data source). `server_post` reads
    `pitch.active_server_post_algorithm_id`.

    Per CLAUDE.md 'Experimental phase — 禁止 silent fallback', missing
    `active_server_post_algorithm_id` for a `server_post` path is an
    invariant violation, not a recoverable fallback: every pitch that
    has server_post frames MUST have the pointer stamped via
    `stamp_server_post_run` at run-time. Pre-Phase-2 disk records were
    migrated once by a one-shot script no longer in-tree. Callers that
    might receive a pitch without the pointer (e.g. a fresh pitch that
    has only live frames) MUST guard with
    `pitch.active_server_post_algorithm_id is not None` before asking
    for the server_post algorithm id.

    Raises:
        ValueError: `path == server_post` and the pointer is None.
    """
    if path == DetectionPath.live:
        return IOS_CAPTURE_TIME_ALGORITHM_ID
    if pitch.active_server_post_algorithm_id is None:
        raise ValueError(
            f"pitch sid={pitch.session_id} cam={pitch.camera_id} has no "
            "active_server_post_algorithm_id; cannot resolve server_post "
            "algorithm id without an explicit pointer. Either stamp the "
            "pointer via stamp_server_post_run, or guard the call site "
            "with `pitch.active_server_post_algorithm_id is not None`."
        )
    return pitch.active_server_post_algorithm_id


def get_algorithm_frames(
    pitch: PitchPayload, algorithm_id: str,
) -> list[FramePayload]:
    """Read frames recorded under `algorithm_id`. Returns `[]` (not None)
    when the algorithm hasn't run for this pitch — match the
    `get_path_frames` invariant so callers don't need to guard."""
    return list(pitch.frames_by_algorithm.get(algorithm_id, []))


def set_algorithm_frames(
    pitch: PitchPayload,
    algorithm_id: str,
    frames: list[FramePayload],
) -> None:
    """Store frames under `algorithm_id` in `frames_by_algorithm`.

    **Low-level helper. Prefer `stamp_server_post_run`** for
    server-side detection results — it stamps the
    `active_server_post_algorithm_id` pointer + the
    `config_used_by_algorithm` snapshot atomically so the
    `frames_server_post` / `server_post_config_used` computed-field
    projections stay coherent.

    Writes to a different algorithm id (e.g. v12 while v11 is still
    server_post-canonical) leave `active_server_post_algorithm_id`
    alone — they live only in the dict. The path-keyed projections
    keep surfacing whichever id is the current server_post pointer.
    """
    pitch.frames_by_algorithm[algorithm_id] = list(frames)


def stamp_server_post_run(
    pitch: PitchPayload,
    snapshot,
    frames: list[FramePayload],
) -> None:
    """Atomically stamp one server-side detection run onto a pitch:
    update `server_post_config_used` to the snapshot that produced
    these frames, then route the frames through `set_algorithm_frames`
    so the new algorithm's bucket in `frames_by_algorithm` is filled
    AND `frames_server_post` back-syncs (because the snapshot we just
    set declares this algorithm to be the current server_post slot).

    Phase 7 entry point for `routes/pitch.py::_run_server_detection`
    and `reprocess_sessions.py`. Callers must NOT split this into
    individual mutations: the mirror helpers run on every load /
    persist and would interpret an intermediate state (snapshot
    updated, frames not yet replaced) as canonical, mis-filing the
    previous algorithm's frames under the new id.

    Multi-algorithm semantics: a previous run under a DIFFERENT
    algorithm id leaves its frames in `frames_by_algorithm[<old id>]`
    untouched (union mirror preserves it). Only the algorithm id that
    matches the new snapshot is overwritten. So running v11 then v12
    leaves dict={v11: <v11 frames>, v12: <v12 frames>} and
    frames_server_post=<v12 frames> as the "current" surface.

    The non-runnable id `ios_capture_time` is rejected: it represents
    iOS-side capture-time detection (read-only) and has special
    storage semantics in `set_algorithm_frames` (back-syncs
    `frames_live`, NOT `frames_server_post`) that would corrupt the
    server-post slot if used here.
    """
    if snapshot.algorithm_id == IOS_CAPTURE_TIME_ALGORITHM_ID:
        raise ValueError(
            f"stamp_server_post_run rejects non-runnable id "
            f"{IOS_CAPTURE_TIME_ALGORITHM_ID!r}: server-post slot "
            "is reserved for runnable algorithms"
        )
    pitch.active_server_post_algorithm_id = snapshot.algorithm_id
    pitch.config_used_by_algorithm[snapshot.algorithm_id] = snapshot
    pitch.frames_by_algorithm[snapshot.algorithm_id] = list(frames)


def pitch_with_algorithm_frames(
    pitch: PitchPayload, algorithm_id: str,
) -> PitchPayload:
    """Algorithm-keyed counterpart to `pitch_with_path_frames`.
    Promotes the chosen algorithm to the active server_post slot on a
    clone so existing downstream consumers (reconstruct, ray builders)
    can read `frames_server_post` regardless of which algorithm
    produced the frames. The original pitch is unchanged."""
    clone = pitch.model_copy(deep=True)
    clone.active_server_post_algorithm_id = algorithm_id
    return clone
