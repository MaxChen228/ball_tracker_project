"""Frame-bucket and detection-path selection helpers.

Every pitch carries two parallel frame buckets (`frames_live`,
`frames_server_post`). These helpers decide, for any given
`(pitch, path)` pair, which bucket is the authoritative source and how to
project the pitch onto a single path for triangulation.

The pure helpers (`normalize_paths`, `has_server_frames`, `get_path_frames`,
`pitch_with_path_frames`) depend on nothing and are safe to call anywhere.
The state-dependent helper (`paths_for_pitch`) reads via the public State
accessors `session_paths_for` / `default_detection_paths`.

Phase 6b adds algorithm-id-keyed peers (`get_algorithm_frames`,
`set_algorithm_frames`, `pitch_with_algorithm_frames`) for the
multi-algorithm refactor. Path-keyed helpers above remain for back-
compat — they delegate to the algorithm-keyed accessors via the
fixed `live → ios_capture_time` / `server_post → <stamped alg id>`
mapping. Phase 7's `POST /sessions/{sid}/runs/{algorithm_id}` endpoint
writes into the dict via `set_algorithm_frames`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schemas import (
    DetectionPath,
    FramePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    _LEGACY_PRE_SNAPSHOT_ALGORITHM_ID,
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
    clone = pitch.model_copy(deep=True)
    # Project the chosen bucket into `frames_server_post` so downstream
    # consumers (reconstruct.build_scene, ray builders) can read a single
    # field regardless of which path produced the frames. The clone's other
    # bucket is left intact since it isn't read past this point.
    clone.frames_server_post = get_path_frames(pitch, path)
    return clone


# --- Phase 6b: algorithm-id-keyed accessors ---------------------------------
#
# These read from / write to `pitch.frames_by_algorithm` directly. They
# are the API Phase 7's `POST /sessions/{sid}/runs/{algorithm_id}` will
# build on. Path-keyed helpers above keep working: their internal
# semantics are equivalent to calling the algorithm-keyed peer with
# the path's resolved algorithm id (`live → ios_capture_time`,
# `server_post → <whichever alg the server_post snapshot stamped>`).


def algorithm_id_for_path(pitch: PitchPayload, path: DetectionPath) -> str:
    """Resolve the algorithm id a path's frames should live under in
    `frames_by_algorithm`. `live` always maps to `ios_capture_time`
    (the iOS-side capture-time data source). `server_post` reads the
    stamped snapshot's `algorithm_id`, falling back to the legacy
    pre-snapshot bucket for pitches that predate snapshot
    persistence."""
    if path == DetectionPath.live:
        return IOS_CAPTURE_TIME_ALGORITHM_ID
    if pitch.server_post_config_used is not None:
        return pitch.server_post_config_used.algorithm_id
    return _LEGACY_PRE_SNAPSHOT_ALGORITHM_ID


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
    """**Low-level helper. Prefer `stamp_server_post_run`** for
    server-side detection results — it maintains the snapshot ↔
    frames invariant atomically. This function does NOT mutate
    `server_post_config_used`, so calling it on its own with an
    `algorithm_id` that doesn't match the current snapshot's id will
    leave the invariant `frames_server_post == frames produced under
    server_post_config_used` temporarily broken until the caller also
    updates the snapshot.

    Store frames under `algorithm_id` and keep the legacy old field
    in sync when applicable so existing path-keyed readers continue to
    see what they expect:

    - `ios_capture_time` → also writes `pitch.frames_live`
    - any other id whose frames are currently in the server_post slot
      (i.e. the pitch's `server_post_config_used.algorithm_id` matches,
      OR the pitch has no snapshot and we're writing under the legacy
      pre-snapshot bucket) → also writes `pitch.frames_server_post`

    Writes to a different algorithm id (e.g. v12 while v11 is still
    server_post-canonical) DO NOT touch `frames_server_post` — they
    live only in the dict. Phase 6b path-keyed readers will keep
    surfacing whichever id is the current server_post stamp.
    """
    pitch.frames_by_algorithm[algorithm_id] = list(frames)
    if algorithm_id == IOS_CAPTURE_TIME_ALGORITHM_ID:
        pitch.frames_live = list(frames)
        return
    snap = pitch.server_post_config_used
    server_post_alg = snap.algorithm_id if snap is not None else _LEGACY_PRE_SNAPSHOT_ALGORITHM_ID
    if algorithm_id == server_post_alg:
        pitch.frames_server_post = list(frames)


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
    pitch.server_post_config_used = snapshot
    pitch.config_used_by_algorithm[snapshot.algorithm_id] = snapshot
    set_algorithm_frames(pitch, snapshot.algorithm_id, frames)


def pitch_with_algorithm_frames(
    pitch: PitchPayload, algorithm_id: str,
) -> PitchPayload:
    """Algorithm-keyed counterpart to `pitch_with_path_frames`.
    Projects the chosen algorithm's frames into `frames_server_post`
    on a clone so existing downstream consumers (reconstruct,
    ray builders) read a single field regardless of which algorithm
    produced the frames."""
    clone = pitch.model_copy(deep=True)
    clone.frames_server_post = get_algorithm_frames(pitch, algorithm_id)
    return clone
