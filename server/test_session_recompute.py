"""Tests for POST /sessions/{sid}/recompute (per-session gap_threshold_m).

Verifies:
- Stamped gap_threshold_m round-trips through the route + result
- Out-of-range / missing input → 400
- Unknown session → 404
- Segmenter applies per-algorithm cost gate before fit
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
    bucket_a = payload_a.active_server_post_algorithm_id or "v11_hsv_cc"
    bucket_b = payload_b.active_server_post_algorithm_id or "v11_hsv_cc"
    payload_a.frames_by_algorithm[bucket_a][0] = FramePayload(
        frame_index=0, timestamp_s=fa.timestamp_s,
        candidates=cands_a, ball_detected=True,
    )
    payload_b.frames_by_algorithm[bucket_b][0] = FramePayload(
        frame_index=0, timestamp_s=fb.timestamp_s,
        candidates=cands_b, ball_detected=True,
    )
    state.pitches[("A", session_id)] = payload_a
    state.pitches[("B", session_id)] = payload_b


def test_recompute_persists_gap_threshold(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_bbbb0002"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r = client.post(f"/sessions/{sid}/recompute", json={"gap_threshold_m": 0.08})
    assert r.status_code == 200
    body = r.json()["result"]
    assert body["gap_threshold_m"] == pytest.approx(0.08)

    # GET /results/{sid} also surfaces the value.
    g = client.get(f"/results/{sid}")
    assert g.status_code == 200
    assert g.json()["gap_threshold_m"] == pytest.approx(0.08)


def test_recompute_response_no_longer_includes_cost_threshold(tmp_path, monkeypatch):
    """Cost-absorption refactor: cost is per-algorithm, not per-session.
    The recompute response must NOT carry a `cost_threshold` field on
    the SessionResult dump (Pydantic strips the dropped field)."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_cccc0003"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r = client.post(f"/sessions/{sid}/recompute", json={"gap_threshold_m": 0.10})
    assert r.status_code == 200
    body = r.json()["result"]
    assert "cost_threshold" not in body, body.keys()


def test_recompute_broadcasts_fit_with_cause(tmp_path, monkeypatch):
    """Recompute path of the fit-broadcast contract — cycle_end /
    server_post are covered in test_ws_broadcast / test_pitch_endpoints
    respectively. The viewer's SSE handler skips `cause == 'recompute'`
    to avoid double-applying the scene (the inline /recompute response
    handler already patches it), so the field MUST land on the wire."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_eeee0004"
    _seed_session(main.state, sid)

    events: list[tuple[str, dict]] = []

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    client = TestClient(main.app)
    r = client.post(f"/sessions/{sid}/recompute", json={"gap_threshold_m": 0.06})
    assert r.status_code == 200
    fit_events = [d for n, d in events if n == "fit" and d.get("sid") == sid]
    assert len(fit_events) == 1
    fe = fit_events[0]
    assert fe["cause"] == "recompute"
    assert "segments" in fe
    assert "gap_threshold_m" in fe
    # Cost is no longer per-session; broadcast must NOT carry it.
    assert "cost_threshold" not in fe


def test_recompute_rejects_missing_gap(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_cccc0030"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r = client.post(f"/sessions/{sid}/recompute", json={})
    assert r.status_code == 400


def test_recompute_rejects_out_of_range_gap(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_cccc1004"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    for bad in (-0.01, 2.5):
        r = client.post(
            f"/sessions/{sid}/recompute",
            json={"gap_threshold_m": bad},
        )
        assert r.status_code == 400, f"expected 400 for gap={bad}: {r.text}"

    r = client.post(
        f"/sessions/{sid}/recompute",
        json={"gap_threshold_m": "garbage"},
    )
    assert r.status_code == 400


def test_recompute_emit_invariant_to_stamped_gap(tmp_path, monkeypatch):
    """Architectural invariant after pairing-full-emit: stamped
    gap_threshold_m no longer gates pairing emit. Two recomputes with
    different stamped gaps produce identical `triangulated_by_path`
    counts; only `segments` (fit input is filtered subset) and the
    persisted stamped value differ."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_dddd1005"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r1 = client.post(
        f"/sessions/{sid}/recompute",
        json={"gap_threshold_m": 2.0},
    )
    assert r1.status_code == 200, r1.text
    n_loose = sum(
        len(v) for v in r1.json()["result"]["triangulated_by_path"].values()
    )

    r2 = client.post(
        f"/sessions/{sid}/recompute",
        json={"gap_threshold_m": 0.005},
    )
    assert r2.status_code == 200
    n_tight = sum(
        len(v) for v in r2.json()["result"]["triangulated_by_path"].values()
    )
    assert n_tight == n_loose, (n_loose, n_tight)
    assert r2.json()["result"]["gap_threshold_m"] == pytest.approx(0.005)


def test_segmenter_filters_input_by_per_algorithm_cost_threshold(monkeypatch):
    """Direct unit-level check that `stamp_segments_on_result` runs the
    segmenter against the per-algorithm cost-filtered SUBSET of
    `result.triangulated`, not the full set. The cost gate now comes
    from `algorithms.cost_threshold_for_algorithm` keyed on the path's
    algorithm id, not from a SessionResult field.

    Architectural significance: this decouples emit (full set persisted)
    from fit (algorithm-stamped subset). Without this, the cost-absorption
    refactor would have lost the cost-side filter entirely.
    """
    import numpy as np
    import algorithms as algorithms_mod
    from session_results import stamp_segments_on_result
    from schemas import (
        DetectionConfigSnapshotPayload,
        DetectionPath,
        SessionResult,
        TriangulatedPoint,
    )

    # Build a private fake algorithm with a TIGHT cost threshold so we
    # can assert the segmenter sees only low-cost points.
    fake_entry = algorithms_mod.AlgorithmEntry(
        algorithm_id="v99_tight",
        label="tight test",
        description="tight test",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
        cost_threshold=0.20,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v99_tight", fake_entry)

    # 12 frames of clean ballistic, each with a "real" + "distractor" point.
    G = np.array([0.0, 0.0, -9.81])
    p0 = np.array([0.0, 0.0, 1.5])
    v0 = np.array([0.0, 25.0, 5.0])
    pts: list[TriangulatedPoint] = []
    for i in range(12):
        t = i * (1.0 / 240.0)
        pos = p0 + v0 * t + 0.5 * G * t * t
        pts.append(TriangulatedPoint(
            t_rel_s=t, x_m=float(pos[0]), y_m=float(pos[1]), z_m=float(pos[2]),
            residual_m=0.001,
            cost_a=0.10, cost_b=0.10,  # real ball, below 0.20 gate
        ))
        pts.append(TriangulatedPoint(
            t_rel_s=t, x_m=float(pos[0]) + 5.0, y_m=float(pos[1]),
            z_m=float(pos[2]),  # distractor 5 m sideways
            residual_m=0.001,
            cost_a=0.80, cost_b=0.80,  # above 0.20 gate
        ))

    snap = DetectionConfigSnapshotPayload(
        algorithm_id="v99_tight",
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30, "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )
    result = SessionResult(
        session_id="s_seg_tight",
        cameras_received={"A": True, "B": True},
        triangulated_by_algorithm={"v99_tight": list(pts)},
        algorithms_completed={"v99_tight"},
        config_used_by_algorithm={"v99_tight": snap},
        active_server_post_algorithm_id="v99_tight",
        gap_threshold_m=0.20,
    )
    stamp_segments_on_result(
        result, legacy_points_path=DetectionPath.server_post,
    )

    assert len(result.segments) >= 1
    tight_total_indices = sum(
        len(s.original_indices) for s in result.segments
    )
    # Only the 12 real-ball points pass the v99_tight 0.20 cost gate.
    assert tight_total_indices <= 12, tight_total_indices
    # And `original_indices` index back into the FULL list (size 24).
    full_n = len(result.triangulated)
    assert full_n == 24
    for s in result.segments:
        for idx in s.original_indices:
            assert 0 <= idx < full_n


def test_recompute_unknown_session_returns_404(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/s_dddd0004/recompute", json={"gap_threshold_m": 0.20})
    assert r.status_code == 404


def test_recompute_accepts_live_only_session_in_live_pairings(tmp_path, monkeypatch):
    """Live-only WS sessions live in `_live_pairings` until persist_live_frames
    flushes them to `state.pitches`. The 404 guard MUST recognise them via
    `_live_pairings` (matching `state.store_result`'s own guard) — otherwise
    operator hits Apply mid-session and gets a misleading 404."""
    import main
    from live_pairing import LivePairingSession
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_eeee0005"
    main.state._live_pairings[sid] = LivePairingSession(sid)
    client = TestClient(main.app)
    r = client.post(f"/sessions/{sid}/recompute", json={"gap_threshold_m": 0.20})
    # Not 404 — the guard recognised the session. May be 200 with empty
    # result (no pitches persisted yet), but the response code is what
    # we're asserting here.
    assert r.status_code != 404


def test_recompute_invalid_session_id_returns_422(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/not-a-session-id/recompute", json={"gap_threshold_m": 0.20})
    assert r.status_code == 422
