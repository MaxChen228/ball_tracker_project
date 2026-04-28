"""Shared test helpers for the server test suite.

Rule: a helper lands here only when **≥ 2 test files** consume it. If you
find yourself adding a helper only one test file uses, keep it local to
that file. This keeps `_test_helpers.py` small and makes each test file's
dependency surface explicit.

Pytest collection: the leading underscore in the filename prevents pytest
from collecting this module as tests (we have no test_ functions here).
"""
from __future__ import annotations

import json as _json
from fractions import Fraction
from pathlib import Path

import av  # type: ignore[import]
import cv2
import numpy as np
from fastapi.testclient import TestClient

import main
from triangulate import build_K


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
    intr: dict = {"fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]}
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


def _seed_ready_stereo(client: TestClient, K: np.ndarray, H_a: np.ndarray, H_b: np.ndarray) -> None:
    def _post_cal(cam: str, H: np.ndarray):
        return client.post(
            "/calibration",
            json={
                "camera_id": cam,
                "intrinsics": {
                    "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
                },
                "homography": H.flatten().tolist(),
                "image_width_px": 1920,
                "image_height_px": 1080,
            },
        )

    assert _post_cal("A", H_a).status_code == 200
    assert _post_cal("B", H_b).status_code == 200
    main.state.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)
    main.state.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0)


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
