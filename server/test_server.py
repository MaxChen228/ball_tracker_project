"""End-to-end triangulation test + FastAPI ingest smoke test.

The server no longer accepts per-frame ball data on the wire. The iPhone
uploads only the H.264 MOV + minimal metadata, and the server decodes +
detects + triangulates. Tests here either:

  (a) Pure math — exercise `triangulate_*` / `recover_extrinsics` directly.
  (b) Direct-State tests — construct a `PitchPayload` with pre-populated
      `frames` (bypassing the /pitch path) to verify state management,
      lock discipline, pairing edge cases.
  (c) /pitch E2E — encode a synthetic MOV with a blue circle whose pixel
      coords are ground-truthed projections of a known 3D point; POST
      the MOV + JSON and verify server-side detection recovers the 3D
      point within numerical tolerance.
"""
from __future__ import annotations

import io
import json as _json
import logging
import os
import threading
import time
from fractions import Fraction
from pathlib import Path

import av  # type: ignore[import]
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
import pairing
from cleanup_old_sessions import cleanup_expired_sessions
from conftest import sid
from main import app
from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)


# ---------------------------- Scene helpers ----------------------------------


def _look_at(pos: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 0.0, 1.0])):
    """Build R_wc, t_wc for a camera at `pos` looking at `target`.

    Camera frame (OpenCV): X right, Y down, Z forward.
    """
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
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


def _project_pixels(K: np.ndarray, R: np.ndarray, t: np.ndarray, P_world: np.ndarray):
    """Project a world point to (undistorted) pixel coords (u, v)."""
    P_cam = R @ P_world + t
    u = K[0, 0] * P_cam[0] / P_cam[2] + K[0, 2]
    v = K[1, 1] * P_cam[1] / P_cam[2] + K[1, 2]
    return float(u), float(v)


def _make_scene():
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)

    # Stereo pair looking at the plate.
    C_a = np.array([1.8, -2.5, 1.2])
    C_b = np.array([-1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R_a, t_a = _look_at(C_a, target)
    R_b, t_b = _look_at(C_b, target)

    # Homography each camera measures for plate plane (Z=0).
    H_a = K @ np.column_stack([R_a[:, 0], R_a[:, 1], t_a])
    H_b = K @ np.column_stack([R_b[:, 0], R_b[:, 1], t_b])
    H_a /= H_a[2, 2]
    H_b /= H_b[2, 2]
    return K, fx, fy, cx, cy, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b)


# ---------------------------- MOV / frame synthesis --------------------------


def _make_frame_with_ball(
    width: int,
    height: int,
    center_px: tuple[float, float] | None,
    radius: int = 15,
) -> np.ndarray:
    """Paint a yellow-green tennis-ball-coloured circle on a black BGR
    frame. `center_px=None` means "no ball" (pure black → HSV detection
    returns None)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    if center_px is not None:
        cx_i = int(round(center_px[0]))
        cy_i = int(round(center_px[1]))
        # BGR (50, 220, 220) → HSV ≈ (30, 197, 220): yellow-green hue,
        # saturated, bright enough to pass the default HSV range
        # (25-55, 90-255, 90-255) for a fluorescent tennis ball.
        cv2.circle(frame, (cx_i, cy_i), radius, (50, 220, 220), -1)
    return frame


def _encode_mov(frames: list[np.ndarray], fps: float, path: Path) -> None:
    """Encode a list of BGR uint8 frames as H.264 MOV. First frame's
    container PTS is 0 by convention (PyAV starts from 0 for freshly
    opened streams) — the server reconstructs the absolute iOS session
    clock via `video_start_pts_s` on the upload payload."""
    if not frames:
        raise ValueError("need at least one frame")
    height, width = frames[0].shape[:2]
    container = av.open(str(path), mode="w")
    try:
        rate = Fraction(int(round(fps * 1000)), 1000)
        stream = container.add_stream("h264", rate=rate)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        for bgr in frames:
            frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def _post_pitch(client, payload_body: dict, video_path: Path | None):
    data = {"payload": _json.dumps(payload_body)}
    if video_path is None:
        return client.post("/pitch", data=data)
    with open(video_path, "rb") as f:
        files = {"video": (video_path.name, f.read(), "video/quicktime")}
    return client.post("/pitch", data=data, files=files)


def _base_payload(
    cam_id: str,
    session_id: str,
    K: np.ndarray,
    H: np.ndarray,
    *,
    anchor_ts: float | None = 0.0,
    video_start_pts: float = 0.0,
    video_fps: float = 30.0,
    width: int = 1920,
    height: int = 1080,
    distortion: list[float] | None = None,
    capture_telemetry: dict | None = None,
) -> dict:
    intr: dict = {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]}
    if distortion is not None:
        intr["distortion"] = distortion
    payload = {
        "camera_id": cam_id,
        "session_id": session_id,
        "sync_id": "sy_deadbeef",
        "sync_anchor_timestamp_s": anchor_ts,
        "video_start_pts_s": video_start_pts,
        "video_fps": video_fps,
        "intrinsics": intr,
        "homography": H.flatten().tolist(),
        "image_width_px": width,
        "image_height_px": height,
    }
    if capture_telemetry is not None:
        payload["capture_telemetry"] = capture_telemetry
    return payload


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


# --------------------------- API smoke + detection E2E -----------------------


def test_status_initially_idle():
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def _encode_single_ball_mov(
    tmp_path: Path,
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    P_true: np.ndarray,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    n_frames: int = 3,
    filename: str,
) -> Path:
    u, v = _project_pixels(K, R, t, P_true)
    frames = [
        _make_frame_with_ball(width, height, (u, v)) for _ in range(n_frames)
    ]
    out = tmp_path / filename
    _encode_mov(frames, fps=fps, path=out)
    return out


def test_post_pitch_with_video_triangulates_server_side(tmp_path):
    """End-to-end: paint a blue circle at the projected pixel of a known
    3D point on both cameras' MOVs, POST them, verify the server-detected
    centroid triangulates to ≤ 1 px of the true 3D location."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(7)

    client = TestClient(app)

    mov_a = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="a.mov"
    )
    mov_b = _encode_single_ball_mov(
        tmp_path, K, R_b, t_b, P_true, filename="b.mov"
    )

    r1 = _post_pitch(client, _base_payload("A", session_id, K, H_a), mov_a)
    assert r1.status_code == 200, r1.text
    assert r1.json()["triangulated_points"] == 0  # B not yet received

    r2 = _post_pitch(client, _base_payload("B", session_id, K, H_b), mov_b)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["paired"] is True
    # Server detection runs as a BackgroundTask (post-response), so the
    # per-request triangulated_points reflects only what was available
    # when the response was composed. The authoritative count comes from
    # /results/{sid}, queried below once background detection finished.
    assert body2["detection_pending"] is True

    # The server detected pixel can be sub-pixel off from ground truth
    # due to connected-components centroid quantisation, so allow a small
    # triangulation tolerance.
    r3 = client.get(f"/results/{session_id}").json()
    assert len(r3["points"]) >= 1
    pt = r3["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 2e-3
    assert abs(pt["y_m"] - P_true[1]) < 2e-3
    assert abs(pt["z_m"] - P_true[2]) < 2e-3


def test_post_pitch_without_video_or_frames_returns_422(tmp_path):
    """Mode-one requires a video; mode-two requires frames. Sending neither
    means there's nothing to triangulate — reject up-front instead of
    silently recording an empty pitch."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    client = TestClient(app)
    r = _post_pitch(client, _base_payload("A", sid(501), K, H_a), None)
    assert r.status_code == 422, r.text


def test_post_pitch_mode_two_accepts_frames_without_video(tmp_path):
    """Mode-two (on-device detection) path: iPhone posts precomputed frames
    alongside metadata, no MOV. Server must skip decode + detection and
    triangulate directly against the uploaded frames."""
    K, *_, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b) = _make_scene()
    P_true = np.array([0.15, 0.35, 1.1])
    session_id = sid(600)
    client = TestClient(app)

    # Project P_true into each camera to get ground-truth px/py.
    def _project_to_px(R, t):
        P_cam = R @ P_true + t
        u = K[0, 0] * P_cam[0] / P_cam[2] + K[0, 2]
        v = K[1, 1] * P_cam[1] / P_cam[2] + K[1, 2]
        return float(u), float(v)

    px_a, py_a = _project_to_px(R_a, t_a)
    px_b, py_b = _project_to_px(R_b, t_b)

    frames_a = [{
        "frame_index": 0,
        "timestamp_s": 0.0,
        "px": px_a, "py": py_a,
        "ball_detected": True,
    }]
    frames_b = [{
        "frame_index": 0,
        "timestamp_s": 0.0,
        "px": px_b, "py": py_b,
        "ball_detected": True,
    }]

    body_a = _base_payload("A", session_id, K, H_a)
    body_a["frames"] = frames_a
    body_b = _base_payload("B", session_id, K, H_b)
    body_b["frames"] = frames_b

    r1 = _post_pitch(client, body_a, None)
    assert r1.status_code == 200, r1.text
    assert r1.json()["clip"] is None
    assert r1.json()["triangulated_points"] == 0

    r2 = _post_pitch(client, body_b, None)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["paired"] is True
    assert body2["triangulated_points"] == 1
    assert body2["clip"] is None

    result = client.get(f"/results/{session_id}").json()
    pt = result["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6


def test_pitch_upload_fills_from_calibration_db(tmp_path):
    """Phase-1 iOS decoupling: a pitch upload without intrinsics /
    homography / image dims gets those fields filled from the per-camera
    calibration snapshot persisted via POST /calibration. The enriched
    pitch on disk carries the filled values so restart-reload triangulation
    keeps working unchanged."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(700)
    client = TestClient(app)

    # Seed the calibration DB for camera A via the public endpoint so the
    # test exercises the same code path the iPhone actually uses.
    cal_body = {
        "camera_id": "A",
        "intrinsics": {
            "fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        },
        "homography": H_a.flatten().tolist(),
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    r_cal = client.post("/calibration", json=cal_body)
    assert r_cal.status_code == 200, r_cal.text

    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="fill.mov"
    )

    # Slim wire payload — NO intrinsics / homography / image dims / video_fps.
    slim_body = {
        "camera_id": "A",
        "session_id": session_id,
        "sync_id": "sy_deadbeef",
        "sync_anchor_timestamp_s": 0.0,
        "video_start_pts_s": 0.0,
    }
    r = _post_pitch(client, slim_body, mov)
    assert r.status_code == 200, r.text

    # Verify the enriched pitch now carries the values from the calibration DB.
    stored = main.state.pitches[("A", session_id)]
    assert stored.intrinsics is not None
    assert stored.homography is not None and len(stored.homography) == 9
    assert stored.image_width_px == 1920
    assert stored.image_height_px == 1080
    assert abs(stored.intrinsics.fx - K[0, 0]) < 1e-9


def test_pitch_upload_rejected_when_no_calibration(tmp_path):
    """Phase-1 iOS decoupling contract: if the iPhone omits intrinsics and
    the server has no calibration snapshot on file for that camera, the
    upload is rejected with 422. "Calibrate before you pitch" replaces the
    old "echo your calibration on every upload" path."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="nocal.mov"
    )

    client = TestClient(app)
    # No prior POST /calibration → state._calibrations is empty.
    slim_body = {
        "camera_id": "A",
        "session_id": sid(701),
        "sync_id": "sy_deadbeef",
        "sync_anchor_timestamp_s": 0.0,
        "video_start_pts_s": 0.0,
    }
    r = _post_pitch(client, slim_body, mov)
    assert r.status_code == 422, r.text
    assert "no calibration on file" in r.text


def test_post_pitch_anchorless_sets_error(tmp_path):
    """Upload with `sync_anchor_timestamp_s=null` is accepted (so the clip
    still lands on disk for forensics) but the session is flagged
    `error="no time sync"` and triangulation is skipped."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="anchorless.mov"
    )
    client = TestClient(app)
    body = _base_payload("A", sid(502), K, H_a, anchor_ts=None)
    r = _post_pitch(client, body, mov)
    assert r.status_code == 200, r.text
    assert r.json()["error"] == "no time sync"


def test_dashboard_drives_mode_two_end_to_end(tmp_path):
    """End-to-end integration: dashboard picks on_device → arm → each
    phone POSTs frames-only → server triangulates → events + viewer
    surface the on-device tag → no MOV ever lands on disk.

    This is the smoke test that protects the whole mode-two control
    plane; any future change that silently breaks a link in the chain
    (set_mode, arm snapshot, /pitch frames-only branch, events mode
    tagging, viewer banner) will trip this test."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.2, 0.4, 1.1])
    client = TestClient(app)

    # 1. Dashboard flips global capture mode.
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "on_device"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["capture_mode"] == "on_device"

    # 2. Arm — session.mode should snapshot the dashboard choice.
    arm = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm["session"]["id"]
    assert arm["session"]["mode"] == "on_device"
    assert arm["session"]["armed"] is True

    # 3. /status mirrors the armed session + locked mode.
    status = client.get("/status").json()
    assert status["session"]["mode"] == "on_device"
    assert status["capture_mode"] == "on_device"

    # 4. Both phones post frames-only (mirroring what BTDetectionSession
    #    would emit after MOG2 warmup).
    def _to_px(R: np.ndarray, t: np.ndarray) -> tuple[float, float]:
        return _project_pixels(K, R, t, P_true)

    ua, va = _to_px(R_a, t_a)
    ub, vb = _to_px(R_b, t_b)
    body_a = _base_payload("A", session_id, K, H_a)
    body_a["frames"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ua, "py": va, "ball_detected": True,
    }]
    body_b = _base_payload("B", session_id, K, H_b)
    body_b["frames"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ub, "py": vb, "ball_detected": True,
    }]

    r_a = _post_pitch(client, body_a, None)
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["clip"] is None

    r_b = _post_pitch(client, body_b, None)
    assert r_b.status_code == 200, r_b.text
    rb = r_b.json()
    assert rb["paired"] is True
    assert rb["triangulated_points"] == 1
    assert rb["clip"] is None

    # 5. /results has the ground-truth 3D point.
    result = client.get(f"/results/{session_id}").json()
    pt = result["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6

    # 6. Dashboard events row is tagged on_device.
    events = client.get("/events").json()
    matched = [e for e in events if e["session_id"] == session_id]
    assert matched, events
    assert matched[0]["mode"] == "on_device"
    assert matched[0]["n_triangulated"] == 1

    # 7. Viewer hero banner surfaces mode + no video is embedded.
    viewer_html = client.get(f"/viewer/{session_id}").text
    assert "mode on-device" in viewer_html
    assert "no clips on disk" in viewer_html
    assert "<video" not in viewer_html

    # 8. Final invariant: no MOV was ever written to disk for this
    #    session. The whole point of mode-two is bandwidth + storage
    #    savings, so if this flips we've lost the primary win.
    videos_on_disk = list(
        main.state.video_dir.glob(f"session_{session_id}_*")
    )
    assert videos_on_disk == []


def test_dashboard_drives_mode_one_end_to_end(tmp_path):
    """Symmetric mode-one integration: dashboard picks camera_only → arm
    → each phone POSTs a MOV → server detects + triangulates → events
    + viewer show the camera-only tag + the MOV is accessible."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    client = TestClient(app)

    # 1. Dashboard locks mode to camera_only (default, but exercise the
    #    explicit toggle path).
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "camera_only"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200

    # 2. Arm.
    arm = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm["session"]["id"]
    assert arm["session"]["mode"] == "camera_only"

    # 3. Both phones upload a MOV with a ball at the projected pixel.
    mov_a = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="e2e_a.mov"
    )
    mov_b = _encode_single_ball_mov(
        tmp_path, K, R_b, t_b, P_true, filename="e2e_b.mov"
    )
    r_a = _post_pitch(client, _base_payload("A", session_id, K, H_a), mov_a)
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["clip"] is not None
    r_b = _post_pitch(client, _base_payload("B", session_id, K, H_b), mov_b)
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["paired"] is True
    # Server detection runs post-response as a BackgroundTask; query the
    # canonical result after TestClient drains background tasks.
    assert len(client.get(f"/results/{session_id}").json()["points"]) >= 1

    # 4. Events tags it as camera_only.
    events = client.get("/events").json()
    matched = [e for e in events if e["session_id"] == session_id]
    assert matched, events
    assert matched[0]["mode"] == "camera_only"

    # 5. Viewer banner + embedded <video> element.
    viewer_html = client.get(f"/viewer/{session_id}").text
    assert "mode camera-only" in viewer_html
    assert "<video" in viewer_html
    assert 'id="real-plate-overlay-A"' in viewer_html
    assert 'id="real-plate-overlay-B"' in viewer_html


def test_dashboard_drives_dual_mode_end_to_end(tmp_path):
    """Dual mode smoke test: iPhone ships MOV + frames_on_device. Server
    runs its own detection on the MOV AND keeps the iOS stream, then
    triangulates both independently. Viewer surfaces both strips + both
    3D point clouds; events row is tagged `dual`."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    client = TestClient(app)

    # 1. Dashboard → dual.
    r = client.post(
        "/sessions/set_mode",
        data={"mode": "dual"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["capture_mode"] == "dual"

    # 2. Arm — session snapshot must be dual.
    arm = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm["session"]["id"]
    assert arm["session"]["mode"] == "dual"

    # 3. Upload A and B with a MOV each + precomputed frames_on_device
    #    covering the same projected pixel. In dual mode the server
    #    overwrites `frames` with its own detection output; the iOS list
    #    lives on `frames_on_device` untouched.
    mov_a = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="dual_a.mov"
    )
    mov_b = _encode_single_ball_mov(
        tmp_path, K, R_b, t_b, P_true, filename="dual_b.mov"
    )
    ua, va = _project_pixels(K, R_a, t_a, P_true)
    ub, vb = _project_pixels(K, R_b, t_b, P_true)

    body_a = _base_payload("A", session_id, K, H_a)
    body_a["frames_on_device"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ua, "py": va, "ball_detected": True,
    }]
    body_b = _base_payload("B", session_id, K, H_b)
    body_b["frames_on_device"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ub, "py": vb, "ball_detected": True,
    }]

    r_a = _post_pitch(client, body_a, mov_a)
    assert r_a.status_code == 200, r_a.text
    r_b = _post_pitch(client, body_b, mov_b)
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["paired"] is True

    # 4. /results carries BOTH triangulation streams.
    result = client.get(f"/results/{session_id}").json()
    assert result["points"], "server-side triangulation missing"
    assert result["points_on_device"], "on-device triangulation missing"
    # On-device input is a single synthetic frame → exactly one point.
    assert len(result["points_on_device"]) == 1
    od_pt = result["points_on_device"][0]
    assert abs(od_pt["x_m"] - P_true[0]) < 1e-6
    assert abs(od_pt["y_m"] - P_true[1]) < 1e-6
    assert abs(od_pt["z_m"] - P_true[2]) < 1e-6

    # 5. Events row tagged dual + exposes both counts.
    events = client.get("/events").json()
    matched = [e for e in events if e["session_id"] == session_id]
    assert matched, events
    assert matched[0]["mode"] == "dual"
    assert matched[0]["n_triangulated"] >= 1
    assert matched[0]["n_triangulated_on_device"] == 1

    # 6. Reconstruction scene carries rays tagged per source.
    scene = client.get(f"/reconstruction/{session_id}").json()
    sources_seen = {r.get("source", "server") for r in scene["rays"]}
    assert "server" in sources_seen
    assert "on_device" in sources_seen
    assert scene["triangulated_on_device"], "scene missing on-device points"

    # 7. Viewer HTML includes the dual strip + per-layer visibility matrix.
    viewer_html = client.get(f"/viewer/{session_id}").text
    assert "detection-canvas-on-device" in viewer_html
    assert 'data-layer="traj"' in viewer_html
    assert 'data-src="on_device"' in viewer_html
    # Hero banner must surface `mode dual`, not regress to camera-only —
    # previously the viewer only checked MOV presence and ignored
    # frames_on_device, so dual sessions mis-labelled as camera-only.
    assert "mode dual" in viewer_html
    # MOV must still be embedded (dual uploads the clip).
    assert "<video" in viewer_html


def test_live_websocket_stream_pairs_frames_and_emits_events(monkeypatch):
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    client = TestClient(app)

    cal_a = {
        "camera_id": "A",
        "intrinsics": {
            "fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        },
        "homography": H_a.flatten().tolist(),
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    cal_b = {
        "camera_id": "B",
        "intrinsics": {
            "fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        },
        "homography": H_b.flatten().tolist(),
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    assert client.post("/calibration", json=cal_a).status_code == 200
    assert client.post("/calibration", json=cal_b).status_code == 200

    events: list[tuple[str, dict]] = []

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    with client.websocket_connect("/ws/device/A") as ws_a, client.websocket_connect("/ws/device/B") as ws_b:
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        ws_a.send_json({
            "type": "hello",
            "cam": "A",
            "time_synced": True,
            "time_sync_id": "sy_deadbeef",
            "sync_anchor_timestamp_s": 0.0,
        })
        ws_b.send_json({
            "type": "hello",
            "cam": "B",
            "time_synced": True,
            "time_sync_id": "sy_deadbeef",
            "sync_anchor_timestamp_s": 0.0,
        })
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        arm = client.post(
            "/sessions/arm",
            json={"paths": ["live"]},
            headers={"Accept": "application/json"},
        )
        assert arm.status_code == 200, arm.text
        session_id = arm.json()["session"]["id"]
        assert arm.json()["session"]["paths"] == ["live"]

        assert ws_a.receive_json()["type"] == "arm"
        assert ws_b.receive_json()["type"] == "arm"

        ua, va = _project_pixels(K, R_a, t_a, P_true)
        ub, vb = _project_pixels(K, R_b, t_b, P_true)
        ws_a.send_json({
            "type": "frame",
            "cam": "A",
            "sid": session_id,
            "i": 0,
            "ts": 0.25,
            "px": ua,
            "py": va,
            "detected": True,
        })
        ws_b.send_json({
            "type": "frame",
            "cam": "B",
            "sid": session_id,
            "i": 0,
            "ts": 0.25,
            "px": ub,
            "py": vb,
            "detected": True,
        })
        ws_a.send_json({
            "type": "cycle_end",
            "cam": "A",
            "sid": session_id,
            "reason": "disarmed",
        })
        ws_b.send_json({
            "type": "cycle_end",
            "cam": "B",
            "sid": session_id,
            "reason": "disarmed",
        })

    result = client.get(f"/results/{session_id}").json()
    assert len(result["points"]) == 1
    pt = result["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6
    assert result["paths_completed"] == ["live"]
    assert result["triangulated_by_path"]["live"]

    live_status = client.get("/status").json()["live_session"]
    assert live_status["session_id"] == session_id
    assert live_status["frame_counts"] == {"A": 1, "B": 1}
    assert live_status["point_count"] == 1

    event_names = [name for name, _ in events]
    assert "device_status" in event_names
    assert ("session_armed", {"sid": session_id, "paths": ["live"], "armed_at": arm.json()["session"]["started_at"]}) in events
    assert any(name == "frame_count" and data["cam"] == "A" and data["count"] == 1 for name, data in events)
    assert any(name == "frame_count" and data["cam"] == "B" and data["count"] == 1 for name, data in events)
    assert any(name == "point" and data["sid"] == session_id and abs(data["x"] - P_true[0]) < 1e-6 for name, data in events)
    assert any(name == "path_completed" and data["sid"] == session_id and data["cam"] == "A" for name, data in events)
    assert any(name == "path_completed" and data["sid"] == session_id and data["point_count"] == 1 for name, data in events)


def test_dual_mode_on_device_surfaces_before_server_detection(tmp_path, monkeypatch):
    """Early-surface guarantee: in dual mode, `result.points_on_device`
    becomes available as soon as both cameras' payloads arrive, even if
    server MOV detection never finishes. Implemented by monkey-patching
    `detect_pitch` to a no-op — if the on-device triangulation were
    coupled to server detection, stubbing detection would also zero out
    `points_on_device`. It must not."""
    import main as server_main

    detect_calls: list[Path] = []

    def _stub_detect(clip_path, video_start_pts_s):
        detect_calls.append(Path(clip_path))
        return []

    monkeypatch.setattr(server_main, "detect_pitch", _stub_detect)

    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.05, 0.2, 1.1])
    client = TestClient(app)

    client.post(
        "/sessions/set_mode",
        data={"mode": "dual"},
        headers={"Accept": "application/json"},
    )
    arm = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm["session"]["id"]

    mov_a = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="early_a.mov"
    )
    mov_b = _encode_single_ball_mov(
        tmp_path, K, R_b, t_b, P_true, filename="early_b.mov"
    )
    ua, va = _project_pixels(K, R_a, t_a, P_true)
    ub, vb = _project_pixels(K, R_b, t_b, P_true)

    body_a = _base_payload("A", session_id, K, H_a)
    body_a["frames_on_device"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ua, "py": va, "ball_detected": True,
    }]
    body_b = _base_payload("B", session_id, K, H_b)
    body_b["frames_on_device"] = [{
        "frame_index": 0, "timestamp_s": 0.0,
        "px": ub, "py": vb, "ball_detected": True,
    }]

    r_a = _post_pitch(client, body_a, mov_a)
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["detection_pending"] is True

    r_b = _post_pitch(client, body_b, mov_b)
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["detection_pending"] is True

    # Canonical result: on-device path populated from the iOS frames list;
    # server path intentionally empty because detect_pitch was stubbed.
    result = client.get(f"/results/{session_id}").json()
    assert result["points_on_device"], "on-device triangulation must run independently of server detect_pitch"
    assert result["points"] == [], "stub detect_pitch → server points stays empty"
    od = result["points_on_device"][0]
    assert abs(od["x_m"] - P_true[0]) < 1e-6
    assert abs(od["y_m"] - P_true[1]) < 1e-6
    assert abs(od["z_m"] - P_true[2]) < 1e-6

    # Background detection was still invoked for each MOV (just stubbed
    # to return []) — proves the scheduling path fires, not that the
    # handler skipped it.
    assert len(detect_calls) == 2

    # Events row immediately reports the on-device count.
    events = client.get("/events").json()
    matched = [e for e in events if e["session_id"] == session_id]
    assert matched
    assert matched[0]["n_triangulated_on_device"] == 1


def test_pitch_analysis_merges_late_on_device_frames_and_capture_telemetry(tmp_path):
    main.state = main.State(data_dir=tmp_path)
    client = TestClient(app)

    K, *_ , (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.08, 0.55, 1.10])
    u_a, v_a = _project_pixels(K, R_a, t_a, P_true)
    u_b, v_b = _project_pixels(K, R_b, t_b, P_true)

    session_id = "s_a1b2c3d4"
    body_a = _base_payload(
        "A", session_id, K, H_a, anchor_ts=0.0, video_start_pts=0.0,
        capture_telemetry={
            "width_px": 1920,
            "height_px": 1080,
            "target_fps": 240.0,
            "applied_fps": 240.0,
            "format_fov_deg": 73.828,
            "format_index": 38,
            "is_video_binned": True,
            "tracking_exposure_cap": "shutter_1000",
            "applied_max_exposure_s": 0.001,
        },
    )
    body_b = _base_payload(
        "B", session_id, K, H_b, anchor_ts=0.0, video_start_pts=0.0,
        capture_telemetry={
            "width_px": 1920,
            "height_px": 1080,
            "target_fps": 240.0,
            "applied_fps": 240.0,
            "format_fov_deg": 73.828,
            "format_index": 39,
            "is_video_binned": True,
            "tracking_exposure_cap": "shutter_1000",
            "applied_max_exposure_s": 0.001,
        },
    )
    body_a["frames"] = [{
        "frame_index": 0, "timestamp_s": 0.0, "px": u_a, "py": v_a, "ball_detected": True,
    }]
    body_b["frames"] = [{
        "frame_index": 0, "timestamp_s": 0.0, "px": u_b, "py": v_b, "ball_detected": True,
    }]

    assert client.post("/pitch", data={"payload": _json.dumps(body_a)}).status_code == 200
    assert client.post("/pitch", data={"payload": _json.dumps(body_b)}).status_code == 200

    analysis_a = {
        "camera_id": "A",
        "session_id": session_id,
        "frames_on_device": [{
            "frame_index": 0, "timestamp_s": 0.0, "px": u_a, "py": v_a, "ball_detected": True,
        }],
        "capture_telemetry": body_a["capture_telemetry"],
    }
    analysis_b = {
        "camera_id": "B",
        "session_id": session_id,
        "frames_on_device": [{
            "frame_index": 0, "timestamp_s": 0.0, "px": u_b, "py": v_b, "ball_detected": True,
        }],
        "capture_telemetry": body_b["capture_telemetry"],
    }

    r = client.post("/pitch_analysis", json=analysis_a)
    assert r.status_code == 200
    r = client.post("/pitch_analysis", json=analysis_b)
    assert r.status_code == 200
    assert r.json()["triangulated_on_device"] == 1

    result = client.get(f"/results/{session_id}").json()
    assert len(result["points_on_device"]) == 1

    events = client.get("/events").json()
    match = [e for e in events if e["session_id"] == session_id]
    assert match
    assert match[0]["capture_telemetry"]["A"]["tracking_exposure_cap"] == "shutter_1000"

    health = main._build_viewer_health(session_id)
    assert health["cameras"]["A"]["capture_telemetry"]["format_index"] == 38
    assert health["cameras"]["B"]["capture_telemetry"]["format_fov_deg"] == 73.828


def test_pitch_analysis_requires_existing_base_pitch(tmp_path):
    main.state = main.State(data_dir=tmp_path)
    client = TestClient(app)
    r = client.post("/pitch_analysis", json={
        "camera_id": "A",
        "session_id": "s_deadbeef",
        "frames_on_device": [{
            "frame_index": 0,
            "timestamp_s": 0.0,
            "px": 10.0,
            "py": 10.0,
            "ball_detected": True,
        }],
    })
    assert r.status_code == 409


def test_pitch_writes_annotated_clip_alongside_raw(tmp_path):
    """After /pitch, server has BOTH the raw MOV and a `_annotated` sibling
    that re-encodes the clip with a circle drawn on every detected frame."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(540)
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="src.mov"
    )
    client = TestClient(app)
    r = _post_pitch(client, _base_payload("A", session_id, K, H_a), mov)
    assert r.status_code == 200, r.text

    raw = main.state.video_dir / f"session_{session_id}_A.mov"
    annotated = main.state.video_dir / f"session_{session_id}_A_annotated.mov"
    assert raw.exists()
    assert annotated.exists()
    # The annotated MOV must be a valid H.264 file that PyAV can decode.
    from video import count_frames
    assert count_frames(annotated) > 0


def test_pitch_clip_persisted_with_canonical_filename(tmp_path):
    """Uploaded MOV is saved under data/videos/session_{sid}_{cam}.{ext}."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(503)
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, P_true, filename="src.mov"
    )
    client = TestClient(app)
    r = _post_pitch(client, _base_payload("A", session_id, K, H_a), mov)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clip"]["filename"] == f"session_{session_id}_A.mov"
    assert body["clip"]["bytes"] > 0

    saved = main.state.video_dir / f"session_{session_id}_A.mov"
    assert saved.exists()
    assert saved.stat().st_size > 0


# --------------------------- Distortion plumbing -----------------------------


def test_nonzero_distortion_recovers_true_point_via_mov(tmp_path):
    """Pre-distort projected pixels via cv2.projectPoints, render them as
    MOV frames, and verify triangulation with server-side detection +
    5-coef undistortion recovers the original 3D point."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    # Point chosen to keep the distorted projection inside frame — the
    # high-distortion case at (0.15, 0.55, 1.3) pushes camera A's pixel
    # above the image top, which the centroid detector cannot see.
    P_true = np.array([0.1, 0.3, 1.0])
    # Modest smartphone-lens distortion. Bigger k1 starts shoving
    # peripheral pixels off-screen in the synthesised scene.
    dist = np.array([0.04, -0.12, 0.001, -0.0015, 0.02], dtype=np.float64)

    def project_distorted(R: np.ndarray, t: np.ndarray) -> tuple[float, float]:
        rvec, _ = cv2.Rodrigues(R)
        tvec = t.reshape(3, 1)
        pts_obj = P_true.reshape(1, 1, 3).astype(np.float64)
        proj, _ = cv2.projectPoints(pts_obj, rvec, tvec, K.astype(np.float64), dist)
        u, v = float(proj[0, 0, 0]), float(proj[0, 0, 1])
        return u, v

    u_a, v_a = project_distorted(R_a, t_a)
    u_b, v_b = project_distorted(R_b, t_b)

    # Paint the distorted pixels into the MOVs.
    frames_a = [_make_frame_with_ball(1920, 1080, (u_a, v_a)) for _ in range(3)]
    frames_b = [_make_frame_with_ball(1920, 1080, (u_b, v_b)) for _ in range(3)]
    mov_a = tmp_path / "dist_a.mov"
    mov_b = tmp_path / "dist_b.mov"
    _encode_mov(frames_a, fps=30.0, path=mov_a)
    _encode_mov(frames_b, fps=30.0, path=mov_b)

    session_id = sid(99)
    client = TestClient(app)

    dist_list = dist.tolist()
    body_a = _base_payload("A", session_id, K, H_a, distortion=dist_list)
    body_b = _base_payload("B", session_id, K, H_b, distortion=dist_list)

    r1 = _post_pitch(client, body_a, mov_a)
    assert r1.status_code == 200, r1.text
    r2 = _post_pitch(client, body_b, mov_b)
    assert r2.status_code == 200, r2.text
    # Background-detect path: poll the result endpoint for the final count.
    result_points = client.get(f"/results/{session_id}").json()["points"]
    assert len(result_points) >= 1

    pt = result_points[0]
    # Detection + undistortion + triangulation: a couple mm of slack is
    # plenty for a ~1 m-distant ball encoded via lossy H.264.
    assert abs(pt["x_m"] - P_true[0]) < 5e-3
    assert abs(pt["y_m"] - P_true[1]) < 5e-3
    assert abs(pt["z_m"] - P_true[2]) < 5e-3


# --------------------------- save_clip ---------------------------------------


def test_save_clip_writes_atomically_and_overwrites(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_id = sid(900)
    first = s.save_clip("A", session_id, b"alpha", "mov")
    assert first == tmp_path / "videos" / f"session_{session_id}_A.mov"
    assert first.read_bytes() == b"alpha"

    second = s.save_clip("A", session_id, b"beta beta", "mov")
    assert second == first
    assert second.read_bytes() == b"beta beta"

    assert not any(p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir())


def test_save_clip_rejects_path_traversal_extensions(tmp_path):
    s = main.State(data_dir=tmp_path)
    path_bad = s.save_clip("B", sid(7), b"x", "../etc/passwd")
    assert path_bad.parent == tmp_path / "videos"
    assert path_bad.suffix == ".mov"

    path_empty = s.save_clip("B", sid(8), b"y", "")
    assert path_empty.suffix == ".mov"


# --------------------------- Payload validation ------------------------------


def test_malformed_payload_returns_422(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    client = TestClient(app)
    with open(mov, "rb") as f:
        files = {"video": (mov.name, f.read(), "video/quicktime")}
    r = client.post(
        "/pitch",
        data={"payload": '{"bogus": true}'},
        files=files,
    )
    assert r.status_code == 422


def test_path_traversing_camera_id_is_rejected(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    body = _base_payload("A", sid(600), K, H_a)
    body["camera_id"] = "../etc"
    client = TestClient(app)
    r = _post_pitch(client, body, mov)
    assert r.status_code == 422


def test_malformed_session_id_is_rejected(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    body = _base_payload("A", "../etc", K, H_a)
    client = TestClient(app)
    r = _post_pitch(client, body, mov)
    assert r.status_code == 422


# --------------------------- Concurrency + DoS guards ------------------------


def test_save_clip_is_lock_protected(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_id = sid(910)

    payloads = [bytes([i]) * (640 * 1024) for i in (0x11, 0x22, 0x33, 0x44)]
    barrier = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def worker(data: bytes):
        try:
            barrier.wait(timeout=5.0)
            s.save_clip("A", session_id, data, "mov")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "worker hung"
    assert not errors, errors

    final_path = tmp_path / "videos" / f"session_{session_id}_A.mov"
    assert final_path.exists()
    final_bytes = final_path.read_bytes()
    assert final_bytes in payloads, (
        f"clip is a torn mix of writes (len={len(final_bytes)})"
    )
    assert not any(
        p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir()
    )


def test_record_does_not_hold_lock_during_io(tmp_path, monkeypatch):
    """`State.record` must release `self._lock` before its disk writes."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(920)

    s = main.State(data_dir=tmp_path)

    release = threading.Event()
    entered_io = threading.Event()
    original_atomic_write = s._atomic_write

    def blocking_atomic_write(path, payload):
        entered_io.set()
        assert release.wait(timeout=5.0), "release event never fired"
        return original_atomic_write(path, payload)

    monkeypatch.setattr(s, "_atomic_write", blocking_atomic_write)

    u, v = _project_pixels(K, R_a, t_a, P_true)
    pitch = main.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=[
            main.FramePayload(
                frame_index=0, timestamp_s=0.0,
                px=u, py=v, ball_detected=True,
            )
        ],
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )

    recorder = threading.Thread(target=s.record, args=(pitch,))
    recorder.start()
    assert entered_io.wait(timeout=5.0), "record never reached _atomic_write"

    t0 = time.perf_counter()
    s.heartbeat("A")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 50, (
        f"heartbeat took {elapsed_ms:.1f} ms — record is still holding the lock"
    )

    release.set()
    recorder.join(timeout=5.0)
    assert not recorder.is_alive()


def test_pitch_upload_rejects_oversize_video():
    """413 guard fires on declared Content-Length before the handler
    touches the multipart parts."""
    body = _base_payload(
        "A", sid(930),
        K=build_K(1600, 1600, 960, 540),
        H=np.eye(3),
    )
    client = TestClient(app)
    fake_video = b"\x00" * 1024
    files = {"video": ("clip.mov", fake_video, "video/quicktime")}
    data = {"payload": _json.dumps(body)}
    oversize = main._MAX_PITCH_UPLOAD_BYTES + 1
    r = client.post(
        "/pitch",
        data=data,
        files=files,
        headers={"Content-Length": str(oversize)},
    )
    assert r.status_code == 413, r.text


def test_pitch_upload_rejects_oversize_body_after_read():
    body = _base_payload(
        "A", sid(931),
        K=build_K(1600, 1600, 960, 540),
        H=np.eye(3),
    )
    client = TestClient(app)

    original_cap = main._MAX_PITCH_UPLOAD_BYTES
    try:
        main._MAX_PITCH_UPLOAD_BYTES = 4 * 1024
        fake_video = b"A" * (8 * 1024)
        files = {"video": ("clip.mov", fake_video, "video/quicktime")}
        data = {"payload": _json.dumps(body)}
        r = client.post("/pitch", data=data, files=files)
        assert r.status_code == 413, r.text
    finally:
        main._MAX_PITCH_UPLOAD_BYTES = original_cap


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


# --- Dashboard-triggered time-sync (CALIBRATE TIME button) ----------------


def test_sync_trigger_flags_all_online_cameras():
    """With no camera_ids argument, trigger_sync_command targets every
    currently-online camera and returns them sorted + deduped."""
    import main
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    dispatched = main.state.trigger_sync_command(None)
    assert dispatched == ["A", "B"]
    cmd_a, sync_id_a = main.state.consume_sync_command("A")
    cmd_b, sync_id_b = main.state.consume_sync_command("B")
    assert cmd_a == "start"
    assert cmd_b == "start"
    assert sync_id_a is not None
    assert sync_id_a == sync_id_b
    # Both cams show up in the pending-commands snapshot.
    assert main.state.pending_sync_commands() == {}


def test_sync_trigger_skips_armed_session():
    """Firing CALIBRATE TIME while a session is armed must dispatch to NO
    camera — running a chirp-listen in the middle of a recording would
    disrupt the armed clip."""
    import main
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    main.state.arm_session()
    dispatched = main.state.trigger_sync_command(None)
    assert dispatched == []
    assert main.state.pending_sync_commands() == {}


def test_sync_command_drains_on_heartbeat_consumption():
    """Once a phone consumes the flag via heartbeat, subsequent heartbeats
    don't re-fire (one-shot dispatch)."""
    import main
    main.state.heartbeat("A")
    main.state.trigger_sync_command(["A"])
    # First consume returns the command.
    first = main.state.consume_sync_command("A")
    assert first[0] == "start"
    assert first[1] is not None
    # Second consume is empty — flag drained.
    assert main.state.consume_sync_command("A") == (None, None)
    assert main.state.pending_sync_commands() == {}


def test_sync_command_expires_after_ttl(tmp_path, monkeypatch):
    """Stale flags self-expire after _SYNC_COMMAND_TTL_S so a command
    doesn't fire hours later if the operator gave up on the request."""
    import main
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state.heartbeat("A")
    main.state.trigger_sync_command(["A"])
    assert "A" in main.state.pending_sync_commands()
    # Advance past the TTL.
    clock[0] += main._SYNC_COMMAND_TTL_S + 1.0
    assert main.state.consume_sync_command("A") == (None, None)
    assert main.state.pending_sync_commands() == {}


def test_sync_claim_reuses_live_intent_then_rolls_after_window(tmp_path, monkeypatch):
    import main
    clock = [1000.0]

    def fake_time() -> float:
        return clock[0]

    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    first = main.state.claim_time_sync_intent()
    second = main.state.claim_time_sync_intent()
    assert first.id == second.id

    clock[0] += main._TIME_SYNC_INTENT_WINDOW_S + 0.1
    third = main.state.claim_time_sync_intent()
    assert third.id != first.id


def test_paired_payloads_with_mismatched_sync_ids_fail_before_triangulation(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_deadbeef"
    frame = [main.FramePayload(frame_index=0, timestamp_s=0.0, px=100.0, py=100.0, ball_detected=True)]
    pa = main.PitchPayload(
        camera_id="A",
        session_id=sid,
        sync_id="sy_aaaaaaaa",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frame,
    )
    pb = main.PitchPayload(
        camera_id="B",
        session_id=sid,
        sync_id="sy_bbbbbbbb",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frame,
    )
    main.state.record(pa)
    result = main.state.record(pb)
    assert result.error == "sync id mismatch"
    assert result.points == []


# ----------------- Runtime tunables (chirp threshold + heartbeat interval) ----


def _fetch_ws_settings(test_client, camera_id: str):
    with test_client.websocket_connect(f"/ws/device/{camera_id}") as ws:
        ws.send_json({"type": "hello"})
        for _ in range(5):
            msg = ws.receive_json()
            if msg.get("type") == "settings":
                return msg
        return {}


def test_chirp_threshold_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    # Default surfaces on /status.
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["chirp_detect_threshold"] == pytest.approx(0.18)

    # JSON push.
    r = client.post("/settings/chirp_threshold", json={"threshold": 0.27})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": pytest.approx(0.27)}

    # Surfaces on /status and WS settings message.
    assert client.get("/status").json()["chirp_detect_threshold"] == pytest.approx(0.27)
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["chirp_detect_threshold"] == pytest.approx(0.27)

    # Form push (HTML caller) redirects 303.
    r = client.post(
        "/settings/chirp_threshold",
        data={"threshold": "0.33"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.chirp_detect_threshold() == pytest.approx(0.33)

    # Persisted to disk.
    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["chirp_detect_threshold"] == pytest.approx(0.33)


def test_chirp_threshold_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in (0.0, -0.1, 1.5, 10.0):
        r = client.post("/settings/chirp_threshold", json={"threshold": bad})
        assert r.status_code == 400, f"expected 400 for {bad}"
    # State unchanged.
    assert main.state.chirp_detect_threshold() == pytest.approx(0.18)
    # Direct setter also raises.
    with pytest.raises(ValueError):
        main.state.set_chirp_detect_threshold(2.0)


def test_heartbeat_interval_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["heartbeat_interval_s"] == pytest.approx(1.0)

    r = client.post("/settings/heartbeat_interval", json={"interval_s": 3.5})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": pytest.approx(3.5)}

    assert client.get("/status").json()["heartbeat_interval_s"] == pytest.approx(3.5)
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["heartbeat_interval_s"] == pytest.approx(3.5)

    r = client.post(
        "/settings/heartbeat_interval",
        data={"interval_s": "5"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.heartbeat_interval_s() == pytest.approx(5.0)

    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["heartbeat_interval_s"] == pytest.approx(5.0)


def test_heartbeat_interval_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in (0.0, 0.5, 61.0, -1.0):
        r = client.post("/settings/heartbeat_interval", json={"interval_s": bad})
        assert r.status_code == 400, f"expected 400 for {bad}"
    assert main.state.heartbeat_interval_s() == pytest.approx(1.0)
    with pytest.raises(ValueError):
        main.state.set_heartbeat_interval_s(0.1)


def test_tracking_exposure_cap_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["tracking_exposure_cap"] == "frame_duration"

    r = client.post("/settings/tracking_exposure_cap", json={"mode": "shutter_1000"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": "shutter_1000"}

    assert client.get("/status").json()["tracking_exposure_cap"] == "shutter_1000"
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["tracking_exposure_cap"] == "shutter_1000"

    r = client.post(
        "/settings/tracking_exposure_cap",
        data={"mode": "shutter_500"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.tracking_exposure_cap().value == "shutter_500"

    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["tracking_exposure_cap"] == "shutter_500"


def test_tracking_exposure_cap_rejects_invalid_value(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in ("", "1/1000", "fast", "240fps"):
        r = client.post("/settings/tracking_exposure_cap", json={"mode": bad})
        assert r.status_code == 400, f"expected 400 for {bad!r}"
    assert main.state.tracking_exposure_cap().value == "frame_duration"


def test_runtime_settings_restored_from_disk_on_state_init(tmp_path):
    import main
    # Seed a file and confirm a fresh State picks it up.
    (tmp_path / "runtime_settings.json").write_text(
        _json.dumps({
            "chirp_detect_threshold": 0.42,
            "heartbeat_interval_s": 7.5,
            "tracking_exposure_cap": "shutter_1000",
        })
    )
    s = main.State(data_dir=tmp_path)
    assert s.chirp_detect_threshold() == pytest.approx(0.42)
    assert s.heartbeat_interval_s() == pytest.approx(7.5)
    assert s.tracking_exposure_cap().value == "shutter_1000"

    # Out-of-range values on disk are ignored, defaults retained.
    (tmp_path / "runtime_settings.json").write_text(
        _json.dumps({"chirp_detect_threshold": 99.0, "heartbeat_interval_s": 0.001})
    )
    s2 = main.State(data_dir=tmp_path)
    assert s2.chirp_detect_threshold() == pytest.approx(0.18)
    assert s2.heartbeat_interval_s() == pytest.approx(1.0)


# ---------------------------- Phase 4a · live preview -------------------------


def _minimal_jpeg() -> bytes:
    """A tiny valid-enough JPEG for the buffer round-trip tests. We don't
    decode it — the buffer treats the bytes opaquely — so a few bytes with
    a JPEG SOI/EOI is enough to represent a "frame"."""
    # SOI + a single APP0 + EOI — not a displayable image, but `push`
    # accepts it and `latest` round-trips exactly these bytes.
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def test_preview_push_rejected_when_not_requested():
    client = TestClient(app)
    r = client.post("/camera/A/preview_frame", content=_minimal_jpeg(),
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 409
    # Buffer must not have stored anything.
    assert main.state._preview.latest("A") is None


def test_preview_push_and_fetch_round_trip():
    client = TestClient(app)
    # Dashboard enables preview.
    r = client.post("/camera/A/preview_request", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": True}
    # Status surfaces the flag.
    assert client.get("/status").json()["preview_requested"] == {"A": True}
    # Phone pushes a frame (raw image/jpeg body).
    jpeg = _minimal_jpeg()
    r = client.post("/camera/A/preview_frame", content=jpeg,
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # Dashboard fetches the latest JPEG.
    r = client.get("/camera/A/preview")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == jpeg
    # Disable → flag drops AND cached frame is cleared.
    r = client.post("/camera/A/preview_request", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert main.state._preview.latest("A") is None
    r = client.get("/camera/A/preview")
    assert r.status_code == 404


def test_preview_request_flag_expires_after_ttl(tmp_path, monkeypatch):
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state._preview.request("A", enabled=True)
    assert main.state._preview.is_requested("A")
    # Walk past the TTL.
    clock[0] += main._PREVIEW_REQUEST_TTL_S + 0.1
    assert not main.state._preview.is_requested("A")
    # Lazy sweep dropped the entry — requested_map is empty.
    assert main.state._preview.requested_map() == {}


def test_preview_oversize_rejected_413():
    client = TestClient(app)
    client.post("/camera/A/preview_request", json={"enabled": True})
    # 2 MB + 1 byte.
    huge = b"\xff\xd8" + b"\x00" * (2 * 1024 * 1024)
    r = client.post("/camera/A/preview_frame", content=huge,
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 413
    assert main.state._preview.latest("A") is None


def test_status_surfaces_preview_requested_map():
    client = TestClient(app)
    # Initially empty.
    assert client.get("/status").json().get("preview_requested") == {}
    client.post("/camera/A/preview_request", json={"enabled": True})
    client.post("/camera/B/preview_request", json={"enabled": True})
    got = client.get("/status").json()["preview_requested"]
    assert got == {"A": True, "B": True}
    # WS response carries the per-camera scalar for the connected phone.
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["preview_requested"] is True
    # Turn A off; B's flag is independent.
    client.post("/camera/A/preview_request", json={"enabled": False})
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["preview_requested"] is False
    hb_json_b = _fetch_ws_settings(client, "B")
    assert hb_json_b["preview_requested"] is True


# =======================================================================
# Phase 5: dashboard auto-calibration + extended markers
# =======================================================================


def _render_aruco_scene(
    marker_world_xy: dict[int, tuple[float, float]],
    image_size: tuple[int, int] = (1920, 1080),
    scale_px_per_m: float = 800.0,
    center_px: tuple[float, float] | None = None,
    marker_side_m: float = 0.08,
) -> tuple[np.ndarray, np.ndarray]:
    """Render a synthetic BGR image with DICT_4X4_50 markers pasted at
    world-projected locations. Uses a pure-scale+translate homography so
    the inverse is exact and the registration math can be checked against
    sub-cm tolerances.

    Returns `(bgr_image, H_3x3)` where H maps world (wx, wy, 1) → image
    pixels in homogeneous coords (h33 normalised to 1)."""
    w_img, h_img = image_size
    if center_px is None:
        center_px = (w_img / 2.0, h_img / 2.0)
    H = np.array([
        [scale_px_per_m, 0.0, center_px[0]],
        [0.0, scale_px_per_m, center_px[1]],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)

    bgr = np.full((h_img, w_img, 3), 255, dtype=np.uint8)
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    side_px = int(round(marker_side_m * scale_px_per_m))
    assert side_px >= 40, "marker too small for robust detection"
    for mid, (wx, wy) in marker_world_xy.items():
        proj = H @ np.array([wx, wy, 1.0])
        cx, cy = proj[:2] / proj[2]
        x0 = int(round(cx - side_px / 2))
        y0 = int(round(cy - side_px / 2))
        if x0 < 0 or y0 < 0 or x0 + side_px > w_img or y0 + side_px > h_img:
            raise ValueError(f"marker {mid} falls off the canvas")
        marker_img = cv2.aruco.generateImageMarker(aruco_dict, mid, side_px)
        bgr[y0:y0 + side_px, x0:x0 + side_px] = marker_img[:, :, None]
    return bgr, H


def _jpeg_encode(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()


def _project_world(K: np.ndarray, R: np.ndarray, t: np.ndarray, P_world: np.ndarray) -> tuple[float, float]:
    P_cam = R @ P_world + t
    u = K[0, 0] * P_cam[0] / P_cam[2] + K[0, 2]
    v = K[1, 1] * P_cam[1] / P_cam[2] + K[1, 2]
    return float(u), float(v)


def _render_aruco_scene_3d(
    marker_world_xyz: dict[int, tuple[float, float, float]],
    *,
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    image_size: tuple[int, int] = (1920, 1080),
    marker_side_m: float = 0.08,
) -> np.ndarray:
    """Project DICT_4X4_50 markers into an arbitrary 3D scene.

    Each marker is rendered as a square billboard parallel to the plate plane
    (constant Z for all four corners). That is sufficient for robust ArUco
    detection and gives the dual-camera marker-scan tests a controlled 3D
    target set without needing a photoreal renderer.
    """
    w_img, h_img = image_size
    bgr = np.full((h_img, w_img, 3), 255, dtype=np.uint8)
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker_px = 200
    src_quad = np.array(
        [[0, 0], [marker_px - 1, 0], [marker_px - 1, marker_px - 1], [0, marker_px - 1]],
        dtype=np.float32,
    )
    half = marker_side_m / 2.0
    for mid, (x_m, y_m, z_m) in marker_world_xyz.items():
        marker_img = np.full((marker_px, marker_px), 255, dtype=np.uint8)
        core_px = 140
        margin = (marker_px - core_px) // 2
        core = cv2.aruco.generateImageMarker(aruco_dict, mid, core_px)
        marker_img[margin:margin + core_px, margin:margin + core_px] = core
        world_quad = np.array(
            [
                [x_m - half, y_m - half, z_m],
                [x_m + half, y_m - half, z_m],
                [x_m + half, y_m + half, z_m],
                [x_m - half, y_m + half, z_m],
            ],
            dtype=np.float64,
        )
        dst_quad = np.array(
            [_project_world(K, R, t, pt) for pt in world_quad],
            dtype=np.float32,
        )
        signed_area = 0.0
        for i in range(4):
            x1, y1 = dst_quad[i]
            x2, y2 = dst_quad[(i + 1) % 4]
            signed_area += float(x1 * y2 - x2 * y1)
        if signed_area < 0.0:
            dst_quad = dst_quad[[0, 3, 2, 1]]
        H = cv2.getPerspectiveTransform(src_quad, dst_quad)
        warped = cv2.warpPerspective(
            marker_img,
            H,
            (w_img, h_img),
            flags=cv2.INTER_NEAREST,
            borderValue=255,
        )
        mask = warped < 250
        bgr[mask] = np.repeat(warped[mask][:, None], 3, axis=1)
    return bgr


def _seed_calibration_frame(camera_id: str, jpeg: bytes) -> None:
    """Simulate an iPhone pushing a native-resolution calibration JPEG.
    Bypasses the request/TTL handshake — the polling loop in
    /calibration/auto finds the cached frame on its first iteration."""
    main.state.store_calibration_frame(camera_id, jpeg)


def test_calibration_auto_writes_snapshot_from_calibration_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    bgr, _H = _render_aruco_scene(PLATE_MARKER_WORLD)
    _seed_calibration_frame("A", _jpeg_encode(bgr))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["camera_id"] == "A"
    assert sorted(body["detected_ids"]) == sorted(PLATE_MARKER_WORLD.keys())
    assert body["missing_plate_ids"] == []
    assert body["n_extended_used"] == 0
    assert body["image_width_px"] == 1920
    assert body["image_height_px"] == 1080
    assert len(body["homography"]) == 9

    cal_state = client.get("/calibration/state").json()
    cam_ids = {c["camera_id"] for c in cal_state["calibrations"]}
    assert "A" in cam_ids
    assert (tmp_path / "calibrations" / "A.json").exists()


def test_calibration_auto_returns_422_when_too_few_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    partial = {k: PLATE_MARKER_WORLD[k] for k in (0, 1, 5)}
    bgr, _H = _render_aruco_scene(partial)
    _seed_calibration_frame("A", _jpeg_encode(bgr))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 422, r.text
    assert "need" in r.json()["detail"].lower()


def test_calibration_auto_returns_408_when_no_frame_delivered(tmp_path, monkeypatch):
    """No pre-seeded cal frame + no iOS uploader in the test harness →
    /calibration/auto polls the burst budget then times out with 408."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    r = client.post("/calibration/auto/A")
    assert r.status_code == 408, r.text
    assert "within 6 s" in r.json()["detail"].lower()


def test_calibration_auto_uses_pose_solver_when_3d_markers_available(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, _cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=7,
            x_m=-0.40,
            y_m=-0.60,
            z_m=0.15,
            on_plate_plane=False,
            source_camera_ids=["A", "B"],
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=12,
            x_m=-0.40,
            y_m=-0.40,
            z_m=0.0,
            on_plate_plane=True,
            source_camera_ids=["A", "B"],
        )
    )

    from calibration_solver import PLATE_MARKER_WORLD
    marker_xyz = {mid: (xy[0], xy[1], 0.0) for mid, xy in PLATE_MARKER_WORLD.items()}
    marker_xyz.update({
        7: (-0.40, -0.60, 0.15),
        12: (-0.40, -0.40, 0.0),
    })
    bgr_a = _render_aruco_scene_3d(marker_xyz, K=K, R=R_a, t=t_a)
    _seed_calibration_frame("A", _jpeg_encode(bgr_a))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["used_pose_solver"] is True
    assert body["n_3d_markers_used"] >= 1
    assert 7 in body["detected_ids"]


def test_markers_scan_triangulates_dual_camera_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    R_b, t_b, _C_b, H_b = cam_b
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="B",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_b.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )

    from calibration_solver import PLATE_MARKER_WORLD
    marker_xyz = {mid: (xy[0], xy[1], 0.0) for mid, xy in PLATE_MARKER_WORLD.items()}
    truth_new = {
        7: (-0.40, -0.60, 0.15),
        12: (-0.40, -0.40, 0.0),
    }
    marker_xyz.update(truth_new)
    bgr_a = _render_aruco_scene_3d(marker_xyz, K=K, R=R_a, t=t_a)
    bgr_b = _render_aruco_scene_3d(marker_xyz, K=K, R=R_b, t=t_b)
    _seed_calibration_frame("A", _jpeg_encode(bgr_a))
    _seed_calibration_frame("B", _jpeg_encode(bgr_b))

    r = client.post("/markers/scan?camera_a_id=A&camera_b_id=B")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    got = {row["marker_id"]: row for row in body["candidates"]}
    assert set(got.keys()) == {7, 12}
    for mid, (x_m, y_m, z_m) in truth_new.items():
        row = got[mid]
        assert abs(row["x_m"] - x_m) < 0.03
        assert abs(row["y_m"] - y_m) < 0.03
        assert abs(row["z_m"] - z_m) < 0.03
    assert got[12]["suggest_on_plate_plane"] is True
    assert got[7]["suggest_on_plate_plane"] is False


def test_markers_crud_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    state = main.state
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=7, x_m=1.0, y_m=2.0, z_m=0.0, on_plate_plane=True)
    )
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=8, x_m=-1.0, y_m=0.5, z_m=0.4, on_plate_plane=False)
    )
    assert client.get("/markers/state").json()["markers"] == [
        {
            "marker_id": 7,
            "label": None,
            "x_m": 1.0,
            "y_m": 2.0,
            "z_m": 0.0,
            "on_plate_plane": True,
            "residual_m": None,
            "source_camera_ids": [],
        },
        {
            "marker_id": 8,
            "label": None,
            "x_m": -1.0,
            "y_m": 0.5,
            "z_m": 0.4,
            "on_plate_plane": False,
            "residual_m": None,
            "source_camera_ids": [],
        },
    ]
    assert client.get("/calibration/markers").json()["markers"] == [
        {"id": 7, "wx": 1.0, "wy": 2.0},
    ]

    # Persistence: recreate State from the same dir, registry must survive.
    main.state = main.State(data_dir=tmp_path)
    persisted = {rec.marker_id: rec for rec in main.state._marker_registry.all_records()}
    assert persisted[7].on_plate_plane is True
    assert persisted[8].z_m == 0.4

    r = client.delete("/markers/7")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main.state._marker_registry.get(7) is None

    r = client.delete("/markers/99")
    assert r.status_code == 404

    r = client.post("/markers/clear")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared_count": 1}
    assert client.get("/markers/state").json()["markers"] == []


def test_markers_reject_plate_reserved_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    db = main.state._marker_registry
    for reserved in (0, 1, 2, 3, 4, 5):
        with pytest.raises(Exception):
            db.upsert(main.MarkerRecord(marker_id=reserved, x_m=0.0, y_m=0.0, z_m=0.0))
    with pytest.raises(Exception):
        db.upsert(main.MarkerRecord(marker_id=50, x_m=0.0, y_m=0.0, z_m=0.0))
