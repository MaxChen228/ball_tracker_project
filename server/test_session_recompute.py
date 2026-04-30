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


def test_recompute_emit_invariant_to_stamped_cost(tmp_path, monkeypatch):
    """Architectural invariant after pairing-full-emit: stamped
    cost_threshold no longer gates pairing emit. Two recomputes with
    different stamped costs produce identical `triangulated_by_path`
    counts; only `segments` (fit input is filtered subset) and the
    persisted stamped value differ. Each emitted point carries
    `cost_a`/`cost_b` so the viewer slider can filter client-side."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_aaaa0001"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r1 = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 1.0})
    assert r1.status_code == 200, r1.text
    by_path_loose = r1.json()["result"]["triangulated_by_path"]
    n_loose = sum(len(v) for v in by_path_loose.values())

    r2 = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 0.2})
    assert r2.status_code == 200
    by_path_tight = r2.json()["result"]["triangulated_by_path"]
    n_tight = sum(len(v) for v in by_path_tight.values())

    # Emit count is INVARIANT to stamped cost — full set is persisted.
    assert n_tight == n_loose, (n_loose, n_tight)
    # Stamped value still round-trips to SessionResult so viewer slider
    # initialises correctly.
    assert r1.json()["result"]["cost_threshold"] == pytest.approx(1.0)
    assert r2.json()["result"]["cost_threshold"] == pytest.approx(0.2)
    # Each emitted point carries cost_a/cost_b so client-side filter has
    # the data it needs.
    for path_pts in by_path_tight.values():
        for p in path_pts:
            assert "cost_a" in p
            assert "cost_b" in p


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


def test_recompute_persists_both_thresholds(tmp_path, monkeypatch):
    """Per-session sibling of cost_threshold: gap_threshold_m must round-trip
    through the recompute body and surface on SessionResult / GET /results."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_bbbb1002"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    r = client.post(
        f"/sessions/{sid}/recompute",
        json={"cost_threshold": 0.45, "gap_threshold_m": 0.08},
    )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["cost_threshold"] == pytest.approx(0.45)
    assert body["gap_threshold_m"] == pytest.approx(0.08)

    g = client.get(f"/results/{sid}")
    assert g.status_code == 200
    assert g.json()["gap_threshold_m"] == pytest.approx(0.08)


def test_recompute_omitted_gap_falls_back_to_state_default(tmp_path, monkeypatch):
    """Body without gap_threshold_m → route resolves it from
    state.pairing_tuning() and stamps it on the result so the viewer slider
    can re-init from a concrete value (not None)."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_bbbb1003"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    expected_gap = main.state.pairing_tuning().gap_threshold_m
    r = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 1.0})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["gap_threshold_m"] == pytest.approx(expected_gap)


def test_recompute_rejects_out_of_range_gap(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_cccc1004"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    for bad in (-0.01, 2.5):
        r = client.post(
            f"/sessions/{sid}/recompute",
            json={"cost_threshold": 0.5, "gap_threshold_m": bad},
        )
        assert r.status_code == 400, f"expected 400 for gap={bad}: {r.text}"

    r = client.post(
        f"/sessions/{sid}/recompute",
        json={"cost_threshold": 0.5, "gap_threshold_m": "garbage"},
    )
    assert r.status_code == 400


def test_recompute_emit_invariant_to_stamped_gap(tmp_path, monkeypatch):
    """Sibling of the cost-axis emit-invariance test: stamped
    gap_threshold_m no longer gates pairing emit. The stamped value
    persists on SessionResult so the viewer slider can initialise from
    it; the segmenter and viewer client filter downstream."""
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    sid = "s_dddd1005"
    _seed_session(main.state, sid)
    client = TestClient(main.app)

    # Wide gap → all pairs flow through emit.
    r1 = client.post(
        f"/sessions/{sid}/recompute",
        json={"cost_threshold": 1.0, "gap_threshold_m": 2.0},
    )
    assert r1.status_code == 200, r1.text
    n_loose = sum(
        len(v) for v in r1.json()["result"]["triangulated_by_path"].values()
    )

    # Tight gap — emit count must still match (gap_threshold_m no longer
    # gates pairing emit; it's a stamped-filter input the segmenter and
    # viewer slider apply downstream). The stamped value DOES round-trip
    # to SessionResult so the viewer header strip re-initialises.
    r2 = client.post(
        f"/sessions/{sid}/recompute",
        json={"cost_threshold": 1.0, "gap_threshold_m": 0.005},
    )
    assert r2.status_code == 200
    n_tight = sum(
        len(v) for v in r2.json()["result"]["triangulated_by_path"].values()
    )
    assert n_tight == n_loose, (n_loose, n_tight)
    assert r2.json()["result"]["gap_threshold_m"] == pytest.approx(0.005)


def test_segmenter_filters_input_by_stamped_cost_threshold():
    """Direct unit-level check that `stamp_segments_on_result` runs the
    segmenter against the stamped-tuning SUBSET of `result.triangulated`,
    not the full set. Builds a synthetic ballistic with two candidates
    per timestamp — a low-cost "real ball" and a high-cost distractor.
    Tightening cost_threshold to exclude the distractor changes which
    points the segmenter sees.

    Architectural significance: this is the test that decouples emit
    (full set persisted) from fit (stamped subset). Without this,
    raising the cost slider on the viewer would have no effect on the
    fit segments — defeating the whole "Apply re-runs segmenter" UX.
    """
    import numpy as np
    from session_results import stamp_segments_on_result
    from schemas import SessionResult, TriangulatedPoint

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
            cost_a=0.10, cost_b=0.10,  # real ball, low cost
        ))
        pts.append(TriangulatedPoint(
            t_rel_s=t, x_m=float(pos[0]) + 5.0, y_m=float(pos[1]),
            z_m=float(pos[2]),  # distractor 5 m sideways
            residual_m=0.001,
            cost_a=0.80, cost_b=0.80,  # distractor, high cost
        ))

    # Loose cost: distractors flow into segmenter and may produce a fake
    # second segment / contaminate the fit.
    loose = SessionResult(
        session_id="s_seg_loose",
        camera_a_received=True,
        camera_b_received=True,
        triangulated=list(pts),
        cost_threshold=1.0,
        gap_threshold_m=0.20,
    )
    stamp_segments_on_result(loose)

    # Tight cost: only real-ball points reach segmenter — clean single fit.
    tight = SessionResult(
        session_id="s_seg_tight",
        camera_a_received=True,
        camera_b_received=True,
        triangulated=list(pts),
        cost_threshold=0.20,
        gap_threshold_m=0.20,
    )
    stamp_segments_on_result(tight)

    # Both produce ≥1 segment, but the tight cost excludes 12 distractor
    # points so the segment(s) cover at most the 12 real points.
    assert len(tight.segments) >= 1
    tight_total_indices = sum(len(s.original_indices) for s in tight.segments)
    assert tight_total_indices <= 12, tight_total_indices
    # And `original_indices` index back into the FULL list (size 24), so
    # they must all be < 24 — the remap step in stamp_segments_on_result
    # correctly translates filter-subset indices to full-list indices.
    full_n = len(tight.triangulated)
    assert full_n == 24
    for s in tight.segments:
        for idx in s.original_indices:
            assert 0 <= idx < full_n


def test_recompute_unknown_session_returns_404(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/s_dddd0004/recompute", json={"cost_threshold": 0.5})
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
    r = client.post(f"/sessions/{sid}/recompute", json={"cost_threshold": 0.5})
    # Not 404 — the guard recognised the session. May be 200 with empty
    # result (no pitches persisted yet), but the response code is what
    # we're asserting here.
    assert r.status_code != 404


def test_recompute_invalid_session_id_returns_422(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post("/sessions/not-a-session-id/recompute", json={"cost_threshold": 0.5})
    assert r.status_code == 422
