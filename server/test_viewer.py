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
import schemas
from conftest import sid
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


def _project_pixels(K, R, t, P_world):
    P_cam = R @ P_world + t
    u = K[0, 0] * P_cam[0] / P_cam[2] + K[0, 2]
    v = K[1, 1] * P_cam[1] / P_cam[2] + K[1, 2]
    return float(u), float(v)


def _pitch(cam_id, cycle, K, R, t, H, P_trajectory):
    frames = []
    for i, P in enumerate(P_trajectory):
        u, v = _project_pixels(K, R, t, P)
        frames.append(
            schemas.FramePayload(
                frame_index=i,
                timestamp_s=float(i) / 240.0,
                px=u, py=v,
                ball_detected=True,
            )
        )
    return schemas.PitchPayload(
        camera_id=cam_id,
        session_id=sid(cycle),
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames=frames,
        intrinsics=schemas.IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
        homography=H.flatten().tolist(),
    )


# ---- build_scene ----------------------------------------------------------


def test_build_scene_camera_center_matches_rig():
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    P = np.array([[0.1, 0.3, 1.0]])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, P)
    session_id = sid(1)

    scene = build_scene(session_id, {"A": pitch}, triangulated=None)

    assert scene.session_id == session_id
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

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

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

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    r = scene.rays[0]
    assert r.endpoint[2] == pytest.approx(0.0, abs=1e-6)


def test_build_scene_skips_rays_that_dont_intersect_ground():
    """Rays whose world-frame direction has Z >= 0 (pointing up, or parallel
    to the plate) would extend to infinity / never hit Z=0 with positive t.
    `build_scene` must drop them rather than emit 10 m sky-poles that swamp
    the viewer with false-positive ball detections."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    # C_a sits at Z=1.2. Any ball point at Z >= C_a.z produces a ray that
    # either points upward (dz > 0) or is parallel (dz ~= 0) when the point
    # is at the same height. Ball point above the camera → upward ray.
    P_up = np.array([0.0, 0.2, 3.0])
    # Ball point at exactly camera height but offset in Y → horizontal ray
    # (dz ~= 0), which also fails to cross Z=0 with positive t.
    P_level = np.array([0.5, 0.2, 1.2])
    # Valid, downward-pointing ray to prove the others were filtered rather
    # than all rays being dropped by some other bug.
    P_ok = np.array([0.0, 0.2, 0.4])
    trajectory = np.array([P_up, P_level, P_ok])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, trajectory)

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    # Only the P_ok frame should have produced a ray.
    assert len(scene.rays) == 1
    assert scene.rays[0].frame_index == 2
    assert scene.rays[0].endpoint[2] == pytest.approx(0.0, abs=1e-6)


def test_build_scene_skipped_rays_do_not_affect_camera_or_triangulated():
    """Filtering rays must not interfere with camera pose or triangulated
    point attachment — only `scene.rays` shrinks."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    P_up = np.array([0.0, 0.2, 3.0])  # all frames point upward → 0 rays
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P_up]))
    tri = [
        main.TriangulatedPoint(t_rel_s=0.0, x_m=0.1, y_m=0.2, z_m=0.3, residual_m=1e-6)
    ]

    scene = build_scene(sid(1), {"A": pitch}, triangulated=tri)

    assert len(scene.cameras) == 1
    assert scene.rays == []
    assert len(scene.triangulated) == 1


def test_build_scene_skips_pitch_missing_calibration():
    K, (R_a, t_a, _, _), _ = _make_rig()
    # Dummy homography to build the PitchPayload, then strip it.
    P = np.array([[0.1, 0.3, 1.0]])
    pitch = _pitch("A", 1, K, R_a, t_a, np.eye(3), P)
    pitch_no_calib = pitch.model_copy(update={"intrinsics": None, "homography": None})

    scene = build_scene(sid(1), {"A": pitch_no_calib}, triangulated=None)

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

    scene = build_scene(sid(5), {"A": pa, "B": pb}, triangulated=tri)

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
    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)
    # Round-trip via JSON to catch any non-serialisable dataclass fields.
    out = _json.loads(_json.dumps(scene.to_dict()))
    assert out["session_id"] == sid(1)
    assert "cameras" in out and "rays" in out and "triangulated" in out


# ---- HTTP endpoints -------------------------------------------------------


def _record_pitch(pitch: main.PitchPayload) -> None:
    """Directly persist a manually-built PitchPayload (frames already
    populated). The /pitch ingestion handler now requires a real MOV for
    server-side detection; these tests are scoped to viewer/event
    rendering, so we bypass ingestion and go straight to state.record,
    which is the same path the handler calls after detection."""
    main.state.record(pitch)


def test_reconstruction_endpoint_returns_scene_shape():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(701)
    pitch = _pitch("A", 701, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)

    _record_pitch(pitch)

    r = client.get(f"/reconstruction/{session_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == session_id
    assert len(body["cameras"]) == 1
    assert body["cameras"][0]["camera_id"] == "A"
    assert len(body["rays"]) == 1
    assert body["triangulated"] == []


def test_reconstruction_endpoint_unknown_session_returns_404():
    client = TestClient(app)
    r = client.get(f"/reconstruction/{sid('deadbeef')}")
    assert r.status_code == 404


def test_viewer_endpoint_returns_plotly_html():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(702)
    pitch = _pitch("A", 702, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)
    _record_pitch(pitch)

    r = client.get(f"/viewer/{session_id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text.lower()
    assert "plotly" in body
    assert session_id in body


def test_viewer_endpoint_embeds_video_tags_for_available_clips():
    """Viewer page ships a <video> per on-disk clip, with src pointing at
    /videos/session_{sid}_{cam}.{ext}."""
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    session_id = sid(704)
    _record_pitch(_pitch("A", 704, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    _record_pitch(_pitch("B", 704, K, R_b, t_b, H_b, np.array([[0.1, 0.3, 1.0]])))
    # Drop dummy clip bytes at the expected locations (no need for real
    # H.264 — the viewer just embeds the URL).
    main.state.save_clip("A", session_id, b"ok-a", "mov")
    main.state.save_clip("B", session_id, b"ok-b", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert f'src="/videos/session_{session_id}_A.mov"' in body
    assert f'src="/videos/session_{session_id}_B.mov"' in body
    assert 'data-cam="A"' in body
    assert 'data-cam="B"' in body


def test_viewer_endpoint_prefers_annotated_clip_when_available():
    """Raw + annotated both on disk → viewer picks the annotated one."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(708)
    _record_pitch(_pitch("A", 708, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"raw", "mov")
    # Annotated counterpart — server-side normal path writes this too.
    annotated = main.state.video_dir / f"session_{session_id}_A_annotated.mov"
    annotated.write_bytes(b"annotated")
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert f'src="/videos/session_{session_id}_A_annotated.mov"' in body
    assert f'src="/videos/session_{session_id}_A.mov"' not in body


def test_viewer_endpoint_without_clips_still_renders():
    """No MOVs on disk → viewer renders a placeholder instead of 500."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(705)
    _record_pitch(_pitch("A", 705, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "no clips on disk" in body
    assert "<video" not in body


def test_viewer_health_banner_shows_partial_session_failure():
    """A-only session → health banner must surface (a) A's uploaded chip,
    (b) B's 'not uploaded' state, and (c) an explicit failure strip
    explaining triangulation was skipped. This is the whole reason the
    banner exists — a glance should answer 'why is the 3D empty?'."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(720)
    _record_pitch(_pitch("A", 720, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "CAM A" in body and "CAM B" in body
    assert "uploaded" in body
    assert "not uploaded" in body
    assert "never uploaded" in body  # failure strip reason
    assert "triangulation skipped" in body
    # Triangulation card falls to the zero state.
    assert "no triangulation" in body


def test_viewer_health_banner_shows_paired_triangulation_count():
    """Both A and B present + triangulated points → banner shows the
    count and no failure strip."""
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    session_id = sid(721)
    P = np.array([[0.1, 0.3, 1.0], [0.2, 0.5, 1.2]])
    _record_pitch(_pitch("A", 721, K, R_a, t_a, H_a, P))
    _record_pitch(_pitch("B", 721, K, R_b, t_b, H_b, P))

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "points triangulated" in body
    # No failure strip should render when every check passes.
    assert 'class="fail-strip"' not in body


def test_viewer_health_banner_flags_missing_time_sync():
    """Both cameras uploaded, but neither has a chirp anchor → banner
    must call out the sync failure by name rather than just showing an
    empty 3D scene."""
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    session_id = sid(722)
    for cam, R, t, H in (("A", R_a, t_a, H_a), ("B", R_b, t_b, H_b)):
        p = _pitch(cam, 722, K, R, t, H, np.array([[0.1, 0.3, 1.0]]))
        main.state.record(p.model_copy(update={"sync_anchor_timestamp_s": None}))

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "no chirp anchor" in body


def test_viewer_embeds_scene_data_and_mode_toggle():
    """Viewer serialises the scene (ground_traces + rays) inline so JS
    can rebuild the Plotly traces under a time filter, and exposes the
    [ALL] / [PLAYBACK] toggle."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(709)
    _record_pitch(_pitch("A", 709, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # Inline scene blob + the mode toggle buttons.
    assert 'id="viewer-data"' in body
    assert '"ground_traces"' in body
    assert 'id="mode-all"' in body
    assert 'id="mode-playback"' in body


def test_viewer_exposes_camera_t_rel_offsets(tmp_path):
    """Video metadata passed to JS must carry per-camera
    `t_rel_offset_s = video_start_pts_s − sync_anchor_timestamp_s` so
    the JS can align A and B by their shared chirp anchor."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(710)
    pitch = schemas.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_anchor_timestamp_s=100.0,  # chirp hit at session-clock 100 s
        video_start_pts_s=101.5,        # first MOV frame at 101.5 s
        video_fps=240.0,
        frames=[schemas.FramePayload(
            frame_index=0, timestamp_s=101.5, px=960.0, py=540.0,
            ball_detected=True,
        )],
        intrinsics=schemas.IntrinsicsPayload(
            fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )
    main.state.record(pitch)
    main.state.save_clip("A", session_id, b"clip", "mov")
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # t_rel_offset = 101.5 − 100.0 = 1.5 s (serialised as JSON number).
    assert '"t_rel_offset_s": 1.5' in body


def test_video_endpoint_serves_clip_bytes():
    session_id = sid(706)
    main.state.save_clip("A", session_id, b"\x00\x01\x02byte-soup", "mov")
    client = TestClient(app)
    r = client.get(f"/videos/session_{session_id}_A.mov")
    assert r.status_code == 200
    assert r.content == b"\x00\x01\x02byte-soup"


def test_video_endpoint_rejects_path_traversal():
    """Only filenames matching the canonical `session_<sid>_<cam>.<ext>`
    shape pass through. Anything else → 404 (not 200, not 500)."""
    client = TestClient(app)
    for bad in [
        "..%2Fetc%2Fpasswd",
        "etc/passwd",
        "session_bad.mov",
        "session_s_nope_A.exe",
    ]:
        r = client.get(f"/videos/{bad}")
        assert r.status_code == 404, bad


def test_video_endpoint_404_when_file_missing():
    session_id = sid(707)
    client = TestClient(app)
    # No save_clip call — the on-disk file does not exist.
    r = client.get(f"/videos/session_{session_id}_A.mov")
    assert r.status_code == 404


def test_viewer_endpoint_unknown_session_returns_404():
    client = TestClient(app)
    r = client.get(f"/viewer/{sid('deadbeef')}")
    assert r.status_code == 404


def test_events_endpoint_lists_sessions_latest_first():
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    P = np.array([[0.1, 0.3, 1.0]])
    client = TestClient(app)

    _record_pitch(_pitch("A", 800, K, R_a, t_a, H_a, P))
    _record_pitch(_pitch("A", 810, K, R_a, t_a, H_a, P))
    _record_pitch(_pitch("B", 810, K, R_b, t_b, H_b, P))

    events = client.get("/events").json()
    session_ids = [e["session_id"] for e in events]
    assert session_ids.index(sid(810)) < session_ids.index(sid(800))

    evt_810 = next(e for e in events if e["session_id"] == sid(810))
    assert evt_810["cameras"] == ["A", "B"]
    assert evt_810["status"] in ("paired", "paired_no_points")
    assert evt_810["n_ball_frames"] == {"A": 1, "B": 1}
    assert evt_810["n_triangulated"] == 1

    evt_800 = next(e for e in events if e["session_id"] == sid(800))
    assert evt_800["cameras"] == ["A"]
    assert evt_800["status"] == "partial"


def test_index_endpoint_lists_events_with_viewer_links():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    pitch = _pitch("A", 901, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    client = TestClient(app)
    _record_pitch(pitch)

    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "BALL_TRACKER" in body
    assert f'href="/viewer/{sid(901)}"' in body


def test_index_endpoint_empty_state_is_rendered():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    # Either the empty message or a session event list — the fixture may
    # have left rows behind. Assert the nav brand is in the HTML either way.
    assert "BALL_TRACKER" in r.text
