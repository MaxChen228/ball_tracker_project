"""Tests for the ChArUco intrinsics calibration script.

Synthetic test: render a board, project it under known (K, pose) pairs, feed the
projected points into calibrateCameraCharuco, and assert the recovered K is
within 1 px of ground truth. This validates the calibration pipeline end-to-end
without needing physical photos.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from calibrate_intrinsics import (
    BoardSpec,
    calibrate_from_images,
    main as cli_main,
    render_board_png,
)


def _board_spec() -> BoardSpec:
    return BoardSpec(squares_x=5, squares_y=7, square_length_m=0.040, marker_length_m=0.030)


def test_print_board_writes_readable_png(tmp_path: Path):
    out = tmp_path / "board.png"
    render_board_png(_board_spec(), out)
    assert out.exists() and out.stat().st_size > 1000
    img = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)
    assert img is not None and img.shape[0] > 100 and img.shape[1] > 100


def test_calibration_recovers_known_intrinsics_from_synthetic_projections():
    board_spec = _board_spec()
    board, _ = board_spec.build()

    # Ground-truth intrinsics: 1920x1080 sensor, ~60° horizontal FOV.
    fx_true = 1600.0
    fy_true = 1600.0
    cx_true = 960.0
    cy_true = 540.0
    K_true = np.array([[fx_true, 0, cx_true], [0, fy_true, cy_true], [0, 0, 1.0]])
    dist_true = np.zeros(5)
    img_size = (1920, 1080)

    # 3D positions of all inner ChArUco corners on the board plane (Z=0).
    corners_3d = board.getChessboardCorners().astype(np.float64)  # (N, 3)
    ids = np.arange(len(corners_3d), dtype=np.int32).reshape(-1, 1)

    # 18 varied poses: tilt/yaw combinations + a range of distances. Need ≥4,
    # but a realistic calibration uses ~15–25 well-distributed views.
    rng = np.random.default_rng(seed=42)
    poses = []
    for i in range(18):
        rx = (i - 9) * 0.12
        ry = rng.uniform(-0.5, 0.5)
        rz = rng.uniform(-0.2, 0.2)
        tx = rng.uniform(-0.15, 0.15)
        ty = rng.uniform(-0.10, 0.10)
        tz = 0.55 + rng.uniform(-0.10, 0.20)
        poses.append((np.array([rx, ry, rz]), np.array([tx, ty, tz])))

    all_corners: list[np.ndarray] = []
    all_ids: list[np.ndarray] = []
    for rvec, tvec in poses:
        pts2d, _ = cv2.projectPoints(corners_3d, rvec, tvec, K_true, dist_true)
        all_corners.append(pts2d.reshape(-1, 1, 2).astype(np.float32))
        all_ids.append(ids.copy())

    rms, K_rec, _, _, _ = cv2.aruco.calibrateCameraCharuco(
        all_corners,
        all_ids,
        board,
        img_size,
        cameraMatrix=None,
        distCoeffs=None,
        flags=0,
    )

    # Noise-free projections → calibration should be tight to ground truth.
    assert rms < 0.5, f"rms too high: {rms}"
    assert abs(K_rec[0, 0] - fx_true) < 1.0
    assert abs(K_rec[1, 1] - fy_true) < 1.0
    assert abs(K_rec[0, 2] - cx_true) < 1.0
    assert abs(K_rec[1, 2] - cy_true) < 1.0


def test_end_to_end_cli_with_rendered_views(tmp_path: Path, capsys):
    """Render the board, warp it into multiple camera views, and run the CLI."""
    board_spec = _board_spec()
    board, _ = board_spec.build()

    # Canonical (fronto-parallel) render.
    px_per_m = 4000
    w_px = int(board_spec.squares_x * board_spec.square_length_m * px_per_m)
    h_px = int(board_spec.squares_y * board_spec.square_length_m * px_per_m)
    canonical = board.generateImage((w_px, h_px), marginSize=40, borderBits=1)

    # Warp into synthetic "camera views" via planar homographies. Each view is a
    # mild perspective transform of the canonical image, simulating a camera
    # looking at the board from slightly different angles.
    img_size = (canonical.shape[1], canonical.shape[0])
    src_pts = np.float32([[0, 0], [img_size[0], 0], [img_size[0], img_size[1]], [0, img_size[1]]])

    images_dir = tmp_path / "imgs"
    images_dir.mkdir()

    offsets = [
        (-80, -60, 40, -30, -30, 50, 60, 70),
        (-30, -80, 60, -40, -50, 40, 40, 80),
        (20, -100, 80, -20, 30, 100, -30, 60),
        (-60, -40, 60, -60, -60, 60, 40, 40),
        (-40, -30, 40, -20, -30, 30, 40, 30),
        (-20, -60, 50, -10, -30, 50, 30, 80),
    ]
    for i, (dx1, dy1, dx2, dy2, dx3, dy3, dx4, dy4) in enumerate(offsets):
        dst_pts = np.float32([
            [dx1, dy1],
            [img_size[0] + dx2, dy2],
            [img_size[0] + dx3, img_size[1] + dy3],
            [dx4, img_size[1] + dy4],
        ])
        H = cv2.getPerspectiveTransform(src_pts, dst_pts)
        warped = cv2.warpPerspective(canonical, H, img_size, borderValue=255)
        cv2.imwrite(str(images_dir / f"view_{i:02d}.png"), warped)

    out_json = tmp_path / "intrinsics.json"
    rc = cli_main([
        "--images-glob", str(images_dir / "*.png"),
        "--squares-x", str(board_spec.squares_x),
        "--squares-y", str(board_spec.squares_y),
        "--square-length-m", str(board_spec.square_length_m),
        "--marker-length-m", str(board_spec.marker_length_m),
        "--dict", board_spec.dict_name,
        "--out", str(out_json),
    ])
    assert rc == 0
    assert out_json.exists()

    import json
    data = json.loads(out_json.read_text())
    assert data["num_images_used"] >= 4
    assert data["fx"] > 0 and data["fy"] > 0
    assert 0 < data["cx"] < data["image_width"]
    assert 0 < data["cy"] < data["image_height"]


def test_too_few_images_errors(tmp_path: Path):
    board_spec = _board_spec()
    board, _ = board_spec.build()
    canonical = board.generateImage((800, 1100), marginSize=20, borderBits=1)

    # Only 2 usable views — below the 4-image minimum.
    images_dir = tmp_path / "imgs"
    images_dir.mkdir()
    cv2.imwrite(str(images_dir / "a.png"), canonical)
    cv2.imwrite(str(images_dir / "b.png"), canonical)

    with pytest.raises(SystemExit, match="need ≥4"):
        calibrate_from_images(
            sorted(str(p) for p in images_dir.glob("*.png")),
            board_spec,
        )
