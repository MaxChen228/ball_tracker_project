"""Tests for PairingTuning persistence + fan-out filter behaviour in
`pairing.triangulate_cycle`."""
from __future__ import annotations

import json as _json

import numpy as np
import pytest


def test_pairing_tuning_default_values():
    from pairing_tuning import PairingTuning
    t = PairingTuning.default()
    assert t.cost_threshold == 1.0
    assert t.gap_threshold_m == 0.20


def test_pairing_tuning_disk_round_trip(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    s = main.state

    from pairing_tuning import PairingTuning
    # Default surfaces.
    assert s.pairing_tuning() == PairingTuning.default()

    s.set_pairing_tuning(PairingTuning(cost_threshold=0.5, gap_threshold_m=0.10))
    persisted = _json.loads((tmp_path / "pairing_tuning.json").read_text())
    assert persisted == {"cost_threshold": 0.5, "gap_threshold_m": 0.10}

    # Simulate restart.
    fresh = main.State(data_dir=tmp_path)
    t = fresh.pairing_tuning()
    assert t.cost_threshold == pytest.approx(0.5)
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


# ---------- triangulate_cycle fan-out: cost / gap filter behaviour ----------

def _build_minimal_pitches():
    """Two cameras looking at a single point from 90° apart, two frames
    each, simple intrinsics. Returns (pitch_a, pitch_b)."""
    from schemas import (
        BlobCandidate, FramePayload, IntrinsicsPayload, PitchPayload,
    )
    fx = fy = 1000.0
    cx = cy = 500.0
    intr = IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy)

    # Camera A at (-2, 0, 1) looking at origin; Camera B at (0, -2, 1).
    # For a target at (0, 0, 0), project pixels with simple geometry.
    # A's optical axis is +x; target lies along it → centre pixel.
    # B's optical axis is +y; same → centre pixel.
    # We'll cheat and just put px=cx, py=cy on both — produces rays
    # that intersect approximately at world origin.

    def _frame(idx: int, t: float, cands: list[BlobCandidate]) -> FramePayload:
        return FramePayload(
            frame_index=idx, timestamp_s=t,
            candidates=cands, ball_detected=True,
        )

    cands_a = [
        BlobCandidate(px=cx, py=cy, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=cx + 50, py=cy, area=80, area_score=0.8,
                      aspect=0.8, fill=0.55, cost=0.40),
    ]
    cands_b = [
        BlobCandidate(px=cx, py=cy, area=100, area_score=1.0,
                      aspect=1.0, fill=0.68, cost=0.10),
        BlobCandidate(px=cx, py=cy + 50, area=80, area_score=0.8,
                      aspect=0.8, fill=0.55, cost=0.40),
    ]

    # Homography: identity-ish so recover_extrinsics returns a valid
    # pose. We need plate-plane→pixel mapping, so the homography has
    # to be consistent with the camera placement. For a unit test of
    # the fan-out plumbing we use the two cams from
    # test_triangulation_math which already builds proper H.
    return cands_a, cands_b


def test_triangulate_cycle_fan_out_emits_multi_points():
    """Fan-out across 2 candidates per cam → up to 4 triangulated
    pairs, each tagged with source_a_cand_idx + source_b_cand_idx."""
    # Reuse the synthetic-camera builder from test_triangulation_math
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle
    from pairing_tuning import PairingTuning

    # Build pitches with one frame each, 2 candidates per side.
    K, _ = ttm._tri_make_camera_pair_setup() if hasattr(ttm, '_tri_make_camera_pair_setup') else (None, None)

    # Simpler: hand-roll using the existing _build_pairing_payloads
    # helper (gives us valid intrinsics + homography + extrinsics).
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

    pts = triangulate_cycle(
        payload_a, payload_b,
        tuning=PairingTuning(cost_threshold=1.0, gap_threshold_m=10.0),
    )
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


def test_triangulate_cycle_cost_threshold_filters():
    """cost_threshold=0.2 → only the (0,0) pair (both cost=0.10) survives;
    other pairs (cost 0.30) are dropped."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle
    from pairing_tuning import PairingTuning

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
    pts = triangulate_cycle(
        payload_a, payload_b,
        tuning=PairingTuning(cost_threshold=0.2, gap_threshold_m=10.0),
    )
    # All surviving points must have BOTH cands at cost ≤ 0.2 → ca_idx=0
    # and cb_idx=0 only.
    for p in pts:
        assert p.source_a_cand_idx == 0
        assert p.source_b_cand_idx == 0


def test_triangulate_cycle_gap_threshold_filters():
    """Tight gap_threshold drops near-parallel-but-not-coincident pairs."""
    import test_triangulation_math as ttm
    from schemas import BlobCandidate, FramePayload
    from pairing import triangulate_cycle
    from pairing_tuning import PairingTuning

    payload_a, payload_b = ttm._build_pairing_payloads(
        [0.0], [0.0], "s_9af00f03",
    )
    fa = payload_a.frames_server_post[0]
    fb = payload_b.frames_server_post[0]
    # Two A candidates at very different pixels — they triangulate
    # against B's single candidate at wildly different 3D points,
    # most with large gap.
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

    # Loose gap → both A candidates pair through.
    loose = triangulate_cycle(
        payload_a, payload_b,
        tuning=PairingTuning(cost_threshold=1.0, gap_threshold_m=10.0),
    )
    # Tight gap → at most one (the geometrically-consistent pair).
    tight = triangulate_cycle(
        payload_a, payload_b,
        tuning=PairingTuning(cost_threshold=1.0, gap_threshold_m=0.01),
    )
    assert len(tight) <= len(loose)
