"""Tests for the preset CRUD endpoints (`routes/presets.py`) and the
dangling-preset UI branch in the dashboard renderer.

Phase 2 of the preset library refactor: phase 1 made the registry
disk-backed; this phase exposes operator-facing CRUD and surfaces the
"deleted reference" identity state in the dashboard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    return main


# ----- list / get ----------------------------------------------------


def test_list_presets_returns_seeded_builtins(tmp_path, monkeypatch):
    """Fresh boot: tennis + blue_ball seeds are written to disk and
    surface in `GET /presets`."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets")
    assert r.status_code == 200, r.text
    names = sorted(p["name"] for p in r.json()["presets"])
    assert names == ["blue_ball", "tennis"]


def test_get_preset_returns_full_record(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets/blue_ball")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "blue_ball"
    assert p["label"] == "Blue ball"
    assert p["hsv"]["h_min"] == 105
    assert p["shape_gate"]["aspect_min"] == pytest.approx(0.75)


def test_get_unknown_preset_returns_404(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/presets/no_such")
    assert r.status_code == 404


# ----- create --------------------------------------------------------


_BODY_VALID = {
    "name": "indoor_overcast",
    "label": "Indoor / overcast",
    "hsv": {"h_min": 100, "h_max": 130, "s_min": 80, "s_max": 255, "v_min": 60, "v_max": 255},
    "shape_gate": {"aspect_min": 0.7, "fill_min": 0.5},
}


def test_create_preset_persists_to_disk(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets", json=_BODY_VALID)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "indoor_overcast"
    assert (tmp_path / "presets" / "indoor_overcast.json").exists()
    # Round-trip: list now includes it.
    r = client.get("/presets")
    names = sorted(p["name"] for p in r.json()["presets"])
    assert "indoor_overcast" in names


def test_create_preset_rejects_duplicate_name(tmp_path, monkeypatch):
    """Built-in `tennis` already exists at boot — create must 409,
    not silently overwrite. Use PUT to overwrite."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {**_BODY_VALID, "name": "tennis"}
    r = client.post("/presets", json=body)
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


def test_create_preset_rejects_invalid_slug(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for bad in ("With Space", "UPPER", "has-dash", "x" * 33, ""):
        body = {**_BODY_VALID, "name": bad}
        r = client.post("/presets", json=body)
        assert r.status_code == 400, (bad, r.text)


def test_create_preset_rejects_missing_fields(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for missing in ("name", "label", "hsv", "shape_gate"):
        body = {**_BODY_VALID}
        del body[missing]
        r = client.post("/presets", json=body)
        assert r.status_code == 400, (missing, r.text)


def test_create_preset_rejects_invalid_hsv_bounds(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        **_BODY_VALID,
        "name": "bad_hsv",
        "hsv": {"h_min": 100, "h_max": 50, "s_min": 0, "s_max": 255, "v_min": 0, "v_max": 255},
    }
    r = client.post("/presets", json=body)
    assert r.status_code == 400, r.text
    assert "h_min" in r.json()["detail"]


# ----- replace (PUT) -------------------------------------------------


def test_put_preset_overwrites_existing(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "label": "Tennis (relabeled)",
        "hsv": {"h_min": 30, "h_max": 60, "s_min": 100, "s_max": 255, "v_min": 100, "v_max": 255},
        "shape_gate": {"aspect_min": 0.65, "fill_min": 0.55},
    }
    r = client.put("/presets/tennis", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Tennis (relabeled)"
    # GET reflects the change.
    r = client.get("/presets/tennis")
    assert r.json()["hsv"]["h_min"] == 30


def test_put_preset_404_when_missing(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "label": "ghost",
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        "shape_gate": {"aspect_min": 0.0, "fill_min": 0.0},
    }
    r = client.put("/presets/no_such", json=body)
    assert r.status_code == 404, r.text


def test_put_preset_rejects_body_name_disagreement(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "name": "blue_ball",  # disagrees with URL
        "label": "Tennis",
        "hsv": {"h_min": 25, "h_max": 55, "s_min": 90, "s_max": 255, "v_min": 90, "v_max": 255},
        "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
    }
    r = client.put("/presets/tennis", json=body)
    assert r.status_code == 400, r.text
    assert "URL is canonical" in r.json()["detail"]


# ----- delete --------------------------------------------------------


def test_delete_preset_unlinks_file(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.delete("/presets/blue_ball")
    assert r.status_code == 200, r.text
    assert not (tmp_path / "presets" / "blue_ball.json").exists()
    r = client.get("/presets/blue_ball")
    assert r.status_code == 404


def test_delete_unknown_preset_returns_404(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.delete("/presets/no_such")
    assert r.status_code == 404


# ----- dangling preset reference in dashboard -----------------------


def test_dashboard_shows_deleted_when_bound_preset_removed(tmp_path, monkeypatch):
    """Set live config to `preset=blue_ball` (preset-pure), then delete
    the preset file. Dashboard render must NOT crash and must show the
    `identity-deleted` branch with the dangling slug visible to the
    operator."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig

    # Bind to blue_ball cleanly.
    bb = main.state.load_preset("blue_ball")
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    ))

    # Operator (or cli) deletes the preset file out from under us.
    main.state.delete_preset("blue_ball")

    client = TestClient(main.app)
    body = client.get("/").text
    # Renderer survived — dashboard still serves.
    assert 'id="hsv-body"' in body
    # Dangling-reference branch surfaced visually.
    assert "identity-deleted" in body
    assert "(preset deleted)" in body
    # No reset-to-preset button (target is gone).
    assert 'data-detection-reset-preset="blue_ball"' not in body


def test_dashboard_renders_after_creating_custom_preset(tmp_path, monkeypatch):
    """A user-created preset surfaces in the dashboard's preset-button
    row, with its label HTML-escaped."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {
        "name": "rainy_day",
        "label": "Rainy <day>",  # angle brackets to verify escape
        "hsv": {"h_min": 100, "h_max": 130, "s_min": 80, "s_max": 255, "v_min": 60, "v_max": 255},
        "shape_gate": {"aspect_min": 0.7, "fill_min": 0.5},
    }
    r = client.post("/presets", json=body)
    assert r.status_code == 200, r.text
    page = client.get("/").text
    assert 'data-hsv-preset="rainy_day"' in page
    # Label should be escaped — raw angle bracket must NOT appear in the
    # button text. (HTML attributes elsewhere may carry escaped forms.)
    assert "Rainy &lt;day&gt;" in page
    assert "Rainy <day>" not in page
