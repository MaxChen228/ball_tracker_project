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
    """Built-in `tennis` already exists at boot — create must 409, not
    silently overwrite. Preset filenames are immutable in the new
    model; operator must pick a fresh name to save tweaked values."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = {**_BODY_VALID, "name": "tennis"}
    r = client.post("/presets", json=body)
    assert r.status_code == 409, r.text
    assert "immutable" in r.json()["detail"]


def test_create_preset_switches_active_to_new(tmp_path, monkeypatch):
    """`POST /presets` is the dashboard Apply path: save + auto-switch.
    The newly-created preset becomes the active one so the operator
    sees their just-applied values bound by name on the next render."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Default boot active is `tennis` — confirm before swap.
    assert main.state.detection_config().preset == "tennis"
    r = client.post("/presets", json=_BODY_VALID)
    assert r.status_code == 200, r.text
    assert main.state.detection_config().preset == "indoor_overcast"


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


# ----- set active ----------------------------------------------------


def test_set_active_preset_switches_without_writing(tmp_path, monkeypatch):
    """Pure switch: `POST /presets/active` only loads the preset's
    values and binds the live `DetectionConfig` to it. No new file is
    written — the dashboard preset dropdown calls this when the
    operator selects an existing preset."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert main.state.detection_config().preset == "tennis"
    r = client.post("/presets/active", json={"name": "blue_ball"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "active": "blue_ball"}
    assert main.state.detection_config().preset == "blue_ball"
    # Sliders snap to blue_ball's values.
    bb = main.state.load_preset("blue_ball")
    assert main.state.hsv_range() == bb.hsv
    assert main.state.shape_gate() == bb.shape_gate


def test_set_active_preset_rejects_unknown_name(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets/active", json={"name": "no_such"})
    assert r.status_code == 404, r.text


def test_set_active_preset_rejects_missing_name(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/presets/active", json={})
    assert r.status_code == 400, r.text


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


def test_delete_active_preset_returns_409(tmp_path, monkeypatch):
    """Active preset can never be left dangling at the route layer —
    operator must switch active first via POST /presets/active. The
    state-level `delete_preset` accepts the unlink (used by the
    dangling-reference renderer test) but the HTTP route enforces the
    invariant."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Default active is `tennis`.
    assert main.state.detection_config().preset == "tennis"
    r = client.delete("/presets/tennis")
    assert r.status_code == 409, r.text
    assert "currently active" in r.json()["detail"]
    # Switching active first releases the lock.
    r = client.post("/presets/active", json={"name": "blue_ball"})
    assert r.status_code == 200, r.text
    r = client.delete("/presets/tennis")
    assert r.status_code == 200, r.text


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


def test_dashboard_renders_save_as_new_and_manage_buttons(tmp_path, monkeypatch):
    """Phase 3 affordances: the Detection-config card must surface
    `+ Save as new` and `Manage…` buttons, and the Manage modal must
    SSR a row per preset with Use / Duplicate / Delete actions."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    body = client.get("/").text
    assert 'data-preset-save-as' in body
    assert 'data-preset-manage' in body
    assert 'id="preset-manage-modal"' in body
    # Each seeded preset surfaces in the modal table with all three
    # row actions.
    for slug in ("tennis", "blue_ball"):
        assert f'data-preset-use="{slug}"' in body
        assert f'data-preset-duplicate="{slug}"' in body
        assert f'data-preset-delete="{slug}"' in body


def test_dashboard_marks_active_preset_in_manage_modal(tmp_path, monkeypatch):
    """The currently-bound preset is decorated with a ★ marker so the
    operator can locate it in the library list."""
    main = _fresh_main(tmp_path, monkeypatch)
    from detection_config import DetectionConfig

    bb = main.state.load_preset("blue_ball")
    main.state.set_detection_config(DetectionConfig(
        hsv=bb.hsv, shape_gate=bb.shape_gate,
        preset="blue_ball", last_applied_at=None,
    ))
    client = TestClient(main.app)
    body = client.get("/").text
    # `presets.list_presets` returns slugs sorted, so blue_ball precedes
    # tennis in the modal table. The current marker must sit before the
    # tennis row; if it appears after, it's been attached to the wrong
    # preset.
    blue_idx = body.index('data-preset-use="blue_ball"')
    tennis_idx = body.index('data-preset-use="tennis"')
    assert blue_idx < tennis_idx
    star = body.find("★ current")
    assert star != -1
    assert star < tennis_idx


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
