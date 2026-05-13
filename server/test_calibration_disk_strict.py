"""Disk-strict regression tests for CalibrationStore + DeviceIntrinsicsStore.

Covers:
- CalibrationStore.load raises on corrupt JSON
- CalibrationStore.set: write-before-mutate (disk fail leaves cache clean)
- DeviceIntrinsicsStore.load raises on corrupt JSON
- DeviceIntrinsicsStore.delete raises on unlink OSError (not silent warn)

Note: tmp_path already has `calibrations/` and `intrinsics/` sub-dirs
created by the autouse _reset_main_state fixture (via State.__init__).
Tests pass directories directly and use exist_ok=True when calling mkdir.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from schemas import CalibrationSnapshot, DeviceIntrinsics, IntrinsicsPayload
from state_calibration import CalibrationStore, DeviceIntrinsicsStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_snapshot(camera_id: str = "A") -> CalibrationSnapshot:
    return CalibrationSnapshot(
        camera_id=camera_id,
        intrinsics=IntrinsicsPayload(fx=1278.0, fy=1278.0, cx=960.0, cy=540.0),
        homography=[800.0, 0.0, 960.0, 0.0, 800.0, 540.0, 0.0, 0.0, 1.0],
        image_width_px=1920,
        image_height_px=1080,
    )


def _valid_intrinsics(device_id: str = "dev-abc123") -> DeviceIntrinsics:
    return DeviceIntrinsics(
        device_id=device_id,
        source_width_px=1920,
        source_height_px=1080,
        intrinsics=IntrinsicsPayload(fx=1278.0, fy=1278.0, cx=960.0, cy=540.0),
    )


def _noop_atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _failing_atomic_write(path: Path, content: str) -> None:
    raise OSError("simulated disk full")


# ---------------------------------------------------------------------------
# CalibrationStore.load — strict
# ---------------------------------------------------------------------------

def test_calibration_store_load_corrupt_json_raises(tmp_path):
    """Corrupt JSON in calibrations/ must raise, not silently skip."""
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir(exist_ok=True)
    (cal_dir / "A.json").write_text("{this is not valid json")

    with pytest.raises(ValueError, match="not valid JSON"):
        CalibrationStore(cal_dir, atomic_write=_noop_atomic_write)


def test_calibration_store_load_invalid_schema_raises(tmp_path):
    """JSON that fails CalibrationSnapshot.model_validate must raise."""
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir(exist_ok=True)
    # Missing required fields
    (cal_dir / "A.json").write_text(json.dumps({"camera_id": "A"}))

    with pytest.raises(ValueError, match="CalibrationSnapshot validation"):
        CalibrationStore(cal_dir, atomic_write=_noop_atomic_write)


def test_calibration_store_load_valid_snapshots(tmp_path):
    """Valid calibration JSONs load without error."""
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir(exist_ok=True)
    snap = _valid_snapshot("A")
    (cal_dir / "A.json").write_text(snap.model_dump_json())

    store = CalibrationStore(cal_dir, atomic_write=_noop_atomic_write)
    assert store.get("A") is not None
    assert store.get("A").camera_id == "A"


# ---------------------------------------------------------------------------
# CalibrationStore.set — write-before-mutate
# ---------------------------------------------------------------------------

def test_calibration_store_set_atomic_on_disk_fail(tmp_path):
    """If atomic_write raises, in-memory cache must NOT be updated."""
    cal_dir = tmp_path / "calibrations"
    cal_dir.mkdir(exist_ok=True)

    store = CalibrationStore(cal_dir, atomic_write=_noop_atomic_write)
    assert store.get("A") is None

    store._atomic_write = _failing_atomic_write
    snap = _valid_snapshot("A")

    with pytest.raises(OSError, match="simulated disk full"):
        store.set(snap)

    # Cache must still be empty — disk fail must not dirty in-memory state.
    assert store.get("A") is None, (
        "CalibrationStore.set must not update in-memory cache when disk write fails"
    )


# ---------------------------------------------------------------------------
# DeviceIntrinsicsStore.load — strict
# ---------------------------------------------------------------------------

def test_device_intrinsics_store_load_corrupt_json_raises(tmp_path):
    """Corrupt JSON in intrinsics/ must raise, not silently skip."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)
    (intr_dir / "dev-abc123.json").write_text("not json at all!!!")

    with pytest.raises(ValueError, match="not valid JSON"):
        DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)


def test_device_intrinsics_store_load_invalid_schema_raises(tmp_path):
    """JSON that fails DeviceIntrinsics.model_validate must raise."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)
    # Missing required source_width_px / source_height_px / intrinsics
    (intr_dir / "dev-abc123.json").write_text(json.dumps({"device_id": "dev-abc123"}))

    with pytest.raises(ValueError, match="DeviceIntrinsics validation"):
        DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)


def test_device_intrinsics_store_load_valid(tmp_path):
    """Valid intrinsics JSON loads without error."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)
    rec = _valid_intrinsics("dev-abc123")
    (intr_dir / "dev-abc123.json").write_text(rec.model_dump_json())

    store = DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)
    assert store.get("dev-abc123") is not None


# ---------------------------------------------------------------------------
# DeviceIntrinsicsStore.delete — raises on unlink OSError
# ---------------------------------------------------------------------------

def test_device_intrinsics_store_delete_raises_on_unlink_fail(tmp_path):
    """OSError from unlink must propagate — caller must not think delete
    succeeded when the file is still on disk."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)

    store = DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)
    rec = _valid_intrinsics("dev-abc123")
    store.set(rec)
    assert store.get("dev-abc123") is not None

    with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        with pytest.raises(OSError, match="permission denied"):
            store.delete("dev-abc123")

    # In-memory cache must still contain the record — delete was not committed.
    assert store.get("dev-abc123") is not None, (
        "DeviceIntrinsicsStore.delete must not remove from cache when disk unlink fails"
    )


def test_device_intrinsics_store_delete_nonexistent_returns_false(tmp_path):
    """Deleting a device_id not in the store must return False, no error."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)
    store = DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)

    result = store.delete("nonexistent-device")
    assert result is False


# ---------------------------------------------------------------------------
# DeviceIntrinsicsStore.set — write-before-mutate
# ---------------------------------------------------------------------------

def test_device_intrinsics_store_set_atomic_on_disk_fail(tmp_path):
    """If atomic_write raises, in-memory cache must NOT be updated."""
    intr_dir = tmp_path / "intrinsics"
    intr_dir.mkdir(exist_ok=True)

    store = DeviceIntrinsicsStore(intr_dir, atomic_write=_noop_atomic_write)
    assert store.get("dev-abc123") is None

    store._atomic_write = _failing_atomic_write
    rec = _valid_intrinsics("dev-abc123")

    with pytest.raises(OSError, match="simulated disk full"):
        store.set(rec)

    assert store.get("dev-abc123") is None, (
        "DeviceIntrinsicsStore.set must not update in-memory cache when disk write fails"
    )
