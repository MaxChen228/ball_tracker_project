"""End-to-end triangulation test + FastAPI ingest smoke test."""
from __future__ import annotations

import io
import json as _json
import threading
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
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


def _post_pitch(client, body: dict, video_bytes: bytes | None = None):
    """POST /pitch as multipart/form-data; optionally attach a video clip."""
    data = {"payload": _json.dumps(body)}
    if video_bytes is None:
        return client.post("/pitch", data=data)
    files = {"video": ("clip.mov", video_bytes, "video/quicktime")}
    return client.post("/pitch", data=data, files=files)


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
            session_id=sid(1),
            sync_anchor_frame_index=0,
            sync_anchor_timestamp_s=0.0,
            frames=frames,
            intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
            homography=H.flatten().tolist(),
        )

    payload_a = build_payload("A", R_a, t_a, H_a)
    payload_b = build_payload("B", R_b, t_b, H_b)
    points = triangulate_cycle(payload_a, payload_b)
    assert len(points) == len(path)
    recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in points])
    np.testing.assert_allclose(recovered, path, atol=1e-6)
    residuals = [p.residual_m for p in points]
    assert max(residuals) < 1e-6


# --------------------------- API smoke tests ---------------------------------
# Note: the autouse `_reset_main_state` fixture in conftest.py replaces
# `main.state` with a tmp_path-backed State before each test so these
# cases can hit the global app without leaking files across runs.


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

    session_id = sid(7)

    def make_body(cam_id, tx, tz, R, t, H):
        return {
            "camera_id": cam_id,
            "session_id": session_id,
            "sync_anchor_frame_index": 0,
            "sync_anchor_timestamp_s": 0.0,
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
    assert body["session_id"] == session_id
    pt = body["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6

    # /pitch response summary surfaces mean residual + peak z for iOS feedback.
    body2 = r2.json()
    assert body2["paired"] is True
    assert body2["session_id"] == session_id
    assert body2["error"] is None
    assert body2["mean_residual_m"] < 1e-6
    assert abs(body2["peak_z_m"] - P_true[2]) < 1e-6


def test_persistence_reloads_state_across_process_restart(tmp_path):
    """A fresh State pointed at an existing data dir re-triangulates stored pitches."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.2, 0.5, 1.1])
    tx_a, tz_a = _project(K, R_a, t_a, P_true)
    tx_b, tz_b = _project(K, R_b, t_b, P_true)

    session_id = sid(42)

    def make_body(cam_id, tx, tz, H):
        return main.PitchPayload(
            camera_id=cam_id,
            session_id=session_id,
            sync_anchor_frame_index=0,
            sync_anchor_timestamp_s=0.0,
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
    assert latest.session_id == session_id
    assert latest.camera_a_received and latest.camera_b_received
    assert len(latest.points) == 1
    pt = latest.points[0]
    assert abs(pt.x_m - P_true[0]) < 1e-6
    assert abs(pt.y_m - P_true[1]) < 1e-6
    assert abs(pt.z_m - P_true[2]) < 1e-6


# --------------------------- Distortion plumbing -----------------------------


def test_zero_distortion_with_pixels_matches_angle_path():
    """Posting px/py + distortion=[0]*5 must give identical triangulation to angles-only."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    # Two different path points across two cycles to exercise the plumbing.
    path = np.array([[0.1, 0.3, 1.0], [-0.2, 0.8, 1.4]])
    zero_dist = [0.0, 0.0, 0.0, 0.0, 0.0]

    def make_body(cam_id, P_true, R, t, H, session_id_: str, *, with_pixels: bool):
        tx, tz = _project(K, R, t, P_true)
        u, v = _project_pixels(K, R, t, P_true)
        body = {
            "camera_id": cam_id,
            "session_id": session_id_,
            "sync_anchor_frame_index": 0,
            "sync_anchor_timestamp_s": 0.0,
            "frames": [
                {
                    "frame_index": 0,
                    "timestamp_s": 0.0,
                    "theta_x_rad": tx,
                    "theta_z_rad": tz,
                    "ball_detected": True,
                    **({"px": u, "py": v} if with_pixels else {}),
                },
            ],
            "intrinsics": {
                "fx": K[0, 0],
                "fz": K[1, 1],
                "cx": K[0, 2],
                "cy": K[1, 2],
                **({"distortion": zero_dist} if with_pixels else {}),
            },
            "homography": H.flatten().tolist(),
        }
        return body

    client = TestClient(app)
    s1, s2, s3 = sid(1), sid(2), sid(3)

    # Session 1: angles only.
    _post_pitch(client, make_body("A", path[0], R_a, t_a, H_a, s1, with_pixels=False))
    _post_pitch(client, make_body("B", path[0], R_b, t_b, H_b, s1, with_pixels=False))
    pt_angles = client.get(f"/results/{s1}").json()["points"][0]

    # Session 2: pixels + zero distortion.
    _post_pitch(client, make_body("A", path[1], R_a, t_a, H_a, s2, with_pixels=True))
    _post_pitch(client, make_body("B", path[1], R_b, t_b, H_b, s2, with_pixels=True))
    pt_pixels = client.get(f"/results/{s2}").json()["points"][0]

    # Each session should recover its own true point.
    assert abs(pt_angles["x_m"] - path[0][0]) < 1e-6
    assert abs(pt_angles["y_m"] - path[0][1]) < 1e-6
    assert abs(pt_angles["z_m"] - path[0][2]) < 1e-6
    assert abs(pt_pixels["x_m"] - path[1][0]) < 1e-6
    assert abs(pt_pixels["y_m"] - path[1][1]) < 1e-6
    assert abs(pt_pixels["z_m"] - path[1][2]) < 1e-6

    # And for the SAME point, both paths must agree bit-for-bit (numerically):
    # reproject path[0] through pixel path and compare.
    _post_pitch(client, make_body("A", path[0], R_a, t_a, H_a, s3, with_pixels=True))
    _post_pitch(client, make_body("B", path[0], R_b, t_b, H_b, s3, with_pixels=True))
    pt_pixels_same = client.get(f"/results/{s3}").json()["points"][0]
    assert abs(pt_pixels_same["x_m"] - pt_angles["x_m"]) < 1e-9
    assert abs(pt_pixels_same["y_m"] - pt_angles["y_m"]) < 1e-9
    assert abs(pt_pixels_same["z_m"] - pt_angles["z_m"]) < 1e-9


def test_nonzero_distortion_recovers_true_point():
    """Pre-distort pixels via cv2.projectPoints, send them + coeffs back, verify recovery."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.15, 0.55, 1.3])

    # Realistic smartphone-lens distortion magnitude.
    dist = np.array([0.12, -0.25, 0.001, -0.0015, 0.08], dtype=np.float64)

    def project_distorted(R: np.ndarray, t: np.ndarray) -> tuple[float, float]:
        # cv2.projectPoints expects rvec+tvec (object→camera extrinsic).
        rvec, _ = cv2.Rodrigues(R)
        tvec = t.reshape(3, 1)
        pts_obj = P_true.reshape(1, 1, 3).astype(np.float64)
        proj, _ = cv2.projectPoints(pts_obj, rvec, tvec, K.astype(np.float64), dist)
        u, v = float(proj[0, 0, 0]), float(proj[0, 0, 1])
        return u, v

    u_a, v_a = project_distorted(R_a, t_a)
    u_b, v_b = project_distorted(R_b, t_b)

    # Sanity: with non-zero distortion the distorted pixels differ from the
    # pinhole projection, so angle-only triangulation would be wrong.
    u_a_pin, v_a_pin = _project_pixels(K, R_a, t_a, P_true)
    assert abs(u_a - u_a_pin) > 1e-3 or abs(v_a - v_a_pin) > 1e-3

    session_id = sid(99)

    def make_body(cam_id, u, v, H):
        return {
            "camera_id": cam_id,
            "session_id": session_id,
            "sync_anchor_frame_index": 0,
            "sync_anchor_timestamp_s": 0.0,
            "frames": [
                {
                    "frame_index": 0,
                    "timestamp_s": 0.0,
                    "px": u,
                    "py": v,
                    "ball_detected": True,
                },
            ],
            "intrinsics": {
                "fx": K[0, 0],
                "fz": K[1, 1],
                "cx": K[0, 2],
                "cy": K[1, 2],
                "distortion": dist.tolist(),
            },
            "homography": H.flatten().tolist(),
        }

    client = TestClient(app)
    r1 = _post_pitch(client, make_body("A", u_a, v_a, H_a))
    assert r1.status_code == 200
    r2 = _post_pitch(client, make_body("B", u_b, v_b, H_b))
    assert r2.status_code == 200
    assert r2.json()["triangulated_points"] == 1

    body = client.get(f"/results/{session_id}").json()
    pt = body["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-4
    assert abs(pt["y_m"] - P_true[1]) < 1e-4
    assert abs(pt["z_m"] - P_true[2]) < 1e-4


def test_undistorted_ray_cam_zero_dist_matches_angle_ray():
    """Unit: undistorted_ray_cam with zero coeffs equals angle_ray_cam derived from the pixel."""
    K = build_K(1600.0, 1600.0, 960.0, 540.0)
    u, v = 1234.5, 678.9
    theta_x = np.arctan2(u - K[0, 2], K[0, 0])
    theta_z = np.arctan2(v - K[1, 2], K[1, 1])
    d_angle = angle_ray_cam(theta_x, theta_z)
    d_pix = undistorted_ray_cam(u, v, K, np.zeros(5))
    np.testing.assert_allclose(d_pix, d_angle, atol=1e-12)


# --------------------------- Multipart + video clip --------------------------


def _minimal_pitch_body(session_id: str, cam_id: str = "A") -> dict:
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    tx, tz = _project(
        K, R_a if cam_id == "A" else R_b, t_a if cam_id == "A" else t_b, P_true
    )
    H = H_a if cam_id == "A" else H_b
    return {
        "camera_id": cam_id,
        "session_id": session_id,
        "sync_anchor_frame_index": 0,
        "sync_anchor_timestamp_s": 0.0,
        "frames": [
            {"frame_index": 0, "timestamp_s": 0.0,
             "theta_x_rad": tx, "theta_z_rad": tz, "ball_detected": True},
        ],
        "intrinsics": {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]},
        "homography": H.flatten().tolist(),
    }


def test_pitch_without_video_round_trips_like_before():
    """Regression guard: existing clients that send only the JSON payload still
    triangulate correctly under the new multipart endpoint."""
    client = TestClient(app)
    session_id = sid(501)
    r1 = _post_pitch(client, _minimal_pitch_body(session_id, "A"))
    assert r1.status_code == 200
    assert "clip" not in r1.json()
    r2 = _post_pitch(client, _minimal_pitch_body(session_id, "B"))
    assert r2.status_code == 200
    assert "clip" not in r2.json()
    assert r2.json()["triangulated_points"] == 1


def test_pitch_with_video_persists_clip_and_still_triangulates():
    """Attach a byte-string posing as a MOV clip and verify: (a) triangulation
    runs unchanged, (b) the clip is written under the state's video dir with
    the expected (session_id, camera_id) basename."""
    client = TestClient(app)
    session_id = sid(502)
    fake_video = b"fake mov bytes \x00\x01\x02" * 128  # small but non-empty
    r1 = _post_pitch(
        client, _minimal_pitch_body(session_id, "A"), video_bytes=fake_video
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["clip"]["filename"] == f"session_{session_id}_A.mov"
    assert body["clip"]["bytes"] == len(fake_video)

    clip_path = main.state.video_dir / f"session_{session_id}_A.mov"
    assert clip_path.exists()
    assert clip_path.read_bytes() == fake_video

    # Pair with B (no video) — triangulation still works.
    r2 = _post_pitch(client, _minimal_pitch_body(session_id, "B"))
    assert r2.status_code == 200
    assert r2.json()["triangulated_points"] == 1


def test_save_clip_writes_atomically_and_overwrites(tmp_path):
    """Unit-level: State.save_clip targets data/videos/ with a safe filename
    and overwrites on repeat uploads for the same (camera, session)."""
    s = main.State(data_dir=tmp_path)
    session_id = sid(900)
    first = s.save_clip("A", session_id, b"alpha", "mov")
    assert first == tmp_path / "videos" / f"session_{session_id}_A.mov"
    assert first.read_bytes() == b"alpha"

    # Overwriting preserves filename and swaps contents.
    second = s.save_clip("A", session_id, b"beta beta", "mov")
    assert second == first
    assert second.read_bytes() == b"beta beta"

    # No ".tmp" litter left over from atomic writes.
    assert not any(p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir())


def test_save_clip_rejects_path_traversal_extensions(tmp_path):
    """Malformed `ext` (path separators, empty) falls back to .mov so the
    filename stays inside data/videos/."""
    s = main.State(data_dir=tmp_path)
    path_bad = s.save_clip("B", sid(7), b"x", "../etc/passwd")
    assert path_bad.parent == tmp_path / "videos"
    assert path_bad.suffix == ".mov"

    path_empty = s.save_clip("B", sid(8), b"y", "")
    assert path_empty.suffix == ".mov"


def test_malformed_payload_returns_422():
    """A JSON form field that does not match PitchPayload shape yields 422,
    not 500, so iOS can surface a precise error."""
    client = TestClient(app)
    r = client.post("/pitch", data={"payload": '{"bogus": true}'})
    assert r.status_code == 422


def test_path_traversing_camera_id_is_rejected():
    """`camera_id` ends up in server-side file paths (pitch JSON + clip MOV).
    Pydantic must reject non-identifier values so the upload can't escape
    `data/` via `../` or embedded separators."""
    body = _minimal_pitch_body(sid(600), "A")
    body["camera_id"] = "../etc"
    client = TestClient(app)
    r = _post_pitch(client, body, video_bytes=b"x")
    assert r.status_code == 422


def test_malformed_session_id_is_rejected():
    """`session_id` also ends up in paths. Same Pydantic constraint applies
    — path-traversal or wrong-shape ids return 422."""
    body = _minimal_pitch_body("../etc", "A")
    client = TestClient(app)
    r = _post_pitch(client, body)
    assert r.status_code == 422


# --------------------------- Concurrency + DoS guards ------------------------


def test_save_clip_is_lock_protected(tmp_path):
    """P1-A regression: four threads overwrite the same (camera, session)
    clip simultaneously. Without the lock, concurrent tmp-writes clobber each
    other mid-stream so the final `.mov` is a mix of different thread
    payloads. The final file must equal exactly one of the inputs."""
    s = main.State(data_dir=tmp_path)
    session_id = sid(910)

    # Distinct byte patterns large enough that a mid-write clobber would be
    # detectable — each thread writes ~640 KB of its own repeating pattern.
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
    # Must equal one input exactly — not a concatenation or interleave.
    assert final_bytes in payloads, (
        f"clip is a torn mix of writes (len={len(final_bytes)})"
    )
    # No tmp leftovers (lock released + rename completed on every worker).
    assert not any(
        p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir()
    )


def test_record_does_not_hold_lock_during_io(tmp_path, monkeypatch):
    """P1-B regression: `State.record` must release `self._lock` before its
    disk writes. Monkey-patch `_atomic_write` to block on a threading.Event,
    run `record` in a worker, and prove `state.heartbeat` still returns
    quickly from the main thread while record is parked mid-I/O."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    tx, tz = _project(K, R_a, t_a, P_true)
    session_id = sid(920)

    s = main.State(data_dir=tmp_path)

    release = threading.Event()
    entered_io = threading.Event()
    original_atomic_write = s._atomic_write

    def blocking_atomic_write(path, payload):
        entered_io.set()
        # Park the "disk write" until the test releases us. If this runs
        # while the lock is still held, `heartbeat` below will block too.
        assert release.wait(timeout=5.0), "release event never fired"
        return original_atomic_write(path, payload)

    monkeypatch.setattr(s, "_atomic_write", blocking_atomic_write)

    pitch = main.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_anchor_frame_index=0,
        sync_anchor_timestamp_s=0.0,
        frames=[
            main.FramePayload(
                frame_index=0, timestamp_s=0.0,
                theta_x_rad=tx, theta_z_rad=tz, ball_detected=True,
            )
        ],
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )

    recorder = threading.Thread(target=s.record, args=(pitch,))
    recorder.start()
    # Wait until record() is parked inside _atomic_write (i.e. past the
    # first critical section and into the unlocked I/O phase).
    assert entered_io.wait(timeout=5.0), "record never reached _atomic_write"

    # Heartbeat must be fast — it only briefly touches `_devices` under
    # the lock, and the lock is currently idle because record() released
    # it before entering disk I/O.
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
    """P1-C regression: a /pitch POST whose body exceeds
    `_MAX_PITCH_UPLOAD_BYTES` must be rejected with 413 and never reach
    triangulation. Uses a declared Content-Length slightly over the cap so
    the pre-check path fires without actually buffering 500 MB."""
    session_id = sid(930)
    body = _minimal_pitch_body(session_id, "A")

    # Simulate an oversize request by forging Content-Length. TestClient
    # otherwise computes Content-Length from the real body, so we just
    # override the header — the handler's pre-check reads the header
    # before touching the stream.
    client = TestClient(app)
    fake_video = b"\x00" * 1024  # tiny actual body; header lies about size
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
    """P1-C defence-in-depth: even when the attacker omits Content-Length,
    the post-read size check catches an oversize video. We temporarily
    shrink `_MAX_PITCH_UPLOAD_BYTES` so the test can generate a body
    exceeding the cap without allocating 500 MB."""
    session_id = sid(931)
    body = _minimal_pitch_body(session_id, "A")
    client = TestClient(app)

    original_cap = main._MAX_PITCH_UPLOAD_BYTES
    try:
        main._MAX_PITCH_UPLOAD_BYTES = 4 * 1024  # 4 KB for the test
        # 8 KB payload — well over the shrunken cap but tiny in absolute
        # terms so the test stays cheap.
        fake_video = b"A" * (8 * 1024)
        files = {"video": ("clip.mov", fake_video, "video/quicktime")}
        data = {"payload": _json.dumps(body)}
        r = client.post("/pitch", data=data, files=files)
        assert r.status_code == 413, r.text
    finally:
        main._MAX_PITCH_UPLOAD_BYTES = original_cap
