"""Session result builder and triangulation coordinator.

`rebuild_result_for_session` is the authoritative constructor for
`SessionResult` — events / viewer all read it. It combines per-pipeline
frame counts, triangulation output (live, server_post), sync validation,
and legacy `points` semantics into one immutable-ish snapshot.

Lock discipline: `State._lock` is a `threading.Lock` (non-reentrant). The
two-phase pattern here — snapshot pitches / live / session under the lock,
then call path selection / triangulation / sync validation **outside** it
— is load-bearing; each of those downstream helpers re-acquires the lock
internally. Do not fold them into the outer `with state._lock:` block or
the process will deadlock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from algorithms import IOS_CAPTURE_TIME, cost_threshold_for_algorithm
from detection_paths import (
    algorithm_id_for_path,
    get_path_frames,
    normalize_paths,
    paths_for_pitch,
    pitch_with_algorithm_frames,
    pitch_with_path_frames,
)
from pairing import scale_pitch_to_video_dims, triangulate_all_pairs, triangulate_pair_rays
from schemas import (
    DetectionPath,
    FramePayload,
    PitchPayload,
    SegmentRecord,
    SessionResult,
    TriangulatedPoint,
    _DEFAULT_PATHS,
)
from segmenter import Segment, find_segments

if TYPE_CHECKING:
    from state import State


logger = logging.getLogger(__name__)


_FROZEN_USED_FIELDS = (
    "live_config_used",
    "server_post_config_used",
)


class PitchConfigDivergenceError(ValueError):
    """Raised when 2+ cams hold distinct non-None frozen-config snapshots
    for the same field. Caller catches → writes a SessionResult
    abort_reason so the operator sees the divergence explicitly
    (CLAUDE.md bans silent A-wins fallback)."""


class ServerPostPointerMismatchError(ValueError):
    """Raised when 2+ cams hold distinct non-None
    `active_server_post_algorithm_id` values. Caller catches → writes
    a SessionResult abort_reason."""


def aggregate_pitch_used_configs(
    pitches_by_cam: dict[str, PitchPayload],
    sid: str,
) -> dict[str, object | None]:
    """Aggregate the per-pitch per-path frozen config snapshots across
    every participating camera. Policy: every non-None snapshot must
    agree; raise `PitchConfigDivergenceError` on divergence (CLAUDE.md
    no-silent-fallback). Returns one canonical value per field, or
    None if every cam's snapshot is None for that field.

    Shared by `rebuild_result` and reprocess so both paths enforce
    identical aggregation across N cameras."""
    out: dict[str, object | None] = {}
    for field_name in _FROZEN_USED_FIELDS:
        seen: dict[str, object] = {}
        for cam in sorted(pitches_by_cam):
            v = getattr(pitches_by_cam[cam], field_name)
            if v is not None:
                seen[cam] = v
        if not seen:
            out[field_name] = None
            continue
        chosen = next(iter(seen.values()))
        for v in seen.values():
            if v != chosen:
                per_cam = ", ".join(f"{c}={v!r}" for c, v in seen.items())
                raise PitchConfigDivergenceError(
                    f"session {sid} {field_name} diverged across cams: {per_cam}"
                )
        out[field_name] = chosen
    return out


def _stamp_frozen_config_on_result(
    result: SessionResult,
    pitches_by_cam: dict[str, PitchPayload],
) -> None:
    """Mirror per-pitch per-path frozen snapshots onto the SessionResult's
    canonical `config_used_by_algorithm` dict + `active_server_post_algorithm_id`
    pointer. Aggregation lives in `aggregate_pitch_used_configs` so
    reprocess + rebuild share one source of truth. Raises on cross-cam
    divergence — caller decides whether to translate to abort_reason."""
    used = aggregate_pitch_used_configs(pitches_by_cam, result.session_id)
    live_snap = used.get("live_config_used")
    if live_snap is not None:
        result.config_used_by_algorithm[IOS_CAPTURE_TIME] = live_snap
    server_snap = used.get("server_post_config_used")
    if server_snap is not None:
        result.config_used_by_algorithm[server_snap.algorithm_id] = server_snap
        result.active_server_post_algorithm_id = server_snap.algorithm_id


def stamp_active_pointer_projection(
    result: SessionResult,
    pitches_by_cam: dict[str, PitchPayload],
) -> None:
    """Fast-path companion to `rebuild_result_for_session` — re-stamps
    the four derived projections that change when the active
    server_post pointer flips, **without** rerunning `triangulate_pair`.

    Updates: `active_server_post_algorithm_id`, frozen-config snapshot
    pointer (`config_used_by_algorithm` entry for the new alg), legacy
    `triangulated` / `points` / `segments`, and `segments_by_algorithm`.
    Cached `triangulated_by_algorithm` buckets are invariant under
    pointer flips (frames + calibration + emit ceilings unchanged), so
    no per-bucket triangulation is needed.

    Pre-condition: every participating pitch's
    `active_server_post_algorithm_id` is already flipped to the target
    alg, and `result` is a private copy (use `model_copy(deep=True)` on
    the cached result before calling — `stamp_segments_on_result`
    mutates dicts in-place).

    Called from `state.set_active_server_post_algorithm`. The slow path
    (`rebuild_result_for_session`) still applies on cache miss, mono
    session, or `sync_error` — see that caller for the dispatch.

    Raises `PitchConfigDivergenceError` if any cam's frozen-config
    snapshot diverges from the others; the fast-path caller falls
    through to the canonical rebuild on this error.
    """
    # Match rebuild's `empty_result_for_session` semantics for the
    # frozen-config dict: clear before stamping so the prior active
    # alg's config snapshot doesn't accumulate across switches.
    # `_stamp_frozen_config_on_result` re-populates `live_config` from
    # `pitch.live_config_used` and the new active alg's snap.
    result.config_used_by_algorithm.clear()
    _stamp_frozen_config_on_result(result, pitches_by_cam)
    stamp_segments_on_result(
        result, legacy_points_path=DetectionPath.server_post,
    )


def _resolve_server_post_alg_for_result(
    pitches_by_cam: dict[str, PitchPayload],
) -> str | None:
    """Resolve the server_post algorithm id this result should pin by
    iterating every participating cam's `active_server_post_algorithm_id`:

      - all pointers None → return None (live-only / never ran server_post)
      - exactly one distinct non-None value → return it
      - 2+ distinct values → raise `ServerPostPointerMismatchError`

    Caller catches → records to `result.abort_reasons` so the operator
    sees an explicit signal instead of a silent A-wins fallback.
    """
    distinct: dict[str, str] = {}
    for cam in sorted(pitches_by_cam):
        v = pitches_by_cam[cam].active_server_post_algorithm_id
        if v is not None:
            distinct[cam] = v
    values = set(distinct.values())
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    per_cam = ", ".join(f"{c}={a}" for c, a in distinct.items())
    raise ServerPostPointerMismatchError(
        f"server_post pointer mismatch across cams: {per_cam}"
    )


def triangulate_pair(
    state: "State",
    a: PitchPayload,
    b: PitchPayload,
    *,
    source: str = "server",
) -> list[TriangulatedPoint]:
    """Scale each pitch's intrinsics + homography to its MOV's actual pixel
    grid (using the cached calibration snapshot as the reference resolution)
    and then triangulate. When no snapshot is cached for a camera the scale
    factor falls back to 1.0 and the pitch is passed through unchanged — the
    legacy behaviour for pre-resolution-picker builds that always recorded
    at the calibration resolution.

    `source` picks the detection stream (`"server"` default reads
    `pitch.frames_server_post`).

    Triangulate emits the full set under hard ceilings; per-algorithm
    cost gate + operator gap gate apply downstream in
    `_passes_stamped_filter`."""
    with state._lock:
        cal_a = state._calibration_store.get(a.camera_id)
        cal_b = state._calibration_store.get(b.camera_id)
    a_scaled = scale_pitch_to_video_dims(
        a,
        (cal_a.image_width_px, cal_a.image_height_px) if cal_a else None,
    )
    b_scaled = scale_pitch_to_video_dims(
        b,
        (cal_b.image_width_px, cal_b.image_height_px) if cal_b else None,
    )
    return triangulate_pair_rays(a_scaled, b_scaled, source=source)


def pitches_by_cam_for_session(
    state: "State", session_id: str, expected_cams: list[str],
) -> dict[str, PitchPayload]:
    """Snapshot every camera's PitchPayload for `session_id`. The N-cam
    replacement for the old `(a, b) = state.pitches.get(("A", sid)),
    state.pitches.get(("B", sid))` pattern. Iterates `expected_cams` so a
    third (or fourth) phone whose pitch landed via `/pitch` is picked up
    by every result rebuild downstream.

    Caller takes `state._lock` and MUST pass a precomputed
    `expected_cams` (from `state.expected_camera_ids()`, resolved BEFORE
    acquiring the lock). `expected_camera_ids()` acquires `_lock`
    internally and `_lock` is non-reentrant — resolving it inside this
    function while the caller holds the lock self-deadlocks."""
    out: dict[str, PitchPayload] = {}
    for cam in expected_cams:
        p = state.pitches.get((cam, session_id))
        if p is not None:
            out[cam] = p
    return out


def triangulate_all_pairs_for_session(
    state: "State",
    pitches_by_cam: dict[str, PitchPayload],
    *, source: str = "server",
) -> tuple[list[TriangulatedPoint], list[tuple[str, str, str]]]:
    """N-cam triangulation wrapper. Scales each pitch's intrinsics +
    homography to its MOV grid (existing `scale_pitch_to_video_dims`),
    delegates to `pairing.triangulate_all_pairs` for the C(N,2) fan-out,
    and returns `(flat_points, skipped_pairs)`:
      - `flat_points`: every pair's points flattened + sorted by
        `t_rel_s`. Each point carries its own `pair_key` (stamped by
        `triangulate_pair_rays`).
      - `skipped_pairs`: `(cam_i, cam_j, reason)` for pairs dropped
        because a cam lacked calibration. Caller records these to
        `SessionResult.abort_reasons` (no silent zero-fill per CLAUDE.md).

    N=2 collapses to one pair, producing output identical to the
    pre-N-cam single-pair `triangulate_pair` call (modulo the `pair_key`
    field already required since Phase 3a)."""
    scaled: dict[str, PitchPayload] = {}
    with state._lock:
        cals = {cam: state._calibration_store.get(cam) for cam in pitches_by_cam}
    for cam, pitch in pitches_by_cam.items():
        cal = cals.get(cam)
        dims = (cal.image_width_px, cal.image_height_px) if cal else None
        scaled[cam] = scale_pitch_to_video_dims(pitch, dims)
    per_pair, skipped = triangulate_all_pairs(scaled, source=source)
    flat: list[TriangulatedPoint] = []
    for pts in per_pair.values():
        flat.extend(pts)
    flat.sort(key=lambda p: p.t_rel_s)
    return flat, skipped


def _triangulate_non_current_algorithms(
    state: "State",
    pitches_by_cam: dict[str, PitchPayload],
    sync_error: str | None,
    result: SessionResult,
) -> None:
    """Phase 7 multi-algorithm result builder, N-cam edition.

    The path-loop in `rebuild_result_for_session` triangulates only
    the *current* server_post slot (whatever
    `server_post_config_used.algorithm_id` names), feeding it into
    `triangulated_by_path["server_post"]`. The dict mirror fans that
    one entry into `triangulated_by_algorithm[<current_alg>]`.

    But Phase 7's `stamp_server_post_run` keeps history: running v11
    then v12 leaves *both* algorithms' frames in
    `pitch.frames_by_algorithm`. This helper triangulates each
    non-current algorithm bucket across ALL cams and writes the flat
    multi-pair point list into `result.triangulated_by_algorithm[<alg_id>]`
    so the events list, viewer, and any future Phase-8 N-track UI can
    read v11 trajectories without re-running detection.

    Skipped buckets:
    - `ios_capture_time` (the live data source) — already surfaced via
      the live aggregator; not a server-side triangulation target.
    - The *current* server_post alg — already handled by the path-loop.
      Considered "current" if ANY cam's snapshot names it (union across
      pitches_by_cam), so a partial-failure mismatch among 2+ cams
      skips all of those algs from this helper, leaving the path-loop's
      mixed pairing as the sole — and visibly logged — source of those
      frames.
    """
    if sync_error is not None or len(pitches_by_cam) < 2:
        return

    # Resolve each cam's current server_post alg pointer. Pointer may be
    # None on a session that only ever ran live (no server_post run);
    # `algorithm_id_for_path` raises rather than silently fall back per
    # CLAUDE.md, so we resolve explicitly with a None-sentinel that the
    # skip-set logic below handles cleanly.
    current_alg_by_cam: dict[str, str | None] = {}
    for cam, pitch in pitches_by_cam.items():
        if pitch.active_server_post_algorithm_id is not None:
            current_alg_by_cam[cam] = algorithm_id_for_path(
                pitch, DetectionPath.server_post,
            )
        else:
            current_alg_by_cam[cam] = None
    declared = {a for a in current_alg_by_cam.values() if a is not None}
    if len(declared) > 1:
        # 2+ cams disagree on which server_post alg is the current one —
        # path-loop will pair frames across algorithms (mixed); operator
        # action to recover is to rerun /run_server_post on every cam
        # with the same preset.
        per_cam = ", ".join(
            f"{c}={alg or 'none'}" for c, alg in sorted(current_alg_by_cam.items())
        )
        logger.warning(
            "session %s server_post algorithm mismatch %s — path-loop "
            "will pair frames across algorithms; rerun /run_server_post "
            "on every cam to recover",
            result.session_id, per_cam,
        )
    current_algs: set[str] = declared
    candidate_algs: set[str] = set()
    for pitch in pitches_by_cam.values():
        candidate_algs |= set(pitch.frames_by_algorithm)
    for alg_id in sorted(candidate_algs):
        if alg_id == IOS_CAPTURE_TIME or alg_id in current_algs:
            continue
        # Build a per-cam dict containing only cams whose pitch has this
        # alg's frames — uncalibrated or no-frames cams drop out
        # naturally, leaving triangulate_all_pairs_for_session to fan out
        # over the survivors. A single survivor yields an empty list (no
        # peer to pair against), which we skip by treating as "no
        # contribution to this bucket".
        alg_pitches: dict[str, PitchPayload] = {}
        frame_counts: dict[str, int] = {}
        for cam, pitch in pitches_by_cam.items():
            frames = pitch.frames_by_algorithm.get(alg_id, [])
            if not frames:
                continue
            alg_pitches[cam] = pitch_with_algorithm_frames(pitch, alg_id)
            frame_counts[cam] = len(frames)
        if len(alg_pitches) < 2:
            continue
        try:
            pts, skipped = triangulate_all_pairs_for_session(
                state, alg_pitches, source="server",
            )
        except Exception as exc:
            result.abort_reasons[f"alg:{alg_id}"] = (
                f"{type(exc).__name__}: {exc}"
            )
            continue
        for ci, cj, reason in skipped:
            result.abort_reasons[f"missing_calibration:{alg_id}:{ci}-{cj}"] = reason
        # triangulate_all_pairs_for_session already returns t_rel-sorted
        # over the flattened multi-pair list — the invariant downstream
        # consumers (set_active_server_post_algorithm fast path) rely on.
        result.triangulated_by_algorithm[alg_id] = pts
        result.algorithms_completed.add(alg_id)
        result.frame_counts_by_algorithm[alg_id] = frame_counts


def live_frames_for_camera_locked(
    state: "State", session_id: str, camera_id: str
) -> list[FramePayload]:
    live = state._live_pairings.get(session_id)
    if live is None:
        return []
    return live.frames_for_camera(camera_id)


def session_sync_id_locked(state: "State", session_id: str) -> str | None:
    session = state._lookup_session_locked(session_id)
    if session is not None:
        return session.sync_id
    return None


def validate_session_sync(
    state: "State", pitches_by_cam: dict[str, PitchPayload],
) -> str | None:
    """N-cam sync validator. Every participating cam's pitch must carry
    the same `sync_id`, the same anchor presence, and (if the armed
    session has a sync_id stamped) match it. Returns one of the stable
    error strings (`"no time sync"`, `"sync id missing"`, `"sync id
    mismatch"`, `"sync id mismatch for armed session"`) so SessionResult.error
    consumers can pattern-match. Mono session (1 cam) returns None —
    single cam has no pair to validate against; rebuild collapses mono
    to no-triangulation regardless."""
    if len(pitches_by_cam) < 2:
        return None
    if any(p.sync_anchor_timestamp_s is None for p in pitches_by_cam.values()):
        return "no time sync"
    if any(p.sync_id is None for p in pitches_by_cam.values()):
        return "sync id missing"
    sync_ids = {p.sync_id for p in pitches_by_cam.values()}
    if len(sync_ids) > 1:
        return "sync id mismatch"
    chosen_sync_id = next(iter(sync_ids))
    first_sid = next(iter(pitches_by_cam.values())).session_id
    with state._lock:
        expected_sync_id = session_sync_id_locked(state, first_sid)
    if expected_sync_id is not None and chosen_sync_id != expected_sync_id:
        return "sync id mismatch for armed session"
    return None


def empty_result_for_session(
    state: "State",
    session_id: str,
    *,
    cameras_received: dict[str, bool],
) -> SessionResult:
    """Lock-free pure constructor — do not wrap the call in `state._lock`;
    the only state read is `state.now()`, which is an injectable
    callable."""
    return SessionResult(
        session_id=session_id,
        cameras_received=dict(cameras_received),
        solved_at=state.now(),
    )


def rebuild_result_for_session(state: "State", session_id: str) -> SessionResult:
    # Resolve expected cams BEFORE taking the lock — `expected_camera_ids()`
    # acquires the non-reentrant `_lock` internally, so calling it inside
    # the lock block below self-deadlocks.
    expected_cams = state.expected_camera_ids()
    with state._lock:
        pitches_by_cam = pitches_by_cam_for_session(state, session_id, expected_cams)
        live = state._live_pairings.get(session_id)
        session_obj = state._lookup_session_locked(session_id)
        pairing_tuning = state._pairing_tuning

    result = empty_result_for_session(
        state,
        session_id,
        cameras_received={
            cam: cam in pitches_by_cam for cam in expected_cams
        },
    )
    result.gap_threshold_m = pairing_tuning.gap_threshold_m
    # Aggregate every cam's last-run timestamps — the most recent one
    # wins so a partial rerun (only one cam's MOV reprocessed) still
    # advances the session's "last server_post" age.
    server_post_ts = [
        p.server_post_ran_at for p in pitches_by_cam.values()
        if p.server_post_ran_at is not None
    ]
    if server_post_ts:
        result.server_post_ran_at = max(server_post_ts)

    candidate_paths: set[DetectionPath] = set()
    if session_obj is not None:
        candidate_paths |= set(session_obj.paths)
    for pitch in pitches_by_cam.values():
        candidate_paths |= paths_for_pitch(state, pitch)
        # Auto-include server_post when the bucket is populated so
        # reprocessing can flow through even without an explicit paths
        # snapshot on the pitch JSON.
        if pitch.frames_server_post:
            candidate_paths.add(DetectionPath.server_post)
        # Same for live: persisted `frames_live` (from an old WS
        # streaming run, or `persist_live_frames`) is enough to drive
        # the live triangulation path on rebuild even after restart.
        if pitch.frames_live:
            candidate_paths.add(DetectionPath.live)
    live_frame_counts = live.frame_counts_snapshot() if live is not None else {}
    if any(c for c in live_frame_counts.values()):
        candidate_paths.add(DetectionPath.live)
    if not candidate_paths:
        candidate_paths = set(_DEFAULT_PATHS)
    legacy_points_path = _legacy_points_path(candidate_paths)

    # Stamp the active server_post pointer early — the path-loop below
    # needs it to know which `triangulated_by_algorithm` bucket to fill.
    # Divergence across cams becomes an explicit abort_reason instead of
    # silent A-wins (CLAUDE.md no-silent-fallback).
    try:
        srv_alg = _resolve_server_post_alg_for_result(pitches_by_cam)
    except ServerPostPointerMismatchError as exc:
        result.abort_reasons["server_post_pointer_mismatch"] = str(exc)
        srv_alg = None
    if srv_alg is not None:
        result.active_server_post_algorithm_id = srv_alg

    if live is not None:
        with live._lock:
            triangulated_copy = list(live.triangulated)
            abort_reasons_copy = dict(live.abort_reasons)
        live_counts = {
            cam: int(count) for cam, count in live_frame_counts.items() if count
        }
        if live_counts:
            result.frame_counts_by_algorithm[IOS_CAPTURE_TIME] = live_counts
        if triangulated_copy:
            result.triangulated_by_algorithm[IOS_CAPTURE_TIME] = triangulated_copy
            result.algorithms_completed.add(IOS_CAPTURE_TIME)
        if abort_reasons_copy:
            result.abort_reasons.update(
                {f"live:{cam}": why for cam, why in abort_reasons_copy.items()}
            )

    sync_error = validate_session_sync(state, pitches_by_cam)
    if sync_error is not None:
        result.error = sync_error

    mono_session = len(pitches_by_cam) < 2
    for path in sorted(candidate_paths, key=lambda p: p.value):
        # When the streaming live aggregator already populated this path
        # above, skip — it's authoritative. Otherwise (rebuild for a session
        # restored from disk after server restart, or an offline replay),
        # fall through to the same triangulation flow used by other paths
        # so persisted `frames_live` can still drive the live trajectory.
        if path == DetectionPath.live and live is not None:
            continue
        frames_by_cam: dict[str, list[FramePayload]] = {
            cam: get_path_frames(pitch, path)
            for cam, pitch in pitches_by_cam.items()
        }
        frame_counts: dict[str, int] = {
            cam: len(frames) for cam, frames in frames_by_cam.items() if frames
        }
        path_alg = (
            IOS_CAPTURE_TIME if path == DetectionPath.live
            else result.active_server_post_algorithm_id
        )
        if path_alg is None:
            # server_post path with no resolvable algorithm. After the
            # N-cam upgrade of `_resolve_server_post_alg_for_result`,
            # this only fires when (a) divergence was caught + recorded
            # in abort_reasons["server_post_pointer_mismatch"] above,
            # or (b) no cam has set the pointer at all. Either way the
            # bucket is unknown — skip rather than mis-file. If frames
            # exist in (b), that's an internal invariant violation
            # (writer should always stamp the pointer when populating
            # frames_server_post); assert to surface it loudly per
            # CLAUDE.md.
            assert not frame_counts or (
                "server_post_pointer_mismatch" in result.abort_reasons
            ), (
                f"session {result.session_id}: server_post frames exist "
                f"but no active_server_post_algorithm_id pointer "
                f"(frame_counts={frame_counts})"
            )
            continue
        if frame_counts:
            result.frame_counts_by_algorithm[path_alg] = frame_counts

        if sync_error is None and len(frame_counts) >= 2:
            # Cams with no frames on this path drop out of the per-path
            # pitches_by_cam; only the ones with real frames contribute
            # to fan-out. If fewer than 2 cams have frames, no pair to
            # triangulate — fall through to algorithms_completed below.
            path_pitches: dict[str, PitchPayload] = {
                cam: pitch_with_path_frames(pitches_by_cam[cam], path)
                for cam in frame_counts
            }
            try:
                pts, skipped = triangulate_all_pairs_for_session(
                    state, path_pitches, source="server",
                )
            except Exception as exc:
                result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                continue
            for ci, cj, reason in skipped:
                result.abort_reasons[
                    f"missing_calibration:{path.value}:{ci}-{cj}"
                ] = reason
            result.triangulated_by_algorithm[path_alg] = pts
            result.algorithms_completed.add(path_alg)
        elif mono_session and frame_counts:
            # Single-camera sessions cannot triangulate, but once the path
            # has finalized frames on the sole uploaded camera it should be
            # surfaced as completed instead of lingering in "stopped".
            result.algorithms_completed.add(path_alg)

    # Phase 7 multi-algorithm: triangulate every algorithm bucket
    # present in any cam's `pitch.frames_by_algorithm` so a v11 → v12
    # rerun leaves both algorithms' trajectories surfaced on the result.
    # The path-loop above already covers the *current* server_post
    # alg via the after-validator mirror; this loop handles the
    # non-current ones (the "history" the dict accumulates). Live
    # path is excluded — `ios_capture_time` is already surfaced via
    # the live aggregator at the top of this function.
    _triangulate_non_current_algorithms(
        state, pitches_by_cam, sync_error, result,
    )

    authority: list[TriangulatedPoint] = []
    for path in (
        DetectionPath.server_post.value,
        DetectionPath.live.value,
    ):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority

    if not result.triangulated and result.error is None and pitches_by_cam:
        if result.abort_reasons:
            result.aborted = True
        elif len(pitches_by_cam) >= 2:
            result.error = "no detection completed"
    try:
        _stamp_frozen_config_on_result(result, pitches_by_cam)
    except PitchConfigDivergenceError as exc:
        result.abort_reasons["frozen_config_diverged"] = str(exc)
    stamp_segments_on_result(result, legacy_points_path=legacy_points_path)
    return result


def _passes_stamped_filter(
    p: TriangulatedPoint,
    *,
    cost_threshold: float,
    gap_threshold_m: float,
) -> bool:
    """Stamped-tuning filter applied to TriangulatedPoint before segmenter
    consumption (and mirrored client-side by the viewer's `Gap ≤` slider).
    A point passes when:
      - `residual_m ≤ gap_threshold_m`, AND
      - `max(cost_a, cost_b) ≤ cost_threshold` (None costs treated as
        "no info" → pass; matches the JS legacy fall-through semantics).

    `cost_threshold` is supplied by the caller (resolved per algorithm
    via `algorithms.cost_threshold_for_algorithm`); `gap_threshold_m`
    comes from `SessionResult.gap_threshold_m` (operator slider).
    """
    if p.residual_m > gap_threshold_m:
        return False
    cost_max = -1.0
    if p.cost_a is not None:
        cost_max = max(cost_max, p.cost_a)
    if p.cost_b is not None:
        cost_max = max(cost_max, p.cost_b)
    if cost_max < 0:
        return True  # both costs None → no cost info → pass
    return cost_max <= cost_threshold


def _algorithm_id_for_result_path(
    result: SessionResult, path: str
) -> str | None:
    """Map a `triangulated_by_path` key to its algorithm id. Live path
    → `IOS_CAPTURE_TIME`; server_post → `result.active_server_post_algorithm_id`
    (set by `_stamp_frozen_config_on_result` /
    `_resolve_server_post_alg_for_result`). Returns `None` for the
    server_post path when no active pointer is set (live-only session
    with no server_post run); callers must explicitly skip `None`
    rather than guess a bucket per CLAUDE.md no-silent-fallback.
    Raises `ValueError` for unknown path strings."""
    if path == DetectionPath.live.value:
        return IOS_CAPTURE_TIME
    if path == DetectionPath.server_post.value:
        return result.active_server_post_algorithm_id
    raise ValueError(f"unknown result path key {path!r}")


def _legacy_points_path(candidate_paths: set[DetectionPath]) -> DetectionPath | None:
    """Path used by legacy `result.points` / `result.segments`.

    Explicit branch order is intentional: if server_post was requested but
    produced no points, the legacy surface must be empty instead of silently
    substituting live. That keeps live-vs-server_post comparisons honest.
    """
    if DetectionPath.server_post in candidate_paths:
        return DetectionPath.server_post
    if DetectionPath.live in candidate_paths:
        return DetectionPath.live
    return None


def stamp_segments_on_result(
    result: SessionResult,
    *,
    legacy_points_path: DetectionPath | None = None,
) -> None:
    """Run `find_segments` on the stamped-filter SUBSET of every available
    path and write both `result.segments_by_path` and the legacy
    single-surface `result.segments`. Idempotent — overwrites whatever
    was there.

    Architecture: `result.triangulated` / `result.triangulated_by_path`
    carry the FULL emitted set (every candidate pair under pairing's
    absolute emit ceiling). The segmenter runs against the stamped
    subset — per-algorithm `cost_threshold` (from
    `algorithms.cost_threshold_for_algorithm`) plus operator
    `gap_threshold_m` (from `SessionResult`). Viewer slider mirrors the
    gap predicate client-side; cost is read-only. This decouples "what
    the operator sees" from "what gets fit".

    `result.points` / `result.segments` follow `legacy_points_path` when
    the caller supplies one; this prevents a missing server_post surface from
    silently substituting live points after `rebuild_result_for_session`
    already selected the no-fallback legacy path.

    Sorts every persisted path list by `t_rel_s` BEFORE running the
    segmenter so `Segment.original_indices` is a stable index into a
    time-sorted list.

    `result.gap_threshold_m` may be None only on legacy/manual callers
    that bypass `rebuild_result_for_session` /
    `recompute_result_for_session`. In that case fall back to
    `PairingTuning.default()` so those test-only / migration surfaces
    still render deterministically. Cost is per-algorithm (looked up
    via `_algorithm_id_for_result_path` → `cost_threshold_for_algorithm`),
    so it never has a "missing" branch.

    Empty `triangulated_by_path` ⇒ empty segments (no log noise;
    "nothing to fit" is not an error)."""
    if result.gap_threshold_m is None:
        from pairing_tuning import PairingTuning
        gap = PairingTuning.default().gap_threshold_m
    else:
        gap = result.gap_threshold_m

    path_priority = (
        DetectionPath.server_post.value,
        DetectionPath.live.value,
    )
    # Reset segments_by_algorithm: drop ALL existing segment buckets
    # so a previous run's stale bucket (e.g. v11 segments left behind
    # when the operator switched the server_post algorithm to v12)
    # does not orphan on disk. We only re-populate live + current
    # server_post below — non-current algorithm buckets in
    # `triangulated_by_algorithm` (multi-alg history from
    # `_triangulate_non_current_algorithms`) are NOT segmented here;
    # that surface stays out of scope until a future N-track UI
    # asks for it.
    result.segments_by_algorithm.clear()

    for path in path_priority:
        pts = result.triangulated_by_path.get(path, [])
        if not pts:
            continue
        path_alg = _algorithm_id_for_result_path(result, path)
        if path_alg is None:
            # server_post path has triangulated points but no pointer
            # — that violates the writer-side invariant (path-loop
            # already gates on `path_alg is None`). Belt-and-braces.
            continue
        path_cost = cost_threshold_for_algorithm(path_alg)
        pts_sorted = sorted(pts, key=lambda p: p.t_rel_s)
        # Persist time-sorted order on the canonical dict so reload
        # sees a stable index space for `Segment.original_indices`.
        result.triangulated_by_algorithm[path_alg] = pts_sorted
        fit_input: list[TriangulatedPoint] = []
        fit_to_full: list[int] = []
        for full_idx, p in enumerate(pts_sorted):
            if _passes_stamped_filter(p, cost_threshold=path_cost, gap_threshold_m=gap):
                fit_input.append(p)
                fit_to_full.append(full_idx)
        segs, _pts_sorted = find_segments(fit_input)
        records: list[SegmentRecord] = []
        for s in segs:
            rec = _segment_record_from_segment(s)
            rec.original_indices = [fit_to_full[i] for i in rec.original_indices]
            records.append(rec)
        result.segments_by_algorithm[path_alg] = records

    authority_path: str | None = None
    for path in path_priority:
        pts = result.triangulated_by_path.get(path, [])
        if pts:
            authority_path = path
            break
    if authority_path is None:
        result.triangulated = []
        result.points = []
        result.segments = []
        return
    authority_pts = result.triangulated_by_path[authority_path]
    result.triangulated = authority_pts
    legacy_path = (
        legacy_points_path.value
        if legacy_points_path is not None
        else authority_path
    )
    result.points = list(result.triangulated_by_path.get(legacy_path, []))
    result.segments = list(result.segments_by_path.get(legacy_path, []))


def _segment_record_from_segment(seg: Segment) -> SegmentRecord:
    return SegmentRecord(
        indices=list(seg.indices),
        original_indices=list(seg.original_indices),
        p0=seg.p0.tolist(),
        v0=seg.v0.tolist(),
        t_anchor=float(seg.t_anchor),
        t_start=float(seg.t_start),
        t_end=float(seg.t_end),
        rmse_m=float(seg.rmse_m),
        speed_kph=float(seg.speed_kph),
    )


def recompute_result_for_session(
    state: "State",
    session_id: str,
    *,
    gap_threshold_m: float,
) -> SessionResult:
    """Re-run pairing fan-out + segmenter on this session's already-
    detected frames using a per-session `gap_threshold_m` override.

    Differences from `rebuild_result_for_session`:
      - Always re-triangulates the live path (does NOT reuse
        `LivePairingSession.triangulated`, which was built incrementally
        under the old/global tuning at ingest time).
      - Stamps the chosen gap value into `SessionResult.gap_threshold_m`
        for viewer slider re-init. Cost is per-algorithm (looked up via
        `algorithms.cost_threshold_for_algorithm`) and not stamped.

    Caller is the `POST /sessions/{sid}/recompute` route. No MOV decode,
    no HSV — candidates are read from the persisted `frames_live` /
    `frames_server_post` directly. Sub-second on a typical session."""
    # Resolve expected cams before the lock (see rebuild_result_for_session).
    expected_cams = state.expected_camera_ids()
    with state._lock:
        pitches_by_cam = pitches_by_cam_for_session(state, session_id, expected_cams)

    result = empty_result_for_session(
        state,
        session_id,
        cameras_received={
            cam: cam in pitches_by_cam for cam in expected_cams
        },
    )
    result.gap_threshold_m = float(gap_threshold_m)

    server_post_ts = [
        p.server_post_ran_at for p in pitches_by_cam.values()
        if p.server_post_ran_at is not None
    ]
    if server_post_ts:
        result.server_post_ran_at = max(server_post_ts)

    candidate_paths: set[DetectionPath] = set()
    for pitch in pitches_by_cam.values():
        if pitch.frames_server_post:
            candidate_paths.add(DetectionPath.server_post)
        if pitch.frames_live:
            candidate_paths.add(DetectionPath.live)
    legacy_points_path = _legacy_points_path(candidate_paths)

    sync_error = validate_session_sync(state, pitches_by_cam)
    if sync_error is not None:
        result.error = sync_error

    # Stamp the active server_post pointer up front so the path-loop
    # below can resolve `server_post → algorithm_id` deterministically.
    # Divergence becomes an explicit abort_reason (CLAUDE.md no-silent-fallback).
    try:
        srv_alg = _resolve_server_post_alg_for_result(pitches_by_cam)
    except ServerPostPointerMismatchError as exc:
        result.abort_reasons["server_post_pointer_mismatch"] = str(exc)
        srv_alg = None
    if srv_alg is not None:
        result.active_server_post_algorithm_id = srv_alg

    if sync_error is None and len(pitches_by_cam) >= 2:
        for path in sorted(candidate_paths, key=lambda p: p.value):
            frames_by_cam = {
                cam: get_path_frames(pitch, path)
                for cam, pitch in pitches_by_cam.items()
            }
            frame_counts = {
                cam: len(frames) for cam, frames in frames_by_cam.items() if frames
            }
            if len(frame_counts) < 2:
                continue
            path_alg = (
                IOS_CAPTURE_TIME if path == DetectionPath.live
                else result.active_server_post_algorithm_id
            )
            if path_alg is None:
                # Same invariant as rebuild's path-loop: server_post
                # frames without a resolvable pointer means divergence
                # already raised + was caught above. Bucket is unknown
                # — skip rather than mis-file.
                assert (
                    "server_post_pointer_mismatch" in result.abort_reasons
                ), (
                    f"session {result.session_id}: server_post frames "
                    f"in recompute but no active pointer "
                    f"(frame_counts={frame_counts})"
                )
                continue
            result.frame_counts_by_algorithm[path_alg] = frame_counts
            path_pitches = {
                cam: pitch_with_path_frames(pitches_by_cam[cam], path)
                for cam in frame_counts
            }
            try:
                pts, skipped = triangulate_all_pairs_for_session(
                    state, path_pitches, source="server",
                )
            except Exception as exc:
                result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                continue
            for ci, cj, reason in skipped:
                result.abort_reasons[
                    f"missing_calibration:{path.value}:{ci}-{cj}"
                ] = reason
            result.triangulated_by_algorithm[path_alg] = pts
            result.algorithms_completed.add(path_alg)

    # Also triangulate non-current algorithm buckets so Recompute
    # preserves multi-algorithm history the same way `rebuild_result_for_session`
    # does — without this, recomputing with a v12 active pointer
    # would drop v11 trajectories from the result.
    _triangulate_non_current_algorithms(
        state, pitches_by_cam, sync_error, result,
    )

    authority: list[TriangulatedPoint] = []
    for path in (DetectionPath.server_post.value, DetectionPath.live.value):
        pts = result.triangulated_by_path.get(path)
        if pts:
            authority = pts
            break
    result.triangulated = authority

    if not result.triangulated and result.error is None and pitches_by_cam:
        if result.abort_reasons:
            result.aborted = True
        elif len(pitches_by_cam) >= 2:
            result.error = "no detection completed"
    try:
        _stamp_frozen_config_on_result(result, pitches_by_cam)
    except PitchConfigDivergenceError as exc:
        result.abort_reasons["frozen_config_diverged"] = str(exc)
    stamp_segments_on_result(result, legacy_points_path=legacy_points_path)
    return result


# Re-export `normalize_paths` so `State._normalize_paths` (and the handful
# of external callers that still reach for `state._normalize_paths`) can
# route through session_results without importing detection_paths
# separately.
__all__ = [
    "empty_result_for_session",
    "live_frames_for_camera_locked",
    "normalize_paths",
    "paths_for_pitch",
    "rebuild_result_for_session",
    "recompute_result_for_session",
    "session_sync_id_locked",
    "stamp_segments_on_result",
    "triangulate_pair",
    "validate_session_sync",
]
