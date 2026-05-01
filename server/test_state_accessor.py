"""Regression coverage for the public State accessor surface added in
PR-A (W8). These exist so refactors that rename internals (`_sync`,
`_preview`, `_marker_registry`, `_runtime_settings.default_paths`,
`_lookup_session_locked`) keep the public surface contract — routes/* and
detection_paths.py talk to State only via these.
"""
from __future__ import annotations

from marker_registry import MarkerRegistryDB
from preview import PreviewBuffer
from schemas import DetectionPath
from state import State
from state_sync import SyncCoordinator


def test_sync_property_returns_coordinator(tmp_path):
    state = State(data_dir=tmp_path)
    assert isinstance(state.sync, SyncCoordinator)
    # Stable: same reference each call.
    assert state.sync is state.sync


def test_preview_property_returns_buffer(tmp_path):
    state = State(data_dir=tmp_path)
    assert isinstance(state.preview, PreviewBuffer)
    assert state.preview is state.preview


def test_markers_property_returns_registry(tmp_path):
    state = State(data_dir=tmp_path)
    assert isinstance(state.markers, MarkerRegistryDB)
    assert state.markers is state.markers


def test_calibration_path_per_camera(tmp_path):
    state = State(data_dir=tmp_path)
    pa = state.calibration_path("A")
    pb = state.calibration_path("B")
    assert pa != pb
    assert pa.name.startswith("A") or "A" in pa.name
    assert pb.name.startswith("B") or "B" in pb.name


def test_pitch_path_per_session_camera(tmp_path):
    state = State(data_dir=tmp_path)
    p = state.pitch_path("A", "s_deadbeef")
    assert "s_deadbeef" in p.name and "A" in p.name and p.suffix == ".json"


def test_session_paths_for_unknown_returns_none(tmp_path):
    state = State(data_dir=tmp_path)
    assert state.session_paths_for("s_nonexistent") is None


def test_default_detection_paths_nonempty(tmp_path):
    state = State(data_dir=tmp_path)
    paths = state.default_detection_paths()
    assert isinstance(paths, set)
    assert paths  # operator default is non-empty (live at minimum)
    for p in paths:
        assert isinstance(p, DetectionPath)
    # Returned set is a copy — caller mutation must not corrupt state.
    paths.clear()
    assert state.default_detection_paths()
