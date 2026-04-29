"""Reprocess two-mode contract: the offline `reprocess_sessions.py` script
defaults to **current** `data/*.json` so the operator's tuning workflow
("I tweaked HSV, rerun this session, see if it improves") works as
expected. Opt-in `--use-frozen-snapshot` reuses the per-pitch frozen
detection-config snapshot (`PitchPayload.{hsv_range_used, shape_gate_used,
candidate_selector_tuning_used}`) for reproducibility audits — falling
back to disk for legacy pitches that pre-date the stamp, with a logged
warning per missing field.

The frozen stamps themselves are always preserved on disk (re-stamped at
the end of every detection run with whatever config was actually used),
so the "what was X originally detected with" question stays answerable
even when default reprocess overwrites the run.
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


def test_rerun_detection_default_uses_current_disk_config(tmp_path, monkeypatch):
    """The discriminating test for the operator's tuning workflow:
    pitch was originally detected under HSV/gate/tuning X; operator
    edited disk to Y; default reprocess MUST call detect_pitch with Y
    (so the tweak shows up in the rerun), not X."""
    import reprocess_sessions as R

    # Pitch was originally frozen under X
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
    # Default = current disk Y wins, not frozen X.
    assert kw["hsv_range"].h_min == 100 and kw["hsv_range"].h_max == 110
    assert kw["shape_gate"].aspect_min == pytest.approx(0.99)
    assert kw["shape_gate"].fill_min == pytest.approx(0.99)
    assert kw["selector_tuning"].w_aspect == pytest.approx(0.01)
    assert kw["selector_tuning"].w_fill == pytest.approx(0.99)


def test_rerun_detection_use_frozen_snapshot_replays_original(tmp_path, monkeypatch):
    """For the reproducibility-audit case: pitch frozen under X, disk
    holds Y, `use_frozen_snapshot=True` MUST replay X (ignore disk)."""
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
        use_frozen_snapshot=True,
    )

    kw = captured[0]
    # Frozen X wins, not current Y.
    assert kw["hsv_range"].h_min == 10 and kw["hsv_range"].h_max == 20
    assert kw["hsv_range"].s_min == 30 and kw["hsv_range"].v_max == 60
    assert kw["shape_gate"].aspect_min == pytest.approx(0.61)
    assert kw["shape_gate"].fill_min == pytest.approx(0.62)
    assert kw["selector_tuning"].w_aspect == pytest.approx(0.71)
    assert kw["selector_tuning"].w_fill == pytest.approx(0.29)


def test_rerun_detection_use_frozen_snapshot_legacy_falls_back_with_warning(
    tmp_path, monkeypatch, caplog,
):
    """When `use_frozen_snapshot=True` but pitch lacks `*_used` stamps
    (legacy from before the freeze landed), MUST fall back to disk
    values AND log a warning on each missing field so the operator
    knows the rerun isn't an unambiguous historical reproduction."""
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
            use_frozen_snapshot=True,
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
