"""End-to-end triangulation test + FastAPI ingest smoke test."""
from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import app


def _post_pitch(client, body: dict, audio_bytes: bytes | None = None):
    """POST /pitch as multipart/form-data. `body` is the JSON payload."""
    data = {"payload": json.dumps(body)}
    if audio_bytes is not None:
        return client.post(
            "/pitch",
            data=data,
            files={"audio": ("clap.wav", audio_bytes, "audio/wav")},
        )
    return client.post("/pitch", data=data)
from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
)


def _look_at(pos: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 0.0, 1.0])):
    """Build R_wc, t_wc for a camera at `pos` looking at `target`.

    Camera frame (OpenCV): X right, Y down, Z forward.
    """
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
    # Image down direction in world: project -up onto plane perpendicular to z_cam.
    y_cam = -up - np.dot(-up, z_cam) * z_cam
    y_cam /= np.linalg.norm(y_cam)
    x_cam = np.cross(y_cam, z_cam)
    R_cw = np.column_stack([x_cam, y_cam, z_cam])  # cam→world
    R_wc = R_cw.T
    t_wc = -R_wc @ pos
    return R_wc, t_wc


def _project(K: np.ndarray, R: np.ndarray, t: np.ndarray, P_world: np.ndarray):
    P_cam = R @ P_world + t
    theta_x = float(np.arctan2(P_cam[0], P_cam[2]))
    theta_z = float(np.arctan2(P_cam[1], P_cam[2]))
    return theta_x, theta_z


def _make_scene():
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)

    # Cameras placed as a stereo pair looking at plate.
    # World: X right, Y forward (depth from plate front), Z up.
    C_a = np.array([1.8, -2.5, 1.2])
    C_b = np.array([-1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R_a, t_a = _look_at(C_a, target)
    R_b, t_b = _look_at(C_b, target)

    # Homography each camera would measure for plate plane (Z=0).
    # H = K [r1 r2 t] (up to scale; normalize h33=1 to match iPhone's convention).
    H_a = K @ np.column_stack([R_a[:, 0], R_a[:, 1], t_a])
    H_b = K @ np.column_stack([R_b[:, 0], R_b[:, 1], t_b])
    H_a /= H_a[2, 2]
    H_b /= H_b[2, 2]
    return K, fx, fy, cx, cy, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b)


# --------------------------- Unit tests --------------------------------------


def test_recover_extrinsics_matches_ground_truth():
    K, *_ , (R_a, t_a, _, H_a), _ = _make_scene()
    R_rec, t_rec = recover_extrinsics(K, H_a)
    np.testing.assert_allclose(R_rec, R_a, atol=1e-8)
    np.testing.assert_allclose(t_rec, t_a, atol=1e-8)


def test_triangulate_perfect_rays_recovers_point():
    K, *_, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b) = _make_scene()
    P_true = np.array([0.25, 0.6, 1.55])  # 25cm right, 60cm deep, 1.55m up

    theta_x_a, theta_z_a = _project(K, R_a, t_a, P_true)
    theta_x_b, theta_z_b = _project(K, R_b, t_b, P_true)

    # Follow the server pipeline: K+H → recovered pose → world-space rays → triangulate.
    R_a_r, t_a_r = recover_extrinsics(K, H_a)
    R_b_r, t_b_r = recover_extrinsics(K, H_b)
    C_a_r = camera_center_world(R_a_r, t_a_r)
    C_b_r = camera_center_world(R_b_r, t_b_r)

    d_a = R_a_r.T @ angle_ray_cam(theta_x_a, theta_z_a)
    d_b = R_b_r.T @ angle_ray_cam(theta_x_b, theta_z_b)

    P_rec, gap = triangulate_rays(C_a_r, d_a, C_b_r, d_b)
    np.testing.assert_allclose(P_rec, P_true, atol=1e-6)
    assert gap < 1e-6


def test_triangulate_sweeps_ball_path():
    """Simulate a short pitch: ball starts high-back, ends low-front over plate."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    # 20 points along a parabolic path.
    ts = np.linspace(0.0, 0.4, 20)
    # Start (0, 18m, 2m), end (0, 0, 0.5m), slight lateral curve, gravity drop.
    path = np.stack(
        [
            0.1 * np.sin(ts * 10),
            18.0 - 45.0 * ts,
            2.0 - 4.9 * ts**2 + 2.0 * ts - 2.0 * ts,  # simple drop
        ],
        axis=1,
    )

    from main import PitchPayload, FramePayload, IntrinsicsPayload, triangulate_cycle

    def build_payload(cam_id: str, R, t, H):
        frames = []
        for i, (Pi, ti) in enumerate(zip(path, ts)):
            tx, tz = _project(K, R, t, Pi)
            frames.append(
                FramePayload(
                    frame_index=i, timestamp_s=float(ti),
                    theta_x_rad=tx, theta_z_rad=tz, ball_detected=True,
                )
            )
        return PitchPayload(
            camera_id=cam_id,
            flash_frame_index=0,
            flash_timestamp_s=0.0,
            cycle_number=1,
            frames=frames,
            intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
            homography=H.flatten().tolist(),
        )

    payload_a = build_payload("A", R_a, t_a, H_a)
    payload_b = build_payload("B", R_b, t_b, H_b)
    points, sync_method = triangulate_cycle(payload_a, payload_b)
    assert sync_method == "flash"  # no mac_clock_offset_s in payloads
    assert len(points) == len(path)
    recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in points])
    np.testing.assert_allclose(recovered, path, atol=1e-6)
    residuals = [p.residual_m for p in points]
    assert max(residuals) < 1e-6


# --------------------------- API smoke tests ---------------------------------


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    yield


def test_status_initially_idle():
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_post_pitch_single_camera_then_both_triangulates():
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    tx_a, tz_a = _project(K, R_a, t_a, P_true)
    tx_b, tz_b = _project(K, R_b, t_b, P_true)

    def make_body(cam_id, tx, tz, R, t, H):
        return {
            "camera_id": cam_id,
            "flash_frame_index": 0,
            "flash_timestamp_s": 0.0,
            "cycle_number": 7,
            "frames": [
                {"frame_index": 0, "timestamp_s": 0.0,
                 "theta_x_rad": tx, "theta_z_rad": tz, "ball_detected": True},
            ],
            "intrinsics": {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]},
            "homography": H.flatten().tolist(),
        }

    client = TestClient(app)

    r1 = _post_pitch(client, make_body("A", tx_a, tz_a, R_a, t_a, H_a))
    assert r1.status_code == 200
    assert r1.json()["triangulated_points"] == 0  # B not yet received

    r2 = _post_pitch(client, make_body("B", tx_b, tz_b, R_b, t_b, H_b))
    assert r2.status_code == 200
    assert r2.json()["triangulated_points"] == 1

    r3 = client.get("/results/latest")
    body = r3.json()
    assert body["cycle_number"] == 7
    pt = body["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6

    # /pitch response summary surfaces mean residual + peak z for iOS feedback.
    body2 = r2.json()
    assert body2["paired"] is True
    assert body2["cycle"] == 7
    assert body2["error"] is None
    assert body2["mean_residual_m"] < 1e-6
    assert abs(body2["peak_z_m"] - P_true[2]) < 1e-6


def test_persistence_reloads_state_across_process_restart(tmp_path):
    """A fresh State pointed at an existing data dir re-triangulates stored pitches."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.2, 0.5, 1.1])
    tx_a, tz_a = _project(K, R_a, t_a, P_true)
    tx_b, tz_b = _project(K, R_b, t_b, P_true)

    def make_body(cam_id, tx, tz, H):
        return main.PitchPayload(
            camera_id=cam_id,
            flash_frame_index=0,
            flash_timestamp_s=0.0,
            cycle_number=42,
            frames=[
                main.FramePayload(
                    frame_index=0, timestamp_s=0.0,
                    theta_x_rad=tx, theta_z_rad=tz, ball_detected=True,
                )
            ],
            intrinsics=main.IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
            homography=H.flatten().tolist(),
        )

    s1 = main.State(data_dir=tmp_path)
    s1.record(make_body("A", tx_a, tz_a, H_a))
    s1.record(make_body("B", tx_b, tz_b, H_b))
    del s1

    # Fresh State simulating server restart: same data dir, no in-memory carry-over.
    s2 = main.State(data_dir=tmp_path)
    latest = s2.latest()
    assert latest is not None
    assert latest.cycle_number == 42
    assert latest.camera_a_received and latest.camera_b_received
    assert len(latest.points) == 1
    pt = latest.points[0]
    assert abs(pt.x_m - P_true[0]) < 1e-6
    assert abs(pt.y_m - P_true[1]) < 1e-6
    assert abs(pt.z_m - P_true[2]) < 1e-6
