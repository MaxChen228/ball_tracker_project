"""Scene builder + Plotly viewer + events-index tests.

Kept separate from test_server.py so the viewer dependency (plotly) only
loads for these tests; the core server tests keep their fast import cost.
"""
from __future__ import annotations

import json as _json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import app
from reconstruct import Scene, build_scene


# ---- Scene setup: reuse the same two-camera rig as test_server.py ---------


def _look_at(
    pos: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 0.0, 1.0])
):
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
    y_cam = -up - np.dot(-up, z_cam) * z_cam
    y_cam /= np.linalg.norm(y_cam)
    x_cam = np.cross(y_cam, z_cam)
    R_cw = np.column_stack([x_cam, y_cam, z_cam])
    R_wc = R_cw.T
    t_wc = -R_wc @ pos
    return R_wc, t_wc


def _project(K, R, t, P_world):
    P_cam = R @ P_world + t
    return float(np.arctan2(P_cam[0], P_cam[2])), float(np.arctan2(P_cam[1], P_cam[2]))


def _make_rig():
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])
    C_a = np.array([1.8, -2.5, 1.2])
    C_b = np.array([-1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R_a, t_a = _look_at(C_a, target)
    R_b, t_b = _look_at(C_b, target)
    H_a = K @ np.column_stack([R_a[:, 0], R_a[:, 1], t_a])
    H_b = K @ np.column_stack([R_b[:, 0], R_b[:, 1], t_b])
    H_a /= H_a[2, 2]
    H_b /= H_b[2, 2]
    return K, (R_a, t_a, C_a, H_a), (R_b, t_b, C_b, H_b)


def _pitch(cam_id, cycle, K, R, t, H, P_trajectory, with_pixels=False):
    frames = []
    for i, P in enumerate(P_trajectory):
        tx, tz = _project(K, R, t, P)
        frames.append(
            main.FramePayload(
                frame_index=i,
                timestamp_s=float(i) / 240.0,
                theta_x_rad=tx,
                theta_z_rad=tz,
                ball_detected=True,
            )
        )
    return main.PitchPayload(
        camera_id=cam_id,
        sync_anchor_frame_index=0,
        sync_anchor_timestamp_s=0.0,
        cycle_number=cycle,
        frames=frames,
        intrinsics=main.IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
        homography=H.flatten().tolist(),
    )


# ---- build_scene ----------------------------------------------------------


def test_build_scene_camera_center_matches_rig():
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    P = np.array([[0.1, 0.3, 1.0]])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, P)

    scene = build_scene(1, {"A": pitch}, triangulated=None)

    assert scene.cycle_number == 1
    assert len(scene.cameras) == 1
    cam = scene.cameras[0]
    assert cam.camera_id == "A"
    np.testing.assert_allclose(cam.center_world, C_a.tolist(), atol=1e-8)
    # Forward axis should roughly point toward +Y (plate is forward of cameras).
    assert cam.axis_forward_world[1] > 0.5


def test_build_scene_ray_origin_is_camera_center_and_points_toward_ball():
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    P = np.array([0.5, 0.3, 1.0])  # slightly right, in front, up
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P]))

    scene = build_scene(1, {"A": pitch}, triangulated=None)

    assert len(scene.rays) == 1
    r = scene.rays[0]
    np.testing.assert_allclose(r.origin, C_a.tolist(), atol=1e-8)

    # Ray direction from camera should be roughly (target - camera).
    origin = np.array(r.origin)
    endpoint = np.array(r.endpoint)
    direction = endpoint - origin
    direction /= np.linalg.norm(direction)

    expected = P - C_a
    expected /= np.linalg.norm(expected)
    np.testing.assert_allclose(direction, expected, atol=1e-6)


def test_build_scene_ray_endpoint_hits_ground_when_direction_is_downward():
    """Any ray whose direction has negative Z component in world frame should
    have its endpoint clamped to the Z=0 plane (positive t)."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    # Point below camera height, above plate plane.
    P = np.array([0.0, 0.2, 0.4])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P]))

    scene = build_scene(1, {"A": pitch}, triangulated=None)

    r = scene.rays[0]
    assert r.endpoint[2] == pytest.approx(0.0, abs=1e-6)


def test_build_scene_skips_pitch_missing_calibration():
    K, (R_a, t_a, _, _), _ = _make_rig()
    # Dummy homography to build the PitchPayload, then strip it.
    P = np.array([[0.1, 0.3, 1.0]])
    pitch = _pitch("A", 1, K, R_a, t_a, np.eye(3), P)
    pitch_no_calib = pitch.model_copy(update={"intrinsics": None, "homography": None})

    scene = build_scene(1, {"A": pitch_no_calib}, triangulated=None)

    assert scene.cameras == []
    assert scene.rays == []


def test_build_scene_two_cameras_attaches_triangulated_points():
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    P_path = np.array([[0.1, 0.3, 1.0], [0.2, 0.5, 1.2]])
    pa = _pitch("A", 5, K, R_a, t_a, H_a, P_path)
    pb = _pitch("B", 5, K, R_b, t_b, H_b, P_path)

    tri = [
        main.TriangulatedPoint(
            t_rel_s=i / 240.0,
            x_m=P[0], y_m=P[1], z_m=P[2],
            residual_m=1e-6,
        )
        for i, P in enumerate(P_path)
    ]

    scene = build_scene(5, {"A": pa, "B": pb}, triangulated=tri)

    assert len(scene.cameras) == 2
    assert {c.camera_id for c in scene.cameras} == {"A", "B"}
    assert len(scene.triangulated) == 2
    np.testing.assert_allclose(
        [scene.triangulated[0][k] for k in ("x", "y", "z")],
        P_path[0].tolist(),
        atol=1e-6,
    )


def test_scene_to_dict_is_json_serialisable():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    scene = build_scene(1, {"A": pitch}, triangulated=None)
    # Round-trip via JSON to catch any non-serialisable dataclass fields.
    out = _json.loads(_json.dumps(scene.to_dict()))
    assert out["cycle_number"] == 1
    assert "cameras" in out and "rays" in out and "triangulated" in out


# ---- HTTP endpoints -------------------------------------------------------


def _post_pitch(client, pitch: main.PitchPayload):
    return client.post("/pitch", data={"payload": pitch.model_dump_json()})


def test_reconstruction_endpoint_returns_scene_shape():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    pitch = _pitch("A", 701, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)

    assert _post_pitch(client, pitch).status_code == 200

    r = client.get("/reconstruction/701")
    assert r.status_code == 200
    body = r.json()
    assert body["cycle_number"] == 701
    assert len(body["cameras"]) == 1
    assert body["cameras"][0]["camera_id"] == "A"
    assert len(body["rays"]) == 1
    assert body["triangulated"] == []


def test_reconstruction_endpoint_unknown_cycle_returns_404():
    client = TestClient(app)
    r = client.get("/reconstruction/99999")
    assert r.status_code == 404


def test_viewer_endpoint_returns_plotly_html():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    pitch = _pitch("A", 702, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)
    assert _post_pitch(client, pitch).status_code == 200

    r = client.get("/viewer/702")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Plotly's CDN script tag or a clear Plotly signature must be present.
    body = r.text.lower()
    assert "plotly" in body
    assert "cycle 702" in body


def test_viewer_endpoint_unknown_cycle_returns_404():
    client = TestClient(app)
    r = client.get("/viewer/99999")
    assert r.status_code == 404


def test_events_endpoint_lists_cycles_latest_first():
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    P = np.array([[0.1, 0.3, 1.0]])
    client = TestClient(app)

    assert _post_pitch(client, _pitch("A", 800, K, R_a, t_a, H_a, P)).status_code == 200
    assert _post_pitch(client, _pitch("A", 810, K, R_a, t_a, H_a, P)).status_code == 200
    assert _post_pitch(client, _pitch("B", 810, K, R_b, t_b, H_b, P)).status_code == 200

    events = client.get("/events").json()
    # At least the two cycles we posted, in descending cycle order.
    cycles = [e["cycle_number"] for e in events]
    assert cycles.index(810) < cycles.index(800)

    evt_810 = next(e for e in events if e["cycle_number"] == 810)
    assert evt_810["cameras"] == ["A", "B"]
    assert evt_810["status"] in ("paired", "paired_no_points")
    assert evt_810["n_ball_frames"] == {"A": 1, "B": 1}
    assert evt_810["n_triangulated"] == 1

    evt_800 = next(e for e in events if e["cycle_number"] == 800)
    assert evt_800["cameras"] == ["A"]
    assert evt_800["status"] == "partial"


def test_index_endpoint_lists_events_with_viewer_links():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    pitch = _pitch("A", 901, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)
    assert _post_pitch(client, pitch).status_code == 200

    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "ball_tracker dashboard" in body
    assert 'href="/viewer/901"' in body


def test_index_endpoint_empty_state_is_rendered():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    # Either the empty message or a table — the fixture may have left rows
    # behind. Assert it's renderable HTML with the page title.
    assert "ball_tracker dashboard" in r.text
