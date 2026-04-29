"""Reprocess-freeze contract: the offline `reprocess_sessions.py` script
MUST honour the per-pitch frozen detection-config snapshot
(`PitchPayload.{hsv_range_used, shape_gate_used,
candidate_selector_tuning_used}`) and only fall back to current disk
config on legacy pitches lacking the stamp — except when
`--use-current-config` is passed, which forces the current values.

Without this contract, an operator dragging the dashboard sliders on
2026-04-29 would retroactively change the cost basis of a session
recorded on 2026-03-15 the next time anyone reprocessed it. That breaks
the "frozen at detection time" invariant the cd87995 PairingTuning fix
established for triangulation tuning.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate
from schemas import (
    CandidateSelectorTuningPayload,
    HSVRangePayload,
    PitchPayload,
    ShapeGatePayload,
)


def _make_pitch(
    *,
    hsv_used: HSVRangePayload | None,
    gate_used: ShapeGatePayload | None,
    tuning_used: CandidateSelectorTuningPayload | None,
) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id="s_abcd1234",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        hsv_range_used=hsv_used,
        shape_gate_used=gate_used,
        candidate_selector_tuning_used=tuning_used,
    )


def _write_pitch(path: Path, pitch: PitchPayload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pitch.model_dump_json())


def _capture_detect_pitch_args():
    """Return (mock, captured_kwargs_list) — patches reprocess.detect_pitch
    with a stub that records every call's kwargs and returns []."""
    captured: list[dict] = []

    def fake_detect_pitch(*args, **kwargs):
        captured.append(kwargs)
        return []

    return fake_detect_pitch, captured


def test_rerun_detection_uses_frozen_snapshot_over_current_disk(tmp_path, monkeypatch):
    """The discriminating test: pitch was originally detected under
    HSV/gate/tuning X; current disk holds Y. Reprocess MUST call
    detect_pitch with X, not Y."""
    import reprocess_sessions as R

    # Pitch frozen under X
    frozen_hsv = HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60)
    frozen_gate = ShapeGatePayload(aspect_min=0.61, fill_min=0.62)
    frozen_tuning = CandidateSelectorTuningPayload(w_aspect=0.71, w_fill=0.29)
    pitch = _make_pitch(hsv_used=frozen_hsv, gate_used=frozen_gate, tuning_used=frozen_tuning)
    pitch_path = tmp_path / "pitches" / "session_s_abcd1234_A.json"
    _write_pitch(pitch_path, pitch)

    # Stub video discovery + detect_pitch
    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    # "Current disk" config — Y. Different from X.
    current_hsv = HSVRange(h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150)
    current_gate = ShapeGate(aspect_min=0.99, fill_min=0.99)
    current_tuning = CandidateSelectorTuning(w_aspect=0.01, w_fill=0.99)

    R.rerun_detection(
        pitch_path, current_hsv, current_gate, current_tuning,
        dry_run=True,
    )

    assert len(captured) == 1
    kw = captured[0]
    # Frozen X must win, not current Y.
    assert kw["hsv_range"].h_min == 10 and kw["hsv_range"].h_max == 20
    assert kw["hsv_range"].s_min == 30 and kw["hsv_range"].v_max == 60
    assert kw["shape_gate"].aspect_min == pytest.approx(0.61)
    assert kw["shape_gate"].fill_min == pytest.approx(0.62)
    assert kw["selector_tuning"].w_aspect == pytest.approx(0.71)
    assert kw["selector_tuning"].w_fill == pytest.approx(0.29)


def test_rerun_detection_legacy_pitch_falls_back_to_current_with_warning(
    tmp_path, monkeypatch, caplog,
):
    """Legacy pitch (no `*_used` stamps) MUST fall back to disk values
    AND log a warning on each missing field."""
    import logging

    import reprocess_sessions as R

    pitch = _make_pitch(hsv_used=None, gate_used=None, tuning_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_legacy01_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current_hsv = HSVRange(h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150)
    current_gate = ShapeGate(aspect_min=0.99, fill_min=0.99)
    current_tuning = CandidateSelectorTuning(w_aspect=0.01, w_fill=0.99)

    with caplog.at_level(logging.WARNING, logger="reprocess"):
        R.rerun_detection(
            pitch_path, current_hsv, current_gate, current_tuning,
            dry_run=True,
        )

    kw = captured[0]
    # Fell back to current disk — Y wins.
    assert kw["hsv_range"].h_min == 100
    assert kw["shape_gate"].fill_min == pytest.approx(0.99)
    assert kw["selector_tuning"].w_aspect == pytest.approx(0.01)
    # All three legacy-fallback warnings fired.
    msgs = " ".join(r.message for r in caplog.records)
    assert "lacks hsv_range_used" in msgs
    assert "lacks shape_gate_used" in msgs
    assert "lacks candidate_selector_tuning_used" in msgs


def test_rerun_detection_use_current_config_overrides_freeze(tmp_path, monkeypatch):
    """`--use-current-config=True` MUST bypass the frozen snapshot even
    when present, and call detect_pitch with the disk values."""
    import reprocess_sessions as R

    frozen_hsv = HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60)
    frozen_gate = ShapeGatePayload(aspect_min=0.61, fill_min=0.62)
    frozen_tuning = CandidateSelectorTuningPayload(w_aspect=0.71, w_fill=0.29)
    pitch = _make_pitch(hsv_used=frozen_hsv, gate_used=frozen_gate, tuning_used=frozen_tuning)
    pitch_path = tmp_path / "pitches" / "session_s_abcd1234_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current_hsv = HSVRange(h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150)
    current_gate = ShapeGate(aspect_min=0.99, fill_min=0.99)
    current_tuning = CandidateSelectorTuning(w_aspect=0.01, w_fill=0.99)

    R.rerun_detection(
        pitch_path, current_hsv, current_gate, current_tuning,
        dry_run=True,
        use_current_config=True,
    )

    kw = captured[0]
    # Forced override — disk Y wins, not frozen X.
    assert kw["hsv_range"].h_min == 100 and kw["hsv_range"].h_max == 110
    assert kw["shape_gate"].aspect_min == pytest.approx(0.99)
    assert kw["selector_tuning"].w_aspect == pytest.approx(0.01)


def test_rerun_detection_stamps_used_values_back_on_legacy(tmp_path, monkeypatch):
    """After reprocessing a legacy pitch, the freshly-used values are
    stamped back so the next reprocess can honour the freeze."""
    import reprocess_sessions as R

    pitch = _make_pitch(hsv_used=None, gate_used=None, tuning_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_legacy02_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, _ = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current_hsv = HSVRange(h_min=42, h_max=43, s_min=44, s_max=45, v_min=46, v_max=47)
    current_gate = ShapeGate(aspect_min=0.55, fill_min=0.66)
    current_tuning = CandidateSelectorTuning(w_aspect=0.5, w_fill=0.5)

    R.rerun_detection(
        pitch_path, current_hsv, current_gate, current_tuning,
        dry_run=False,  # write back so we can re-read
    )

    # Re-read and verify the freeze stamp landed
    written = PitchPayload.model_validate_json(pitch_path.read_text())
    assert written.hsv_range_used is not None
    assert written.hsv_range_used.h_min == 42
    assert written.shape_gate_used is not None
    assert written.shape_gate_used.aspect_min == pytest.approx(0.55)
    assert written.candidate_selector_tuning_used is not None
    assert written.candidate_selector_tuning_used.w_aspect == pytest.approx(0.5)
