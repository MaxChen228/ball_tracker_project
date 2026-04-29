"""Tests for POST /sessions/{sid}/recompute (per-session cost_threshold).

Verifies:
- Lower threshold drops more candidates (fan-out fewer points)
- SessionResult.cost_threshold persists what the operator chose
- Out-of-range / missing input → 400
- Unknown session → 404
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _seed_session(state, session_id: str):
    """Build a 1-frame A/B session in `state.pitches` with two candidates
    per camera at different costs. Reuses test_triangulation_math's
    extrinsics builder so the pitch geometry is calibratable."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload

    payload_a, payload_b = ttm._build_pairing_payloads(
        [0.0], [0.0], session_id,
    )
    fa = payload_a.frames_server_post[0]
    fb = payload_b.frames_server_post[0]
    cands_a = [
        BlobCandidate(px=fa.px, py=fa.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fa.px + 30, py=fa.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.50),
    ]
    cands_b = [
        BlobCandidate(px=fb.px, py=fb.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fb.px + 30, py=fb.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.50),
    ]
    payload_a.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fa.timestamp_s,
        candidates=cands_a, ball_detected=True,
    )
    payload_b.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fb.timestamp_s,
        candidates=cands_b, ball_detected=True,
    )
    state.pitches[("A", session_id)] = payload_a
    state.pitches[("B", session_id)] = payload_b


def test_recompute_lower_threshold_drops_more_candidates(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_aaaa0001"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    # cost_threshold=1.0 → all 4 candidate pairs pass → fan-out cap.
    r1 = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 1.0})
    assert r1.status_code == 200, r1.text
    n_loose = sum(
        len(v) for v in r1.json()["result"]["triangulated_by_path"].values()
    )

    # cost_threshold=0.2 → only the (0,0) pair (both cost=0.10) passes.
    r2 = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 0.2})
    assert r2.status_code == 200
    n_tight = sum(
        len(v) for v in r2.json()["result"]["triangulated_by_path"].values()
    )
    assert n_tight < n_loose


def test_recompute_persists_cost_threshold(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_bbbb0002"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 0.45})
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["cost_threshold"] == pytest.approx(0.45)

    # GET /results/{sid} also surfaces the value.
    g = client.get(f"/results/{sid}")
    assert g.status_code == 200
    assert g.json()["cost_threshold"] == pytest.approx(0.45)


def test_recompute_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_cccc0003"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    for bad in (-0.1, 1.5):
        r = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": bad})
        assert r.status_code == 400, f"expected 400 for {bad}: {r.text}"

    # Missing field → 400.
    r = client.post(f"/sessions/{sid}/recompute", json={})
    assert r.status_code == 400


def test_recompute_unknown_session_returns_404(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/s_dddd0004/recompute", json={"cost_threshold": 0.5})
    assert r.status_code == 404


def test_recompute_invalid_session_id_returns_422(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/not-a-session-id/recompute", json={"cost_threshold": 0.5})
    assert r.status_code == 422
