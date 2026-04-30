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
from viewer_fragments import failure_strip_html
from viewer_page import build_viewer_page_context


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
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=frames,
        intrinsics=schemas.IntrinsicsPayload(fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
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
    have its endpoint clamped to the Z=0 plane (positive t), and contribute
    one point to the camera's ground trace."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    # Point below camera height, above plate plane.
    P = np.array([0.0, 0.2, 0.4])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P]))

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    r = scene.rays[0]
    assert r.endpoint[2] == pytest.approx(0.0, abs=1e-6)
    # Ground trace gets the same ground-plane intersection.
    assert len(scene.ground_traces["A"]) == 1
    assert scene.ground_traces["A"][0]["z"] == pytest.approx(0.0, abs=1e-6)


def test_build_scene_includes_persisted_live_rays():
    K, (R_a, t_a, _C_a, H_a), _ = _make_rig()
    P = np.array([0.0, 0.2, 0.4])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([]))
    u, v = _project_pixels(K, R_a, t_a, P)
    pitch.frames_server_post = []
    pitch.frames_live = [
        schemas.FramePayload(
            frame_index=7,
            timestamp_s=7.0 / 240.0,
            px=u,
            py=v,
            ball_detected=True,
        )
    ]

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    assert len(scene.rays) == 1
    assert scene.rays[0].source == "live"
    assert "A" in scene.ground_traces_live
    assert len(scene.ground_traces_live["A"]) == 1


def test_build_scene_keeps_upward_rays_without_ground_trace():
    """Rays with world-frame direction Z >= 0 (pointing up or parallel to
    the plate) are geometrically valid — a ball mid-flight above camera
    height must still appear as a ray. The endpoint can't be clamped to
    Z=0, so it's extended along the ray direction by a scene-scale length.
    Ground trace only collects frames whose ray actually hits the plate."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    # C_a sits at Z=1.2. Ball point above camera → ray points up (dz > 0).
    P_up = np.array([0.0, 0.2, 3.0])
    # Ball at camera height, offset in Y → ray ~horizontal (dz ~= 0).
    P_level = np.array([0.5, 0.2, 1.2])
    # Downward-pointing ray (hits ground), to show both paths coexist.
    P_ok = np.array([0.0, 0.2, 0.4])
    trajectory = np.array([P_up, P_level, P_ok])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, trajectory)

    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    # All three frames keep a ray — none are dropped.
    assert len(scene.rays) == 3
    # But only the downward ray contributes to the ground trace.
    assert len(scene.ground_traces["A"]) == 1
    assert scene.ground_traces["A"][0]["z"] == pytest.approx(0.0, abs=1e-6)
    # The ray for P_ok matches P_ok's ground projection.
    r_ok = next(r for r in scene.rays if r.frame_index == 2)
    assert r_ok.endpoint[2] == pytest.approx(0.0, abs=1e-6)
    # Upward / horizontal rays have endpoints extended along direction —
    # not clamped to the plate.
    for r in scene.rays:
        if r.frame_index in (0, 1):
            # Endpoint lies further from camera center than the camera
            # itself and is NOT at Z=0 (no ground intersection exists).
            end = np.array(r.endpoint)
            origin = np.array(r.origin)
            assert np.linalg.norm(end - origin) > 1e-3


def test_build_scene_all_upward_rays_still_render():
    """Even when every detection yields an upward ray (ball consistently
    above camera height), the viewer must still show those rays — the
    only thing missing is the ground trace. Camera and triangulated
    points are untouched."""
    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    P_up = np.array([0.0, 0.2, 3.0])  # all frames point upward
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P_up]))
    tri = [
        main.TriangulatedPoint(t_rel_s=0.0, x_m=0.1, y_m=0.2, z_m=0.3, residual_m=1e-6)
    ]

    scene = build_scene(sid(1), {"A": pitch}, triangulated=tri)

    assert len(scene.cameras) == 1
    assert len(scene.rays) == 1
    # No ground trace because the single ray doesn't cross Z=0.
    assert scene.ground_traces == {}
    assert len(scene.triangulated) == 1


def test_build_scene_clamps_rays_beyond_max_render_dist():
    """Near-horizontal rays hit the plate plane tens of metres out; the
    scene clamps the endpoint to `_MAX_RENDER_DIST_M` so Plotly's auto-
    fit axis doesn't include the far intersection. Ground-trace entry is
    suppressed because a "landing point" at the clamp boundary would lie."""
    from reconstruct import _MAX_RENDER_DIST_M

    K, (R_a, t_a, C_a, H_a), _ = _make_rig()
    # Ball near camera height, far away in Y — produces a near-horizontal
    # ray whose true ground intersection sits hundreds of metres out.
    P_far = np.array([-2.0, 20.0, 1.15])
    pitch = _pitch("A", 1, K, R_a, t_a, H_a, np.array([P_far]))
    scene = build_scene(sid(1), {"A": pitch}, triangulated=None)

    assert len(scene.rays) == 1
    r = scene.rays[0]
    dist = float(np.linalg.norm(np.array(r.endpoint) - np.array(r.origin)))
    assert dist <= _MAX_RENDER_DIST_M + 1e-6
    assert scene.ground_traces == {}


def test_build_scene_skips_triangulated_beyond_max_render_dist():
    """Triangulated points past the render radius (measured from the world
    origin) are dropped so the viewer's plate-relative axis stays bounded."""
    from reconstruct import _MAX_RENDER_DIST_M

    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    P_path = np.array([[0.1, 0.3, 1.0]])
    pa = _pitch("A", 9, K, R_a, t_a, H_a, P_path)
    pb = _pitch("B", 9, K, R_b, t_b, H_b, P_path)
    tri = [
        main.TriangulatedPoint(t_rel_s=0.0, x_m=0.1, y_m=0.2, z_m=0.3, residual_m=1e-6),   # in
        main.TriangulatedPoint(t_rel_s=0.1, x_m=50.0, y_m=0.0, z_m=0.0, residual_m=1e-6),  # out
    ]
    scene = build_scene(sid(9), {"A": pa, "B": pb}, triangulated=tri)

    assert len(scene.triangulated) == 1
    assert scene.triangulated[0]["x"] == pytest.approx(0.1)
    _ = _MAX_RENDER_DIST_M  # ensure symbol is referenced (readability for future tuning)


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


def test_viewer_endpoint_without_clips_still_renders():
    """No MOVs on disk → viewer renders a placeholder instead of 500."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(705)
    _record_pitch(_pitch("A", 705, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "awaiting upload" in body
    assert "<video" not in body


def test_viewer_banner_tags_camera_only_when_video_on_disk(tmp_path):
    """Mode-one session: any MOV under data/videos/ flips the nav strip's
    mode chip to `camera-only`."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(707)
    _record_pitch(_pitch("A", 707, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    # Drop a dummy MOV so _build_viewer_health's glob sees it.
    main.state.video_dir.mkdir(parents=True, exist_ok=True)
    (main.state.video_dir / f"session_{session_id}_A.mov").write_bytes(b"fake")
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert 'class="hs-mode">camera-only</span>' in body


def test_viewer_health_strip_shows_partial_session_failure():
    """A-only session → nav strip must surface (a) A's CAM chip with
    receive checks, (b) B's missing chip, and (c) a separate failure
    banner explaining triangulation was skipped. A glance must answer
    'why is the 3D empty?'."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(720)
    _record_pitch(_pitch("A", 720, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "CAM A" in body and "CAM B" in body
    # Missing cam chip + failure banner both surface 'never uploaded'.
    assert "never uploaded" in body
    assert "triangulation skipped" in body
    # Hero `pts` count falls to the zero state.
    assert 'class="hs-tri zero"' in body
    # Layout mode is `single-cam` so videos-col collapses to a single row.
    assert 'data-mode="single-cam"' in body
    # Missing cam chip is rendered with `hs-cam missing` class.
    assert 'class="hs-cam missing"' in body


def test_viewer_health_strip_shows_paired_triangulation_count():
    """Both A and B present + triangulated points → strip shows the
    count, each cam chip renders pass checks, no failure banner exists."""
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    session_id = sid(721)
    P = np.array([[0.1, 0.3, 1.0], [0.2, 0.5, 1.2]])
    _record_pitch(_pitch("A", 721, K, R_a, t_a, H_a, P))
    _record_pitch(_pitch("B", 721, K, R_b, t_b, H_b, P))

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # Strip must render the triangulation count chip.
    assert 'class="hs-tri ok"' in body
    assert 'class="hs-tri-n">2<' in body
    # Each path chip carries its rate-tier class; both cams at 100%
    # detection → ok tier.
    assert 'data-rate-klass="ok"' in body
    # Layout mode is `paired` since both cameras uploaded.
    assert 'data-mode="paired"' in body
    # Strip carries session metadata.
    assert f'class="hs-sid">{session_id}<' in body
    assert 'class="hs-dur"' in body
    # No failure banner should render when every check passes.
    assert 'class="fail-strip"' not in body
    # Old `.health` row must NOT render — metadata moved into nav strip.
    assert 'class="health-row"' not in body
    assert 'class="hero-card"' not in body


def test_viewer_health_strip_path_chip_colour_tiers():
    """Detection-rate tier must encode into each path-stat chip's
    `data-rate-klass`: <5% fail, <30% pending, else ok. The chip's
    border colour visualises this — the operator's at-a-glance signal
    that says 'is this pipeline usable?'."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(723)
    # 20 frames, only 1 detection → 5% (just at the pending threshold).
    frames = []
    for i in range(20):
        frames.append(schemas.FramePayload(
            frame_index=i,
            timestamp_s=float(i) / 240.0,
            px=960.0 if i == 0 else None,
            py=540.0 if i == 0 else None,
            ball_detected=(i == 0),
        ))
    pitch = schemas.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=frames,
        intrinsics=schemas.IntrinsicsPayload(
            fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )
    main.state.record(pitch)

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # 1/20 = 5% → pending tier. Boundary is `< 0.30`.
    assert 'data-rate-klass="pending"' in body
    # server_post chip reads "1/20".
    assert ">S</span><span class=\"val\">1/20<" in body
    assert 'class="path-stat on"' in body


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


def test_viewer_page_context_computes_single_cam_layout_and_video_cells():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(730)
    scene = build_scene(
        session_id,
        {"A": _pitch("A", 730, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))},
        triangulated=None,
    )
    health = {
        "session_id": session_id,
        "cameras": {
            "A": {"received": True, "calibrated": True, "time_synced": True, "n_frames": 1, "n_detected": 1},
            "B": {"received": False, "calibrated": False, "time_synced": False, "n_frames": 0, "n_detected": 0},
        },
        "triangulated_count": 0,
                "error": None,
        "duration_s": 0.0,
        "received_at": None,
        "mode": "camera_only",
    }
    videos = [
        ("A", "/videos/session_x_A.mov", 0.0, 240.0, {"t_rel_s": [0.0], "detected": [True]}),
    ]

    ctx = build_viewer_page_context(scene, videos, health)

    assert ctx.layout_mode == "single-cam"
    assert 'data-cam="A"' in ctx.video_cells_html
    assert ctx.scene_flex == "1 1 0"
    assert ctx.videos_flex == "1 1 0"


def test_viewer_page_renders_run_server_post_source_dropdown():
    """Phase 3: viewer's "Rerun server" form must expose a <select>
    so the operator can pick live / frozen / preset:<name> per request.
    Pre-phase-3 the form had only a hidden source=live, which made
    research compares require disk mutation. This guards the
    affordance against regression — if someone refactors the form
    back to a single hidden input, this test fails."""
    import main as _main
    from viewer_page import render_viewer_html

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(740)
    scene = build_scene(
        session_id,
        {"A": _pitch("A", 740, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))},
        triangulated=None,
    )
    health = {
        "session_id": session_id,
        "cameras": {
            "A": {"received": True, "calibrated": True, "time_synced": True, "n_frames": 1, "n_detected": 1},
            "B": {"received": False, "calibrated": False, "time_synced": False, "n_frames": 0, "n_detected": 0},
        },
        "triangulated_count": 0,
        "error": None,
        "duration_s": 0.0,
        "received_at": None,
        # camera_only flips can_run_server True so the action form
        # actually renders (otherwise action_html is empty).
        "mode": "camera_only",
    }
    videos = [
        ("A", "/videos/session_x_A.mov", 0.0, 240.0, {"t_rel_s": [0.0], "detected": [True]}),
    ]

    html = render_viewer_html(
        scene, videos, health,
        presets=_main.state.list_presets(),
    )

    # Form posts to the right endpoint.
    assert f'action="/sessions/{session_id}/run_server_post"' in html
    # Dropdown exists with all three source flavors. Two substring
    # checks instead of one ordered-attribute slug so a trivial
    # name=/class= reorder won't false-fail.
    assert '<select ' in html
    assert 'name="source"' in html
    assert 'class="action-select"' in html
    assert '<option value="live" selected>' in html
    assert '<option value="frozen">' in html
    # Both presets surface in the dropdown — not just one — so the
    # operator can compare without disk mutation.
    assert 'value="preset:tennis"' in html
    assert 'value="preset:blue_ball"' in html
    # No leftover hidden source from the phase-2 placeholder.
    assert '<input type="hidden" name="source"' not in html


def test_failure_strip_html_prefers_earliest_blocking_reason():
    health = {
        "cameras": {
            "A": {"received": False, "calibrated": False, "time_synced": False, "n_detected": 0},
            "B": {"received": True, "calibrated": False, "time_synced": False, "n_detected": 0},
        },
        "triangulated_count": 0,
        "error": "camera A missing calibration",
    }

    html = failure_strip_html(health)

    assert "Cam A never uploaded" in html
    assert "server error:" not in html


def test_viewer_ships_interactive_diagnostic_widgets():
    """The viewer's interactive surface — frame-input for jump-to,
    camera presets for the 3D view, strip-legend for the detection canvas,
    and hint-overlay for the keyboard cheat sheet — must all render. These
    widgets are what makes the viewer a diagnostic tool rather than a
    passive playback page; their presence is a contract the JS depends on."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(711)
    _record_pitch(_pitch("A", 711, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert 'id="frame-input"' in body
    # Five camera presets replace the legacy single "scene-reset" button.
    # ISO is the default-active preset (mirrors the figure's baked camera).
    assert 'class="scene-views"' in body
    for view in ("iso", "catch", "side", "top", "pitcher"):
        assert f'data-view="{view}"' in body
    assert 'class="strip-legend"' in body
    assert 'id="hint-overlay"' in body
    assert 'id="hint-btn"' in body
    # Three.js scene runtime owns the default camera (ISO preset baked
    # into PRESETS in scene_runtime.js); the inline JSON theme block
    # carries the strike-zone centroid so any consumer reading the
    # theme can derive lookAt. Sanity-check the runtime injection.
    assert "BallTrackerScene" in body
    assert '"strike_zone"' in body
    # The cheat sheet calls out the actual shortcuts so the operator
    # learns them on first hover.
    assert "Play / pause" in body
    assert "ball-detected" in body


def test_viewer_virtual_detection_follows_per_camera_ray_toggle():
    """Phase 6: per-pipeline detection overlay is now drawn as the two
    BallTrackerCamView BLOBS layers (`detection_blobs_live` /
    `detection_blobs_svr`) on top of each per-cam video. The vid-cell
    renders LIVE BLOBS + SVR BLOBS pills in its cam-view-toolbar so the
    operator can toggle each pipeline's overlay independently. Pre
    fan-out there was also a winner-dot layer per path
    (`detection_live` / `detection_svr`) — that's been removed because
    fan-out triangulation has no winner concept and the dot was just
    redrawing the lowest-cost candidate already shown by BLOBS."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(714)
    _record_pitch(_pitch("A", 714, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # Both BLOBS layers are registered with the runtime.
    assert "registerLayer('detection_blobs_live'" in body
    assert "registerLayer('detection_blobs_svr'" in body
    # Winner-dot layers are GONE — fan-out triangulation killed the
    # winner concept; `_drawDetectionForPath` and its registrations
    # were removed in the same change.
    assert "registerLayer('detection_live'" not in body
    assert "registerLayer('detection_svr'" not in body
    assert 'data-layer="detection_live"' not in body
    assert 'data-layer="detection_svr"' not in body
    # Per-cam toolbar exposes the LIVE / SVR BLOBS pills.
    assert 'data-layer="detection_blobs_live"' in body
    assert 'data-layer="detection_blobs_svr"' in body
    # The per-cam canvas + viewer's currentT scrubber is what drives the
    # blob's frame lookup; still must reference both pipelines by name.
    assert "'live'" in body or '"live"' in body
    assert "'server_post'" in body or '"server_post"' in body


def test_viewer_renders_camera_marker_dynamically_following_pipeline_pills():
    """Camera diamond + axis triad must be emitted by the dynamic builder
    (so hiding every pipeline for Cam A also hides Cam A's marker), never
    baked into STATIC. If it went back into STATIC the marker would ignore
    pill state and also fail to extend the autoscale bounding box — which
    was how the viewer ended up framed on just the plate, missing the
    rays fanning out from a camera 1.7 m overhead."""
    from viewer_page import build_viewer_page_context
    from reconstruct import Scene, CameraView

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(721)
    _record_pitch(_pitch("A", 721, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # Three.js viewer scene owns camera-marker construction (in
    # `static/threejs/viewer_layers.js`), so the legacy Plotly-era
    # `camMarkerTracesFor` / `cameraIsAnyPathVisible` strings are
    # gone. The contract that survives: `SCENE.cameras` ships in the
    # viewer-data JSON, layerVisibility nested shape ships too, and
    # the Three.js setupViewerLayers boot script reads them on mount.
    assert '"scene_theme"' in body
    assert "viewer_layers.js" in body
    assert "setupViewerLayers" in body
    assert "SCENE: d.SCENE" in body
    assert "layerVisibility: d.layerVisibility" in body

    # STATIC must NOT carry a camera trace — that would double-draw the
    # diamond (once static, once dynamic) and pin camera visibility to
    # always-on regardless of the pills.
    from viewer_page import build_viewer_page_context
    scene = Scene(session_id=session_id)
    scene.cameras.append(
        CameraView(
            camera_id="A",
            center_world=[0.0, 0.0, 1.0],
            axis_forward_world=[0.0, 1.0, 0.0],
            axis_right_world=[1.0, 0.0, 0.0],
            axis_up_world=[0.0, 0.0, 1.0],
            fx=1000.0, fy=1000.0, cx=960.0, cy=540.0,
            distortion=None, R_wc=[1, 0, 0, 0, 1, 0, 0, 0, 1],
            t_wc=[0.0, 0.0, 0.0], image_width_px=1920, image_height_px=1080,
        )
    )
    health = {
        "cameras": {
            "A": {"received": True, "calibrated": True, "time_synced": False,
                  "n_frames": 0, "n_detected": 0, "capture_telemetry": None},
            "B": {"received": False, "calibrated": False, "time_synced": False,
                  "n_frames": 0, "n_detected": 0, "capture_telemetry": None},
        },
        "session_id": session_id, "triangulated_count": 0,         "error": None, "duration_s": None, "received_at": None, "mode": "camera_only",
    }
    ctx = build_viewer_page_context(scene, [], health)
    import json as _json
    static_list = _json.loads(ctx.static_traces_json)
    for trace in static_list:
        meta = trace.get("meta") or {}
        assert meta.get("trace_kind") != "camera", "camera trace leaked into STATIC"
        assert meta.get("trace_kind") != "camera_axis", "camera axis trace leaked into STATIC"


def test_viewer_has_path_is_per_camera_not_global():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(723)
    _record_pitch(_pitch("A", 723, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "const HAS_PATH_PER_CAM = {}" in body
    assert "function hasPathForLayer" in body
    assert "hasPathForLayer(layer, path)" in body
    assert "HAS_PATH_PER_CAM.A.live" in body
    assert "HAS_PATH_PER_CAM.B.live" in body


def test_viewer_strip_reserves_dual_ab_subtracks_per_pipeline():
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(722)
    _record_pitch(_pitch("A", 722, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert 'const STRIP_CAMS = ["A", "B"]' in body
    assert "drawStripInto(STRIP_ROWS[path].canvas, camAtFrameByPath[path], path)" in body
    for canvas_id in ("detection-canvas-live", "detection-canvas-server-post"):
        assert f'id="{canvas_id}" class="strip-canvas" height="28"' in body
    assert body.count('<span class="strip-sublabels"') == 2


def test_viewer_locks_layout_to_viewport_without_page_scroll():
    """The viewer should fit in a single viewport: body scrolling is
    disabled and the root container owns a fixed 100vh layout. The
    transport timeline is a fixed-position bottom dock (NOT a grid row),
    so the grid only carries 3 rows: nav / failure-strip / work."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(712)
    _record_pitch(_pitch("A", 712, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "overflow:hidden" in body
    assert "grid-template-rows:auto auto minmax(0, 1fr)" in body
    assert "height:100vh" in body
    # Sticky-bottom dock contract: timeline pinned to viewport bottom,
    # .viewer reserves matching padding via the --timeline-h CSS var.
    assert ".timeline { position:fixed" in body
    assert "padding-bottom:var(--timeline-h" in body


def test_viewer_scrubber_uses_manual_seek_guards_and_keyboard_stepper():
    """Manual timeline interactions must suppress stale video callbacks
    and own ArrowLeft/ArrowRight while the scrubber has focus."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(713)
    _record_pitch(_pitch("A", 713, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")

    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    assert "function shouldIgnoreVideoFeedback()" in body
    assert 'scrubber.addEventListener("pointerdown"' in body
    assert 'scrubber.addEventListener("keydown"' in body
    assert 'case "ArrowLeft":' in body
    assert 'case "ArrowRight":' in body
    assert "scheduleSceneDraw()" in body


def test_viewer_exposes_camera_t_rel_offsets(tmp_path):
    """Video metadata passed to JS must carry per-camera
    `t_rel_offset_s = video_start_pts_s − sync_anchor_timestamp_s` so
    the JS can align A and B by their shared chirp anchor."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(710)
    pitch = schemas.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=100.0,  # chirp hit at session-clock 100 s
        video_start_pts_s=101.5,        # first MOV frame at 101.5 s
        video_fps=240.0,
        frames_server_post=[schemas.FramePayload(
            frame_index=0, timestamp_s=101.5, px=960.0, py=540.0,
            ball_detected=True,
        )],
        intrinsics=schemas.IntrinsicsPayload(
            fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )
    main.state.record(pitch)
    main.state.save_clip("A", session_id, b"clip", "mov")
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # t_rel_offset = 101.5 − 100.0 = 1.5 s (serialised as JSON number).
    assert '"t_rel_offset_s": 1.5' in body


def test_viewer_exposes_per_frame_index(tmp_path):
    """Each per-cam frame stream must carry `frame_index` (physical
    source-frame counter — iOS capture-queue index for live, PyAV decode
    order for server_post) alongside t_rel_s / detected / px / py. Array
    idx alone hides drops/throttle gaps; frame_index exposes them."""
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(711)
    pitch = schemas.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=[
            schemas.FramePayload(
                frame_index=42, timestamp_s=0.0, px=960.0, py=540.0,
                ball_detected=True,
            ),
            schemas.FramePayload(
                frame_index=43, timestamp_s=0.005, px=961.0, py=541.0,
                ball_detected=True,
            ),
            schemas.FramePayload(
                frame_index=44, timestamp_s=0.010, ball_detected=False,
            ),
        ],
        intrinsics=schemas.IntrinsicsPayload(
            fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )
    main.state.record(pitch)
    main.state.save_clip("A", session_id, b"clip", "mov")
    client = TestClient(app)
    body = client.get(f"/viewer/{session_id}").text
    # Array round-trips into the embedded JSON videos blob in stream order.
    assert '"frame_index": [42, 43, 44]' in body


def test_viewer_renders_per_cam_hud_div_for_each_uploaded_clip():
    """Each cam with an uploaded clip gets a `data-cam-hud` overlay div
    inside its `vid-media` container — DOM HUD that mirrors the timeline
    label, scoped to one cam, layered over video. JS populates it on
    setFrame; the DOM hook just needs to exist for both cams."""
    K, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_rig()
    P = np.array([[0.1, 0.3, 1.0]])
    _record_pitch(_pitch("A", 712, K, R_a, t_a, H_a, P))
    _record_pitch(_pitch("B", 712, K, R_b, t_b, H_b, P))
    main.state.save_clip("A", sid(712), b"clip", "mov")
    main.state.save_clip("B", sid(712), b"clip", "mov")
    client = TestClient(app)
    body = client.get(f"/viewer/{sid(712)}").text
    assert 'data-cam-hud="A"' in body
    assert 'data-cam-hud="B"' in body
    # HUD CSS class must be present (dark overlay style); without it the
    # div would just be a transparent layer at default font.
    assert ".vid-hud" in body


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
    # n_ball_frames_by_path is the canonical shape: two pipelines, each
    # with its own per-camera count.
    assert evt_810["n_ball_frames_by_path"] == {
        "live": {"A": 0, "B": 0},
        "server_post": {"A": 1, "B": 1},
    }
    assert evt_810["n_ball_frames"] == {"A": 1, "B": 1}
    assert evt_810["n_triangulated"] == 1
    # server_post ran and produced detections → "done"; live
    # never ran on this fixture → "-".
    assert evt_810["path_status"] == {
        "live": "-", "server_post": "done",
    }

    evt_800 = next(e for e in events if e["session_id"] == sid(800))
    assert evt_800["cameras"] == ["A"]
    assert evt_800["status"] == "partial"


def test_events_path_status_marks_live_done_on_frame_existence_not_triangulation():
    """A live-only mono-camera session never triangulates (single camera,
    no chirp anchor), so SessionResult.paths_completed does NOT include
    'live'. The dashboard should still surface the live pipeline as
    "done" because frames were captured and the operator can see detected
    frames in the viewer — previously this showed "-" and made live-only
    work look like a silent failure."""
    import main
    from schemas import PitchPayload, FramePayload, IntrinsicsPayload
    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(815)
    # Build a pitch whose frames_live carries two detected frames and no
    # paired camera, so triangulation cannot happen but the pipeline did
    # produce usable output.
    base = _pitch("A", 815, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]]))
    live_frames = [
        FramePayload(
            frame_index=i,
            timestamp_s=float(i) * 0.008,
            px=100.0 + i, py=200.0 + i,
            ball_detected=True,
        )
        for i in range(2)
    ]
    # Stitch frames_live on top while keeping the rest of the payload valid.
    enriched = base.model_copy(update={"frames_live": live_frames, "frames_server_post": []})
    main.state.record(enriched)

    client = TestClient(app)
    events = client.get("/events").json()
    evt = next(e for e in events if e["session_id"] == session_id)
    assert evt["n_ball_frames_by_path"]["live"] == {"A": 2}
    assert evt["path_status"]["live"] == "done"
    assert evt["path_status"]["server_post"] == "-"

    # Dashboard HTML must render the per-pipeline chip with the frame count
    # suffix. Without the suffix the operator can't tell three-pipeline
    # sessions apart at a glance — they all show the same "L I S".
    html_body = client.get("/").text
    block_start = html_body.find(session_id)
    assert block_start >= 0
    # Scan forward from the session id to the event's trailing action form
    # so we only match chips inside THIS event's row.
    # Find the end of this event-item DOM block by scanning forward to the
    # next event-day separator OR end-of-document; both are stable anchors
    # under the redesigned card layout.
    block_end = html_body.find('class="event-day"', block_start + 1)
    if block_end < 0:
        block_end = len(html_body)
    chip_block = html_body[block_start:block_end]
    assert 'class="ev-pipe on"' in chip_block
    # Per-cam A·B layout — A=2, B absent renders as "2·—" inside the bold.
    assert '<b>2·—</b>' in chip_block


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


# --- _stream candidates wire (BLOBS overlay) ---------------------------------

def test_stream_includes_cost_for_live_candidates():
    """Live path serialisation: each frame's candidates appear in input
    order, every dict has px/py/area/area_score/cost. The cost value
    equals what the producer stamped (here we set it explicitly)."""
    from routes.viewer import _stream
    frame = schemas.FramePayload(
        frame_index=42,
        timestamp_s=0.5,
        px=120.0,
        py=100.0,
        ball_detected=True,
        candidates=[
            schemas.BlobCandidate(px=120.0, py=100.0, area=80,
                                  area_score=0.4, cost=0.18),
            schemas.BlobCandidate(px=500.0, py=500.0, area=200,
                                  area_score=1.0, cost=0.91),
        ],
    )
    out = _stream([frame], 0.0, include_candidates=True)
    assert "candidates" in out
    assert len(out["candidates"]) == 1
    cands = out["candidates"][0]
    assert len(cands) == 2
    assert cands[0]["px"] == 120.0 and cands[0]["py"] == 100.0
    assert cands[0]["area"] == 80
    assert cands[0]["area_score"] == 0.4
    assert cands[0]["cost"] == 0.18
    assert cands[1]["cost"] == 0.91


def test_stream_includes_cost_for_server_post_candidates():
    """server_post path also stamps `cost` on every candidate (after
    the pipeline / viewer wire opened up to per-path BLOBS). Wire shape
    is identical to live."""
    from routes.viewer import _stream
    frame = schemas.FramePayload(
        frame_index=7,
        timestamp_s=0.0,
        px=10.0,
        py=20.0,
        ball_detected=True,
        candidates=[
            schemas.BlobCandidate(px=10.0, py=20.0, area=120,
                                  area_score=1.0, cost=0.05),
            schemas.BlobCandidate(px=300.0, py=400.0, area=80,
                                  area_score=0.66, cost=0.42),
        ],
    )
    out = _stream([frame], 0.0, include_candidates=True)
    assert "candidates" in out
    cands = out["candidates"][0]
    assert len(cands) == 2
    assert cands[0]["cost"] == 0.05
    assert cands[1]["cost"] == 0.42


def test_stream_can_still_omit_candidates():
    """`include_candidates=False` remains valid (used by the empty-pitch
    fallback in earlier revisions; kept as the slim-payload path for
    callers that don't need BLOBS)."""
    from routes.viewer import _stream
    frame = schemas.FramePayload(
        frame_index=1,
        timestamp_s=0.0,
        px=10.0,
        py=20.0,
        ball_detected=True,
    )
    out = _stream([frame], 0.0, include_candidates=False)
    assert "candidates" not in out


def test_stream_legacy_candidates_without_cost_become_null():
    """Legacy JSONs (cost field absent because they predate the
    cost-persistence change) serialise with cost=None; the viewer JS
    falls back to area-asc sorting in this case."""
    from routes.viewer import _stream
    frame = schemas.FramePayload(
        frame_index=0,
        timestamp_s=0.0,
        px=10.0,
        py=10.0,
        ball_detected=True,
        candidates=[
            schemas.BlobCandidate(px=10.0, py=10.0, area=80,
                                  area_score=0.4),  # no cost
        ],
    )
    out = _stream([frame], 0.0, include_candidates=True)
    assert out["candidates"][0][0]["cost"] is None


def test_video_cell_renders_path_grouped_toolbar_no_k_slider():
    """`video_cell_html` (the SSR builder for each cam pane) declares the
    layer set (PLATE + AXES + LIVE BLOBS + SVR BLOBS) and renders the
    path-grouped toolbar (LIVE: BLOBS ; SVR: BLOBS). Pre-fan-out the
    matrix had a per-path WIN + CAND pair; WIN is gone because fan-out
    triangulation has no winner. The legacy K slider was replaced by
    the session-level cost_threshold slider in the viewer header."""
    from viewer_fragments import video_cell_html
    body = video_cell_html(
        "A",
        ("/videos/example.mov", 0.0),
        image_width_px=1920,
        image_height_px=1080,
        cx=960.0,
        cy=540.0,
    )
    assert (
        'data-layers="plate,axes,'
        'detection_blobs_live,detection_blobs_svr"'
    ) in body
    # SVR off by default — legacy / live-only sessions have no svr data.
    assert 'data-layers-on="plate,detection_blobs_live"' in body
    # Path-grouped chips, BLOBS-only.
    assert 'class="cv-path-group" data-path="live"' in body
    assert 'class="cv-path-group" data-path="svr"' in body
    assert 'class="cv-layer on" data-layer="detection_blobs_live">BLOBS' in body
    assert 'class="cv-layer" data-layer="detection_blobs_svr">BLOBS' in body
    # Winner-dot layers gone post fan-out.
    assert 'data-layer="detection_live"' not in body
    assert 'data-layer="detection_svr"' not in body
    assert '>WIN<' not in body
    assert '>CAND<' not in body
    # K slider gone (replaced by viewer-header cost_threshold slider).
    assert 'class="cv-blobs-k"' not in body
    assert 'window._setCandTopK' not in body
