"""Tests for Mac-sync clock-offset triangulation path and /sync/time endpoint."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import (
    FramePayload,
    IntrinsicsPayload,
    PitchPayload,
    triangulate_cycle,
    app,
)


# ── helpers copied from test_server.py (scene geometry) ──────────────────────

def _look_at(pos: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 0.0, 1.0])):
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
    y_cam = -up - np.dot(-up, z_cam) * z_cam
    y_cam /= np.linalg.norm(y_cam)
    x_cam = np.cross(y_cam, z_cam)
    R_cw = np.column_stack([x_cam, y_cam, z_cam])
    R_wc = R_cw.T
    t_wc = -R_wc @ pos
    return R_wc, t_wc


from triangulate import build_K


def _make_scene():
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)
    C_a = np.array([1.8, -2.5, 1.2])
    C_b = np.array([-1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R_a, t_a = _look_at(C_a, target)
    R_b, t_b = _look_at(C_b, target)
    H_a = K @ np.column_stack([R_a[:, 0], R_a[:, 1], t_a])
    H_b = K @ np.column_stack([R_b[:, 0], R_b[:, 1], t_b])
    H_a /= H_a[2, 2]
    H_b /= H_b[2, 2]
    return K, fx, fy, cx, cy, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b)


def _project(K: np.ndarray, R: np.ndarray, t: np.ndarray, P_world: np.ndarray):
    P_cam = R @ P_world + t
    theta_x = float(np.arctan2(P_cam[0], P_cam[2]))
    theta_z = float(np.arctan2(P_cam[1], P_cam[2]))
    return theta_x, theta_z


# ── /sync/time endpoint ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    yield


def test_sync_time_returns_float():
    client = TestClient(app)
    r = client.post("/sync/time")
    assert r.status_code == 200
    body = r.json()
    assert "server_time_s" in body
    ts = body["server_time_s"]
    assert isinstance(ts, float)
    assert ts > 0.0


def test_sync_time_monotonically_increases():
    """Two successive calls must return increasing timestamps."""
    client = TestClient(app)
    r1 = client.post("/sync/time")
    r2 = client.post("/sync/time")
    t1 = r1.json()["server_time_s"]
    t2 = r2.json()["server_time_s"]
    # Monotonic: second call must be >= first.
    assert t2 >= t1


# ── mac-aligned triangulation ─────────────────────────────────────────────────

def _build_payload_mac(
    cam_id: str,
    R: np.ndarray,
    t: np.ndarray,
    K: np.ndarray,
    H: np.ndarray,
    path: np.ndarray,
    ts_phone: np.ndarray,
    mac_offset: float,
) -> PitchPayload:
    """Build a PitchPayload whose frame timestamps are on the phone clock.

    mac_offset = phone_clock - server_clock.
    So phone_ts = server_ts + mac_offset, and
    server_ts  = phone_ts  - mac_offset  (what server aligns to).
    """
    frames = []
    for i, (Pi, ti_phone) in enumerate(zip(path, ts_phone)):
        tx, tz = _project(K, R, t, Pi)
        frames.append(
            FramePayload(
                frame_index=i,
                timestamp_s=float(ti_phone),
                theta_x_rad=tx,
                theta_z_rad=tz,
                ball_detected=True,
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
        mac_clock_offset_s=mac_offset,
    )


def test_mac_sync_triangulation_with_offset():
    """Two cameras with independent clock offsets should still triangulate correctly.

    Camera A: phone clock runs 0.050 s ahead of server clock (offset_A = +0.050).
    Camera B: phone clock runs -0.030 s behind server clock (offset_B = -0.030).
    After alignment both cameras' frames should have matching server-relative times
    and triangulate to the ground-truth path.
    """
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()

    ts_server = np.linspace(0.0, 0.4, 20)  # true server-clock timestamps
    offset_a = +0.050   # phone_A clock = server_clock + 0.050
    offset_b = -0.030   # phone_B clock = server_clock - 0.030

    ts_phone_a = ts_server + offset_a
    ts_phone_b = ts_server + offset_b

    path = np.stack(
        [
            0.1 * np.sin(ts_server * 10),
            18.0 - 45.0 * ts_server,
            2.0 - 4.9 * ts_server**2,
        ],
        axis=1,
    )

    payload_a = _build_payload_mac("A", R_a, t_a, K, H_a, path, ts_phone_a, offset_a)
    payload_b = _build_payload_mac("B", R_b, t_b, K, H_b, path, ts_phone_b, offset_b)

    points, sync_method = triangulate_cycle(payload_a, payload_b)

    assert sync_method == "mac"
    assert len(points) == len(path), f"Expected {len(path)} points, got {len(points)}"

    recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in points])
    np.testing.assert_allclose(recovered, path, atol=1e-5)
    residuals = [p.residual_m for p in points]
    assert max(residuals) < 1e-5


def test_mac_sync_falls_back_to_flash_when_offset_missing():
    """If only one camera has mac_clock_offset_s, fall back to flash path."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()

    ts = np.linspace(0.0, 0.1, 5)
    path = np.stack(
        [np.zeros(5), 10.0 - 20.0 * ts, 1.5 - ts],
        axis=1,
    )

    # Camera A has offset, B does not.
    frames_a, frames_b = [], []
    for i, (Pi, ti) in enumerate(zip(path, ts)):
        tx_a, tz_a = _project(K, R_a, t_a, Pi)
        tx_b, tz_b = _project(K, R_b, t_b, Pi)
        frames_a.append(FramePayload(
            frame_index=i, timestamp_s=float(ti),
            theta_x_rad=tx_a, theta_z_rad=tz_a, ball_detected=True,
        ))
        frames_b.append(FramePayload(
            frame_index=i, timestamp_s=float(ti),
            theta_x_rad=tx_b, theta_z_rad=tz_b, ball_detected=True,
        ))

    payload_a = PitchPayload(
        camera_id="A", flash_frame_index=0, flash_timestamp_s=0.0,
        cycle_number=1, frames=frames_a,
        intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
        homography=H_a.flatten().tolist(),
        mac_clock_offset_s=0.010,  # only A has it
    )
    payload_b = PitchPayload(
        camera_id="B", flash_frame_index=0, flash_timestamp_s=0.0,
        cycle_number=1, frames=frames_b,
        intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
        homography=H_b.flatten().tolist(),
        # mac_clock_offset_s intentionally absent
    )

    points, sync_method = triangulate_cycle(payload_a, payload_b)
    assert sync_method == "flash"  # must fall back
    assert len(points) == len(path)


def test_mac_sync_via_api_records_sync_method(tmp_path, monkeypatch):
    """End-to-end: POST /pitch with mac offsets → response contains sync_method='mac'.

    Both cameras observe the same server-clock instant (ts_server=1.0).
    Each phone timestamp = ts_server + mac_offset_for_that_phone.
    After mac alignment, server recovers ts_server for both → frames pair within 8ms.
    """
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()

    P_true = np.array([0.1, 0.3, 1.0])
    tx_a, tz_a = _project(K, R_a, t_a, P_true)
    tx_b, tz_b = _project(K, R_b, t_b, P_true)

    ts_server = 1.0          # common server-clock instant both cameras see
    offset_a = +0.020        # phone_A clock = server_clock + 0.020
    offset_b = -0.015        # phone_B clock = server_clock - 0.015
    ts_phone_a = ts_server + offset_a  # 1.020
    ts_phone_b = ts_server + offset_b  # 0.985

    def make_body(cam_id, tx, tz, H, ts_phone, offset):
        return {
            "camera_id": cam_id,
            "flash_frame_index": 0,
            "flash_timestamp_s": 0.0,
            "cycle_number": 1,
            "frames": [{"frame_index": 0, "timestamp_s": ts_phone,
                        "theta_x_rad": tx, "theta_z_rad": tz, "ball_detected": True}],
            "intrinsics": {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]},
            "homography": H.flatten().tolist(),
            "mac_clock_offset_s": offset,
        }

    client = TestClient(app)
    client.post("/pitch", json=make_body("A", tx_a, tz_a, H_a, ts_phone_a, offset_a))
    r = client.post("/pitch", json=make_body("B", tx_b, tz_b, H_b, ts_phone_b, offset_b))
    body = r.json()
    assert body["triangulated_points"] == 1
    assert body["sync_method"] == "mac"
