# Hue & colour-space — SoT

This is the **single source of truth** for OpenCV hue convention, iOS↔server
colour-matrix gap, and the BT.601 vs BT.709 decision. Conflict with other
docs → this file wins.

## OpenCV hue is 0-179 (not 0-360)

`cv2.cvtColor(..., COLOR_BGR2HSV)` produces uint8. Hue caps at 255 in the
container but only 0-179 is used (each unit = 2°). S and V are standard
0-255.

| Standard 0-360°    | OpenCV 0-179            |
|--------------------|-------------------------|
| Blue 210-250°      | **105-125** (deep-blue) |
| Yellow-green 50-110° | **25-55** (tennis)    |

### Pitfalls

- Dashboard DETECTION · HSV card rejects 210 with "must be ≤ 179". That
  is an OpenCV constraint, not a UI bug.
- `/detection/hsv` endpoint and `State.set_hsv_range` clip to `[0, 179]`
  for hue and `[0, 255]` for S/V.
- When the operator says "set hue to 210" — clarify whether they mean
  OpenCV or standard 0-360. UI / image-editor 0-360 values must be
  halved before storing.

## iOS↔server colour-matrix gap

iOS converts NV12 → BGR via `cv::COLOR_YUV2BGR_NV12`, which hard-codes
the **BT.601** YUV→RGB matrix. server_post decodes H.264 through the
system AVFoundation / libswscale stack which honours the **BT.709**
matrix tag carried in the bitstream (encoder writes BT.709 for HD).
Same chroma samples → different RGB → different HSV.

### Decision: leave unaligned

Quantified 2026-04-30 via `server/chroma_alignment_check.py`. The effective
hue offset is **≤ 3 OpenCV units** (not the earlier ~3–4 estimate).

#### Synthetic (pure matrix math)

| swatch              | Δh   | Δs  | Δv   |
|---------------------|------|-----|------|
| `deep_blue`         | 0    | 0   | -8   |
| `tennis_yellow_green` | +1 | +5  | +11  |
| `red_safety` (saturated) | -3 | +3 | -10 |

**Deep-blue is unaffected by matrix choice on hue** — only V is slightly
lower.

#### Empirical (real session ball ROI)

`s_55731532` (tennis), 100 frames, 21×21 ROI:

| stat | Δh         | Δs    | Δv   |
|------|------------|-------|------|
| mean | +2.03      | -4.98 | +5   |
| p50  | +2         | -5    | +5   |
| p95  | abs=3      | —     | —    |
| max  | 11         | —     | —    |

- Tennis-preset gate-mask agreement: per-frame Jaccard mean=0.974,
  p50=0.994, p95=1.000.
- Switching server to BT.709 → 1.9% in-gate pixels drop out, 0.2%
  out-of-gate pixels join. **Detection behaviour essentially unchanged.**

#### Conclusion

The 6-8° estimate was over-cautious. Real-world offset is ~4° (2 OpenCV
units), and both `blue_ball` and `tennis` presets have enough margin
to absorb it. **Leave BT.601 (iOS) + BT.709 (server) unaligned.**
Alignment changes carry their own risk.

## When to revisit

Re-run `server/chroma_alignment_check.py` whenever:

- A new HSV preset's hue width is < 6 OpenCV units (e.g. fluorescent
  yellow 25-30 is too narrow to absorb a 2-unit drift).
- iOS chip or OS major upgrade — NV12 capture pipeline details may
  change.
- libswscale upgrade — server-side could silently switch to BT.601
  giving 100% overlap, but verify.

## How to apply (when changing HSV / colour pipeline)

### Tweaking only the HSV preset (not the capture stack)

```bash
uv run python server/chroma_alignment_check.py --synthetic
```

Inspect the new colour swatch's Δh/Δs/Δv. Expect ≤ 3 OpenCV units.

### Real-device verification

```bash
uv run python server/chroma_alignment_check.py --session <sid> --preset <name>
```

Target Jaccard mean ≥ 0.95.

### Hue-offset suddenly jumps

When swapping device / lens:

1. Re-run `chroma_alignment_check`.
2. Inspect MOV stream tag:
   ```python
   av.open(path).streams.video[0].codec_context.colorspace
   ```
   to confirm no third colourspace silently appeared.

## DCT gap (separate physical-layer gap)

There's a second iOS↔server divergence that is **not** the colour matrix:
H.264's lossy DCT quantization perturbs luma + chroma in patches around
block edges, shifting low-area HSV mask boundaries. The live WS path
skips encoding entirely (raw NV12 → BGR → detect). This is the gap the
original "DCT-only" framing referred to. `server/dry_run_live_vs_server.py`
reports per-pitch centroid Δpx as a downstream observable; it does NOT
attribute the delta between the matrix gap and the DCT gap.

## How to apply (live vs server_post mismatch)

When live and server_post detection diverge on the same session:

1. **Do not start with HSV.** Run
   `server/dry_run_live_vs_server.py --session <sid>` to quantify
   centroid Δ.
2. Decide whether it's the BT.709 offset (this file) or DCT loss (above).
3. Same-frame comparison: **iOS is closer to ground truth** (upstream
   lossless). Detection rate across time windows cannot be directly
   compared (iOS live streams from arm to disarm; server_post processes
   the full MOV).
4. Timestamp alignment precision is ≈ ±1.67 ms (MOV time_base 1/600);
   pairing window is 8 ms.
