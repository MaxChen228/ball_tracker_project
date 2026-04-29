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
    assert cst["r_px_expected"] == pytest.approx(12.0)
    assert cst["w_size"] == pytest.approx(0.5)
    assert cst["w_aspect"] == pytest.approx(0.3)
    assert cst["w_fill"] == pytest.approx(0.2)

    # JSON push.
    r = client.post(
        "/detection/candidate_selector",
        json={"r_px_expected": 18.0, "w_size": 0.6, "w_aspect": 0.25, "w_fill": 0.15},
    )
    assert r.status_code == 200
    body = r.json()["candidate_selector_tuning"]
    assert body["r_px_expected"] == pytest.approx(18.0)
    assert body["w_size"] == pytest.approx(0.6)
    assert body["w_aspect"] == pytest.approx(0.25)
    assert body["w_fill"] == pytest.approx(0.15)

    # Surfaces on /status.
    assert client.get("/status").json()["candidate_selector_tuning"] == body

    # Persisted to disk.
    persisted = _json.loads((tmp_path / "candidate_selector_tuning.json").read_text())
    assert persisted == {
        "r_px_expected": 18.0,
        "w_size": 0.6,
        "w_aspect": 0.25,
        "w_fill": 0.15,
    }

    # Form push (HTML caller) redirects 303.
    r = client.post(
        "/detection/candidate_selector",
        data={"r_px_expected": "10", "w_size": "0.7", "w_aspect": "0.2", "w_fill": "0.1"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = main.state.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(10.0)
    assert t.w_size == pytest.approx(0.7)
    assert t.w_aspect == pytest.approx(0.2)
    assert t.w_fill == pytest.approx(0.1)


def test_candidate_selector_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    valid = {"r_px_expected": 12.0, "w_size": 0.5, "w_aspect": 0.3, "w_fill": 0.2}
    bad_payloads = [
        {**valid, "r_px_expected": 0.0},
        {**valid, "r_px_expected": 500.0},
        {**valid, "w_size": -0.1},
        {**valid, "w_size": 1.5},
        {**valid, "w_aspect": 1.5},
        {**valid, "w_fill": -0.1},
    ]
    for body in bad_payloads:
        r = client.post("/detection/candidate_selector", json=body)
        assert r.status_code == 400, f"expected 400 for {body}"

    # Defaults unchanged.
    t = main.state.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(12.0)
    assert t.w_size == pytest.approx(0.5)


def test_candidate_selector_persists_across_state_restart(tmp_path, monkeypatch):
    """Reload State from same data dir → tuning sticks."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post(
        "/detection/candidate_selector",
        json={"r_px_expected": 22.5, "w_size": 0.4, "w_aspect": 0.4, "w_fill": 0.2},
    )
    assert r.status_code == 200

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(22.5)
    assert t.w_size == pytest.approx(0.4)
    assert t.w_aspect == pytest.approx(0.4)
    assert t.w_fill == pytest.approx(0.2)
