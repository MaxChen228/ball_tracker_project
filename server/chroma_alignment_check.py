"""Pixel-level BT.601 vs BT.709 chroma diff — closes the pixel layer of
the iOS↔server alignment scorecard (CLAUDE.md).

Why this tool exists
--------------------
iOS detection runs `cv::cvtColorTwoPlane(Y, UV, COLOR_YUV2BGR_NV12)`,
which is hardcoded to BT.601 limited-range coefficients. iPhone's
H.264 video stream is tagged BT.709 (verified: `colorspace=1` on every
session MOV). Server `frame.to_ndarray(format='bgr24')` goes through
libswscale, which honours the stream tag → uses BT.709.

So on the SAME source NV12, the two pipelines reconstruct BGR with
different matrices. Existing tooling (`dry_run_live_vs_server.py`) only
sees centroid (px, py) drift, not channel values. This tool measures the
chroma drift directly at ball pixels and reports HSV gate agreement.

Two modes
---------
  --synthetic
        Tabulate canonical color swatches (deep-blue, tennis-yellow,
        white, mid-gray) under both matrices. No MOV needed. Establishes
        an upper bound on per-channel offset for the gates currently in
        use, and serves as a regression baseline (the BT.601↔BT.709
        relationship is fixed math; if these numbers ever change, the
        regression is in our code, not the standard).

  --session SID
        Decode the session MOV, walk every server-detected frame, extract
        a ROI (default 21 px square) at the centroid, apply both BT.601
        and BT.709 to the SAME decoded YUV, and report HSV channel
        deltas + (optionally) gate-mask agreement against a named preset.

Decoupled from `detect_pitch` on purpose: this measures color matrix
drift, not detection logic. The codec DCT loss is identical for both
paths (we apply the matrices to the SAME server-decoded YUV), so it
cancels out — what remains is purely the colorspace-matrix delta.

Usage:
    uv run python chroma_alignment_check.py --synthetic
    uv run python chroma_alignment_check.py --session s_55731532
    uv run python chroma_alignment_check.py --session s_55731532 --preset blue_ball
    uv run python chroma_alignment_check.py --session s_55731532 --max-frames 50
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
VIDEO_DIR = DATA_DIR / "videos"
REPORT_DIR = DATA_DIR / "alignment_reports"

EXIT_OK = 0
EXIT_NOT_FOUND = 4

# ---------- BT.709 limited-range YCbCr → BGR (numpy) -------------------------
#
# OpenCV's COLOR_YUV2BGR_NV12 is BT.601 limited-range, integer-coefficient,
# byte-identical to what iOS production runs. For BT.709 we apply the
# canonical floating-point matrix from ITU-R BT.709 (limited range — the
# stream signals video range / colorspace=1).
#
#   Y'  = (Y  - 16) / 219
#   Cb' = (Cb - 128) / 224
#   Cr' = (Cr - 128) / 224
#
#   R = Y' + 1.5748 * Cr'
#   G = Y' - 0.1873 * Cb' - 0.4681 * Cr'
#   B = Y' + 1.8556 * Cb'
#
# We deliberately implement this in numpy (rather than asking libswscale)
# so the comparison is matrix-only — no swscale chroma-upsample artefacts
# beyond nearest-neighbour leak into the diff. The libswscale path used
# in production (`frame.to_ndarray(format='bgr24')`) is also reported
# separately in --session mode so the operator can see how close our
# pure-math 709 is to swscale's 709.

_BT709_LIMITED_MATRIX = np.array(
    [
        # row order: B, G, R (OpenCV BGR convention) so the output is
        # directly stack-able into a uint8 BGR ndarray
        [1.0,  1.8556,  0.0],
        [1.0, -0.1873, -0.4681],
        [1.0,  0.0,     1.5748],
    ],
    dtype=np.float32,
)


def yuv_nv12_to_bgr_bt709(y: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Limited-range BT.709 NV12→BGR in pure numpy. Nearest-neighbour
    chroma upsample to match what cv::cvtColorTwoPlane does, so any
    residual diff vs `bgr_601` is purely matrix coefficients."""
    h, w = y.shape
    if uv.shape[:2] != (h // 2, w // 2):
        raise ValueError(f"UV plane shape {uv.shape} does not match Y {y.shape}")

    # Nearest-neighbour upsample U,V to full res. UV interleaved (h/2,w/2,2):
    # plane 0 = Cb (U), plane 1 = Cr (V).
    cb = uv[:, :, 0].repeat(2, axis=0).repeat(2, axis=1)
    cr = uv[:, :, 1].repeat(2, axis=0).repeat(2, axis=1)

    yp = (y.astype(np.float32) - 16.0) / 219.0
    cbp = (cb.astype(np.float32) - 128.0) / 224.0
    crp = (cr.astype(np.float32) - 128.0) / 224.0

    yuv_stack = np.stack([yp, cbp, crp], axis=-1)
    bgr = yuv_stack @ _BT709_LIMITED_MATRIX.T
    return np.clip(bgr * 255.0, 0.0, 255.0).astype(np.uint8)


def yuv_nv12_to_bgr_bt601(y: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Production iOS path — byte-identical to `BallDetector.mm`'s call.
    The integer math inside cv::cvtColorTwoPlane gives ±1 unit rounding
    vs a numpy reproduction; using cv2 keeps the diff exact."""
    return cv2.cvtColorTwoPlane(y, uv, cv2.COLOR_YUV2BGR_NV12)


# ---------- shared frame extraction helpers ---------------------------------


def split_nv12(nv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """PyAV `frame.to_ndarray(format='nv12')` returns a (H*1.5, W) uint8
    buffer — the standard NV12 layout. Split into (Y, UV-interleaved-2ch)."""
    if nv.ndim != 2:
        raise ValueError(f"NV12 buffer must be 2D, got shape {nv.shape}")
    h_full, w = nv.shape
    if h_full % 3 != 0:
        raise ValueError(f"NV12 buffer height {h_full} not divisible by 3")
    h = h_full * 2 // 3
    y = nv[:h, :]
    uv = nv[h:, :].reshape(h // 2, w // 2, 2)
    return y, uv


# ---------- HSV mask agreement ----------------------------------------------


@dataclass
class HSVRangeLite:
    h_min: int
    h_max: int
    s_min: int
    s_max: int
    v_min: int
    v_max: int

    def mask(self, hsv: np.ndarray) -> np.ndarray:
        lo = np.array([self.h_min, self.s_min, self.v_min], dtype=np.uint8)
        hi = np.array([self.h_max, self.s_max, self.v_max], dtype=np.uint8)
        return cv2.inRange(hsv, lo, hi)


def load_preset_range(preset_name: str) -> HSVRangeLite:
    """Read the disk-canonical preset and pull just the HSV bounds.

    Imports `presets` lazily so --synthetic mode runs without server
    package state (smoke tests / CI without `data/`)."""
    import presets  # type: ignore
    p = presets.load_preset(DATA_DIR, preset_name)
    return HSVRangeLite(
        h_min=p.hsv.h_min, h_max=p.hsv.h_max,
        s_min=p.hsv.s_min, s_max=p.hsv.s_max,
        v_min=p.hsv.v_min, v_max=p.hsv.v_max,
    )


def jaccard(a: np.ndarray, b: np.ndarray) -> float | None:
    """Mask agreement (intersection over union). Returns None on empty
    union — neither matrix put any pixel in-gate, no signal to compare."""
    inter = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    if union == 0:
        return None
    return inter / union


# ---------- synthetic swatches mode -----------------------------------------


# Canonical NV12 limited-range samples covering the gates this project
# actually uses. Each entry is a single (Y, Cb, Cr) triple expanded into
# a 2×2 NV12 block. Picked to land near real preset hue centers:
#  - deep_blue : matches the project ball (h≈108 in OpenCV space)
#  - tennis_yellow_green : tennis preset center (h≈40)
#  - white / black / mid_gray : achromatic (Cb=Cr=128) — matrix-invariant,
#       used as a sanity row (must show ~0 delta in all three modes)
#  - red_safety : a saturated red (chroma triangle corner) to expose the
#       widest possible 601 vs 709 hue offset
_SWATCHES: list[tuple[str, int, int, int]] = [
    ("white",                 235, 128, 128),
    ("mid_gray",              126, 128, 128),
    ("black",                  16, 128, 128),
    ("deep_blue",              50, 220,  90),
    ("tennis_yellow_green",   170, 100, 110),
    ("red_safety",            100, 100, 220),
]


def synthetic_table() -> list[dict[str, Any]]:
    """For each canonical Y/Cb/Cr triple, compute BT.601 vs BT.709 BGR/HSV
    and the per-channel delta. Pure math — the only source of variation
    is the choice of matrix."""
    rows: list[dict[str, Any]] = []
    for name, y_val, cb_val, cr_val in _SWATCHES:
        # 2×2 minimal NV12 block (OpenCV requires h,w both even for
        # cvtColorTwoPlane).
        y = np.full((2, 2), y_val, dtype=np.uint8)
        uv = np.zeros((1, 1, 2), dtype=np.uint8)
        uv[0, 0, 0] = cb_val
        uv[0, 0, 1] = cr_val

        bgr_601 = yuv_nv12_to_bgr_bt601(y, uv)
        bgr_709 = yuv_nv12_to_bgr_bt709(y, uv)
        hsv_601 = cv2.cvtColor(bgr_601, cv2.COLOR_BGR2HSV)
        hsv_709 = cv2.cvtColor(bgr_709, cv2.COLOR_BGR2HSV)

        b6, g6, r6 = (int(c) for c in bgr_601[0, 0])
        b7, g7, r7 = (int(c) for c in bgr_709[0, 0])
        h6, s6, v6 = (int(c) for c in hsv_601[0, 0])
        h7, s7, v7 = (int(c) for c in hsv_709[0, 0])

        # Wrap H delta so saturated hues near 0/179 don't print ±177
        # for what's mathematically a 3-unit offset.
        d_h = h6 - h7
        if d_h > 90: d_h -= 180
        elif d_h < -90: d_h += 180
        rows.append({
            "name": name,
            "y": y_val, "cb": cb_val, "cr": cr_val,
            "bgr_601": [b6, g6, r6],
            "bgr_709": [b7, g7, r7],
            "hsv_601": [h6, s6, v6],
            "hsv_709": [h7, s7, v7],
            # iOS - server: positive means iOS reads higher than server.
            "d_h": d_h,
            "d_s": s6 - s7,
            "d_v": v6 - v7,
        })
    return rows


def render_synthetic(rows: list[dict[str, Any]]) -> str:
    out = ["# Synthetic BT.601 (iOS) vs BT.709 (server) on canonical NV12 swatches",
           "",
           "Both matrices applied to the SAME (Y, Cb, Cr) byte triple — pure",
           "matrix-coefficient delta, no codec / swscale path involved.",
           "",
           "| swatch | Y/Cb/Cr | BGR 601 | BGR 709 | HSV 601 | HSV 709 | Δh | Δs | Δv |",
           "|--------|---------|---------|---------|---------|---------|----|----|----|"]
    for r in rows:
        out.append(
            f"| {r['name']:20s} | {r['y']},{r['cb']},{r['cr']} | "
            f"{r['bgr_601']} | {r['bgr_709']} | "
            f"{r['hsv_601']} | {r['hsv_709']} | "
            f"{r['d_h']:+d} | {r['d_s']:+d} | {r['d_v']:+d} |"
        )
    out.append("")
    out.append(
        "Δh is in OpenCV hue units (0-179, each unit ≈ 2°). Achromatic rows "
        "(white / mid_gray / black) must read all-zero — Cb=Cr=128 makes both "
        "matrices reduce to the same Y → R=G=B linear ramp. Non-zero on those "
        "rows = bug in this script."
    )
    return "\n".join(out)


# ---------- session empirical mode ------------------------------------------


@dataclass
class SessionStat:
    sid: str
    cam: str
    n_frames: int
    n_skipped_oob: int
    # per-channel pixelwise diffs over all ROI pixels of all sampled frames
    d_b: list[float] = field(default_factory=list)
    d_g: list[float] = field(default_factory=list)
    d_r: list[float] = field(default_factory=list)
    d_h: list[float] = field(default_factory=list)
    d_s: list[float] = field(default_factory=list)
    d_v: list[float] = field(default_factory=list)
    # mask agreement Jaccard at the ROI per frame, when --preset given
    mask_jaccards: list[float] = field(default_factory=list)
    only_601_pixels: int = 0  # pixels in-gate under 601 but not 709
    only_709_pixels: int = 0
    both_pixels: int = 0


def _stats(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "abs_max": None}
    a = np.asarray(xs, dtype=np.float64)
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "p50": float(np.median(a)),
        "p95": float(np.quantile(a, 0.95)),
        "abs_max": float(np.abs(a).max()),
    }


def find_session_pitches(sid: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in PITCH_DIR.glob(f"session_{sid}_*.json"):
        cam = p.stem.split("_")[-1]
        out[cam] = p
    return out


def _build_frame_index(pitch_obj: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Map server-side frame_index → (px, py) for every detected frame."""
    out: dict[int, tuple[float, float]] = {}
    for f in pitch_obj.get("frames_server_post") or []:
        if not f.get("ball_detected"):
            continue
        if f.get("px") is None or f.get("py") is None:
            continue
        out[int(f["frame_index"])] = (float(f["px"]), float(f["py"]))
    return out


def _resolve_video_path(sid: str, cam: str) -> Path | None:
    p = VIDEO_DIR / f"session_{sid}_{cam}.mov"
    return p if p.exists() else None


def measure_session_cam(
    sid: str,
    cam: str,
    pitch_path: Path,
    video_path: Path,
    *,
    roi_half: int,
    max_frames: int | None,
    preset_range: HSVRangeLite | None,
) -> SessionStat | None:
    import av  # heavy: only imported in this mode

    pitch_obj = json.loads(pitch_path.read_text())
    centroids = _build_frame_index(pitch_obj)
    if not centroids:
        print(f"[{sid}/{cam}] no detected frames in frames_server_post — "
              f"skipping. (Need centroid coords to extract a ball ROI; run "
              f"`reprocess_sessions.py` first if the session has live "
              f"detections only.)", file=sys.stderr)
        return None

    target_indices = sorted(centroids)
    if max_frames is not None and max_frames < len(target_indices):
        # uniform sample to keep early-arc / late-arc coverage rather than
        # always grabbing the first N (which clusters at the release point)
        step = len(target_indices) / max_frames
        target_indices = [target_indices[int(i * step)] for i in range(max_frames)]
    target_set = set(target_indices)

    stat = SessionStat(sid=sid, cam=cam, n_frames=0, n_skipped_oob=0)

    container = av.open(str(video_path))
    try:
        s = container.streams.video[0]
        s.thread_type = "AUTO"
        for i, frame in enumerate(container.decode(s)):
            if i not in target_set:
                continue
            cx, cy = centroids[i]
            ix, iy = int(round(cx)), int(round(cy))

            nv = frame.to_ndarray(format="nv12")
            y, uv = split_nv12(nv)
            h, w = y.shape

            x0, x1 = ix - roi_half, ix + roi_half + 1
            y0, y1 = iy - roi_half, iy + roi_half + 1
            if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
                stat.n_skipped_oob += 1
                continue

            bgr_601 = yuv_nv12_to_bgr_bt601(y, uv)
            bgr_709 = yuv_nv12_to_bgr_bt709(y, uv)

            roi_601 = bgr_601[y0:y1, x0:x1]
            roi_709 = bgr_709[y0:y1, x0:x1]

            # diffs in int16 to allow negatives
            d_bgr = roi_601.astype(np.int16) - roi_709.astype(np.int16)
            stat.d_b.extend(d_bgr[..., 0].ravel().tolist())
            stat.d_g.extend(d_bgr[..., 1].ravel().tolist())
            stat.d_r.extend(d_bgr[..., 2].ravel().tolist())

            hsv_601 = cv2.cvtColor(roi_601, cv2.COLOR_BGR2HSV)
            hsv_709 = cv2.cvtColor(roi_709, cv2.COLOR_BGR2HSV)
            d_hsv = hsv_601.astype(np.int16) - hsv_709.astype(np.int16)
            # OpenCV hue is 0-179 (mod 180). On a saturated red the wraparound
            # can produce ±179 as a mathematical 1-unit offset. Normalize.
            d_h_raw = d_hsv[..., 0].ravel()
            d_h_wrap = np.where(d_h_raw > 90, d_h_raw - 180,
                       np.where(d_h_raw < -90, d_h_raw + 180, d_h_raw))
            stat.d_h.extend(d_h_wrap.tolist())
            stat.d_s.extend(d_hsv[..., 1].ravel().tolist())
            stat.d_v.extend(d_hsv[..., 2].ravel().tolist())

            if preset_range is not None:
                m601 = preset_range.mask(hsv_601)
                m709 = preset_range.mask(hsv_709)
                j = jaccard(m601, m709)
                if j is not None:
                    stat.mask_jaccards.append(j)
                stat.both_pixels += int(np.count_nonzero(m601 & m709))
                stat.only_601_pixels += int(np.count_nonzero(m601 & ~m709))
                stat.only_709_pixels += int(np.count_nonzero(~m601 & m709))

            stat.n_frames += 1
    finally:
        container.close()
    return stat


def render_session_stat(stat: SessionStat, *, preset_name: str | None) -> str:
    out = [f"## {stat.sid} cam {stat.cam}", ""]
    out.append(
        f"frames sampled = {stat.n_frames}  oob skipped = {stat.n_skipped_oob}"
    )
    if stat.n_frames == 0:
        out.append("")
        out.append("No usable frames — every detected ball ROI fell off-frame "
                   "(ball at edge with too-large `--roi`?). Try `--roi 5`.")
        return "\n".join(out)
    total_pix = len(stat.d_b)
    out.append(f"ROI pixels analysed = {total_pix:,}")
    out.append("")
    out.append("| channel | n | mean Δ | p50 Δ | p95 |Δ| | max |Δ| |")
    out.append("|---------|--:|------:|------:|------:|------:|")
    for label, vals in [
        ("B", stat.d_b), ("G", stat.d_g), ("R", stat.d_r),
        ("H", stat.d_h), ("S", stat.d_s), ("V", stat.d_v),
    ]:
        st = _stats(vals)
        out.append(
            f"| {label} | {st['n']} | {st['mean']:+.2f} | "
            f"{st['p50']:+.2f} | {st['p95']:.2f} | {st['abs_max']:.0f} |"
        )
    out.append("")
    out.append("Sign convention: Δ = (BT.601 iOS) − (BT.709 server). "
               "Positive ΔH means iOS reads higher hue than server "
               "on the same source.")
    if preset_name is not None:
        out.append("")
        out.append(f"### Gate-mask agreement ({preset_name})")
        if not stat.mask_jaccards and stat.both_pixels == 0:
            out.append(
                f"No ROI pixels passed the {preset_name} gate under either "
                "matrix at any sampled frame. Either wrong preset for this "
                "session's ball, or ROI does not intersect the ball.")
        else:
            jstat = _stats(stat.mask_jaccards)
            out.append(
                f"per-frame Jaccard: n={jstat['n']} mean={jstat['mean']:.3f} "
                f"p50={jstat['p50']:.3f} p95={jstat['p95']:.3f}"
            )
            total_either = (stat.both_pixels
                            + stat.only_601_pixels
                            + stat.only_709_pixels)
            out.append(
                f"pooled pixel-class counts: both={stat.both_pixels:,}  "
                f"only-601={stat.only_601_pixels:,}  "
                f"only-709={stat.only_709_pixels:,}  "
                f"(total in-gate union = {total_either:,})"
            )
            if total_either > 0:
                only601_pct = 100.0 * stat.only_601_pixels / total_either
                only709_pct = 100.0 * stat.only_709_pixels / total_either
                out.append(
                    f"→ if iOS switched to BT.709 today, {only601_pct:.1f}% of "
                    f"its current in-gate pixels would drop out, and "
                    f"{only709_pct:.1f}% of currently-out pixels would join."
                )
    return "\n".join(out)


def session_stat_to_json(stat: SessionStat, *, preset_name: str | None) -> dict:
    out = {
        "session_id": stat.sid,
        "camera_id": stat.cam,
        "n_frames": stat.n_frames,
        "n_skipped_oob": stat.n_skipped_oob,
        "channel_delta_iOS_minus_server": {
            "B": _stats(stat.d_b), "G": _stats(stat.d_g), "R": _stats(stat.d_r),
            "H": _stats(stat.d_h), "S": _stats(stat.d_s), "V": _stats(stat.d_v),
        },
    }
    if preset_name is not None:
        out["gate_agreement"] = {
            "preset": preset_name,
            "per_frame_jaccard": _stats(stat.mask_jaccards),
            "pooled_both_pixels": stat.both_pixels,
            "pooled_only_601_pixels": stat.only_601_pixels,
            "pooled_only_709_pixels": stat.only_709_pixels,
        }
    return out


# ---------- CLI -------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Quantify BT.601 (iOS) vs BT.709 (server) chroma drift at the "
            "pixel level. Closes pixel layer of the alignment scorecard."
        )
    )
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument(
        "--synthetic", action="store_true",
        help="tabulate canonical color swatches under both matrices",
    )
    sub.add_argument(
        "--session",
        help="session id (e.g. s_55731532) — empirical at ball ROIs",
    )
    p.add_argument(
        "--preset", default=None,
        help="preset name (e.g. blue_ball, tennis) for HSV gate agreement",
    )
    p.add_argument(
        "--roi", type=int, default=10,
        help="ROI half-side around the centroid in px (default 10 → 21×21)",
    )
    p.add_argument(
        "--max-frames", type=int, default=None,
        help="cap frames sampled per cam (uniform spread, default = all)",
    )
    return p


def run_synthetic() -> int:
    rows = synthetic_table()
    print(render_synthetic(rows))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / "chroma_synthetic.json"
    out_path.write_text(json.dumps({"rows": rows}, indent=2))
    print(f"\nwrote {out_path}")
    # Sanity check the achromatic rows — defensive guard against future
    # accidental edits to the matrix constants. Achromatic Cb=Cr=128 must
    # be matrix-invariant.
    for r in rows:
        if r["cb"] == 128 and r["cr"] == 128:
            if r["d_h"] != 0 or r["d_s"] != 0 or r["d_v"] != 0:
                print(
                    f"\nERROR: achromatic swatch {r['name']!r} shows non-zero "
                    f"delta {r['d_h']}/{r['d_s']}/{r['d_v']} — matrix bug.",
                    file=sys.stderr,
                )
                return 1
    return EXIT_OK


def run_session(
    sid: str, *, roi_half: int, max_frames: int | None, preset: str | None
) -> int:
    sid = sid if sid.startswith("s_") else f"s_{sid}"
    pitches = find_session_pitches(sid)
    if not pitches:
        print(f"[{sid}] no pitch JSON found under {PITCH_DIR}", file=sys.stderr)
        return EXIT_NOT_FOUND

    preset_range = load_preset_range(preset) if preset else None

    print(f"# Chroma alignment empirical — session {sid}")
    print()
    if preset:
        pr = preset_range
        assert pr is not None
        print(f"preset = `{preset}`  HSV gate = "
              f"h[{pr.h_min},{pr.h_max}] s[{pr.s_min},{pr.s_max}] "
              f"v[{pr.v_min},{pr.v_max}]")
        print()

    stats: list[SessionStat] = []
    for cam in sorted(pitches):
        video_path = _resolve_video_path(sid, cam)
        if video_path is None:
            print(f"[{sid}/{cam}] MOV not found in {VIDEO_DIR} — skipping",
                  file=sys.stderr)
            continue
        stat = measure_session_cam(
            sid, cam, pitches[cam], video_path,
            roi_half=roi_half, max_frames=max_frames,
            preset_range=preset_range,
        )
        if stat is None:
            continue
        stats.append(stat)
        print(render_session_stat(stat, preset_name=preset))
        print()

    if not stats:
        return EXIT_NOT_FOUND

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"chroma_{sid}.json"
    out_path.write_text(json.dumps({
        "session_id": sid,
        "roi_half_px": roi_half,
        "max_frames": max_frames,
        "preset": preset,
        "cameras": [session_stat_to_json(s, preset_name=preset) for s in stats],
    }, indent=2))
    print(f"wrote {out_path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.synthetic:
        if args.preset:
            print("--preset is ignored in --synthetic mode (no MOV decode, "
                  "no ROI to gate)", file=sys.stderr)
        return run_synthetic()
    return run_session(
        args.session,
        roi_half=args.roi,
        max_frames=args.max_frames,
        preset=args.preset,
    )


if __name__ == "__main__":
    sys.exit(main())
