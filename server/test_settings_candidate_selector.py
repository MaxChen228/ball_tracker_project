"""Route tests for POST /detection/candidate_selector."""
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
    assert cst["w_dist"] == pytest.approx(0.7)
    assert cst["w_area"] == pytest.approx(0.3)
    assert cst["dist_cost_sat_radii"] == pytest.approx(8.0)

    # JSON push: w_area is derived from w_dist server-side.
    r = client.post(
        "/detection/candidate_selector",
        json={"r_px_expected": 18.0, "w_dist": 0.4, "dist_cost_sat_radii": 5.5},
    )
    assert r.status_code == 200
    body = r.json()["candidate_selector_tuning"]
    assert body["r_px_expected"] == pytest.approx(18.0)
    assert body["w_dist"] == pytest.approx(0.4)
    assert body["w_area"] == pytest.approx(0.6)
    assert body["dist_cost_sat_radii"] == pytest.approx(5.5)

    # Surfaces on /status.
    assert client.get("/status").json()["candidate_selector_tuning"] == body

    # Persisted to disk.
    persisted = _json.loads((tmp_path / "candidate_selector_tuning.json").read_text())
    assert persisted == {
        "r_px_expected": 18.0,
        "w_area": 0.6,
        "w_dist": 0.4,
        "dist_cost_sat_radii": 5.5,
    }

    # Form push (HTML caller) redirects 303.
    r = client.post(
        "/detection/candidate_selector",
        data={"r_px_expected": "10", "w_dist": "0.9", "dist_cost_sat_radii": "12"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = main.state.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(10.0)
    assert t.w_dist == pytest.approx(0.9)
    assert t.w_area == pytest.approx(0.1)
    assert t.dist_cost_sat_radii == pytest.approx(12.0)


def test_candidate_selector_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    bad_payloads = [
        {"r_px_expected": 0.0, "w_dist": 0.5, "dist_cost_sat_radii": 8.0},
        {"r_px_expected": 12.0, "w_dist": 1.5, "dist_cost_sat_radii": 8.0},
        {"r_px_expected": 12.0, "w_dist": -0.1, "dist_cost_sat_radii": 8.0},
        {"r_px_expected": 12.0, "w_dist": 0.5, "dist_cost_sat_radii": 0.0},
        {"r_px_expected": 12.0, "w_dist": 0.5, "dist_cost_sat_radii": 100.0},
        {"r_px_expected": 500.0, "w_dist": 0.5, "dist_cost_sat_radii": 8.0},
    ]
    for body in bad_payloads:
        r = client.post("/detection/candidate_selector", json=body)
        assert r.status_code == 400, f"expected 400 for {body}"

    # Defaults unchanged.
    t = main.state.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(12.0)
    assert t.w_dist == pytest.approx(0.7)


def test_candidate_selector_persists_across_state_restart(tmp_path, monkeypatch):
    """Reload State from same data dir → tuning sticks."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post(
        "/detection/candidate_selector",
        json={"r_px_expected": 22.5, "w_dist": 0.2, "dist_cost_sat_radii": 4.0},
    )
    assert r.status_code == 200

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.candidate_selector_tuning()
    assert t.r_px_expected == pytest.approx(22.5)
    assert t.w_dist == pytest.approx(0.2)
    assert t.w_area == pytest.approx(0.8)
    assert t.dist_cost_sat_radii == pytest.approx(4.0)
