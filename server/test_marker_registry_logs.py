"""Regression guard: silent-skip → logged-skip on malformed marker rows.

Background — `MarkerRegistryDB._load` originally swallowed malformed
rows in the JSON store with `except Exception: continue`, leaving no
trace when an operator's hand-edited file silently lost entries. W5
upgrades that swallow into a `logger.warning(...)` so the operator
sees the skip in stdout.

Until W5 merges, this test fails on the missing log record (by design
— it's the regression guard). After W5 merges (consolidate stage), it
passes and pins the new contract: malformed rows MUST be logged, not
silently dropped.
"""
from __future__ import annotations

import json
import logging

from marker_registry import MarkerRegistryDB


def test_load_logs_warning_on_malformed_row(tmp_path, caplog):
    """Mix one malformed row (missing required `x_m`) with one good
    row. Loader must emit exactly one WARNING for the bad row and
    still load the good one.
    """
    payload = {
        "markers": [
            # Malformed: missing `x_m` (required by MarkerRecord schema).
            {"marker_id": 12, "y_m": 0.5, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
            # Valid row.
            {"marker_id": 13, "x_m": 1.0, "y_m": 2.0, "z_m": 0.0,
             "on_plate_plane": True, "source_camera_ids": []},
        ]
    }
    (tmp_path / "markers.json").write_text(json.dumps(payload))

    with caplog.at_level(logging.WARNING, logger="marker_registry"):
        registry = MarkerRegistryDB(data_dir=tmp_path)

    # Exactly one record loaded (the valid one).
    records = registry.all_records()
    assert len(records) == 1, (
        f"expected 1 valid record loaded, got {len(records)}: {records!r}"
    )
    assert records[0].marker_id == 13

    # Exactly one WARNING-level record about the skipped malformed row.
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "skipped malformed row" in r.getMessage().lower()
    ]
    assert len(warnings) == 1, (
        "expected exactly one WARNING containing 'skipped malformed row' "
        f"after loading a registry with one bad + one good row, got "
        f"{len(warnings)} matching record(s); all warnings: "
        f"{[r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]}"
    )
