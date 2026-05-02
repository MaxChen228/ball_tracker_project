"""Phase 6b — algorithm-id-keyed accessor contract.

Pins the read/write API that `POST /sessions/{sid}/runs/{algorithm_id}`
(Phase 7) will build on. The accessors live in `detection_paths` so
the path-keyed and algorithm-keyed views share resolution logic
(`live → ios_capture_time`, `server_post → stamped or legacy alg id`).
"""
from __future__ import annotations

from detection_paths import (
    algorithm_id_for_path,
    get_algorithm_frames,
    get_path_frames,
    pitch_with_algorithm_frames,
    set_algorithm_frames,
)
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    DetectionPath,
    FramePayload,
    HSVRangePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    ShapeGatePayload,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    )


def _snapshot(alg_id: str) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        hsv=HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=200, v_min=40, v_max=210),
        shape_gate=ShapeGatePayload(aspect_min=0.7, fill_min=0.55),
        preset_name=None,
    )


def _pitch(**kw) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
        **kw,
    )


def test_algorithm_id_for_live_path_is_ios_capture_time():
    p = _pitch()
    assert algorithm_id_for_path(p, DetectionPath.live) == IOS_CAPTURE_TIME_ALGORITHM_ID


def test_algorithm_id_for_server_post_reads_snapshot_stamp():
    snap = _snapshot("v11_hsv_cc")
    p = _pitch(server_post_config_used=snap)
    assert algorithm_id_for_path(p, DetectionPath.server_post) == "v11_hsv_cc"


def test_algorithm_id_for_server_post_falls_back_to_legacy_when_no_snapshot():
    p = _pitch()
    # Legacy bucket — pre-Phase-2 pitches lack the snapshot.
    assert algorithm_id_for_path(p, DetectionPath.server_post) == "v11_hsv_cc"


def test_get_algorithm_frames_returns_empty_for_unrun_algorithm():
    """Match `get_path_frames` invariant: callers don't have to guard."""
    p = _pitch()
    assert get_algorithm_frames(p, "v12_xyz") == []


def test_get_algorithm_frames_returns_frames_after_validator_mirror():
    p = _pitch(frames_live=[_frame(1), _frame(2)])
    assert len(get_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID)) == 2


def test_set_algorithm_frames_for_ios_capture_time_syncs_frames_live():
    """Back-compat: writers using the new algorithm-keyed API must
    leave path-keyed readers seeing the same frames. ios_capture_time
    is the canonical mirror of frames_live."""
    p = _pitch()
    set_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID, [_frame(1), _frame(2)])
    assert len(p.frames_live) == 2
    assert get_path_frames(p, DetectionPath.live) == p.frames_live


def test_set_algorithm_frames_for_current_server_post_alg_syncs_frames_server_post():
    snap = _snapshot("v11_hsv_cc")
    p = _pitch(server_post_config_used=snap)
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(1), _frame(2), _frame(3)])
    assert len(p.frames_server_post) == 3


def test_set_algorithm_frames_for_other_alg_leaves_frames_server_post_alone():
    """Writing v12 frames while v11 is the stamped server_post must
    NOT clobber the v11 frames in `frames_server_post`. v12 lives
    only in the dict until a future endpoint promotes it."""
    snap = _snapshot("v11_hsv_cc")
    v11_frames = [_frame(1), _frame(2)]
    p = _pitch(server_post_config_used=snap, frames_server_post=v11_frames)
    set_algorithm_frames(p, "v12_future", [_frame(10), _frame(11), _frame(12)])
    # v11 untouched in legacy bucket:
    assert len(p.frames_server_post) == 2
    # v12 in the dict only:
    assert len(p.frames_by_algorithm["v12_future"]) == 3
    assert "v11_hsv_cc" in p.frames_by_algorithm  # mirrored from frames_server_post


def test_set_algorithm_frames_legacy_pre_snapshot_writes_through_to_server_post():
    """Pre-snapshot pitches: `server_post_config_used is None`. The
    legacy bucket fallback is `v11_hsv_cc`; writing under that id
    should still sync `frames_server_post` so existing readers see
    the new frames."""
    p = _pitch()
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(1)])
    assert len(p.frames_server_post) == 1


def test_pitch_with_algorithm_frames_projects_into_server_post_slot():
    """Counterpart to `pitch_with_path_frames`. Downstream code
    (reconstruct, rays) reads `frames_server_post` on the clone."""
    snap_v11 = _snapshot("v11_hsv_cc")
    p = _pitch(
        server_post_config_used=snap_v11,
        frames_server_post=[_frame(1)],
        frames_by_algorithm={"v12_future": [_frame(10), _frame(11)]},
    )
    clone = pitch_with_algorithm_frames(p, "v12_future")
    assert len(clone.frames_server_post) == 2
    # Original untouched:
    assert len(p.frames_server_post) == 1
