"""Device-keyed ChArUco intrinsics endpoints + auto-cal integration."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
from main import app


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
    from routes.calibration import _derive_auto_cal_intrinsics

    intrinsics, legacy_prior = _derive_auto_cal_intrinsics(
        "A", w_img=1920, h_img=1080, h_fov_deg=None,
    )
    assert legacy_prior is None  # not the legacy CalibrationSnapshot path
    assert intrinsics.fx == pytest.approx(1580.5)
    # Distortion propagated verbatim
    assert intrinsics.distortion == [0.12, -0.25, 0.001, -0.001, 0.08]

    # Same device, different target dims (720p): linear scale applies.
    intrinsics_720, _ = _derive_auto_cal_intrinsics(
        "A", w_img=1280, h_img=720, h_fov_deg=None,
    )
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

    from routes.calibration import _derive_auto_cal_intrinsics
    intr, _ = _derive_auto_cal_intrinsics("A", w_img=1920, h_img=1080, h_fov_deg=None)

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
    from routes.calibration import _derive_auto_cal_intrinsics

    intrinsics, _ = _derive_auto_cal_intrinsics(
        "A", w_img=1920, h_img=1080, h_fov_deg=None,
    )
    # FOV path produces no distortion
    assert intrinsics.distortion is None


def test_hello_carries_device_identity(tmp_path, monkeypatch):
    """Verifies the WS parser consumes device_id / device_model from the
    hello envelope end-to-end."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(app)

    with client.websocket_connect("/ws/device/A") as ws:
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
