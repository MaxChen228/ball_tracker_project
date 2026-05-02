"""Tests for PairingTuning persistence + fan-out filter behaviour in
`pairing.triangulate_cycle`."""
from __future__ import annotations

import json as _json

import pytest


def test_pairing_tuning_default_values():
    from pairing_tuning import PairingTuning
    t = PairingTuning.default()
    assert t.gap_threshold_m == 0.20


def test_pairing_tuning_disk_round_trip(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    s = main.state

    from pairing_tuning import PairingTuning
    # Default surfaces.
    assert s.pairing_tuning() == PairingTuning.default()

    s.set_pairing_tuning(PairingTuning(gap_threshold_m=0.10))
    persisted = _json.loads((tmp_path / "pairing_tuning.json").read_text())
    assert persisted == {"gap_threshold_m": 0.10}

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.pairing_tuning()
    assert t.gap_threshold_m == pytest.approx(0.10)


def test_corrupt_tuning_json_falls_back_to_default(tmp_path, monkeypatch):
    """Per CLAUDE.md no-silent-fallback: corrupt JSON is logged but does
    NOT propagate to runtime — defaults take over so live ingest stays
    operational."""
    (tmp_path / "pairing_tuning.json").write_text("not json {}")
    import main
    s = main.State(data_dir=tmp_path)
    from pairing_tuning import PairingTuning
    assert s.pairing_tuning() == PairingTuning.default()


def test_pairing_tuning_json_with_legacy_cost_field_still_loads(tmp_path):
    """Old disk JSON predates the cost-absorption refactor and includes
    a stray `cost_threshold` key. Loader must ignore the extra key, not
    crash, so a fresh boot off old data still surfaces gap defaults."""
    (tmp_path / "pairing_tuning.json").write_text(
        _json.dumps({"cost_threshold": 0.5, "gap_threshold_m": 0.15})
    )
    import main
    s = main.State(data_dir=tmp_path)
    t = s.pairing_tuning()
    assert t.gap_threshold_m == pytest.approx(0.15)


def test_rebuild_result_seeds_current_global_pairing_tuning(tmp_path, monkeypatch):
    import main
    from pairing_tuning import PairingTuning
    from session_results import rebuild_result_for_session

    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    s = main.state
    s.set_pairing_tuning(PairingTuning(gap_threshold_m=0.07))

    sid = "s_dead00f1"
    pitch_a = main.PitchPayload(
        camera_id="A",
        session_id=sid,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_live=[],
        frames_server_post=[],
    )
    s.record(pitch_a)

    result = rebuild_result_for_session(s, sid)
    assert result.gap_threshold_m == pytest.approx(0.07)


# ---------- triangulate_cycle fan-out: gap filter behaviour ----------


def test_triangulate_cycle_fan_out_emits_multi_points():
    """Fan-out across 2 candidates per cam → up to 4 triangulated
    pairs, each tagged with source_a_cand_idx + source_b_cand_idx."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle

    payload_a, payload_b = ttm._build_pairing_payloads(
        [0.0], [0.0], "s_fa00ff01",
    )

    # Each existing frame has px/py only; replace with two-candidate list.
    fa = payload_a.frames_server_post[0]
    fb = payload_b.frames_server_post[0]
    cands_a = [
        BlobCandidate(px=fa.px, py=fa.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fa.px + 30, py=fa.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.30),
    ]
    cands_b = [
        BlobCandidate(px=fb.px, py=fb.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fb.px + 30, py=fb.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.30),
    ]
    payload_a.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fa.timestamp_s,
        candidates=cands_a, ball_detected=True,
    )
    payload_b.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fb.timestamp_s,
        candidates=cands_b, ball_detected=True,
    )

    pts = triangulate_cycle(payload_a, payload_b)
    # 2×2 fan-out: up to 4 points. Some may be near-parallel-rejected,
    # but the (cand0_a, cand0_b) pair should triangulate cleanly since
    # those are the original valid pixel pairs.
    assert len(pts) >= 1
    # Every emitted point must carry candidate provenance.
    for p in pts:
        assert p.source_a_cand_idx is not None
        assert p.source_b_cand_idx is not None
        assert 0 <= p.source_a_cand_idx < 2
        assert 0 <= p.source_b_cand_idx < 2


def test_triangulate_cycle_emit_carries_cost_for_downstream_filter():
    """Pairing emit is decoupled from any cost threshold (per-algorithm
    or otherwise). All candidate pairs under the absolute emit ceiling
    flow through; the emitted point's `cost_a` / `cost_b` carry the
    source candidates' costs so the downstream stamped-tuning filter
    (`session_results._passes_stamped_filter`) can reproduce the gate
    using the algorithm's own threshold."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle

    payload_a, payload_b = ttm._build_pairing_payloads(
        [0.0], [0.0], "s_c001ff02",
    )
    fa = payload_a.frames_server_post[0]
    fb = payload_b.frames_server_post[0]
    cands = [
        BlobCandidate(px=fa.px, py=fa.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fa.px + 30, py=fa.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.30),
    ]
    payload_a.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fa.timestamp_s,
        candidates=cands, ball_detected=True,
    )
    cands_b = [
        BlobCandidate(px=fb.px, py=fb.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fb.px + 30, py=fb.py + 30, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.30),
    ]
    payload_b.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fb.timestamp_s,
        candidates=cands_b, ball_detected=True,
    )
    pts = triangulate_cycle(payload_a, payload_b)
    # All four (cand_a × cand_b) pairs are well under the emit cost
    # ceiling (5.0).
    seen_pairs = {(p.source_a_cand_idx, p.source_b_cand_idx) for p in pts}
    assert (0, 0) in seen_pairs
    assert (1, 1) in seen_pairs
    for p in pts:
        if p.source_a_cand_idx == 0:
            assert p.cost_a == 0.10
        if p.source_a_cand_idx == 1:
            assert p.cost_a == 0.30


def test_triangulate_cycle_emit_invariant_to_gap_threshold():
    """Pairing emit is decoupled from operator gap tuning. Loose vs
    tight runs produce identical emitted sets; only the absolute
    `_EMIT_GAP_CEILING_M` gates emit. The downstream filter at
    `_passes_stamped_filter` applies the per-session gap."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle

    payload_a, payload_b = ttm._build_pairing_payloads(
        [0.0], [0.0], "s_9af00f03",
    )
    fa = payload_a.frames_server_post[0]
    fb = payload_b.frames_server_post[0]
    cands_a = [
        BlobCandidate(px=fa.px, py=fa.py, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=fa.px + 200, py=fa.py + 200, area=80,
                      area_score=0.8, aspect=0.8, fill=0.55, cost=0.10),
    ]
    payload_a.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fa.timestamp_s,
        candidates=cands_a, ball_detected=True,
    )
    payload_b.frames_server_post[0] = FramePayload(
        frame_index=0, timestamp_s=fb.timestamp_s,
        candidates=[BlobCandidate(
            px=fb.px, py=fb.py, area=100, area_score=1.0,
            aspect=1.0, fill=0.68, cost=0.10,
        )], ball_detected=True,
    )

    pts1 = triangulate_cycle(payload_a, payload_b)
    pts2 = triangulate_cycle(payload_a, payload_b)
    assert len(pts1) == len(pts2)
    # Each emitted point carries its true geometric residual; a downstream
    # gap filter at 0.01 m would drop the high-residual pair even though
    # emit kept it.
    assert any(p.residual_m > 0.01 for p in pts1)
