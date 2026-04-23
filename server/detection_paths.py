"""Frame-bucket and detection-path selection helpers.

Every pitch carries three parallel frame buckets (`frames_live`,
`frames_on_device`/`frames_ios_post`, `frames_server_post`/`frames`). These
helpers decide, for any given `(pitch, path)` pair, which bucket is the
authoritative source and how to project the pitch onto a single path for
triangulation.

The pure helpers (`normalize_paths`, `has_on_device_frames`,
`has_server_frames`) depend on nothing and are safe to call anywhere. The
state-dependent helpers (`paths_for_pitch`, `get_path_frames`,
`pitch_with_path_frames`) read `state._current_session` /
`state._last_ended_session` / `state._runtime_settings` under
`state._lock`. When the SessionStore refactor lands, the state-touching
three will move with it (the pure three stay here).
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


def has_on_device_frames(pitch: PitchPayload) -> bool:
    """Dual-mode detection: if any pitch carries `frames_on_device`, the
    session was armed dual and we owe the caller a second triangulation
    pass over the iOS detection stream."""
    return bool(pitch and pitch.frames_on_device)


def has_server_frames(pitch: PitchPayload) -> bool:
    """True once the server-side MOV detection has populated `pitch.frames`.
    Used to gate `triangulate_pair(source="server")` so the early-surface
    path (record runs before detection finishes, with `frames=[]`) doesn't
    flag a spurious error — it just leaves `result.points=[]` until the
    background detect task updates the pitch and we re-record."""
    return bool(pitch and pitch.frames)


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
    if path == DetectionPath.ios_post:
        if pitch.frames_ios_post:
            return list(pitch.frames_ios_post)
        if pitch.frames_on_device:
            return list(pitch.frames_on_device)
        if DetectionPath.server_post not in paths_for_pitch(state, pitch) and pitch.frames:
            return list(pitch.frames)
        return []
    if pitch.frames_server_post:
        return list(pitch.frames_server_post)
    if pitch.frames and (
        pitch.frames_on_device or DetectionPath.ios_post not in paths_for_pitch(state, pitch)
    ):
        return list(pitch.frames)
    return []


def pitch_with_path_frames(
    state: "State",
    pitch: PitchPayload,
    path: DetectionPath,
) -> PitchPayload:
    clone = pitch.model_copy(deep=True)
    clone.frames_on_device = []
    if path == DetectionPath.live:
        clone.frames = list(pitch.frames_live)
    elif path == DetectionPath.ios_post:
        clone.frames = get_path_frames(state, pitch, DetectionPath.ios_post)
    else:
        clone.frames = get_path_frames(state, pitch, DetectionPath.server_post)
    return clone
