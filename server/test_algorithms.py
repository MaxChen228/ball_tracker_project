"""Algorithm registry contract: `run_detection` is the single entry
point for every detection callsite, validates `algorithm_id` and the
`params` dict shape, and dispatches to the registered Detector.

V11 is wrapped behind the same Detector ABC that V12 / V13 will use,
so the dispatch path is exercised end-to-end here even though only one
algorithm is currently registered.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import algorithms
from algorithms.base import Detector
from algorithms.v11_hsv_cc import V11Detector, V11Params
from schemas import HSVRangePayload, ShapeGatePayload


def _v11_params_dict() -> dict:
    return {
        "hsv": HSVRangePayload(
            h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        ),
        "shape_gate": ShapeGatePayload(aspect_min=0.7, fill_min=0.55),
    }


def test_v11_detector_registered_under_default_id():
    entry = algorithms.get(algorithms.DEFAULT_ALGORITHM_ID)
    assert entry.algorithm_id == algorithms.V11_HSV_CC
    assert isinstance(entry.detector, V11Detector)
    assert entry.detector.params_schema is V11Params


def test_v11_detector_subclass_of_detector_abc():
    """V11Detector implements the Detector contract — guards against
    accidental signature drift if someone refactors the ABC."""
    assert issubclass(V11Detector, Detector)


def test_v11_params_rejects_unknown_field():
    """V11Params has `extra='forbid'`; misspelled keys (e.g. operator
    types `hsl` instead of `hsv`) fail loudly rather than silently
    drop."""
    with pytest.raises(ValidationError):
        V11Params.model_validate({**_v11_params_dict(), "hsl": {}})


def test_run_detection_unknown_algorithm_id_raises_before_video_open(tmp_path):
    """Bad algorithm_id at the system boundary fails fast — no video
    decode attempted. Caller (HTTP route, CLI) translates ValueError
    to 400 / SystemExit."""
    fake_video = tmp_path / "nonexistent.mov"
    with pytest.raises(ValueError, match="v999_not_registered"):
        algorithms.run_detection(
            "v999_not_registered",
            fake_video,
            0.0,
            _v11_params_dict(),
        )


def test_run_detection_invalid_params_raises_before_video_open(tmp_path):
    """Bad params dict (missing required field) raises ValidationError
    before the detector runs — prevents partial work / corrupt output."""
    fake_video = tmp_path / "nonexistent.mov"
    bad_params = {"hsv": HSVRangePayload(
        h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
    )}  # missing shape_gate
    with pytest.raises(ValidationError):
        algorithms.run_detection(
            algorithms.V11_HSV_CC,
            fake_video,
            0.0,
            bad_params,
        )


def test_run_detection_dispatches_to_registered_detector(tmp_path, monkeypatch):
    """End-to-end dispatch: run_detection → entry.detector.detect with
    typed params materialised from the dict. Uses a stub iter_frames
    so no real video is required."""
    captured: dict = {}

    def fake_detect(self, video_path, video_start_pts_s, params, **kwargs):
        captured["video_path"] = video_path
        captured["video_start_pts_s"] = video_start_pts_s
        captured["params"] = params
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(V11Detector, "detect", fake_detect)
    fake_video = tmp_path / "fake.mov"

    algorithms.run_detection(
        algorithms.V11_HSV_CC,
        fake_video,
        1.5,
        _v11_params_dict(),
    )

    assert captured["video_path"] == fake_video
    assert captured["video_start_pts_s"] == 1.5
    assert isinstance(captured["params"], V11Params)
    assert captured["params"].hsv.h_min == 10


def test_v11_detector_translates_payload_to_legacy_types(tmp_path, monkeypatch):
    """V11Detector bridges the wire shape (HSVRangePayload / ShapeGatePayload)
    to the legacy `detect_pitch(hsv_range=HSVRange, shape_gate=ShapeGate)`
    signature. Verify both args reach detect_pitch with the right values."""
    captured: dict = {}

    def fake_detect_pitch(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["args"] = args
        return []

    import pipeline
    monkeypatch.setattr(pipeline, "detect_pitch", fake_detect_pitch)

    detector = V11Detector()
    params = V11Params.model_validate(_v11_params_dict())
    detector.detect(tmp_path / "fake.mov", 0.0, params)

    kw = captured["kwargs"]
    assert kw["hsv_range"].h_min == 10
    assert kw["hsv_range"].v_max == 60
    assert kw["shape_gate"].aspect_min == pytest.approx(0.7)
    assert kw["shape_gate"].fill_min == pytest.approx(0.55)
