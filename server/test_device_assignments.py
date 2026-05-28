"""Tests for `state_device_assignments.DeviceAssignmentStore`.

Covers: persist/reload round-trip, cam-id collision rejection, re-assign
release-and-replace, format validation, atomic rollback on write failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from state_device_assignments import (
    AssignmentError,
    DeviceAssignment,
    DeviceAssignmentStore,
)


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def _make_store(tmp_path: Path, *, time_value: float = 1000.0) -> DeviceAssignmentStore:
    return DeviceAssignmentStore(
        tmp_path / "device_assignments.json",
        atomic_write=_atomic_write,
        time_fn=lambda: time_value,
    )


def test_empty_store_returns_no_entries(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.snapshot() == []
    assert store.get_by_camera("A") is None
    assert store.get_by_device("uuid-1") is None


def test_assign_persists_and_reloads(tmp_path: Path) -> None:
    store = _make_store(tmp_path, time_value=1234.5)
    rec = store.assign(
        device_uuid="uuid-1",
        camera_id="A",
        device_model="iPhone15,3",
    )
    assert rec.device_uuid == "uuid-1"
    assert rec.camera_id == "A"
    assert rec.assigned_at == 1234.5

    # Reload from disk into a fresh store — must round-trip identically.
    reloaded = _make_store(tmp_path)
    snap = reloaded.snapshot()
    assert len(snap) == 1
    assert snap[0] == rec


def test_camera_id_collision_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")
    with pytest.raises(AssignmentError, match="already assigned"):
        store.assign(device_uuid="uuid-2", camera_id="A")


def test_reassign_same_device_to_new_camera_releases_old(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")
    # Same device → new cam. Old cam_id A must be released atomically so
    # a follow-up assign(uuid-2, A) succeeds.
    store.assign(device_uuid="uuid-1", camera_id="B")
    assert store.get_by_camera("A") is None
    assert store.get_by_camera("B").device_uuid == "uuid-1"
    # Now uuid-2 can grab A.
    store.assign(device_uuid="uuid-2", camera_id="A")
    assert store.get_by_camera("A").device_uuid == "uuid-2"


def test_unassign_by_camera(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")
    assert store.unassign_by_camera("A") is True
    assert store.snapshot() == []
    # Idempotent: second call returns False.
    assert store.unassign_by_camera("A") is False
    # Reload confirms persistence of the empty state.
    reloaded = _make_store(tmp_path)
    assert reloaded.snapshot() == []


def test_unassign_by_device(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")
    assert store.unassign_by_device("uuid-1") is True
    assert store.unassign_by_device("uuid-1") is False


def test_invalid_camera_id_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    for bad in ["", "too-long-camera-id-name-here", "A B", "a/b", "💥"]:
        with pytest.raises(AssignmentError, match="fails regex"):
            store.assign(device_uuid="uuid-1", camera_id=bad)


def test_invalid_device_uuid_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(AssignmentError):
        store.assign(device_uuid="", camera_id="A")
    with pytest.raises(AssignmentError):
        store.assign(device_uuid="x" * 100, camera_id="A")


def test_device_model_length_capped(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(AssignmentError):
        store.assign(
            device_uuid="uuid-1", camera_id="A", device_model="x" * 100
        )


def test_load_rejects_duplicate_camera_id_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "device_assignments.json"
    payload = {
        "assignments": [
            {"device_uuid": "u1", "camera_id": "A", "device_model": None, "assigned_at": 1.0},
            {"device_uuid": "u2", "camera_id": "A", "device_model": None, "assigned_at": 2.0},
        ]
    }
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate camera_id"):
        DeviceAssignmentStore(path, atomic_write=_atomic_write)


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "device_assignments.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        DeviceAssignmentStore(path, atomic_write=_atomic_write)


def test_write_failure_rolls_back_in_memory(tmp_path: Path) -> None:
    """If atomic_write raises OSError, in-memory state must be rolled
    back to the prior snapshot. Otherwise reload would silently revert
    a value caller thought succeeded — exactly the silent-fallback hazard
    CLAUDE.md prohibits."""
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")

    def failing_write(_path: Path, _payload: str) -> None:
        raise OSError("disk full")

    store._atomic_write = failing_write  # noqa: SLF001 — test injects fault

    with pytest.raises(OSError):
        store.assign(device_uuid="uuid-2", camera_id="B")
    # uuid-2 must not be present in memory.
    assert store.get_by_device("uuid-2") is None
    # A still maps to uuid-1.
    assert store.get_by_camera("A").device_uuid == "uuid-1"


def test_reassign_same_device_failed_write_rollback(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="uuid-1", camera_id="A")

    def failing_write(_path: Path, _payload: str) -> None:
        raise OSError("disk full")

    store._atomic_write = failing_write  # noqa: SLF001

    with pytest.raises(OSError):
        store.assign(device_uuid="uuid-1", camera_id="B")
    # uuid-1 must still be on A (prior state restored).
    assert store.get_by_device("uuid-1").camera_id == "A"


def test_snapshot_sorted_by_camera_id(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="u1", camera_id="C")
    store.assign(device_uuid="u2", camera_id="A")
    store.assign(device_uuid="u3", camera_id="B")
    cams = [rec.camera_id for rec in store.snapshot()]
    assert cams == ["A", "B", "C"]


def test_clear_persists_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.assign(device_uuid="u1", camera_id="A")
    store.clear()
    assert store.snapshot() == []
    reloaded = _make_store(tmp_path)
    assert reloaded.snapshot() == []
