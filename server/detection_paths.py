"""Frame-bucket and detection-path selection helpers.

Every pitch carries two parallel frame buckets (`frames_live`,
`frames_server_post`). These helpers decide, for any given
`(pitch, path)` pair, which bucket is the authoritative source and how to
project the pitch onto a single path for triangulation.

The pure helpers (`normalize_paths`, `has_server_frames`, `get_path_frames`,
`pitch_with_path_frames`) depend on nothing and are safe to call anywhere.
The state-dependent helper (`paths_for_pitch`) reads via the public State
accessors `session_paths_for` / `default_detection_paths`.
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
