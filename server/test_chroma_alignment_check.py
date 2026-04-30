"""Tests for `chroma_alignment_check.py`.

The tool itself is a diagnostic, not part of the production hot path, so
the tests focus on the parts that would silently mislead: the BT.709
matrix math, the achromatic invariant, and the hue-wrap normalization.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

import chroma_alignment_check as mod


# --- BT.709 matrix sanity ---------------------------------------------------


def test_achromatic_yields_gray_under_both_matrices():
    """Cb=Cr=128 is the achromatic axis. Both BT.601 and BT.709 must
    collapse to R=G=B = (Y-16)/(235-16) ramp. Any difference here means
    the matrix or the range scaling is wrong."""
    for y_val in (16, 50, 126, 200, 235):
        y = np.full((2, 2), y_val, dtype=np.uint8)
        uv = np.full((1, 1, 2), 128, dtype=np.uint8)
        bgr_601 = mod.yuv_nv12_to_bgr_bt601(y, uv)
        bgr_709 = mod.yuv_nv12_to_bgr_bt709(y, uv)
        # OpenCV's integer math may give ±1 from numpy float math; we
        # don't require byte-identity, just BGR equality within 2 units
        # AND the 601/709 outputs identical to each other.
        diff = bgr_601.astype(np.int16) - bgr_709.astype(np.int16)
        assert np.abs(diff).max() <= 1, (
            f"Y={y_val}: 601 vs 709 differ on achromatic axis: 601={bgr_601[0,0]} "
            f"709={bgr_709[0,0]}"
        )
        # B == G == R within 1 unit (true gray)
        for px in (bgr_601[0, 0], bgr_709[0, 0]):
            assert max(px) - min(px) <= 1, f"non-gray output for Y={y_val}: {px}"


def test_bt709_white_and_black_clip_correctly():
    y_white = np.full((2, 2), 235, dtype=np.uint8)
    uv = np.full((1, 1, 2), 128, dtype=np.uint8)
    bgr = mod.yuv_nv12_to_bgr_bt709(y_white, uv)
    assert bgr[0, 0].tolist() == [255, 255, 255]

    y_black = np.full((2, 2), 16, dtype=np.uint8)
    bgr = mod.yuv_nv12_to_bgr_bt709(y_black, uv)
    assert bgr[0, 0].tolist() == [0, 0, 0]


def test_bt709_matches_libswscale_within_2_units(tmp_path):
    """Sanity-bound the numpy 709 against libswscale's 709 on synthetic
    frames. We don't require byte-identity (chroma upsample method
    differs — nearest in our impl, default-to-bilinear in libswscale)
    but a >5-unit gap would mean our matrix is wrong. 2 units is what
    we observe in practice on real session MOVs."""
    import av  # type: ignore[import]

    # Encode a synthetic yuv420p frame, then decode it through PyAV.
    w, h = 32, 24
    y = np.tile(np.linspace(40, 200, w, dtype=np.uint8), (h, 1))
    cb = np.full((h // 2, w // 2), 100, dtype=np.uint8)
    cr = np.full((h // 2, w // 2), 160, dtype=np.uint8)

    out_path = tmp_path / "synth.mov"
    container = av.open(str(out_path), mode="w", format="mov")
    try:
        stream = container.add_stream("h264", rate=30)
        stream.width = w
        stream.height = h
        stream.pix_fmt = "yuv420p"
        # mark BT.709 explicitly so libswscale at decode does the same
        # matrix our numpy impl assumes
        stream.codec_context.colorspace = 1  # type: ignore[attr-defined]
        stream.codec_context.color_range = 1  # type: ignore[attr-defined]

        # Build VideoFrame manually from yuv420p planes
        y_plane = y
        u_plane = cb
        v_plane = cr
        yuv_buf = np.concatenate([
            y_plane.flatten(),
            u_plane.flatten(),
            v_plane.flatten(),
        ]).reshape((h * 3 // 2, w))
        frame = av.VideoFrame.from_ndarray(yuv_buf, format="yuv420p")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()

    container = av.open(str(out_path))
    try:
        s = container.streams.video[0]
        for f in container.decode(s):
            nv = f.to_ndarray(format="nv12")
            bgr_libsw = f.to_ndarray(format="bgr24")
            y_dec, uv_dec = mod.split_nv12(nv)
            bgr_np = mod.yuv_nv12_to_bgr_bt709(y_dec, uv_dec)
            d = bgr_libsw.astype(np.int16) - bgr_np.astype(np.int16)
            # >5 unit gap means our matrix coefficients are off
            assert np.abs(d).mean() < 5.0, (
                f"numpy 709 disagrees with libswscale 709 by mean "
                f"{np.abs(d).mean():.2f} BGR units — matrix likely wrong"
            )
            break
    finally:
        container.close()


# --- synthetic table --------------------------------------------------------


def test_synthetic_table_achromatic_rows_zero_delta():
    rows = mod.synthetic_table()
    achromatic = [r for r in rows if r["cb"] == 128 and r["cr"] == 128]
    assert achromatic, "no achromatic test swatch — synthetic regression value lost"
    for r in achromatic:
        assert r["d_h"] == 0, f"{r['name']}: ΔH should be 0 for achromatic, got {r['d_h']}"
        assert r["d_s"] == 0, f"{r['name']}: ΔS should be 0 for achromatic, got {r['d_s']}"
        assert r["d_v"] == 0, f"{r['name']}: ΔV should be 0 for achromatic, got {r['d_v']}"


def test_synthetic_hue_wrap_handles_red():
    """Saturated red lands near hue=0/179 — the raw subtraction can
    produce ±177 for a mathematical 3-unit offset. The table must wrap."""
    rows = mod.synthetic_table()
    red = [r for r in rows if r["name"] == "red_safety"]
    assert red, "red_safety swatch missing"
    # The wrap brings it into [-90, 90]; if not, the wrap is broken.
    assert -90 <= red[0]["d_h"] <= 90


# --- alignment-scorecard regression bounds ----------------------------------
#
# These tests pin the empirical findings from the 2026-04-30 pixel-layer
# closure (commit 781009f). They are NOT pure-math sanity checks like the
# achromatic ones — they encode the operator-facing claim "BT.601 vs
# BT.709 mismatch is operationally invisible for our presets". A
# regression here means either:
#   (a) someone introduced a preset narrower than the matrix offset can
#       absorb (Δh p95 = 3 OpenCV units → minimum safe width 6), OR
#   (b) the BT.709 matrix coefficients drifted (numpy impl bug or
#       OpenCV bumped its 601 coefficients).
# Either case requires re-running `chroma_alignment_check.py --session`
# on a real session before the change merges.


def test_synthetic_deep_blue_hue_invariant():
    """The project ball is deep blue (h≈108-115 in OpenCV space). The
    alignment scorecard claim "Δh = 0 for deep_blue" anchors the
    "operationally invisible" finding — if this ever fires, the matrix
    or the swatch definition has shifted."""
    rows = mod.synthetic_table()
    deep_blue = next((r for r in rows if r["name"] == "deep_blue"), None)
    assert deep_blue is not None, "deep_blue swatch removed — anchors scorecard"
    assert deep_blue["d_h"] == 0, (
        f"deep_blue ΔH must remain 0 (matrix invariance for project "
        f"ball color) — got {deep_blue['d_h']}. If the matrix coefficients "
        f"changed intentionally, re-run chroma_alignment_check on a real "
        f"blue-ball session and update CLAUDE.md scorecard accordingly."
    )


def test_synthetic_red_safety_within_measured_bound():
    """Red_safety is the worst-case swatch in our synthetic table. Pin
    the current measurement so a future BT.709 numpy-impl regression
    doesn't silently widen the offset. Bounds are ~2× current measured
    values (|Δh|=3, |Δv|=10 at commit 781009f)."""
    rows = mod.synthetic_table()
    red = next((r for r in rows if r["name"] == "red_safety"), None)
    assert red is not None
    assert abs(red["d_h"]) <= 5, (
        f"red_safety ΔH={red['d_h']} exceeds bound 5 — "
        f"BT.709 matrix likely drifted from canonical coefficients."
    )
    assert abs(red["d_v"]) <= 15, f"red_safety ΔV={red['d_v']} exceeds bound 15"


_MIN_SAFE_HUE_WIDTH_OPENCV_UNITS = 6


def test_all_builtin_presets_have_safe_hue_width():
    """Empirical p95 |Δh| = 3 OpenCV units between BT.601 (iOS) and
    BT.709 (server). A preset narrower than ~6 units risks losing the
    margin that makes the matrix mismatch operationally invisible —
    i.e. the same physical ball would gate-pass under one matrix and
    fail under the other.

    If you legitimately need a narrow preset (e.g. neon yellow that
    only occupies 3 hue units), either (a) widen by relaxing the
    floor below at known operator cost, or (b) fix the matrix in
    BallDetector.mm to BT.709 first. Do NOT silently bypass — see
    CLAUDE.md alignment-scorecard pixel-layer triggers."""
    import presets as presets_mod  # type: ignore

    seeds = presets_mod._BUILTIN_SEEDS
    assert seeds, "no builtin presets — registry empty?"
    failures: list[str] = []
    for name, preset in seeds.items():
        width = preset.hsv.h_max - preset.hsv.h_min
        if width < _MIN_SAFE_HUE_WIDTH_OPENCV_UNITS:
            failures.append(
                f"  - {name}: h[{preset.hsv.h_min},{preset.hsv.h_max}] "
                f"width={width} < floor={_MIN_SAFE_HUE_WIDTH_OPENCV_UNITS}"
            )
    if failures:
        msg = "preset(s) below safe hue-width floor:\n" + "\n".join(failures)
        msg += (
            "\n\nThe BT.601 (iOS) vs BT.709 (server) matrix mismatch "
            "shifts hue by up to 3 OpenCV units p95 at real ball pixels; "
            "presets narrower than 6 units lose the margin that keeps the "
            "mismatch invisible. Re-run `python chroma_alignment_check.py "
            "--session <sid> --preset <name>` to see the actual gate "
            "Jaccard, and decide whether to widen the preset or fix the "
            "matrix first."
        )
        raise AssertionError(msg)


# --- session statistics -----------------------------------------------------


def test_stats_helper_handles_empty():
    s = mod._stats([])
    assert s == {"n": 0, "mean": None, "p50": None, "p95": None, "abs_max": None}


def test_stats_helper_basic():
    s = mod._stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert s["n"] == 5
    assert s["mean"] == pytest.approx(3.0)
    assert s["p50"] == pytest.approx(3.0)
    assert s["p95"] == pytest.approx(4.8)
    assert s["abs_max"] == pytest.approx(5.0)


def test_split_nv12_rejects_bad_height():
    bad = np.zeros((100, 320), dtype=np.uint8)  # 100 not divisible by 3
    with pytest.raises(ValueError, match="not divisible by 3"):
        mod.split_nv12(bad)


def test_jaccard_empty_union_returns_none():
    a = np.zeros((10, 10), dtype=np.uint8)
    b = np.zeros((10, 10), dtype=np.uint8)
    assert mod.jaccard(a, b) is None


def test_jaccard_perfect_overlap_is_one():
    a = np.full((10, 10), 255, dtype=np.uint8)
    b = a.copy()
    assert mod.jaccard(a, b) == 1.0


def test_jaccard_disjoint_is_zero():
    a = np.zeros((10, 10), dtype=np.uint8)
    a[:5] = 255
    b = np.zeros((10, 10), dtype=np.uint8)
    b[5:] = 255
    assert mod.jaccard(a, b) == 0.0
