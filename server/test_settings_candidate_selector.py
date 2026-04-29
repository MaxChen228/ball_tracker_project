"""Route tests for the candidate-selector tuning surface inside the
unified `POST /detection/config` endpoint (phase 3 of the unified-
config redesign — the legacy `/detection/candidate_selector` endpoint
is retired)."""
from __future__ import annotations

import json as _json

import pytest
from fastapi.testclient import TestClient


def _post_config(client, *, w_aspect: float, w_fill: float):
    return client.post(
        "/detection/config",
        json={
            "hsv": {
                "h_min": 25, "h_max": 55, "s_min": 90, "s_max": 255,
                "v_min": 90, "v_max": 255,
            },
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
            "selector": {"w_aspect": w_aspect, "w_fill": w_fill},
            "preset": None,
        },
    )


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

    # POST through the unified config endpoint.
    r = _post_config(client, w_aspect=0.7, w_fill=0.3)
    assert r.status_code == 200, r.text
    sel = r.json()["selector"]
    assert sel["w_aspect"] == pytest.approx(0.7)
    assert sel["w_fill"] == pytest.approx(0.3)

    # Surfaces on /status.
    assert client.get("/status").json()["candidate_selector_tuning"] == sel

    # Persisted to disk inside the unified detection_config.json.
    persisted = _json.loads((tmp_path / "detection_config.json").read_text())
    assert persisted["selector"] == {"w_aspect": 0.7, "w_fill": 0.3}
    assert persisted["preset"] is None


def test_candidate_selector_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    bad_pairs = [
        (-0.1, 0.4),  # w_aspect under
        (1.5, 0.4),   # w_aspect over
        (0.6, -0.1),  # w_fill under
        (0.6, 1.5),   # w_fill over
    ]
    for wa, wf in bad_pairs:
        r = _post_config(client, w_aspect=wa, w_fill=wf)
        assert r.status_code == 400, f"expected 400 for ({wa},{wf})"

    # Defaults unchanged.
    t = main.state.candidate_selector_tuning()
    assert t.w_aspect == pytest.approx(0.6)
    assert t.w_fill == pytest.approx(0.4)


def test_candidate_selector_persists_across_state_restart(tmp_path, monkeypatch):
    """Reload State from same data dir → tuning sticks."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = _post_config(client, w_aspect=0.4, w_fill=0.2)
    assert r.status_code == 200

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.candidate_selector_tuning()
    assert t.w_aspect == pytest.approx(0.4)
    assert t.w_fill == pytest.approx(0.2)
