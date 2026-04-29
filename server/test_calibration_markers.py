"""Calibration auto + markers tests."""
from __future__ import annotations

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import app

from _test_helpers import _make_scene


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


def test_calibration_auto_crops_4_3_input_and_rescales_to_canonical_16_9(
    tmp_path, monkeypatch
):
    """iPhone 12 MP photo capture is 4032×3024 (4:3). Server must:
      1. Center-crop top/bottom to 4032×2268 (16:9 detection basis).
      2. Run ArUco + solve H on the cropped frame.
      3. Rescale K + H down to canonical 1920×1080 before storing.
    Snapshot dims must always be 1920×1080 regardless of input resolution
    so live-path CameraPose + pitch-time pairing scaling stay consistent.
    """
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    bgr_4_3, _H = _render_aruco_scene(
        PLATE_MARKER_WORLD,
        image_size=(4032, 3024),
        scale_px_per_m=1500.0,  # keeps plate markers within central 16:9 band
    )
    _seed_calibration_frame("A", _jpeg_encode(bgr_4_3))

    r = client.post("/calibration/auto/A?h_fov_deg=73.828")
    assert r.status_code == 200, r.text
    body = r.json()
    # Canonical storage basis — independent of input 4032×3024.
    assert body["image_width_px"] == 1920
    assert body["image_height_px"] == 1080
    assert sorted(body["detected_ids"]) == sorted(PLATE_MARKER_WORLD.keys())

    # Snapshot file on disk must also be at canonical dims (downstream
    # consumers — live CameraPose, pairing, viewer — read it directly).
    import json as _json
    snap = _json.loads((tmp_path / "calibrations" / "A.json").read_text())
    assert snap["image_width_px"] == 1920
    assert snap["image_height_px"] == 1080
    # Principal point should land near image centre after rescale; the
    # 4:3→16:9 crop shifted cy by 378 px in detection basis, then a
    # 0.476× linear rescale to 1080p brings (cx,cy) to (~960, ~540).
    assert 900 < snap["intrinsics"]["cx"] < 1020
    assert 480 < snap["intrinsics"]["cy"] < 600

    # H content check: project plate origin (X=0, Y=0) through the stored
    # canonical homography. With the synthetic scene's center_px at the
    # image centre of the 4032×3024 source, plate origin maps to image
    # centre in detection basis (4032×2268 cropped) → after 0.476× scale
    # to canonical 1920×1080, plate origin should land within a few px of
    # (960, 540). This catches buggy _scale_homography (e.g. forgetting
    # h33 normalisation) that cx/cy bound asserts would miss.
    H_canonical = np.array(snap["homography"], dtype=np.float64).reshape(3, 3)
    plate_origin = np.array([0.0, 0.0, 1.0])
    proj = H_canonical @ plate_origin
    assert abs(proj[2]) > 1e-9, "H projects plate origin to point at infinity"
    u = proj[0] / proj[2]
    v = proj[1] / proj[2]
    assert abs(u - 960.0) < 5.0, f"plate origin u={u} not near canonical centre 960"
    assert abs(v - 540.0) < 5.0, f"plate origin v={v} not near canonical centre 540"


def test_calibration_auto_returns_accumulating_when_too_few_markers(tmp_path, monkeypatch):
    """Sub-threshold marker count is no longer an error — the multi-frame
    accumulator returns phase=accumulating (HTTP 200) so the operator can
    press [Calibrate] again with the cam pointed at a different marker
    subset, or hit Clear if they want to start over."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    partial = {k: PLATE_MARKER_WORLD[k] for k in (0, 1, 5)}
    bgr, _H = _render_aruco_scene(partial)
    _seed_calibration_frame("A", _jpeg_encode(bgr))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phase"] == "accumulating"
    assert body["buffer_summary"]["count"] == 3
    assert body["buffer_summary"]["ready"] is False
    assert sorted(body["buffer_summary"]["marker_ids"]) == [0, 1, 5]
    # No snapshot written — buffer accumulates instead.
    assert not (tmp_path / "calibrations" / "A.json").exists()


def test_calibration_auto_returns_408_when_no_frame_delivered(tmp_path, monkeypatch):
    """No pre-seeded cal frame + no iOS uploader in the test harness →
    /calibration/auto polls the burst budget then times out with 408."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    r = client.post("/calibration/auto/A")
    assert r.status_code == 408, r.text
    assert "within 10 s" in r.json()["detail"].lower()


def test_calibration_auto_uses_pose_solver_when_3d_markers_available(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, _cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=9,
            x_m=-0.40,
            y_m=-0.60,
            z_m=0.15,
            on_plate_plane=False,
            source_camera_ids=["A", "B"],
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=11,
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
        9: (-0.40, -0.60, 0.15),
        11: (-0.40, -0.40, 0.0),
    })
    bgr_a = _render_aruco_scene_3d(marker_xyz, K=K, R=R_a, t=t_a)
    _seed_calibration_frame("A", _jpeg_encode(bgr_a))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["used_pose_solver"] is True
    assert body["n_3d_markers_used"] >= 1
    assert 9 in body["detected_ids"]


def test_calibration_auto_accumulates_across_multiple_presses_then_solves(
    tmp_path, monkeypatch,
):
    """Each POST captures one frame; markers union into the per-cam
    buffer until ≥5 distinct ids accumulate, then solve runs and clears.
    Operator's mental model: 'aim at 3 markers, click; aim at 3 more,
    click; on the second click ≥5 are accumulated and the snapshot
    lands.'"""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD

    # Frame 1: markers 0, 1, 2 only (sub-threshold).
    frame1 = {k: PLATE_MARKER_WORLD[k] for k in (0, 1, 2)}
    bgr1, _ = _render_aruco_scene(frame1)
    _seed_calibration_frame("A", _jpeg_encode(bgr1))
    r1 = client.post("/calibration/auto/A")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["phase"] == "accumulating"
    assert body1["buffer_summary"]["count"] == 3

    # Frame 2: markers 3, 4, 5 — buffer crosses threshold, solve fires.
    # _render_aruco_scene draws all PLATE_MARKER_WORLD entries it gets,
    # so we feed it a different subset to simulate aiming at a new region.
    frame2 = {k: PLATE_MARKER_WORLD[k] for k in (3, 4, 5)}
    bgr2, _ = _render_aruco_scene(frame2)
    _seed_calibration_frame("A", _jpeg_encode(bgr2))
    r2 = client.post("/calibration/auto/A")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["phase"] == "solve_ok"
    assert body2["camera_id"] == "A"
    # Snapshot written.
    assert (tmp_path / "calibrations" / "A.json").exists()
    # Buffer cleared after success.
    assert body2["buffer_summary"]["count"] == 0


def test_calibration_buffer_clear_endpoint_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    frame = {k: PLATE_MARKER_WORLD[k] for k in (0, 1, 2)}
    bgr, _ = _render_aruco_scene(frame)
    _seed_calibration_frame("A", _jpeg_encode(bgr))
    client.post("/calibration/auto/A")  # populates buffer (sub-threshold)

    r1 = client.post("/calibration/buffer/clear/A")
    assert r1.status_code == 200
    assert r1.json() == {"ok": True, "camera_id": "A", "cleared": True}

    # Second clear is idempotent — cleared=False (already empty).
    r2 = client.post("/calibration/buffer/clear/A")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True, "camera_id": "A", "cleared": False}


def test_calibration_reset_rig_wipes_calibrations_markers_buffers(tmp_path, monkeypatch):
    """Dashboard 'Reset rig' clears calibrations + extended markers +
    accumulator buffers. Per-device ChArUco intrinsics survive (they're
    sensor-physical, not rig-geometry)."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    bgr, _ = _render_aruco_scene(PLATE_MARKER_WORLD)
    _seed_calibration_frame("A", _jpeg_encode(bgr))
    r_solve = client.post("/calibration/auto/A")
    assert r_solve.json()["phase"] == "solve_ok"
    assert "A" in main.state.calibrations()

    # Add a partial buffer on B so reset has something to clear there too.
    partial = {k: PLATE_MARKER_WORLD[k] for k in (0, 1)}
    bgr_b, _ = _render_aruco_scene(partial)
    _seed_calibration_frame("B", _jpeg_encode(bgr_b))
    client.post("/calibration/auto/B")
    assert main.state.calibration_buffer_summary("B")["count"] == 2

    r_reset = client.post("/calibration/reset_rig")
    assert r_reset.status_code == 200
    body = r_reset.json()
    assert body["ok"] is True
    assert body["calibrations_removed"] == 1
    assert body["buffers_cleared"] == 1

    assert main.state.calibrations() == {}
    assert main.state.calibration_buffer_summary("B")["count"] == 0


def test_markers_scan_triangulates_dual_camera_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    R_b, t_b, _C_b, H_b = cam_b
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="B",
            intrinsics=main.IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy),
            homography=H_b.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )

    from calibration_solver import PLATE_MARKER_WORLD
    marker_xyz = {mid: (xy[0], xy[1], 0.0) for mid, xy in PLATE_MARKER_WORLD.items()}
    truth_new = {
        9: (-0.40, -0.60, 0.15),
        11: (-0.40, -0.40, 0.0),
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
    assert set(got.keys()) == {9, 11}
    for mid, (x_m, y_m, z_m) in truth_new.items():
        row = got[mid]
        assert abs(row["x_m"] - x_m) < 0.03
        assert abs(row["y_m"] - y_m) < 0.03
        assert abs(row["z_m"] - z_m) < 0.03
    assert got[11]["suggest_on_plate_plane"] is True
    assert got[9]["suggest_on_plate_plane"] is False


def test_markers_crud_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    state = main.state
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=9, x_m=1.0, y_m=2.0, z_m=0.0, on_plate_plane=True)
    )
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=10, x_m=-1.0, y_m=0.5, z_m=0.4, on_plate_plane=False)
    )
    assert client.get("/markers/state").json()["markers"] == [
        {
            "marker_id": 9,
            "label": None,
            "x_m": 1.0,
            "y_m": 2.0,
            "z_m": 0.0,
            "on_plate_plane": True,
            "residual_m": None,
            "source_camera_ids": [],
        },
        {
            "marker_id": 10,
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
        {"id": 9, "wx": 1.0, "wy": 2.0},
    ]

    # Persistence: recreate State from the same dir, registry must survive.
    main.state = main.State(data_dir=tmp_path)
    persisted = {rec.marker_id: rec for rec in main.state._marker_registry.all_records()}
    assert persisted[9].on_plate_plane is True
    assert persisted[10].z_m == 0.4

    r = client.delete("/markers/9")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main.state._marker_registry.get(9) is None

    r = client.delete("/markers/99")
    assert r.status_code == 404

    r = client.post("/markers/clear")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared_count": 1}
    assert client.get("/markers/state").json()["markers"] == []


def test_markers_reject_plate_reserved_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    db = main.state._marker_registry
    for reserved in (0, 1, 2, 3, 4, 5, 6, 7, 8):
        with pytest.raises(Exception):
            db.upsert(main.MarkerRecord(marker_id=reserved, x_m=0.0, y_m=0.0, z_m=0.0))
    with pytest.raises(Exception):
        db.upsert(main.MarkerRecord(marker_id=50, x_m=0.0, y_m=0.0, z_m=0.0))
