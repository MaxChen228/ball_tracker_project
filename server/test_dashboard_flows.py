"""Dashboard control-plane E2E: mode-one, mode-two, dual, and the
early-surface guarantee for on-device triangulation."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import main
from main import app

from _test_helpers import (
    _base_payload,
    _encode_single_ball_mov,
    _make_scene,
    _post_pitch,
    _project_pixels,
    _seed_ready_stereo,
)


def _post_calibration(client: TestClient, camera_id: str, K: np.ndarray, H: np.ndarray):
    return client.post(
        "/calibration",
        json={
            "camera_id": camera_id,
            "intrinsics": {
                "fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
            },
            "homography": H.flatten().tolist(),
            "image_width_px": 1920,
            "image_height_px": 1080,
        },
    )


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
    _seed_ready_stereo(client, K, H_a, H_b)
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
    _seed_ready_stereo(client, K, H_a, H_b)
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
    _seed_ready_stereo(client, K, H_a, H_b)
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

    # 7. Viewer HTML includes the per-pipeline strips + independent visibility
    # matrix. The three pipelines render each to their own canvas, and each
    # pill carries data-path=<DetectionPath> — if any of those go missing
    # the three-way separation collapses back into the old svr/on_device
    # alias and the user can't toggle pipelines independently.
    viewer_html = client.get(f"/viewer/{session_id}").text
    assert "detection-canvas-live" in viewer_html
    assert "detection-canvas-ios-post" in viewer_html
    assert "detection-canvas-server-post" in viewer_html
    assert 'data-layer="traj"' in viewer_html
    assert 'data-path="live"' in viewer_html
    assert 'data-path="ios_post"' in viewer_html
    assert 'data-path="server_post"' in viewer_html
    # Hero banner must surface `mode dual`, not regress to camera-only —
    # previously the viewer only checked MOV presence and ignored
    # frames_on_device, so dual sessions mis-labelled as camera-only.
    assert "mode dual" in viewer_html
    # MOV must still be embedded (dual uploads the clip).
    assert "<video" in viewer_html


def test_dual_mode_on_device_surfaces_before_server_detection(tmp_path, monkeypatch):
    """Early-surface guarantee: in dual mode, `result.points_on_device`
    becomes available as soon as both cameras' payloads arrive, even if
    server MOV detection never finishes. Implemented by monkey-patching
    `detect_pitch` to a no-op — if the on-device triangulation were
    coupled to server detection, stubbing detection would also zero out
    `points_on_device`. It must not."""
    import main as server_main
    import pytest

    detect_calls: list[Path] = []

    def _stub_detect(clip_path, video_start_pts_s, should_cancel=None, **kwargs):
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
    _seed_ready_stereo(client, K, H_a, H_b)
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
    assert len(result["triangulated"]) == 1, "authority should still surface the successful ios_post path"
    assert result["error"] is None, "ios_post-only success in dual mode must not be downgraded to no detection completed"
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
    assert matched[0]["status"] == "paired"
    assert matched[0]["n_triangulated"] == 1
    assert matched[0]["n_triangulated_on_device"] == 1
    assert matched[0]["peak_z_m"] == pytest.approx(P_true[2], abs=1e-6)
