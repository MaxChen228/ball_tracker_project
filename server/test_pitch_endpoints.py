"""/pitch upload variants + pitch_analysis + clip persistence + distortion."""
from __future__ import annotations

import json as _json

import cv2
import numpy as np
from fastapi.testclient import TestClient

import main
from conftest import sid
from main import app
from triangulate import build_K

from _test_helpers import (
    _base_payload,
    _encode_mov,
    _encode_single_ball_mov,
    _make_frame_with_ball,
    _make_scene,
    _post_pitch,
    _project_pixels,
    _seed_ready_stereo,
)


def test_post_pitch_with_video_triangulates_server_side(tmp_path):
    """End-to-end: paint a blue circle at the projected pixel of a known
    3D point on both cameras' MOVs, POST them, operator triggers Run
    server on the session, verify triangulation ≤ 1 px of truth."""
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
    # Server-post no longer auto-runs on upload.
    assert body2["detection_pending"] is False
    assert body2["triangulated_points"] == 0

    # Operator triggers server-post detection on the events row.
    run = client.post(f"/sessions/{session_id}/run_server_post")
    assert run.status_code == 200, run.text
    assert run.json()["queued"] == 2

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
    body_a["frames_server_post"] = frames_a
    body_b = _base_payload("B", session_id, K, H_b)
    body_b["frames_server_post"] = frames_b

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
            "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
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


def test_post_pitch_anchorless_single_camera_keeps_rays(tmp_path):
    """A single camera can upload without a time-sync anchor. The server
    cannot triangulate, but it must keep detections so the viewer can render
    monocular rays."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    u, v = _project_pixels(K, R_a, t_a, P_true)
    client = TestClient(app)
    body = _base_payload("A", sid(502), K, H_a, anchor_ts=None)
    body["paths"] = ["live"]
    body["frames_live"] = [
        {
            "frame_index": 0,
            "timestamp_s": body["video_start_pts_s"],
            "px": u,
            "py": v,
            "ball_detected": True,
        }
    ]
    r = _post_pitch(client, body, None)
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None
    assert r.json()["triangulated_points"] == 0

    # include_rejected=true: single-frame fixture trips chain_filter's
    # min_run_len → rejected_flicker, which the wire payload hides by
    # default. Building a 10-frame fixture just to satisfy the filter
    # would obscure the actual thing under test (monocular ray render).
    scene = client.get(f"/reconstruction/{sid(502)}?include_rejected=true").json()
    assert len(scene["cameras"]) == 1
    assert len(scene["rays"]) == 1
    assert scene["triangulated"] == []


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
    # Operator-triggered server-post detection drives triangulation.
    run = client.post(f"/sessions/{session_id}/run_server_post")
    assert run.status_code == 200, run.text
    result_points = client.get(f"/results/{session_id}").json()["points"]
    assert len(result_points) >= 1

    pt = result_points[0]
    # Detection + undistortion + triangulation: a couple mm of slack is
    # plenty for a ~1 m-distant ball encoded via lossy H.264.
    assert abs(pt["x_m"] - P_true[0]) < 5e-3
    assert abs(pt["y_m"] - P_true[1]) < 5e-3
    assert abs(pt["z_m"] - P_true[2]) < 5e-3


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
