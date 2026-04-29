"""Route tests for POST /detection/candidate_selector (shape-prior knobs)."""
from __future__ import annotations

import json as _json

import pytest
from fastapi.testclient import TestClient


def test_candidate_selector_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    # Default surfaces on /status.
    r = client.get("/status")
    assert r.status_code == 200
    cst = r.json()["candidate_selector_tuning"]
    assert cst["w_aspect"] == pytest.approx(0.6)
    assert cst["w_fill"] == pytest.approx(0.4)

    # JSON push.
    r = client.post(
        "/detection/candidate_selector",
        json={"w_aspect": 0.7, "w_fill": 0.3},
    )
    assert r.status_code == 200
    body = r.json()["candidate_selector_tuning"]
    assert body["w_aspect"] == pytest.approx(0.7)
    assert body["w_fill"] == pytest.approx(0.3)

    # Surfaces on /status.
    assert client.get("/status").json()["candidate_selector_tuning"] == body

    # Persisted to disk inside the unified detection_config.json
    # (phase 2 of unified-config redesign — selector lives alongside
    # HSV + shape_gate in a single atomic file).
    persisted = _json.loads((tmp_path / "detection_config.json").read_text())
    assert persisted["selector"] == {"w_aspect": 0.7, "w_fill": 0.3}
    assert persisted["preset"] is None  # editing a sub-knob clears preset binding

    # Form push (HTML caller) redirects 303.
    r = client.post(
        "/detection/candidate_selector",
        data={"w_aspect": "0.55", "w_fill": "0.45"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = main.state.candidate_selector_tuning()
    assert t.w_aspect == pytest.approx(0.55)
    assert t.w_fill == pytest.approx(0.45)


def test_candidate_selector_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    valid = {"w_aspect": 0.6, "w_fill": 0.4}
    bad_payloads = [
        {**valid, "w_aspect": -0.1},
        {**valid, "w_aspect": 1.5},
        {**valid, "w_fill": -0.1},
        {**valid, "w_fill": 1.5},
        {"w_aspect": 0.6},  # missing w_fill
    ]
    for body in bad_payloads:
        r = client.post("/detection/candidate_selector", json=body)
        assert r.status_code == 400, f"expected 400 for {body}"

    # Defaults unchanged.
    t = main.state.candidate_selector_tuning()
    assert t.w_aspect == pytest.approx(0.6)
    assert t.w_fill == pytest.approx(0.4)


def test_candidate_selector_persists_across_state_restart(tmp_path, monkeypatch):
    """Reload State from same data dir → tuning sticks."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post(
        "/detection/candidate_selector",
        json={"w_aspect": 0.4, "w_fill": 0.2},
    )
    assert r.status_code == 200

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.candidate_selector_tuning()
    assert t.w_aspect == pytest.approx(0.4)
    assert t.w_fill == pytest.approx(0.2)
