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
    """Paint a deep-blue circle on a black BGR frame. `center_px=None`
    means "no ball" (pure black → HSV detection returns None)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    if center_px is not None:
        cx_i = int(round(center_px[0]))
        cy_i = int(round(center_px[1]))
        # BGR (240, 60, 60) → HSV ≈ (120, 191, 240): blue hue, saturated,
        # bright enough to pass the default HSV range (100-130, 140-255,
        # 40-255).
        cv2.circle(frame, (cx_i, cy_i), radius, (240, 60, 60), -1)
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
) -> dict:
    intr: dict = {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]}
    if distortion is not None:
        intr["distortion"] = distortion
    return {
        "camera_id": cam_id,
        "session_id": session_id,
        "sync_anchor_timestamp_s": anchor_ts,
        "video_start_pts_s": video_start_pts,
        "video_fps": video_fps,
        "intrinsics": intr,
        "homography": H.flatten().tolist(),
        "image_width_px": width,
        "image_height_px": height,
    }


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
    assert body2["triangulated_points"] >= 1

    # The server detected pixel can be sub-pixel off from ground truth
    # due to connected-components centroid quantisation, so allow a small
    # triangulation tolerance.
    r3 = client.get(f"/results/{session_id}").json()
    pt = r3["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 2e-3
    assert abs(pt["y_m"] - P_true[1]) < 2e-3
    assert abs(pt["z_m"] - P_true[2]) < 2e-3


def test_post_pitch_missing_video_returns_422(tmp_path):
    """Video is now mandatory — iPhone always produces one. Missing video
    must fail validation, not silently succeed."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    client = TestClient(app)
    r = _post_pitch(client, _base_payload("A", sid(501), K, H_a), None)
    assert r.status_code == 422, r.text


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
    assert r2.json()["triangulated_points"] >= 1

    pt = client.get(f"/results/{session_id}").json()["points"][0]
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
