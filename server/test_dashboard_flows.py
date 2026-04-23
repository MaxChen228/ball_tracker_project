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


def test_dashboard_drives_mode_one_end_to_end(tmp_path):
    """Symmetric integration: arm → each phone POSTs a MOV → operator hits
    Run server on the events row → detection + triangulation → events +
    viewer show the camera-only tag + MOV accessible."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    client = TestClient(app)

    # 1. Arm (no mode toggle — CaptureMode has only camera_only).
    _seed_ready_stereo(client, K, H_a, H_b)
    arm = client.post(
        "/sessions/arm", headers={"Accept": "application/json"}
    ).json()
    session_id = arm["session"]["id"]
    assert arm["session"]["mode"] == "camera_only"

    # 2. Both phones upload a MOV with a ball at the projected pixel.
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
    # Server-post no longer auto-runs. Until the operator asks for it, the
    # session has zero triangulated points from the server pipeline.
    assert r_b.json()["detection_pending"] is False
    assert client.get(f"/results/{session_id}").json()["points"] == []

    # 3. Operator hits Run server on the events row.
    run = client.post(
        f"/sessions/{session_id}/run_server_post",
        headers={"Accept": "application/json"},
    )
    assert run.status_code == 200, run.text
    assert run.json()["queued"] == 2
    # Background jobs finish during the TestClient drain that follows.
    assert len(client.get(f"/results/{session_id}").json()["points"]) >= 1

    # 4. Events tags it as camera_only (MOV on disk).
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


