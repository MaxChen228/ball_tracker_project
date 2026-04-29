"""Tests for the unified detection-config bundle introduced in phase 2
of the unified-config redesign:

- Boot migration from legacy three files (`hsv_range.json`,
  `shape_gate.json`, `candidate_selector_tuning.json`) into the new
  `detection_config.json`.
- Atomic write semantics — `set_detection_config` updates all three
  sub-knobs in one persistence step.
- New `GET /detection/config` view + `POST /detection/config` round
  trip with preset identity validation.
- Legacy per-section endpoints continue to work and explicitly clear
  `preset` to None (editing one knob means leaving the named preset).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Replace `main.state` with a fresh `State` rooted at tmp_path.
    Mirrors the pattern used in test_calibration_markers.py — the State
    constructor reads the data dir directly so we don't have to fight
    `_DEFAULT_DATA_DIR` being captured at module-import time."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    return main


def test_boot_with_no_files_lands_on_tennis_default(tmp_path, monkeypatch):
    """Fresh install path: no legacy and no new file → DetectionConfig
    bound to the Tennis preset (the canonical zero state — every sub-
    default is bound to Tennis values via the preset registry, so this
    is internally self-consistent)."""
    main = _fresh_main(tmp_path, monkeypatch)
    cfg = main.state.detection_config()
    assert cfg.preset == "tennis"
    from presets import PRESETS
    tennis = PRESETS["tennis"]
    assert cfg.hsv == tennis.hsv
    assert cfg.shape_gate == tennis.shape_gate
    assert cfg.selector == tennis.selector
    # Default config is in-memory only — no disk write yet.
    assert not (tmp_path / "detection_config.json").exists()


def test_boot_migrates_legacy_three_files_into_unified(tmp_path, monkeypatch):
    """If the operator boots after upgrading from a server version that
    used the three-file layout, the three files MUST be merged into
    `detection_config.json` and then deleted on the same boot. This is
    the one-shot system-boundary translation — there is no fallback
    runtime read of the legacy paths after this."""
    (tmp_path / "hsv_range.json").write_text(json.dumps({
        "h_min": 105, "h_max": 112, "s_min": 140, "s_max": 255,
        "v_min": 40, "v_max": 255,
    }))
    (tmp_path / "shape_gate.json").write_text(json.dumps({
        "aspect_min": 0.56, "fill_min": 0.45,
    }))
    (tmp_path / "candidate_selector_tuning.json").write_text(json.dumps({
        "w_aspect": 0.7, "w_fill": 0.3,
    }))

    main = _fresh_main(tmp_path, monkeypatch)
    cfg = main.state.detection_config()
    # Custom config — preset is None on migration (operator may have
    # hand-edited any of the three files; we don't risk claiming
    # identity with a preset they never selected).
    assert cfg.preset is None
    assert cfg.hsv.h_min == 105 and cfg.hsv.h_max == 112
    assert cfg.shape_gate.aspect_min == pytest.approx(0.56)
    assert cfg.selector.w_aspect == pytest.approx(0.7)
    # New file written, legacy files cleaned up.
    assert (tmp_path / "detection_config.json").exists()
    assert not (tmp_path / "hsv_range.json").exists()
    assert not (tmp_path / "shape_gate.json").exists()
    assert not (tmp_path / "candidate_selector_tuning.json").exists()


def test_boot_with_partial_legacy_uses_defaults_for_missing(tmp_path, monkeypatch):
    """Only `hsv_range.json` exists — preserve legacy semantics where a
    missing sub-knob file implies the default. (The old per-loader code
    individually fell back to ShapeGate.default() / Selector.default()
    if the file was absent.)"""
    (tmp_path / "hsv_range.json").write_text(json.dumps({
        "h_min": 105, "h_max": 112, "s_min": 140, "s_max": 255,
        "v_min": 40, "v_max": 255,
    }))

    main = _fresh_main(tmp_path, monkeypatch)
    from candidate_selector import CandidateSelectorTuning
    from detection import ShapeGate

    cfg = main.state.detection_config()
    assert cfg.hsv.h_min == 105
    assert cfg.shape_gate == ShapeGate.default()
    assert cfg.selector == CandidateSelectorTuning.default()
    assert cfg.preset is None  # any custom HSV → not preset-pure


def test_set_detection_config_writes_atomic_single_file(tmp_path, monkeypatch):
    """One call updates all three sub-knobs in one disk write — there is
    no observable intermediate state where HSV is new but shape_gate
    still old."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig
    from detection import HSVRange, ShapeGate
    from candidate_selector import CandidateSelectorTuning

    cfg = DetectionConfig(
        hsv=HSVRange(h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60),
        shape_gate=ShapeGate(aspect_min=0.42, fill_min=0.42),
        selector=CandidateSelectorTuning(w_aspect=0.42, w_fill=0.42),
        preset=None,
        last_applied_at=time.time(),
    )
    main.state.set_detection_config(cfg)

    persisted = json.loads((tmp_path / "detection_config.json").read_text())
    assert persisted["hsv"]["h_min"] == 10
    assert persisted["shape_gate"]["aspect_min"] == pytest.approx(0.42)
    assert persisted["selector"]["w_aspect"] == pytest.approx(0.42)
    assert persisted["preset"] is None


def test_get_detection_config_returns_view_with_modified_fields(tmp_path, monkeypatch):
    """`GET /detection/config` returns the triple + preset + diff list.
    The diff is empty for a preset-pure config and surfaces the per-
    field paths when modified."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    from detection_config import DetectionConfig
    from presets import PRESETS

    bb = PRESETS["blue_ball"]
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate, selector=bb.selector,
        preset="blue_ball", last_applied_at=12345.0,
    ))
    body = client.get("/detection/config").json()
    assert body["preset"] == "blue_ball"
    assert body["modified_fields"] == []
    assert body["hsv"] == {
        "h_min": bb.hsv.h_min, "h_max": bb.hsv.h_max,
        "s_min": bb.hsv.s_min, "s_max": bb.hsv.s_max,
        "v_min": bb.hsv.v_min, "v_max": bb.hsv.v_max,
    }

    # Mutate just shape_gate.aspect_min — the diff list must surface
    # exactly that path.
    from detection import ShapeGate
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv,
        shape_gate=ShapeGate(aspect_min=0.5, fill_min=bb.shape_gate.fill_min),
        selector=bb.selector,
        preset="blue_ball",
        last_applied_at=12346.0,
    ))
    body = client.get("/detection/config").json()
    assert body["modified_fields"] == ["shape_gate.aspect_min"]


def test_post_detection_config_round_trip(tmp_path, monkeypatch):
    """`POST /detection/config` accepts the triple, persists, returns
    the canonical view including modified_fields."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "hsv": {"h_min": 10, "h_max": 20, "s_min": 30, "s_max": 40, "v_min": 50, "v_max": 60},
        "shape_gate": {"aspect_min": 0.42, "fill_min": 0.42},
        "selector": {"w_aspect": 0.42, "w_fill": 0.42},
        "preset": None,
    }
    r = client.post("/detection/config", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["preset"] is None
    assert j["hsv"] == body["hsv"]
    assert j["modified_fields"] == []  # custom — no preset to diff against


def test_post_detection_config_rejects_preset_value_mismatch(tmp_path, monkeypatch):
    """If the operator claims `preset=blue_ball` but submits values
    that don't match, refuse — that's the silent-drift failure mode
    the redesign exists to prevent."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    from presets import PRESETS
    bb = PRESETS["blue_ball"]
    body = {
        "hsv": {
            "h_min": bb.hsv.h_min, "h_max": bb.hsv.h_max,
            "s_min": bb.hsv.s_min, "s_max": bb.hsv.s_max,
            "v_min": bb.hsv.v_min, "v_max": bb.hsv.v_max,
        },
        # Wrong shape_gate vs blue_ball preset.
        "shape_gate": {"aspect_min": 0.10, "fill_min": 0.10},
        "selector": {"w_aspect": bb.selector.w_aspect, "w_fill": bb.selector.w_fill},
        "preset": "blue_ball",
    }
    r = client.post("/detection/config", json=body)
    assert r.status_code == 400, r.text
    assert "preset" in r.json()["detail"]


def test_post_detection_config_rejects_unknown_preset(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        "shape_gate": {"aspect_min": 0.0, "fill_min": 0.0},
        "selector": {"w_aspect": 0.0, "w_fill": 0.0},
        "preset": "no_such_preset",
    }
    r = client.post("/detection/config", json=body)
    assert r.status_code == 400, r.text
    assert "no_such_preset" in r.json()["detail"]


def test_post_detection_config_rejects_missing_section(tmp_path, monkeypatch):
    """Per CLAUDE.md no-silent-fallback: a partial body must NOT
    silently re-use the current value for the missing section."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/detection/config", json={
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        # missing shape_gate + selector
    })
    assert r.status_code == 400, r.text
    assert "missing" in r.json()["detail"]


def test_legacy_hsv_endpoint_clears_preset_binding(tmp_path, monkeypatch):
    """Editing HSV alone via the legacy `/detection/hsv` endpoint MUST
    drop preset identity (the resulting config no longer matches any
    named preset by definition). This is the contract that lets the
    dashboard's identity header trustworthy-ly say "blue_ball ·
    modified" — the moment the operator edits, we are no longer
    blue_ball-pure."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig
    from presets import PRESETS

    # Start preset-pure on blue_ball.
    bb = PRESETS["blue_ball"]
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate, selector=bb.selector,
        preset="blue_ball", last_applied_at=time.time(),
    ))

    # Tweak via legacy endpoint.
    client = TestClient(main.app)
    r = client.post("/detection/hsv", json={
        "h_min": 100, "h_max": 120, "s_min": 100, "s_max": 200,
        "v_min": 30, "v_max": 200,
    })
    assert r.status_code == 200, r.text

    cfg = main.state.detection_config()
    assert cfg.preset is None  # binding cleared
    assert cfg.hsv.h_min == 100
    # shape_gate / selector still match blue_ball (we only edited HSV).
    assert cfg.shape_gate == bb.shape_gate
    assert cfg.selector == bb.selector
