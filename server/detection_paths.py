"""Frame-bucket and detection-path selection helpers.

Every pitch carries two parallel frame buckets (`frames_live`,
`frames_server_post`). These helpers decide, for any given `(pitch, path)`
pair, which bucket is the authoritative source and how to project the
pitch onto a single path for triangulation. The legacy flat `frames` field
is gone; `frames_server_post` alone holds server-side detections (with a
wire-compat alias on input).

The pure helpers (`normalize_paths`, `has_server_frames`) depend on nothing
and are safe to call anywhere. The state-dependent helpers
(`paths_for_pitch`, `get_path_frames`, `pitch_with_path_frames`) read
`state._current_session` / `state._last_ended_session` /
`state._runtime_settings` under `state._lock`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schemas import DetectionPath, FramePayload, PitchPayload

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
    with state._lock:
        for session in (state._current_session, state._last_ended_session):
            if session is not None and session.id == pitch.session_id:
                return set(session.paths)
        return set(state._runtime_settings.default_paths)


def get_path_frames(
    state: "State", pitch: PitchPayload, path: DetectionPath
) -> list[FramePayload]:
    if path == DetectionPath.live:
        return list(pitch.frames_live)
    return list(pitch.frames_server_post)


def pitch_with_path_frames(
    state: "State",
    pitch: PitchPayload,
    path: DetectionPath,
) -> PitchPayload:
    """Return a deep-copy pitch whose `frames_server_post` is the projection
    of the requested path's frames. `pairing.triangulate_cycle` always reads
    `frames_server_post`, so callers use this to run triangulation against
    whichever detection stream they care about without touching the
    authoritative pitch record."""
    clone = pitch.model_copy(deep=True)
    if path == DetectionPath.live:
        clone.frames_server_post = list(pitch.frames_live)
    else:
        clone.frames_server_post = get_path_frames(state, pitch, DetectionPath.server_post)
    return clone
