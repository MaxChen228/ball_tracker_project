"""Pure math + detection + pairing diagnostics + cleanup tests."""
from __future__ import annotations

import logging
import os
import time

import numpy as np
import pytest

import main
import pairing
from cleanup_old_sessions import cleanup_expired_sessions
from conftest import sid
from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)

from _test_helpers import (
    _encode_mov,
    _make_frame_with_ball,
    _make_scene,
    _project,
    _project_pixels,
)


def _direct_payload_with_frames(
    cam_id: str,
    session_id: str,
    path: np.ndarray,
    ts: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    H: np.ndarray,
    K: np.ndarray,
) -> "main.PitchPayload":
    frames = []
    for i, (Pi, ti) in enumerate(zip(path, ts)):
        u, v = _project_pixels(K, R, t, Pi)
        frames.append(
            main.FramePayload(
                frame_index=i, timestamp_s=float(ti),
                px=u, py=v, ball_detected=True,
            )
        )
    return main.PitchPayload(
        camera_id=cam_id,
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frames,
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H.flatten().tolist(),
    )


# --------------------------- Unit tests --------------------------------------


def test_recover_extrinsics_matches_ground_truth():
    K, *_ , (R_a, t_a, _, H_a), _ = _make_scene()
    R_rec, t_rec = recover_extrinsics(K, H_a)
    np.testing.assert_allclose(R_rec, R_a, atol=1e-8)
    np.testing.assert_allclose(t_rec, t_a, atol=1e-8)


def test_triangulate_perfect_rays_recovers_point():
    K, *_, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b) = _make_scene()
    P_true = np.array([0.25, 0.6, 1.55])

    theta_x_a, theta_z_a = _project(K, R_a, t_a, P_true)
    theta_x_b, theta_z_b = _project(K, R_b, t_b, P_true)

    R_a_r, t_a_r = recover_extrinsics(K, H_a)
    R_b_r, t_b_r = recover_extrinsics(K, H_b)
    C_a_r = camera_center_world(R_a_r, t_a_r)
    C_b_r = camera_center_world(R_b_r, t_b_r)

    d_a = R_a_r.T @ angle_ray_cam(theta_x_a, theta_z_a)
    d_b = R_b_r.T @ angle_ray_cam(theta_x_b, theta_z_b)

    P_rec, gap = triangulate_rays(C_a_r, d_a, C_b_r, d_b)
    np.testing.assert_allclose(P_rec, P_true, atol=1e-6)
    assert gap < 1e-6


def test_undistorted_ray_cam_zero_dist_matches_angle_ray():
    K = build_K(1600.0, 1600.0, 960.0, 540.0)
    u, v = 1234.5, 678.9
    theta_x = np.arctan2(u - K[0, 2], K[0, 0])
    theta_z = np.arctan2(v - K[1, 2], K[1, 1])
    d_angle = angle_ray_cam(theta_x, theta_z)
    d_pix = undistorted_ray_cam(u, v, K, np.zeros(5))
    np.testing.assert_allclose(d_pix, d_angle, atol=1e-12)


# --------------------------- triangulate_cycle unit (direct, no /pitch) ------


def test_triangulate_sweeps_ball_path():
    """Simulate a short pitch and verify the pixel-only triangulation path
    recovers every point on the trajectory."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    ts = np.linspace(0.0, 0.4, 20)
    path = np.stack(
        [
            0.1 * np.sin(ts * 10),
            18.0 - 45.0 * ts,
            2.0 - 4.9 * ts**2 + 2.0 * ts - 2.0 * ts,
        ],
        axis=1,
    )

    payload_a = _direct_payload_with_frames("A", sid(1), path, ts, R_a, t_a, H_a, K)
    payload_b = _direct_payload_with_frames("B", sid(1), path, ts, R_b, t_b, H_b, K)
    points = pairing.triangulate_cycle(payload_a, payload_b)
    assert len(points) == len(path)
    recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in points])
    np.testing.assert_allclose(recovered, path, atol=1e-6)
    residuals = [p.residual_m for p in points]
    assert max(residuals) < 1e-6


def test_persistence_reloads_state_across_process_restart(tmp_path):
    """State pointed at an existing data dir re-triangulates stored pitches."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.2, 0.5, 1.1])
    ts = np.array([0.0])
    path = P_true.reshape(1, 3)
    session_id = sid(42)

    payload_a = _direct_payload_with_frames("A", session_id, path, ts, R_a, t_a, H_a, K)
    payload_b = _direct_payload_with_frames("B", session_id, path, ts, R_b, t_b, H_b, K)

    s1 = main.State(data_dir=tmp_path)
    s1.record(payload_a)
    s1.record(payload_b)
    del s1

    s2 = main.State(data_dir=tmp_path)
    latest = s2.latest()
    assert latest is not None
    assert latest.session_id == session_id
    assert latest.camera_a_received and latest.camera_b_received
    assert len(latest.points) == 1
    pt = latest.points[0]
    assert abs(pt.x_m - P_true[0]) < 1e-6
    assert abs(pt.y_m - P_true[1]) < 1e-6
    assert abs(pt.z_m - P_true[2]) < 1e-6


# --------------------------- API smoke --------------------------------------


def test_status_initially_idle():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


# --------------------------- Pairing drop diagnostics ------------------------


def _build_pairing_payloads(
    timestamps_a: list[float],
    timestamps_b: list[float],
    session_id: str,
):
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])

    def frames(ts_list: list[float], R, t):
        u, v = _project_pixels(K, R, t, P_true)
        return [
            main.FramePayload(
                frame_index=i, timestamp_s=float(ti),
                px=u, py=v, ball_detected=True,
            )
            for i, ti in enumerate(ts_list)
        ]

    payload_a = main.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frames(timestamps_a, R_a, t_a),
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )
    payload_b = main.PitchPayload(
        camera_id="B",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frames(timestamps_b, R_b, t_b),
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_b.flatten().tolist(),
    )
    return payload_a, payload_b


def test_pairing_drop_diagnostics(caplog):
    session_id = sid(700)
    ts_a = [0.000, 0.100]
    ts_b = [0.000, 0.200]
    payload_a, payload_b = _build_pairing_payloads(ts_a, ts_b, session_id)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="pairing"):
        points = pairing.triangulate_cycle(payload_a, payload_b)

    assert len(points) == 1

    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("outside_window" in m for m in debug_msgs), debug_msgs

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    summaries = [m for m in info_msgs if "pairing cycle complete" in m]
    assert len(summaries) == 1, info_msgs
    summary = summaries[0]
    assert "pairs_in_a=2" in summary
    assert "pairs_in_b=2" in summary
    assert "pairs_out=1" in summary
    assert "drop_outside_window=1" in summary
    assert "drop_near_parallel=0" in summary
    assert session_id in summary


def test_max_dt_env_override(monkeypatch):
    session_id = sid(701)
    ts_a = [0.000, 0.100]
    ts_b = [0.000, 0.120]
    payload_a, payload_b = _build_pairing_payloads(ts_a, ts_b, session_id)

    points_default = pairing.triangulate_cycle(payload_a, payload_b)
    assert len(points_default) == 1

    monkeypatch.setattr(pairing, "_MAX_DT_S", 0.030)
    points_wide = pairing.triangulate_cycle(payload_a, payload_b)
    assert len(points_wide) == 2


# ----- cleanup_expired_sessions --------------------------------------------


def _make_session_json(dir_path, session_id: str, age_seconds: float) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / f"session_{session_id}.json"
    p.write_text("{}")
    mtime = time.time() - age_seconds
    os.utime(p, (mtime, mtime))


def test_cleanup_expired_sessions_removes_old(tmp_path):
    pitches = tmp_path / "pitches"
    _make_session_json(pitches, "s_deadbeef", age_seconds=90 * 86400.0)
    _make_session_json(pitches, "s_cafef00d", age_seconds=0.0)

    sessions, files, bytes_removed = cleanup_expired_sessions(
        tmp_path, days=30, dry_run=False
    )

    assert sessions == 1
    assert files == 1
    assert bytes_removed > 0
    assert not (pitches / "session_s_deadbeef.json").exists()
    assert (pitches / "session_s_cafef00d.json").exists()


def test_cleanup_expired_sessions_disabled_days_zero(tmp_path):
    pitches = tmp_path / "pitches"
    _make_session_json(pitches, "s_deadbeef", age_seconds=90 * 86400.0)

    result = cleanup_expired_sessions(tmp_path, days=0, dry_run=False)

    assert result == (0, 0, 0)
    assert (pitches / "session_s_deadbeef.json").exists()


def test_cleanup_expired_sessions_dry_run(tmp_path):
    pitches = tmp_path / "pitches"
    _make_session_json(pitches, "s_deadbeef", age_seconds=90 * 86400.0)

    sessions, files, bytes_removed = cleanup_expired_sessions(
        tmp_path, days=30, dry_run=True
    )

    assert sessions == 1
    assert files == 1
    assert bytes_removed > 0
    assert (pitches / "session_s_deadbeef.json").exists()


# ----- detection.py / video.py unit -----------------------------------------


def test_detect_ball_finds_blue_circle_centroid():
    from detection import HSVRange, detect_ball
    frame = _make_frame_with_ball(320, 240, (160.0, 120.0), radius=15)
    result = detect_ball(frame, HSVRange.default())
    assert result is not None
    px, py = result
    # Sub-pixel centroid from connected-components stats should be very
    # close to the drawn center.
    assert abs(px - 160.0) < 1.0
    assert abs(py - 120.0) < 1.0


def test_detect_ball_returns_none_on_empty_frame():
    from detection import HSVRange, detect_ball
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert detect_ball(frame, HSVRange.default()) is None


def test_iter_frames_reconstructs_absolute_pts(tmp_path):
    from video import iter_frames
    frames = [_make_frame_with_ball(320, 240, (10.0 + i, 10.0 + i)) for i in range(5)]
    mov = tmp_path / "seq.mov"
    _encode_mov(frames, fps=30.0, path=mov)
    start = 100.5
    yielded = list(iter_frames(mov, video_start_pts_s=start))
    assert len(yielded) == 5
    # PTS must be monotonic and start at `video_start_pts_s`.
    ptses = [t for t, _ in yielded]
    assert ptses[0] == pytest.approx(start, abs=1e-6)
    for a, b in zip(ptses, ptses[1:]):
        assert b > a
    # Frame 4 is 4/30 s after the start.
    assert ptses[-1] == pytest.approx(start + 4.0 / 30.0, abs=1.0 / 30.0)
