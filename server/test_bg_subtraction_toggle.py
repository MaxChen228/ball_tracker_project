"""Endpoint test for /detection/bg_subtraction toggle."""
from __future__ import annotations

from fastapi.testclient import TestClient

import main


def _client() -> TestClient:
    return TestClient(main.app)


def test_default_is_enabled():
    assert main.state.detection_bg_subtraction_enabled() is True


def test_toggle_off_then_on_json():
    c = _client()
    r = c.post("/detection/bg_subtraction", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "enabled": False}
    assert main.state.detection_bg_subtraction_enabled() is False

    r = c.post("/detection/bg_subtraction", json={"enabled": True})
    assert r.status_code == 200
    assert main.state.detection_bg_subtraction_enabled() is True


def test_toggle_form_booleans():
    c = _client()
    r = c.post("/detection/bg_subtraction", data={"enabled": "false"})
    assert r.status_code == 200
    assert main.state.detection_bg_subtraction_enabled() is False
    r = c.post("/detection/bg_subtraction", data={"enabled": "on"})
    assert r.status_code == 200
    assert main.state.detection_bg_subtraction_enabled() is True


def test_invalid_value_400():
    c = _client()
    r = c.post("/detection/bg_subtraction", json={"enabled": "maybe"})
    assert r.status_code == 400
    r = c.post("/detection/bg_subtraction", json={})
    assert r.status_code == 400


def test_persists_across_store_reload(tmp_path, monkeypatch):
    """A fresh State reloading runtime_settings.json must preserve the
    flipped value."""
    data_dir = tmp_path / "data_reload"
    s1 = main.State(data_dir=data_dir)
    assert s1.detection_bg_subtraction_enabled() is True
    s1.set_detection_bg_subtraction_enabled(False)
    s2 = main.State(data_dir=data_dir)
    assert s2.detection_bg_subtraction_enabled() is False
