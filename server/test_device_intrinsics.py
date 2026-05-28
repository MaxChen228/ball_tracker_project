"""Device-keyed ChArUco intrinsics endpoints + auto-cal integration."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
from main import app
from conftest import preassign_and_open_ws


_DEVICE_ID = "abc12345-1234-5678-90ab-cdef00112233"
# ChArUco shots taken in the same 16:9 video format the sensor delivers
# during auto-cal (via AVCapturePhotoOutput on a 1080p activeFormat).
# Using 4:3 Camera.app stills would trip the AR-mismatch guard in
# _derive_auto_cal_intrinsics — see the docstring there.
_VALID_BODY = {
    "device_model": "iPhone15,3",
    "source_width_px": 1920,
    "source_height_px": 1080,
    "intrinsics": {
        "fx": 1580.5,
        "fy": 1581.2,
        "cx": 960.3,
        "cy": 540.7,
        "distortion": [0.12, -0.25, 0.001, -0.001, 0.08],
    },
    "rms_reprojection_px": 0.34,
    "n_images": 18,
    "calibrated_at": 1730000000.0,
    "source_label": "charuco-video-format-iphone15pro",
}


def test_upload_and_list_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    r = client.post(f"/calibration/intrinsics/{_DEVICE_ID}", json=_VALID_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == _DEVICE_ID

    on_disk = tmp_path / "intrinsics" / f"{_DEVICE_ID}.json"
    assert on_disk.exists()
    saved = json.loads(on_disk.read_text())
    assert saved["device_id"] == _DEVICE_ID
    assert saved["intrinsics"]["fx"] == pytest.approx(1580.5)

    r2 = client.get("/calibration/intrinsics")
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["device_model"] == "iPhone15,3"
    assert items[0]["rms_reprojection_px"] == pytest.approx(0.34)


def test_delete_removes_record(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    client.post(f"/calibration/intrinsics/{_DEVICE_ID}", json=_VALID_BODY)

    r = client.delete(f"/calibration/intrinsics/{_DEVICE_ID}")
    assert r.status_code == 200
    assert not (tmp_path / "intrinsics" / f"{_DEVICE_ID}.json").exists()

    r2 = client.delete(f"/calibration/intrinsics/{_DEVICE_ID}")
    assert r2.status_code == 404


def test_invalid_device_id_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    # Contains a "/" — would escape the intrinsics directory if not blocked
    r = client.post("/calibration/intrinsics/..%2Fescape", json=_VALID_BODY)
    # FastAPI's path converter gives 404 before our handler even runs for
    # encoded slashes, which is the same defense-in-depth we want. Bare
    # invalid chars hit our 400 directly.
    assert r.status_code in (400, 404)

    r2 = client.post("/calibration/intrinsics/has spaces", json=_VALID_BODY)
    assert r2.status_code in (400, 404)


def test_principal_point_outside_image_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    bad = json.loads(json.dumps(_VALID_BODY))
    bad["intrinsics"]["cx"] = 9999.0  # way outside 1920
    r = client.post(f"/calibration/intrinsics/{_DEVICE_ID}", json=bad)
    assert r.status_code == 422
    assert "cx" in r.text


def test_distortion_wrong_length_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)
    bad = json.loads(json.dumps(_VALID_BODY))
    bad["intrinsics"]["distortion"] = [0.1, -0.2]  # only 2 coefficients
    r = client.post(f"/calibration/intrinsics/{_DEVICE_ID}", json=bad)
    assert r.status_code == 422


def test_auto_cal_consumes_charuco_prior(tmp_path, monkeypatch):
    """The core win: after uploading a ChArUco record for device X, auto-cal
    for the role that device currently plays must pick up the measured K
    (scaled to the current frame dims + distortion carried over), not fall
    back to FOV approximation."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    state = main.state

    # Simulate the iPhone heartbeating in role "A" with its identifierForVendor.
    state.heartbeat(
        "A",
        device_id=_DEVICE_ID,
        device_model="iPhone15,3",
    )
    # Upload ChArUco prior (4032x3024).
    from schemas import DeviceIntrinsics
    rec = DeviceIntrinsics.model_validate({"device_id": _DEVICE_ID, **_VALID_BODY})
    state.set_device_intrinsics(rec)

    # The auto-cal derive helper should use the prior 1:1 when AR matches.
    from calibration_auto import _derive_auto_cal_intrinsics

    intrinsics, legacy_prior, source = _derive_auto_cal_intrinsics(
        "A", w_img=1920, h_img=1080, h_fov_deg=None,
    )
    assert legacy_prior is None  # not the legacy CalibrationSnapshot path
    assert source == "charuco"
    assert intrinsics.fx == pytest.approx(1580.5)
    # Distortion propagated verbatim
    assert intrinsics.distortion == [0.12, -0.25, 0.001, -0.001, 0.08]

    # Same device, different target dims (720p): linear scale applies.
    intrinsics_720, _, source_720 = _derive_auto_cal_intrinsics(
        "A", w_img=1280, h_img=720, h_fov_deg=None,
    )
    assert source_720 == "charuco"
    assert intrinsics_720.fx == pytest.approx(1580.5 * (1280 / 1920), rel=1e-6)


def test_auto_cal_handles_4_3_source_via_center_crop(tmp_path, monkeypatch):
    """Real-world case: operator has a calibrate_intrinsics.py run from
    4:3 Camera.app stills (4032×3024). Auto-cal frames arrive as 16:9
    (1920×1080). The scale helper center-crops the 4:3 source vertically
    to match the 16:9 target, so cy shifts and the final K matches what
    the video-format sensor crop would produce."""
    from schemas import DeviceIntrinsics
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    state = main.state
    state.heartbeat("A", device_id=_DEVICE_ID, device_model="iPhone15,3")
    rec = DeviceIntrinsics.model_validate({
        "device_id": _DEVICE_ID,
        "device_model": "iPhone15,3",
        "source_width_px": 4032,
        "source_height_px": 3024,
        "intrinsics": {
            "fx": 2879.46,
            "fy": 2893.06,
            "cx": 2019.97,
            "cy": 1505.34,
            "distortion": [0.19, -0.66, -0.002, 0.001, 0.67],
        },
        "rms_reprojection_px": 1.07,
        "n_images": 39,
    })
    state.set_device_intrinsics(rec)

    from calibration_auto import _derive_auto_cal_intrinsics
    intr, _, source = _derive_auto_cal_intrinsics(
        "A", w_img=1920, h_img=1080, h_fov_deg=None,
    )
    assert source == "charuco"

    # Scale factor: 4032→1920 = 0.4762 (same as 2268→1080 after crop).
    scale = 1920 / 4032
    # fx scales linearly. Rel 1e-3 tolerates the tiny fp drift from the
    # crop-then-scale path vs raw scale (AR identical post-crop).
    assert intr.fx == pytest.approx(2879.46 * scale, rel=1e-3)
    # cy was 1505.34 in 4032×3024; center-cropped to 2268 tall means
    # dy = (3024 - 2268) / 2 = 378; cy_cropped = 1127.34; scaled cy ≈ 536.8
    expected_cy = (1505.34 - (3024 - 3024 * (9 / 16) * (4032 / 4032)) / 2) * scale
    # Simpler: new_h = 4032 * 9/16 = 2268, dy = (3024 - 2268)/2 = 378.
    expected_cy = (1505.34 - 378) * (1080 / 2268)
    assert intr.cy == pytest.approx(expected_cy, rel=1e-3)
    # Distortion propagates verbatim
    assert intr.distortion == [0.19, -0.66, -0.002, 0.001, 0.67]


def test_auto_cal_falls_back_without_prior(tmp_path, monkeypatch):
    """No ChArUco record + no CalibrationSnapshot → FOV fallback with zero
    distortion. This is the pre-existing degraded mode."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    from calibration_auto import _derive_auto_cal_intrinsics

    intrinsics, _, source = _derive_auto_cal_intrinsics(
        "A", w_img=1920, h_img=1080, h_fov_deg=None,
    )
    assert source == "fov"
    # FOV path produces no distortion
    assert intrinsics.distortion is None


def test_hello_carries_device_identity(tmp_path, monkeypatch):
    """Verifies the WS parser consumes device_id / device_model from the
    hello envelope end-to-end."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    with preassign_and_open_ws(client, "A") as ws:
        ws.send_json({
            "type": "hello",
            "cam": "A",
            "device_id": _DEVICE_ID,
            "device_model": "iPhone15,3",
        })
        # Settings message is sent back on hello; consume it so the next
        # heartbeat doesn't race the test teardown.
        _ = ws.receive_json()

    assert main.state.device_id_for("A") == _DEVICE_ID
    snap = main.state.device_snapshot("A")
    assert snap is not None
    assert snap.device_model == "iPhone15,3"


def test_video_basis_intrinsics_fov_path_is_pure_pinhole():
    """When intrinsics_source != 'charuco' (or photo_fov_deg unknown),
    the rebuild path produces canonical FOV-only K. Correction factors
    are 1.0 so callers can log them uniformly."""
    from calibration_auto import _video_basis_intrinsics
    from schemas import IntrinsicsPayload

    photo_intr = IntrinsicsPayload(fx=1500.0, fy=1500.0, cx=2016.0, cy=1134.0)
    intr, fxc, fyc = _video_basis_intrinsics(
        photo_intrinsics=photo_intr,
        photo_dims=(4032, 2268),
        photo_fov_deg=73.828,
        video_fov_deg=73.828,
        intrinsics_source="fov",
    )
    assert fxc == 1.0
    assert fyc == 1.0
    # Pure FOV at 1920×1080 / 73.828° → fx ≈ 1278
    assert intr.fx == pytest.approx(1278.0, abs=2.0)
    assert intr.cx == pytest.approx(960.0, abs=0.5)
    assert intr.cy == pytest.approx(540.0, abs=0.5)


def test_video_basis_intrinsics_charuco_carries_correction():
    """ChArUco-derived photo-basis K → rebuild applies the per-device
    correction factor (ChArUco fx ÷ FOV-nominal fx at photo basis) to
    the video-basis FOV K. This is the B2 fix: previously the rebuild
    branch silently downgraded ChArUco to pure FOV approximation."""
    from calibration_auto import _video_basis_intrinsics
    from schemas import IntrinsicsPayload
    import numpy as np

    photo_w, photo_h = 4032, 2268
    photo_fov = 73.828
    # ChArUco measures fx 2 % above nominal FOV — typical per-device
    # deviation on iPhone main 1.0×.
    fov_fx_at_photo = (photo_w / 2.0) / np.tan(np.radians(photo_fov) / 2.0)
    charuco_fx = fov_fx_at_photo * 1.02
    charuco_fy = fov_fx_at_photo * 1.018
    photo_intr = IntrinsicsPayload(
        fx=charuco_fx, fy=charuco_fy,
        cx=2016.0, cy=1134.0,
        distortion=[0.19, -0.66, -0.002, 0.001, 0.67],
    )
    intr, fxc, fyc = _video_basis_intrinsics(
        photo_intrinsics=photo_intr,
        photo_dims=(photo_w, photo_h),
        photo_fov_deg=photo_fov,
        video_fov_deg=73.828,
        intrinsics_source="charuco",
    )
    # Correction factor recovered to ChArUco/nominal ratio.
    assert fxc == pytest.approx(1.02, rel=1e-6)
    assert fyc == pytest.approx(1.018, rel=1e-6)
    # Video-basis fx = nominal video fx × correction. Nominal video fx
    # at 1920×1080 / 73.828° ≈ 1278.0.
    nominal_video_fx = (1920 / 2.0) / np.tan(np.radians(73.828) / 2.0)
    assert intr.fx == pytest.approx(nominal_video_fx * 1.02, rel=1e-6)
    assert intr.fy == pytest.approx(nominal_video_fx * 1.018, rel=1e-3)
    # Distortion survives basis swap unchanged.
    assert intr.distortion == [0.19, -0.66, -0.002, 0.001, 0.67]


def test_video_basis_intrinsics_charuco_without_photo_fov_skips_correction():
    """No photo_fov_deg → cannot compute correction factor → fall back to
    pure FOV. Edge case: operator forced --h_fov_deg override AND
    intrinsics_source happens to be 'charuco' (shouldn't happen because
    h_fov_deg override forces 'fov' path, but defensive)."""
    from calibration_auto import _video_basis_intrinsics
    from schemas import IntrinsicsPayload

    photo_intr = IntrinsicsPayload(fx=2000.0, fy=2000.0, cx=2016.0, cy=1134.0)
    intr, fxc, fyc = _video_basis_intrinsics(
        photo_intrinsics=photo_intr,
        photo_dims=(4032, 2268),
        photo_fov_deg=None,
        video_fov_deg=73.828,
        intrinsics_source="charuco",
    )
    assert fxc == 1.0
    assert fyc == 1.0


# ---------------------------------------------------------------------------
# Algorithm parity — synthetic ChArUco projections → calibrateCamera must
# recover the known ground-truth K to <1px. Migrated from the deleted
# server/test_calibrate_intrinsics.py because the iOS Obj-C++ port in
# ball_tracker/CharucoCalibrator.mm uses the exact same OpenCV pipeline;
# this test locks the server-side reference numbers the iOS solver is
# verified against in CLAUDE.md plan Verification step 2.
# ---------------------------------------------------------------------------


def test_calibration_recovers_known_intrinsics_from_synthetic_projections():
    import cv2
    import numpy as np

    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    board = cv2.aruco.CharucoBoard((5, 7), 0.040, 0.030, aruco_dict)

    fx_true, fy_true, cx_true, cy_true = 1600.0, 1600.0, 960.0, 540.0
    K_true = np.array([[fx_true, 0, cx_true], [0, fy_true, cy_true], [0, 0, 1.0]])
    dist_true = np.zeros(5)
    img_size = (1920, 1080)

    corners_3d = board.getChessboardCorners().astype(np.float64)
    ids = np.arange(len(corners_3d), dtype=np.int32).reshape(-1, 1)

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

    all_obj_pts: list = []
    all_img_pts: list = []
    for rvec, tvec in poses:
        pts2d, _ = cv2.projectPoints(corners_3d, rvec, tvec, K_true, dist_true)
        corners_f32 = pts2d.reshape(-1, 1, 2).astype(np.float32)
        obj_pts, img_pts = board.matchImagePoints(corners_f32, ids.copy())
        all_obj_pts.append(obj_pts)
        all_img_pts.append(img_pts)

    rms, K_rec, _, _, _ = cv2.calibrateCamera(
        all_obj_pts, all_img_pts, img_size, None, None,
    )

    assert rms < 0.5, f"rms too high: {rms}"
    assert abs(K_rec[0, 0] - fx_true) < 1.0
    assert abs(K_rec[1, 1] - fy_true) < 1.0
    assert abs(K_rec[0, 2] - cx_true) < 1.0
    assert abs(K_rec[1, 2] - cy_true) < 1.0
