"""Live + server_post detection state helpers.

Methods touching the live pairing buffer (`State._live_pairings`),
the server_post algorithm pointer / frozen config snapshot, and the
"missing calibration for live" telemetry. Pulled out of `state.py` to
keep that module focused on the orchestration core.

Mirrors the free-function + State-as-facade pattern (state_events.py,
session_results.py, status_view.py). All attributes still live on
`State`; helpers read/write via `state._xxx` so the public API surface
is unchanged.

Lock discipline: `State._lock` is `threading.Lock` (non-reentrant). Each
helper acquires `state._lock` only for the critical-section snapshot/
mutate and drops it before downstream I/O or methods that re-acquire
the lock. See per-method race notes (CS0/CS1/CS2, stale deep-copy
clobber, Lock-non-reentrant rationale) for full discussion.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from detection import HSVRange, ShapeGate
from live_pairing import LivePairingSession
from reconstruct import Ray, rays_for_frame
from schemas import (
    DetectionConfigSnapshotPayload,
    DetectionPath,
    FramePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    SessionResult,
    TriangulatedPoint,
    persist_pitch_json,
    persist_result_json,
)
import session_results

if TYPE_CHECKING:
    from state import State


logger = logging.getLogger("ball_tracker")


def ingest_live_frame(
    state: "State",
    camera_id: str,
    session_id: str,
    frame: FramePayload,
) -> tuple[list[TriangulatedPoint], dict[str, int], FramePayload]:
    with state._lock:
        live = state._live_pairings.setdefault(session_id, LivePairingSession(session_id))
        # Freeze pairing tuning + hsv/shape on the FIRST real frame
        # seen by this LivePairingSession. Mirrors the cd87995
        # PairingTuning-on-SessionResult contract: a session's cost
        # basis is decided at arm time and cannot shift mid-cycle.
        # Dashboard slider edits during an active session land on
        # the NEXT session.
        #
        # Idempotent: arm_session pre-creates LivePairingSession (so a
        # `session_id not in dict` check would never fire on the dashboard
        # path), and tests that bypass arm hit the setdefault above. The
        # `live_config_used is None` freshness check covers both: stamp
        # exactly once on first ingest regardless of who created the
        # LivePairingSession. Subsequent ingests see the fields already
        # set and skip the block.
        if live.live_config_used is None:
            live.pairing_tuning = state._pairing_tuning
            live.live_config_used = (
                DetectionConfigSnapshotPayload.from_detection_config(
                    state._detection_config
                )
            )
        # TODO(n-camera): generalize to iterate state.expected_camera_ids()
        # once `triangulate_live` (below) accepts an N-pose dict. Today
        # the hot path is pair-based (Phase 3 live_pairing peer-set
        # change documented this contract).
        cal_a = state._calibration_store.get("A")
        cal_b = state._calibration_store.get("B")
        dev_a = state._device_registry.get("A")
        dev_b = state._device_registry.get("B")
        session_obj = state._lookup_session_locked(session_id)
        # Snapshot runtime capture height under the same lock that
        # protects every other runtime-settings read in this class
        # (the State docstring at L128-135 names this invariant).
        # Used below to scale snap K + H to actual live frame dims.
        live_h = state._runtime_settings.capture_height_px

    # Each iPhone's `frame.timestamp_s` is its own mach-absolute clock
    # (seconds since device boot), so the two cameras' raw timestamps
    # can be tens of thousands of seconds apart. Hand each device's
    # anchor to `LivePairingSession.ingest` so its 8 ms cross-cam
    # comparison happens on anchor-relative time, while persisted
    # frames keep raw timestamps for downstream consumers.
    anchors = {
        "A": dev_a.sync_anchor_timestamp_s if dev_a is not None else None,
        "B": dev_b.sync_anchor_timestamp_s if dev_b is not None else None,
    }

    # Populate / refresh the per-cam cached pose on the live session
    # so triangulate_live can skip the PitchPayload/scale path and go
    # straight to the ray math.
    #
    # The snapshot is stored at canonical 1920×1080 by auto-cal, but
    # the iPhone may stream live BGRA frames at either 1080p or 720p
    # depending on operator's `capture_height_px` setting. Pre-scale
    # K + H to the actual live frame grid here so triangulate_live_pair
    # consumes a K/H pair that matches frame.px/frame.py basis.
    # Without this, 720p standby silently produces 1.5× off live rays.
    from live_pairing import CameraPose as _CameraPose
    from pairing import _camera_pose as _build_pose, _scale_homography, _scale_intrinsics

    live_w = (live_h * 16) // 9
    live_dims = (live_w, live_h)

    for cam, cal in (("A", cal_a), ("B", cal_b)):
        if cal is None:
            live.update_camera_pose(cam, None)
            continue
        existing = live.camera_pose(cam)
        if existing is not None and existing.image_wh == live_dims:
            continue
        snap_dims = (cal.image_width_px, cal.image_height_px)
        if snap_dims != live_dims:
            sx = live_dims[0] / snap_dims[0]
            sy = live_dims[1] / snap_dims[1]
            live_intr = _scale_intrinsics(cal.intrinsics, sx, sy)
            live_h_flat = _scale_homography(list(cal.homography), sx, sy)
        else:
            live_intr = cal.intrinsics
            live_h_flat = list(cal.homography)
        K, R, _t, C = _build_pose(live_intr, live_h_flat)
        live.update_camera_pose(cam, _CameraPose(
            K=K, R=R, C=C,
            dist=live_intr.distortion,
            image_wh=live_dims,
        ))

    def triangulate_live(frame_a: FramePayload, frame_b: FramePayload) -> list[TriangulatedPoint]:
        # frame_a / frame_b are pre-canonicalized A-first by ingest();
        # the closure name + argument order is the contract. No
        # cam-direction flipping needed here.
        pose_a = live.camera_pose("A")
        pose_b = live.camera_pose("B")
        if pose_a is None or pose_b is None:
            return []
        if dev_a is None or dev_b is None:
            return []
        if dev_a.sync_anchor_timestamp_s is None or dev_b.sync_anchor_timestamp_s is None:
            return []
        from pairing import triangulate_live_pair
        return triangulate_live_pair(
            pose_a, pose_b,
            frame_a, frame_b,
            anchor_a=dev_a.sync_anchor_timestamp_s,
            anchor_b=dev_b.sync_anchor_timestamp_s,
        )

    created = live.ingest(camera_id, frame, triangulate_live, anchors=anchors)
    # The frame stored by live.ingest is the candidate-resolved one
    # (px/py picked by the shape-prior selector); hand it back so
    # callers (WS handler → live_rays_for_frame) work off the resolved
    # version, not the raw inbound.
    resolved = live.latest_frame_for(camera_id)
    if resolved is None:
        raise RuntimeError(
            f"ingest_live_frame: live buffer empty after ingest cam={camera_id} sid={session_id}"
        )
    return created, live.frame_counts_snapshot(), resolved


def live_rays_for_frame(
    state: "State",
    camera_id: str,
    session_id: str,
    frame: FramePayload,
) -> list[Ray]:
    """Project this frame's candidates into world space for dashboard rays.

    Returns one ray per shape-gate-passing candidate (fan-out parity
    with the post-pitch viewer scene). Empty list when no calibration
    on file, no anchor reachable, or `frame.ball_detected` is False.

    Stereo live points still require A/B pairing and a shared time
    anchor. A monocular ray only needs that camera's calibration;
    if the phone has no sync anchor, returns [] (mirrors the
    no-calibration path; emits a one-time info log per cam/session
    for operator visibility).
    """
    with state._lock:
        cal = state._calibration_store.get(camera_id)
        dev = state._device_registry.get(camera_id)
        if cal is None:
            state._live_missing_cal.setdefault(session_id, set()).add(camera_id)
            log_key = (session_id, camera_id)
            should_log = log_key not in state._live_missing_cal_logged
            if should_log:
                state._live_missing_cal_logged.add(log_key)
        sync_log_key = (session_id, camera_id)
        should_log_sync = (
            cal is not None
            and (dev is None or dev.sync_anchor_timestamp_s is None)
            and sync_log_key not in state._live_missing_sync_logged
        )
        if should_log_sync:
            state._live_missing_sync_logged.add(sync_log_key)
    if cal is None:
        if should_log:
            logger.warning(
                "live_rays_for_frame: cam=%s session=%s has no calibration on "
                "file — live rays dropped until /calibration or /calibration/auto runs",
                camera_id,
                session_id,
            )
        return []
    # Silent fallback removed: the previous code synthesised an
    # anchor from `frame.timestamp_s - frame_index/240` when the
    # device had no sync anchor on file. That produced rays whose
    # `t_rel_s` looked plausible but was actually decoupled from
    # mutual-sync clock — they would rendr in the dashboard 3D scene
    # alongside genuinely time-aligned rays and the operator had no
    # way to tell. Mirror the no-calibration path: drop silently
    # (one-time info log per cam/session) instead of fabricating a clock.
    if dev is None or dev.sync_anchor_timestamp_s is None:
        if should_log_sync:
            logger.info(
                "live_rays_for_frame: cam=%s session=%s has no sync anchor — "
                "live rays dropped until chirp/mutual sync completes",
                camera_id,
                session_id,
            )
        return []
    anchor = dev.sync_anchor_timestamp_s
    return rays_for_frame(
        camera_id=camera_id,
        frame=frame,
        intrinsics=cal.intrinsics,
        homography=list(cal.homography),
        anchor_timestamp_s=anchor,
        source="live",
    )


def mark_live_path_ended(
    state: "State", camera_id: str, session_id: str, reason: str | None = None,
) -> None:
    with state._lock:
        live = state._live_pairings.setdefault(session_id, LivePairingSession(session_id))
        live.mark_completed(camera_id)
        if reason and reason != "disarmed":
            live.mark_aborted(camera_id, reason)


def persist_live_frames(
    state: "State", camera_id: str, session_id: str,
) -> SessionResult | None:
    with state._lock:
        existing = state.pitches.get((camera_id, session_id))
        live_frames = session_results.live_frames_for_camera_locked(
            state, session_id, camera_id,
        )
    if existing is None or not live_frames:
        return None
    if existing.frames_live == live_frames:
        return state.get(session_id)
    merged = existing.model_copy(deep=True)
    merged.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID] = list(live_frames)
    return state.record(merged)


def flush_live_frames_for_session(state: "State", session_id: str) -> None:
    """Persist any buffered live frames to disk pitch JSONs for the
    given session. Called at session-end (timeout / Stop) so frames
    survive even if iOS never sent `cycle_end` (WS death, app crash,
    force-kill, network partition).

    For cams that already uploaded /pitch, this is a no-op redirect to
    `persist_live_frames` (which merges live frames into the existing
    pitch JSON). For cams that died before /pitch arrived, synthesise
    a minimal pitch carrying just the live bucket — without it the
    in-memory frames would silently vanish on restart, violating the
    no-silent-fallback rule.

    Idempotent: safe to call repeatedly per session id."""
    with state._lock:
        live = state._live_pairings.get(session_id)
    if live is None:
        return
    cam_ids = live.cameras_with_frames()
    if not cam_ids:
        return
    for cam_id in sorted(cam_ids):
        with state._lock:
            existing = state.pitches.get((cam_id, session_id))
        if existing is not None:
            state.persist_live_frames(cam_id, session_id)
            continue
        with state._lock:
            dev = state._device_registry.get(cam_id)
            cal_snap = state._calibration_store.get(cam_id)
        anchor = dev.sync_anchor_timestamp_s if dev is not None else None
        sync_id = dev.time_sync_id if dev is not None else None
        if anchor is None:
            # No sync anchor → synthesising a pitch with video_start_pts_s=0
            # would peg t_rel_s onto an absolute mach-clock that downstream
            # reconstruct.py / viewer expect to start at 0. Drop the buffer
            # rather than write a forever-broken JSON.
            logger.warning(
                "flush_live_frames: skipping synthesise session=%s cam=%s — "
                "no sync anchor (live frames discarded)",
                session_id, cam_id,
            )
            continue
        # Mirror the /pitch handler: pitches that hit `record()` MUST
        # carry calibration + sync_id, otherwise the viewer reads back
        # a row with intrinsics=None and renders the misleading
        # "Cam X missing calibration" error even though the operator
        # set everything up correctly.
        synthetic = PitchPayload(
            camera_id=cam_id,
            session_id=session_id,
            sync_id=sync_id,
            sync_anchor_timestamp_s=anchor,
            video_start_pts_s=anchor,
            paths=[DetectionPath.live.value],
            intrinsics=cal_snap.intrinsics if cal_snap is not None else None,
            homography=list(cal_snap.homography) if cal_snap is not None else None,
            image_width_px=cal_snap.image_width_px if cal_snap is not None else None,
            image_height_px=cal_snap.image_height_px if cal_snap is not None else None,
        )
        logger.info(
            "flush_live_frames: synthesising live-only pitch session=%s cam=%s "
            "anchor=%s sync_id=%s calibrated=%s",
            session_id, cam_id, anchor, sync_id, cal_snap is not None,
        )
        state.record(synthetic)


def stamp_server_post_config(
    state: "State",
    session_id: str,
    snapshot: "DetectionConfigSnapshotPayload",
) -> SessionResult | None:
    """Set `result.server_post_config_used = snapshot` on the
    in-memory SessionResult and persist. Both cams of a session
    run with the same preset / params (the request body locks
    it), so last-writer-wins is a no-op.

    Returns `None` if the session was deleted between record() and
    this call, or if `store_result`'s internal race guard tripped
    (post-write delete) — matches `record`'s race semantics and
    avoids silently returning a SessionResult that never landed in
    `self.results`. Callers must check for None before using the
    returned object to fan out SSE / persist downstream state.
    """
    with state._lock:
        existing = state.results.get(session_id)
    if existing is None:
        # Race: caller's earlier `record(pitch)` returned a real
        # SessionResult, but the session was deleted between then
        # and now. Loud warning so the caller's `result = stamp_*`
        # doesn't silently shadow a real result with a shell.
        logger.warning(
            "stamp_server_post_config: session %s missing — deleted "
            "during server_post run; snapshot will not be persisted",
            session_id,
        )
        return None
    # Dict-canonical: stamp the new snapshot into
    # `config_used_by_algorithm` and update the active pointer.
    # `model_copy` is shallow; we mutate the cloned dict directly.
    updated = existing.model_copy(deep=True)
    updated.config_used_by_algorithm[snapshot.algorithm_id] = snapshot
    updated.active_server_post_algorithm_id = snapshot.algorithm_id
    state.store_result(updated)
    # `store_result` has its own race guard: if the session was
    # deleted between our `existing` snapshot and the disk write /
    # in-memory republish, it silently returns without writing
    # `self.results`. Without the check below we'd hand the caller a
    # `updated` SessionResult that never landed → SSE fan-out fires
    # `fit` events for a result that doesn't exist in state. Re-read
    # under the lock and verify the stamp is present.
    #
    # Content check, not identity: a benign concurrent `record()`
    # between `store_result` and the re-read publishes a fresher
    # SessionResult object that carries our stamp (record() reads
    # from disk, where store_result just wrote). Identity comparison
    # (`is not updated`) treated those republishes as "race tripped"
    # and silently skipped the SSE broadcast even though the stamp
    # had landed. Verify by membership in `config_used_by_algorithm`.
    with state._lock:
        stored = state.results.get(session_id)
    if stored is None or snapshot.algorithm_id not in stored.config_used_by_algorithm:
        logger.warning(
            "stamp_server_post_config: session %s store_result race "
            "guard tripped — snapshot for algorithm %s NOT persisted",
            session_id, snapshot.algorithm_id,
        )
        return None
    return stored


def set_active_server_post_algorithm(
    state: "State",
    session_id: str,
    algorithm_id: str,
) -> SessionResult | None:
    """Flip the active server_post pointer on every cam's pitch for
    this session, persist the pitches, and re-publish the
    SessionResult. No detection runs — pure pointer flip behind the
    viewer's history dropdown.

    `algorithm_id` must already have at least one frame in some
    cam's `frames_by_algorithm` (the algorithm has been run on this
    session before). Single-cam presence is enough — flipping to an
    algorithm that only cam A has frames for is intentional (mono
    session, or only-A reprocess); the result will just carry an
    empty `triangulated_by_algorithm[id]` for the missing cam side.
    Passing the live bucket id is rejected — server_post and live
    are separate pointers.

    Dispatch: dual-cam sessions with no `sync_error` and a cached
    `triangulated_by_algorithm` bucket for the target alg use the
    fast path (`stamp_active_pointer_projection`) — copy the cached
    result, re-stamp the four pointer-derived projections, no
    re-triangulation. Mono / sync_error / cache miss fall through
    to `rebuild_result_for_session`, which is canonical and cheap
    for those cases (no `triangulate_pair` runs under mono / sync
    anyway). Avoids the multi-second `triangulate_pair` × N-algo
    cost that was making `s_f9ddcbb6` (148K-pt hybrid_28d bucket)
    block for ~10s on every history switch.

    Returns the published SessionResult, or `None` if the session was
    deleted mid-operation (mirrors `record()`'s race guard).

    Raises:
        KeyError: session has no pitches recorded.
        ValueError: `algorithm_id` is the live bucket, or has no
            frames in this session.
    """
    if algorithm_id == IOS_CAPTURE_TIME_ALGORITHM_ID:
        raise ValueError(
            f"{algorithm_id!r} is the live bucket; not a server_post run"
        )
    with state._lock:
        existing_pitches = [
            p for (_cam, sid), p in state.pitches.items()
            if sid == session_id
        ]
        if not existing_pitches:
            raise KeyError(f"session {session_id!r} has no pitches")
        if not any(
            algorithm_id in p.frames_by_algorithm for p in existing_pitches
        ):
            raise ValueError(
                f"algorithm {algorithm_id!r} has no frames in session "
                f"{session_id!r}"
            )
        # Deep-copy under the lock; we'll mutate the copies outside.
        # Mutating the live entries here would desync from disk if
        # any of the _atomic_write calls below raised.
        new_pitches = [p.model_copy(deep=True) for p in existing_pitches]
    for p in new_pitches:
        p.active_server_post_algorithm_id = algorithm_id

    # Write all updated pitches to disk FIRST. If any write fails,
    # the in-memory map keeps the old pointer; on success we publish
    # the new copies atomically under the lock below.
    for pitch in new_pitches:
        state._atomic_write(
            state._pitch_path(pitch.camera_id, pitch.session_id),
            persist_pitch_json(pitch),
        )

    with state._lock:
        still_present = any(
            (p.camera_id, p.session_id) in state.pitches
            for p in new_pitches
        )
        if not still_present:
            logger.info(
                "set_active_server_post_algorithm: session %s deleted "
                "during write — discarding result publish",
                session_id,
            )
            return None
        # Re-read under the lock and apply ONLY the pointer field to
        # the latest in-memory pitch. Between our deep-copy above and
        # this republish a concurrent `record()` (live retry / parallel
        # server_post completion) may have published fresher
        # `frames_*` / `config_used_by_algorithm` data — clobbering
        # with our stale deep-copy would silently drop those writes
        # from both memory and (via the now-published-then-replaced
        # state) downstream reads. The pointer flip is the only field
        # this method owns; everything else must come from the latest.
        republished: list[PitchPayload] = []
        disk_writes_after_republish: list[PitchPayload] = []
        orphan_disk_files: list[Path] = []
        for stale in new_pitches:
            key = (stale.camera_id, stale.session_id)
            latest = state.pitches.get(key)
            if latest is None:
                # Cam-level race: this cam's pitch was deleted (via
                # `delete_session` / `remove_pitch`) while other cams
                # remain. The disk write above wrote our pointer-flipped
                # copy AFTER the delete unlinked the file — so it is
                # an orphan rather than a valid "this cam's latest
                # state". Drop it to keep disk in sync with the
                # in-memory absence; leaving it behind would resurrect
                # a tombstoned pitch on next boot.
                orphan_disk_files.append(state._pitch_path(*key))
                continue
            merged = latest.model_copy(
                update={"active_server_post_algorithm_id": algorithm_id},
            )
            state.pitches[key] = merged
            # Refresh mtime cache so build_events sees the pointer
            # flip in its ordering without an extra stat() syscall.
            state._pitch_mtime_cache[key] = state._time_fn()
            republished.append(merged)
            # Disk-write-under-lock discipline (mirror of `record()`):
            # our line-1584 write went out BEFORE this lock, so a
            # concurrent `record()` may have clobbered disk with fresh
            # `frames_*` / `config_used_by_algorithm` data. The
            # `merged` object is the authoritative latest state
            # (fresh-record fields + our pointer flip) — without
            # rewriting disk, memory diverges from disk and a restart
            # would silently revert the pointer (or worse, lose the
            # fresh frames if our write landed last). Defer the actual
            # write until outside this lock block to keep lock hold
            # short, but capture the references now.
            disk_writes_after_republish.append(merged)
        # Downstream fast-path / rebuild reads the latest pitches via
        # `self.pitches` (and re-reads under lock), so the references
        # below must be refreshed to the merged objects.
        new_pitches = republished

    # Re-persist the merged pitches AND unlink orphan disk files left
    # by the first-round write when a cam-level delete raced us. Both
    # ops are outside the lock — _atomic_write / unlink are blocking
    # I/O and the in-memory state is already coherent above.
    for orphan in orphan_disk_files:
        orphan.unlink(missing_ok=True)
    for pitch in disk_writes_after_republish:
        state._atomic_write(
            state._pitch_path(pitch.camera_id, pitch.session_id),
            persist_pitch_json(pitch),
        )

    # Fast-path eligibility: dual-cam, no sync error, cached result
    # has the target alg's triangulated bucket. Everything else
    # falls through to the canonical rebuild. `validate_pair_sync`
    # grabs `self._lock` itself, so this block must stay outside the
    # publish lock above (Lock() is not re-entrant).
    a_pitch = next((p for p in new_pitches if p.camera_id == "A"), None)
    b_pitch = next((p for p in new_pitches if p.camera_id == "B"), None)
    with state._lock:
        cached = state.results.get(session_id)
    fast_path = (
        cached is not None
        and a_pitch is not None
        and b_pitch is not None
        and algorithm_id in cached.triangulated_by_algorithm
        and session_results.validate_pair_sync(state, a_pitch, b_pitch) is None
    )
    if fast_path:
        # Copy-mutate-swap: avoid in-place mutation on the
        # `self.results[sid]` reference that concurrent SSE
        # serializers / `/results/{sid}` GETs may be iterating.
        # `stamp_segments_on_result` mutates dicts in place, so
        # the deep copy isolates the work.
        result = cached.model_copy(deep=True)
        session_results.stamp_active_pointer_projection(
            result, a_pitch, b_pitch,
        )
    else:
        result = session_results.rebuild_result_for_session(state, session_id)

    state._atomic_write(
        state._result_path(session_id),
        persist_result_json(result),
    )
    with state._lock:
        if not any(
            (p.camera_id, p.session_id) in state.pitches
            for p in new_pitches
        ):
            logger.info(
                "set_active_server_post_algorithm: session %s deleted "
                "between disk write and republish — discarding",
                session_id,
            )
            return None
        state.results[session_id] = result
    return result


def live_missing_calibration_for(state: "State", session_id: str) -> list[str]:
    """Cams whose live frames were dropped for missing calibration in this
    session, sorted. Empty list if none / unknown session."""
    with state._lock:
        return sorted(state._live_missing_cal.get(session_id, set()))


def live_session_frozen_config(
    state: "State", session_id: str,
) -> tuple[HSVRange, ShapeGate] | None:
    """Public accessor for the (hsv_range, shape_gate) pair frozen
    onto a LivePairingSession at first ingest_live_frame.

    Returns None when:
      - no LivePairingSession exists for `session_id` (test fixture
        / replay path that POSTed /pitch without arming + ingesting), OR
      - the LivePairingSession was pre-created by arm_session but no
        live frame has flowed yet (e.g. server_post-only flow where
        iOS never streams live frames between arm and /pitch upload).

    Returns the frozen pair when first-ingest stamping has run; this
    is the dashboard-armed live-streaming production path. Callers must
    treat None as "no frozen snapshot — fall back to current state",
    not as an invariant violation: server_post-only is a real flow.
    """
    with state._lock:
        live = state._live_pairings.get(session_id)
        if live is None or live.live_config_used is None:
            return None
        return live.live_config_used
