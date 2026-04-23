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


def test_calibration_auto_returns_422_when_too_few_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    from calibration_solver import PLATE_MARKER_WORLD
    partial = {k: PLATE_MARKER_WORLD[k] for k in (0, 1, 5)}
    bgr, _H = _render_aruco_scene(partial)
    _seed_calibration_frame("A", _jpeg_encode(bgr))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 422, r.text
    assert "need" in r.json()["detail"].lower()


def test_calibration_auto_returns_408_when_no_frame_delivered(tmp_path, monkeypatch):
    """No pre-seeded cal frame + no iOS uploader in the test harness →
    /calibration/auto polls the burst budget then times out with 408."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    r = client.post("/calibration/auto/A")
    assert r.status_code == 408, r.text
    assert "within 6 s" in r.json()["detail"].lower()


def test_calibration_auto_uses_pose_solver_when_3d_markers_available(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, _cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=7,
            x_m=-0.40,
            y_m=-0.60,
            z_m=0.15,
            on_plate_plane=False,
            source_camera_ids=["A", "B"],
        )
    )
    main.state._marker_registry.upsert(
        main.MarkerRecord(
            marker_id=12,
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
        7: (-0.40, -0.60, 0.15),
        12: (-0.40, -0.40, 0.0),
    })
    bgr_a = _render_aruco_scene_3d(marker_xyz, K=K, R=R_a, t=t_a)
    _seed_calibration_frame("A", _jpeg_encode(bgr_a))

    r = client.post("/calibration/auto/A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["used_pose_solver"] is True
    assert body["n_3d_markers_used"] >= 1
    assert 7 in body["detected_ids"]


def test_markers_scan_triangulates_dual_camera_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    K, fx, fy, cx, cy, cam_a, cam_b = _make_scene()
    R_a, t_a, _C_a, H_a = cam_a
    R_b, t_b, _C_b, H_b = cam_b
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="A",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_a.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )
    main.state.set_calibration(
        main.CalibrationSnapshot(
            camera_id="B",
            intrinsics=main.IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy),
            homography=H_b.flatten().tolist(),
            image_width_px=1920,
            image_height_px=1080,
        )
    )

    from calibration_solver import PLATE_MARKER_WORLD
    marker_xyz = {mid: (xy[0], xy[1], 0.0) for mid, xy in PLATE_MARKER_WORLD.items()}
    truth_new = {
        7: (-0.40, -0.60, 0.15),
        12: (-0.40, -0.40, 0.0),
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
    assert set(got.keys()) == {7, 12}
    for mid, (x_m, y_m, z_m) in truth_new.items():
        row = got[mid]
        assert abs(row["x_m"] - x_m) < 0.03
        assert abs(row["y_m"] - y_m) < 0.03
        assert abs(row["z_m"] - z_m) < 0.03
    assert got[12]["suggest_on_plate_plane"] is True
    assert got[7]["suggest_on_plate_plane"] is False


def test_markers_crud_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    state = main.state
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=7, x_m=1.0, y_m=2.0, z_m=0.0, on_plate_plane=True)
    )
    state._marker_registry.upsert(
        main.MarkerRecord(marker_id=8, x_m=-1.0, y_m=0.5, z_m=0.4, on_plate_plane=False)
    )
    assert client.get("/markers/state").json()["markers"] == [
        {
            "marker_id": 7,
            "label": None,
            "x_m": 1.0,
            "y_m": 2.0,
            "z_m": 0.0,
            "on_plate_plane": True,
            "residual_m": None,
            "source_camera_ids": [],
        },
        {
            "marker_id": 8,
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
        {"id": 7, "wx": 1.0, "wy": 2.0},
    ]

    # Persistence: recreate State from the same dir, registry must survive.
    main.state = main.State(data_dir=tmp_path)
    persisted = {rec.marker_id: rec for rec in main.state._marker_registry.all_records()}
    assert persisted[7].on_plate_plane is True
    assert persisted[8].z_m == 0.4

    r = client.delete("/markers/7")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main.state._marker_registry.get(7) is None

    r = client.delete("/markers/99")
    assert r.status_code == 404

    r = client.post("/markers/clear")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared_count": 1}
    assert client.get("/markers/state").json()["markers"] == []


def test_markers_reject_plate_reserved_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    db = main.state._marker_registry
    for reserved in (0, 1, 2, 3, 4, 5):
        with pytest.raises(Exception):
            db.upsert(main.MarkerRecord(marker_id=reserved, x_m=0.0, y_m=0.0, z_m=0.0))
    with pytest.raises(Exception):
        db.upsert(main.MarkerRecord(marker_id=50, x_m=0.0, y_m=0.0, z_m=0.0))
