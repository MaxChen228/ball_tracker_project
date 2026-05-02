"""Phase 6a — additive `*_by_algorithm` dict mirrors.

Pins the contract that the after-validator on `PitchPayload` /
`SessionResult` mirrors old per-path fields (`frames_live`,
`frames_server_post`, `live_config_used`, `server_post_config_used`,
`triangulated_by_path`, `segments_by_path`, `frame_counts_by_path`,
`paths_completed`) into the new algorithm-id-keyed dicts on every
construction / load. Old fields remain canonical in 6a; these tests
ensure 6b can flip readers safely because the dict is always present.
"""
from __future__ import annotations

import algorithms
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    FramePayload,
    HSVRangePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    SegmentRecord,
    SessionResult,
    ShapeGatePayload,
    TriangulatedPoint,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    )


def _snapshot(alg_id: str, preset: str | None = None) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        hsv=HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=200, v_min=40, v_max=210),
        shape_gate=ShapeGatePayload(aspect_min=0.7, fill_min=0.55),
        preset_name=preset,
    )


def _base_pitch(**kw):
    defaults = dict(
        camera_id="A",
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
    )
    defaults.update(kw)
    return PitchPayload(**defaults)


def test_pitch_mirrors_frames_live_into_ios_capture_time():
    p = _base_pitch(frames_live=[_frame(1), _frame(2)])
    assert IOS_CAPTURE_TIME_ALGORITHM_ID in p.frames_by_algorithm
    assert len(p.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 2


def test_pitch_mirrors_frames_server_post_under_snapshot_algorithm_id():
    snap = _snapshot("v11_hsv_cc")
    p = _base_pitch(
        frames_server_post=[_frame(1), _frame(2), _frame(3)],
        server_post_config_used=snap,
    )
    assert "v11_hsv_cc" in p.frames_by_algorithm
    assert len(p.frames_by_algorithm["v11_hsv_cc"]) == 3


def test_pitch_frames_server_post_without_snapshot_falls_back_to_default():
    """Legacy pitch JSONs predating snapshot persistence — the dict
    mirror still has to file frames somewhere. Use the registry default
    so 6b readers find the frames under a real algorithm id."""
    p = _base_pitch(frames_server_post=[_frame(1)])
    assert algorithms.DEFAULT_ALGORITHM_ID in p.frames_by_algorithm


def test_pitch_mirrors_config_used_into_dict():
    live_snap = _snapshot("ios_capture_time", preset="blue_ball")
    srv_snap = _snapshot("v11_hsv_cc", preset="tennis")
    p = _base_pitch(live_config_used=live_snap, server_post_config_used=srv_snap)
    assert p.config_used_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID].preset_name == "blue_ball"
    assert p.config_used_by_algorithm["v11_hsv_cc"].preset_name == "tennis"


def test_pitch_empty_old_fields_yields_empty_dict():
    """Mirror must NOT fabricate dict keys when old fields are empty —
    otherwise 6b readers would see ghost algorithm rows for sessions
    that never ran that path."""
    p = _base_pitch()
    assert p.frames_by_algorithm == {}
    assert p.config_used_by_algorithm == {}


def test_pitch_mirror_idempotent_on_revalidation():
    p = _base_pitch(frames_live=[_frame(1)])
    dumped = p.model_dump(mode="json")
    p2 = PitchPayload.model_validate(dumped)
    assert list(p.frames_by_algorithm.keys()) == list(p2.frames_by_algorithm.keys())
    assert len(p2.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 1


def _tri_point() -> TriangulatedPoint:
    return TriangulatedPoint(
        t_rel_s=0.1, x_m=1.0, y_m=2.0, z_m=3.0, residual_m=0.01,
        cost_a=0.5, cost_b=0.4,
    )


def _segment() -> SegmentRecord:
    return SegmentRecord(
        indices=[0, 1, 2], original_indices=[0, 1, 2],
        p0=[1.0, 2.0, 3.0], v0=[10.0, 0.0, -1.0],
        t_anchor=0.0, t_start=0.0, t_end=0.2,
        rmse_m=0.01, speed_kph=130.0,
    )


def test_result_mirrors_triangulated_and_segments_by_path():
    srv_snap = _snapshot("v11_hsv_cc")
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_path={"live": [_tri_point()], "server_post": [_tri_point(), _tri_point()]},
        segments_by_path={"server_post": [_segment()]},
        frame_counts_by_path={"live": {"A": 100, "B": 100}, "server_post": {"A": 200, "B": 200}},
        paths_completed={"live", "server_post"},
        live_config_used=_snapshot("ios_capture_time"),
        server_post_config_used=srv_snap,
    )
    assert len(r.triangulated_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 1
    assert len(r.triangulated_by_algorithm["v11_hsv_cc"]) == 2
    assert len(r.segments_by_algorithm["v11_hsv_cc"]) == 1
    assert IOS_CAPTURE_TIME_ALGORITHM_ID not in r.segments_by_algorithm  # nothing to mirror
    assert r.frame_counts_by_algorithm["v11_hsv_cc"] == {"A": 200, "B": 200}
    assert r.algorithms_completed == {IOS_CAPTURE_TIME_ALGORITHM_ID, "v11_hsv_cc"}
    assert set(r.config_used_by_algorithm) == {IOS_CAPTURE_TIME_ALGORITHM_ID, "v11_hsv_cc"}


def test_result_server_post_alg_id_falls_back_when_no_snapshot():
    """Pre-snapshot legacy results — server_post points still have to
    file under a real algorithm id."""
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_path={"server_post": [_tri_point()]},
        paths_completed={"server_post"},
    )
    assert algorithms.DEFAULT_ALGORITHM_ID in r.triangulated_by_algorithm
    assert algorithms.DEFAULT_ALGORITHM_ID in r.algorithms_completed
