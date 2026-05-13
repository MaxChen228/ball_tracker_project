"""Hybrid28dDetector contract — registered, dispatchable through
`algorithms.run_detection`, and produces the lab-validated rerank
behavior on synthetic frames.

The detector itself does no video I/O; the test injects a stub
`frame_iter` factory that yields hand-crafted BGR arrays so we can pin
specific PROD-vs-V11 emit conditions without needing real MOV files.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import algorithms
from algorithms.base import Detector
from algorithms.hybrid_28d import (
    Hybrid28dDetector,
    Hybrid28dParams,
    _emit,
    _persistence,
)
from schemas import BlobCandidate, HSVRangePayload, ShapeGatePayload


def _params_dict() -> dict:
    """Defaults that match the seeded `hybrid_28d_blue_ball` preset.
    PROD = blue_ball tight; V11 loose with morphology."""
    return {
        "prod_hsv": HSVRangePayload(
            h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255,
        ),
        "prod_shape": ShapeGatePayload(aspect_min=0.75, fill_min=0.55),
        "v11_hsv": HSVRangePayload(
            h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255,
        ),
        "v11_shape": ShapeGatePayload(aspect_min=0.40, fill_min=0.35),
    }


# --- Registry contract ----------------------------------------------------


def test_hybrid_28d_registered():
    entry = algorithms.get(algorithms.HYBRID_28D)
    assert entry.algorithm_id == "hybrid_28d"
    assert isinstance(entry.detector, Hybrid28dDetector)
    assert entry.detector.params_schema is Hybrid28dParams


def test_hybrid_28d_subclass_of_detector_abc():
    assert issubclass(Hybrid28dDetector, Detector)


def test_hybrid_28d_listed_alongside_v11():
    """Both detectors visible in `list_all` so dashboard / CLI can
    enumerate every shippable algorithm. Sorted by id for deterministic
    UI."""
    ids = [e.algorithm_id for e in algorithms.list_all()]
    assert "v11_hsv_cc" in ids
    assert "hybrid_28d" in ids


def test_hybrid_28d_params_rejects_unknown_field():
    """`extra='forbid'` — operator typo `prod_hzv` instead of
    `prod_hsv` should fail at validation, not at run time."""
    from pydantic import ValidationError
    bad = _params_dict()
    bad["prod_hzv"] = bad.pop("prod_hsv")  # typo
    with pytest.raises(ValidationError):
        Hybrid28dParams.model_validate(bad)


def test_hybrid_28d_params_default_temporal_constants():
    """Lab-validated defaults: NEIGH_HALF=6 ≈ 50ms @ 240fps,
    MATCH_PX=5.0 = CC centroid noise. Drift-pin so a future tweak is
    deliberate."""
    params = Hybrid28dParams.model_validate(_params_dict())
    assert params.neigh_half == 6
    assert params.match_px == 5.0
    assert params.v11_close_kernel == 3


def test_run_detection_dispatches_to_hybrid_28d_detector(tmp_path, monkeypatch):
    """`run_detection(HYBRID_28D, ...)` routes to Hybrid28dDetector with
    typed params materialised from the dict. End-to-end registry
    plumbing pin."""
    captured: dict = {}

    def fake_detect(self, video_path, video_start_pts_s, params, **kwargs):
        captured["params"] = params
        captured["video"] = video_path
        return []

    monkeypatch.setattr(Hybrid28dDetector, "detect", fake_detect)
    algorithms.run_detection(
        algorithms.HYBRID_28D,
        tmp_path / "fake.mov",
        0.0,
        _params_dict(),
    )
    assert isinstance(captured["params"], Hybrid28dParams)
    assert captured["params"].prod_hsv.h_min == 105


# --- Algorithm behavior on synthetic frames ------------------------------


def _bgr_with_blob(cx: int, cy: int, *, hsv_h: int, hsv_s: int = 200, hsv_v: int = 200, radius: int = 6) -> np.ndarray:
    """Paint a single filled disk on a black background. The disk's
    HSV equivalent is (hsv_h, hsv_s, hsv_v) so we can craft frames
    that pass / fail PROD's tight gate on demand."""
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    # Convert target HSV → BGR via a 1x1 lookup so the disk's measured
    # HSV after `cv2.cvtColor` lands exactly where we wanted it.
    import cv2
    pixel = np.array([[[hsv_h, hsv_s, hsv_v]]], dtype=np.uint8)
    bgr_pixel = cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0]
    cv2.circle(img, (cx, cy), radius, [int(c) for c in bgr_pixel], -1)
    return img


def test_emit_passes_prod_for_in_band_blob():
    """Sanity: a clean in-band blue blob passes PROD's tight gate."""
    bgr = _bgr_with_blob(160, 120, hsv_h=108, hsv_s=200, hsv_v=180)
    cands = _emit(
        bgr,
        HSVRangePayload(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
        ShapeGatePayload(aspect_min=0.75, fill_min=0.55),
        close_kernel=None,
        area_min=20,
    )
    assert len(cands) == 1
    assert abs(cands[0].px - 160) < 2
    assert abs(cands[0].py - 120) < 2


def test_emit_rejects_out_of_band_blob_under_prod():
    """A blob whose hue is just outside PROD's tight band fails — but
    would survive V11's looser band. Drives the rescue path."""
    bgr = _bgr_with_blob(160, 120, hsv_h=115, hsv_s=200, hsv_v=180)
    prod = _emit(
        bgr,
        HSVRangePayload(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
        ShapeGatePayload(aspect_min=0.75, fill_min=0.55),
        close_kernel=None,
        area_min=20,
    )
    assert prod == []
    v11 = _emit(
        bgr,
        HSVRangePayload(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255),
        ShapeGatePayload(aspect_min=0.40, fill_min=0.35),
        close_kernel=3,
        area_min=3,
    )
    assert len(v11) == 1


def test_persistence_counts_neighbor_window_matches():
    """A candidate at (50, 50) appearing in 3 of 4 neighbor frames
    (within MATCH_PX) → persistence = 3."""
    target = BlobCandidate(px=50, py=50, area=100, area_score=1.0,
                           aspect=1.0, fill=0.68)
    other_close = BlobCandidate(px=52, py=51, area=100, area_score=1.0,
                                aspect=1.0, fill=0.68)
    other_far = BlobCandidate(px=200, py=200, area=100, area_score=1.0,
                              aspect=1.0, fill=0.68)
    neighbors = [
        [other_close],          # match
        [other_far],            # no match
        [other_close, other_far],  # match
        [other_close],          # match
    ]
    assert _persistence(target, neighbors, match_px=5.0) == 3
    # Tightening tolerance below the actual delta (Δ=2.24) → 0 matches.
    assert _persistence(target, neighbors, match_px=1.0) == 0


def test_detect_uses_prod_when_prod_emits():
    """End-to-end: a clean in-band blob → PROD emits → output picks
    PROD's centroid. V11 fallback path is not invoked."""
    bgr = _bgr_with_blob(160, 120, hsv_h=108)

    def stub_iter(_path, start):
        for i in range(3):
            yield (start + i / 240.0, bgr.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    assert len(frames) == 3
    for f in frames:
        assert f.ball_detected
        assert abs(f.px - 160) < 2
        assert f.candidates and len(f.candidates) >= 1


def test_detect_falls_back_to_v11_when_prod_empty():
    """Edge-of-band blob → PROD empty, V11 emits → output picks the
    V11 winner."""
    bgr = _bgr_with_blob(160, 120, hsv_h=115)

    def stub_iter(_path, start):
        for i in range(3):
            yield (start + i / 240.0, bgr.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    assert len(frames) == 3
    for f in frames:
        assert f.ball_detected, "V11 fallback should have emitted"
        assert abs(f.px - 160) < 3


def test_detect_emits_no_winner_when_both_empty():
    """Black frames → neither PROD nor V11 emit → no detection."""
    blank = np.zeros((240, 320, 3), dtype=np.uint8)

    def stub_iter(_path, start):
        for i in range(2):
            yield (start + i / 240.0, blank.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    assert len(frames) == 2
    for f in frames:
        assert not f.ball_detected
        assert f.px is None
        assert f.py is None
        assert f.candidates == []


def test_detect_winner_is_always_candidates_zero():
    """Cross-cutting invariant: when ball_detected, the FramePayload's
    `(px, py)` must equal `candidates[0].(px, py)`. Downstream
    consumers reading either surface would silently disagree if these
    diverge."""
    bgr = _bgr_with_blob(160, 120, hsv_h=108)

    def stub_iter(_path, start):
        for i in range(3):
            yield (start + i / 240.0, bgr.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    for f in frames:
        if not f.ball_detected:
            continue
        assert f.candidates, "ball_detected without candidates"
        assert f.px == f.candidates[0].px
        assert f.py == f.candidates[0].py


def test_detect_window_edge_frames_dont_blow_up():
    """Pass 2 clips the persistence window to `[max(0, idx-K),
    min(N-1, idx+K)]`. First and last frames have no left- / right-
    side neighbors. Verify both clip cleanly without crashing or
    inflating persistence by reaching into invalid indices."""
    bgr = _bgr_with_blob(160, 120, hsv_h=115)  # PROD-empty, V11-emit

    def stub_iter(_path, start):
        for i in range(2):
            yield (start + i / 240.0, bgr.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    assert len(frames) == 2
    for f in frames:
        assert f.ball_detected
        assert abs(f.px - 160) < 3


def test_v11_area_floor_below_prod_recovers_micro_blobs():
    """Lab-fidelity pin: V11 area floor must drop to 3 px so the
    rescue path can emit micro-blobs PROD's 20-px floor would
    silently drop. The +0.045 R_top1 lab eval (PR #112) depends on
    this asymmetry — raising V11's floor silently kills the rescue
    path."""
    # Tiny edge-of-band blob ~ π * 1.5² ≈ 7 px. Below PROD floor (20),
    # above V11 floor (3).
    bgr = _bgr_with_blob(160, 120, hsv_h=115, radius=1)
    prod = _emit(
        bgr,
        HSVRangePayload(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
        ShapeGatePayload(aspect_min=0.75, fill_min=0.55),
        close_kernel=None,
        area_min=20,
    )
    assert prod == [], "PROD's 20-px floor + tight HSV must reject micro-blob"
    v11 = _emit(
        bgr,
        HSVRangePayload(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255),
        ShapeGatePayload(aspect_min=0.40, fill_min=0.35),
        close_kernel=3,
        area_min=3,
    )
    assert len(v11) == 1, "V11's 3-px floor must emit the micro-blob"
    # Regression guard: same blob, V11 with PROD's 20-px floor → dies.
    v11_with_prod_floor = _emit(
        bgr,
        HSVRangePayload(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255),
        ShapeGatePayload(aspect_min=0.40, fill_min=0.35),
        close_kernel=3,
        area_min=20,
    )
    assert v11_with_prod_floor == [], (
        "regression guard: if V11's area_min is ever raised to 20, "
        "the +0.045 R_top1 rescue path silently dies"
    )


def test_detect_persistence_demotes_static_distractor_in_v11_fallback():
    """A static V11-only blob that appears in EVERY frame ranks worse
    (high persistence) than a one-off motion blob. Output's top-1 in
    the 'distractor + novel' frame should be the novel candidate."""
    # Static distractor at (50, 50) — visible in all 5 frames
    # Motion blob at (200, 100) — only in frame 2
    static_bgr = _bgr_with_blob(50, 50, hsv_h=115)
    both_bgr = static_bgr.copy()
    import cv2
    # Add the motion blob to frame 2.
    pixel = np.array([[[115, 200, 180]]], dtype=np.uint8)
    bgr_pixel = cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0]
    cv2.circle(both_bgr, (200, 100), 6, [int(c) for c in bgr_pixel], -1)

    frames_seq = [static_bgr, static_bgr, both_bgr, static_bgr, static_bgr]

    def stub_iter(_path, start):
        for i, f in enumerate(frames_seq):
            yield (start + i / 240.0, f.copy())

    detector = Hybrid28dDetector()
    frames = detector.detect(
        Path("/dev/null/fake.mov"),
        0.0,
        Hybrid28dParams.model_validate(_params_dict()),
        frame_iter=stub_iter,
    )
    # In frame 2 the V11 fallback picks BOTH cands. The motion novel
    # candidate at (200, 100) should be cands[0] (top-1 winner).
    novel_frame = frames[2]
    assert novel_frame.ball_detected
    assert abs(novel_frame.px - 200) < 3, (
        f"persistence rerank should pick motion-novel (200,100), "
        f"got ({novel_frame.px}, {novel_frame.py})"
    )


# --- Preset wiring -------------------------------------------------------


def test_seeded_hybrid_28d_blue_ball_preset_loads_via_registry(tmp_path, monkeypatch):
    """The seeded `hybrid_28d_blue_ball` preset round-trips through
    `seed_builtins` → disk → `load_preset` and validates against
    Hybrid28dParams."""
    import presets
    presets.seed_builtins(tmp_path, atomic_write=lambda p, s: p.write_text(s))
    p = presets.load_preset(tmp_path, "hybrid_28d_blue_ball")
    assert p.algorithm_id == "hybrid_28d"
    # `params` round-trips through Hybrid28dParams in `_from_dict`,
    # so a successful load proves the seed shape is valid.
    materialized = Hybrid28dParams.model_validate(p.params)
    assert materialized.prod_hsv.h_min == 105
    assert materialized.v11_hsv.h_min == 103
    assert materialized.neigh_half == 6
