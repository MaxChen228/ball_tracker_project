"""Regression guard: corrupt / malformed markers.json raises on load.

Previous behaviour (W5 era): malformed rows were silently skipped with a
logger.warning. Per CLAUDE.md no-silent-fallback rule, the loader now raises
so operators see a hard error instead of a silent partial load.
"""
from __future__ import annotations

import json
import pytest

from marker_registry import MarkerRegistryDB


def test_load_raises_on_malformed_row(tmp_path):
    """A single malformed row (missing required `x_m`) must raise ValueError,
    not silently skip the row and load the rest."""
    payload = {
        "markers": [
            # Malformed: missing `x_m` (required by MarkerRecord schema).
            {"marker_id": 12, "y_m": 0.5, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
            # Valid row — must NOT be loaded because load aborts on first bad row.
            {"marker_id": 13, "x_m": 1.0, "y_m": 2.0, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
        ]
    }
    (tmp_path / "markers.json").write_text(json.dumps(payload))

    with pytest.raises(Exception):
        MarkerRegistryDB(data_dir=tmp_path)


def test_load_raises_on_corrupt_json(tmp_path):
    """Corrupt JSON (not parseable) must raise ValueError, not start empty."""
    (tmp_path / "markers.json").write_text("{not valid json")

    with pytest.raises(ValueError, match="not valid JSON"):
        MarkerRegistryDB(data_dir=tmp_path)


def test_load_missing_file_is_fine(tmp_path):
    """No markers.json → empty registry, no error."""
    registry = MarkerRegistryDB(data_dir=tmp_path)
    assert registry.all_records() == []


def test_load_valid_markers(tmp_path):
    """Valid markers.json loads all rows correctly."""
    payload = {
        "markers": [
            {"marker_id": 9, "x_m": 1.0, "y_m": 2.0, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
            {"marker_id": 10, "x_m": -1.0, "y_m": 0.5, "z_m": 0.4,
             "on_plate_plane": False, "source_camera_ids": ["A"]},
        ]
    }
    (tmp_path / "markers.json").write_text(json.dumps(payload))

    registry = MarkerRegistryDB(data_dir=tmp_path)
    records = registry.all_records()
    assert len(records) == 2
    assert records[0].marker_id == 9
    assert records[1].marker_id == 10


def test_load_no_longer_loads_legacy_extended_markers(tmp_path):
    """extended_markers.json is the old back-compat path. Per no-backcompat
    rule it must NOT be picked up — only markers.json is canonical."""
    legacy_payload = {
        "markers": [
            {"marker_id": 99, "x_m": 1.0, "y_m": 2.0, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
        ]
    }
    # Write to legacy path only — no markers.json.
    (tmp_path / "extended_markers.json").write_text(json.dumps(legacy_payload))

    registry = MarkerRegistryDB(data_dir=tmp_path)
    # Must be empty — legacy file is ignored.
    assert registry.all_records() == []
