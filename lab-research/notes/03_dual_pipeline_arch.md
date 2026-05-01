# Dual-pipeline on-device detection architecture

iPhone 14 (A15 Bionic, ANE 11.8 TOPS, 5-core GPU on non-Pro / 5-core
Pro split, 6 CPU cores 2P+4E). 240 fps binned 1920×1080 (format[39],
hFOV 73.83°) is the only sensible production capture path; format[22]
720p binned exists as a degraded mode. NV12 VideoRange 4:2:0 bi-planar.
**Per-frame budget at 240 fps = 4.16 ms**; "200 fps target" effectively
means ~5 ms/frame headroom, "100 fps degrade" means 10 ms.

Baselines, measured / documented (not the task's optimistic numbers):

- Current production iOS detector (`ball_tracker/BallDetector.mm`,
  HSV+CC+shape gate, full 1080p, single-core via
  `ConcurrentDetectionPool` serial worker): **~15 ms/frame**
  per `docs/ios.md:44`, effective throughput **~66 detections/s**,
  `maxBacklog=8` → ~74% drop rate at 240 fps.
- V11 Python on M1 = 1.96 ms/frame — **does not transfer to iPhone 14**;
  M1 has more cache + wider SIMD than A15 P-core. Use 15 ms as the
  iOS starting point, not 1 ms.
- The task brief said `ZeroCopyHSVDetector.mm`; the actual file is
  `BallDetector.mm` and the zero-copy NV12 plumbing is already in
  via `cv::cvtColorTwoPlane` (Y + UV stride-aware Mats, no stitching
  buffer). The remaining cost lives in HSV inRange + CC labeling, not
  in the colorspace conversion entry.

The architectural question is therefore "how do we get from 15 ms to
≤5 ms (live) without losing the >5 ms research-quality detector
(post)" — not "how do we extract more from a 1 ms detector."

## 1. Pipeline split

**Live (≤5 ms hard, drop on overrun, no buffering beyond capture
delivery):**

- HSV + CC + shape gate, **but parallelized** across both P-cores via
  `ConcurrentDetectionPool` with `nWorkers = 2` (currently 1).
  Capture is serialized on `camera.frame.queue` but detection
  enqueue is fan-out: even-indexed frames go to worker 0, odd to
  worker 1. Each worker keeps its own `thread_local CVScratch`
  (already done).
- Optional fast-path: drop live to 720p binned (format[22]) — same
  hFOV, ¼ the pixels (1280·720 vs 1920·1080), HSV+CC scales roughly
  linearly with pixel count → ~4 ms/frame on a single P-core. This
  is the **degraded mode** trigger, not the default.
- Output: `BlobCandidate[]` over WS as today (`type: "frame"`).
- Live does **not** invoke the ANE. Live owns latency; ANE owns
  accuracy (post path).

**Post (≤30 ms/frame soft, drained from ring buffer asynchronously
on E-cores + ANE):**

- Tiny FCN heatmap (≤500 K params, distilled from SAM2 GT — the
  931-frame label set already in `lab-research/`) on Core ML
  targeting `MLComputeUnits.cpuAndNeuralEngine`. Argmax + sub-pixel
  parabolic refine.
- Optional motion verifier (multi-frame ring-buffer consistency
  check) folded in as a CPU post-filter — only added if Phase 3
  shows residual false positives.
- Output: separate candidate list, distinct time index, shipped
  later (see § 5).

| Mode    | Live budget | Live detector            | Post budget | Post detector            |
|---------|-------------|--------------------------|-------------|--------------------------|
| 200 fps | 5.0 ms      | HSV+CC, 1080p, 2 workers | 30 ms      | Tiny FCN @ ANE (1080p)   |
| 100 fps | 10 ms       | HSV+CC, 720p, 1 worker   | 50 ms      | Tiny FCN @ ANE + verifier|

**Why this split.** Live's job is operator preview + downstream
pairing latency budget; "good enough" is the V11 HSV+CC point on the
existing recall curve (0.687→0.868 vs PROD per
`01_final_report.md`). Post's job is ground-truth-grade: it is
allowed seconds, so it pays the ANE warmup + heatmap argmax cost to
catch the cases HSV misses (motion blur, shadow side, partial
occlusion). The two paths are structurally different *algorithms* on
the same frame stream — that's the point.

## 2. Frame buffering

NV12 1920×1080 = 1920·1080·1.5 = **3,110,400 bytes ≈ 3.11 MB/frame**.
At 240 fps, 1 s of buffered frames = ~750 MB — far past anything we
can keep around. The ring buffer must be **bounded and drained
fast**.

- **Buffer size: 60 frames = 250 ms = ~187 MB**. This is the hard
  cap. Sized so that one missed pitch (~150 ms flight + slack)
  remains recoverable even if the ANE worker stalls briefly.
- **Storage: `CVPixelBufferRef` retains, not pixel copies.** This
  keeps zero-copy semantics into Core ML
  (`MLFeatureValue(pixelBuffer:)`). Trade-off: the buffers come from
  AVCaptureSession's IOSurface pool, which has a fixed depth (~10–
  15 buffers in practice). **Holding 60 references will exhaust the
  pool and AVCaptureSession will silently drop captures.**
  Mitigation: shadow-copy the Y plane only (1920·1080 = 2.07 MB) and
  the UV plane (518 KB) into a pre-allocated slab; release the
  CVPixelBuffer immediately. Total slab = 60 · 2.59 MB ≈ 155 MB,
  pinned at app start. Live path still gets the original
  CVPixelBuffer (no copy in the hot path).
- **Drain SLA: post worker must consume within 250 ms of arrival**
  or the slot gets overwritten and that frame's post detection is
  forfeited. Forfeit is logged as a counter in the cycle payload —
  silent loss is forbidden by project rule.
- **Backpressure policy: drop post frames, never live.** If the
  post worker is behind, the oldest unprocessed slab slot is
  overwritten by the newest capture. Live continues unaffected.
  This is the inverse of the standard FIFO — research-grade output
  is recoverable from server_post (the MOV is still being
  recorded), so dropping post frames degrades to "operator must
  rerun server_post to fill the gap." Dropping live frames would
  break operator preview.

The ring buffer lives on a dedicated GCD queue
(`post.ringbuffer.queue`, serial, QoS `.utility`) so it never
contends with the live `camera.frame.queue` (QoS `.userInteractive`).

## 3. iOS framework — Core ML direct, with `MLComputeUnits.cpuAndNeuralEngine`

**Recommended**: Core ML directly (no Vision wrapper), feeding the
CVPixelBuffer via `MLFeatureValue(pixelBuffer:)`, model compiled with
`MLModelConfiguration.computeUnits = .cpuAndNeuralEngine`.

Rationale:
- **Zero-copy in.** `MLFeatureValue(pixelBuffer:)` accepts a
  CVPixelBuffer of NV12 / BGRA / OneComponent8 and Core ML's
  preprocessing layer does the colorspace conversion + normalization
  on-GPU/ANE without bouncing through CPU memory. Vision's
  `VNCoreMLRequest` re-wraps in `VNImageRequestHandler` which
  re-decodes orientation and can copy.
- **Scheduling control.** With raw Core ML we own the dispatch
  queue; we can pin the inference call to a dedicated post worker
  and avoid contending with capture. Vision uses an internal pool
  whose contention with `AVCaptureVideoDataOutput` is opaque.
- **Warmup explicit.** First Core ML inference on ANE is **100s of
  ms** (model load, ANECompiler JIT, weight upload). We warm at app
  launch with a dummy black NV12 buffer, well before arm; Vision
  hides this and the first armed pitch eats the cost.

Rejected:
- **Vision (`VNCoreMLRequest`)**: wraps Core ML, adds preprocessing
  copy, hides warmup, harder to schedule against the capture queue.
  Net negative for this pipeline; Vision is the right tool when you
  want Apple's built-in detectors (face, text, body pose) — we
  don't.
- **Metal Performance Shaders / MPSGraph**: forces us to hand-write
  the FCN graph in MPS primitives, allocate `MTLTexture`s, manage
  command buffers. We get nothing the ANE doesn't already give us
  for a <500 K param model — and we lose ANE acceleration entirely
  (MPS targets the GPU). Right tool for custom shader work
  (e.g. a hand-tuned HSV kernel), wrong tool for a stock FCN.
- **"Direct ANE"**: the ANECompiler / Espresso APIs are private.
  No production path exists outside Core ML.

Memory: a <500 K param FP16 FCN = ~1 MB weights, plus working
tensors. Fits easily; ANE itself has ~8 MB SRAM that holds the
working set. Not a constraint at this size.

## 4. Capture handoff

The current path is already zero-copy on the live side
(`BallDetector.mm:303` `mapPixelBufferToBGR` reads NV12 planes
in-place via `cv::cvtColorTwoPlane`). The only copy is HSV-space
output, which is local scratch.

For the post path:

- The capture callback (`captureOutput`) gets one `CMSampleBuffer`,
  retained by AVFoundation's IOSurface pool.
- Live worker pool (`ConcurrentDetectionPool`) takes the
  CVPixelBuffer out via `CMSampleBufferGetImageBuffer` and runs
  HSV+CC+gate on it directly. **No copy.**
- Post ring-buffer ingestion runs on the same callback thread:
  before the buffer is released back to the pool, copy Y + UV
  planes into the pre-allocated slab slot. **One copy, ~2.6 MB,
  ~0.3 ms** with `memcpy` against pinned aligned memory.
- Core ML inference reads from the slab via
  `CVPixelBufferCreateWithBytes` with `kCVPixelBufferLock_ReadOnly`
  — wraps the slab bytes as a transient CVPixelBuffer that Core ML
  consumes. No additional copy on the inference side.
- `ClipRecorder` (H.264 MOV) keeps consuming the original
  `CMSampleBuffer` exactly as today — H.264 encoder is hardware
  fixed-function and gets the buffer via VideoToolbox's own
  IOSurface handoff.

The cost is one ~0.3 ms `memcpy` per frame on the capture thread.
Acceptable inside the live 5 ms budget because HSV+CC runs on a
*different* core (the worker pool); the capture thread itself is
mostly idle waiting for the next sample.

## 5. Merge semantics

`PitchPayload` and the WS `frame` schema today carry one candidate
list per frame, no path discriminator at the wire layer (the path
identity comes from which channel it arrived on: WS frames →
`live`, server-post on the MOV → `server_post`). The on-device post
path is a **third path**, not a flavor of `live`.

**New required field on `PitchPayload`: `frames_post_iphone:
list[FramePayload]`**. Same `BlobCandidate` shape as `frames_live`,
but distinct because:

1. **Different selector cost.** FCN heatmap argmax cost ≠ HSV
   shape-prior cost. Mixing into `frames_live` pollutes the live
   selector's pairing math (which assumes shape-prior weights).
2. **Different time base.** FCN runs out-of-order from the ring
   buffer drain; post candidates arrive on a different schedule
   from the live WS stream. The `i` (frame index) field bridges
   them — server merges by `(camera_id, session_id, frame_index)`.
3. **Research integrity.** The whole point of this project's
   experimental-phase rule is no silent fallback — if FCN found
   the ball where HSV missed, the operator needs to *see* that
   delta in a separate viewer pill, exactly like
   `live` vs `server_post` today.

Per project rule "no Optional / no default" for new fields:
`frames_post_iphone` is required when the iOS build advertises
post-path capability via a new `hello` field
`supports_post_iphone: bool`. Builds without the capability send
empty list; builds with it send the actual frames. Server reads
the bool and stamps `paths` accordingly.

Wire-up at the server: add `post_iphone` to `DetectionPath`
(`server/schemas.py`), persist into pitch JSON, render as a third
viewer pill set, fold into `SessionResult.segments` on a per-path
basis like `live` and `server_post` already do. The audit
checklist in `docs/protocols.md` § "Operator audit checklist"
needs to cover the new `post_iphone` send/receive sites.

## 6. Thermal / power

I have no measured iPhone 14 data for sustained 240 fps capture +
H.264 encode + ANE inference + 2-worker HSV pool. **Don't fabricate
numbers.** What we know structurally:

- 240 fps capture alone is already the hottest sustained workload
  the device has, even before adding ANE.
- ANE on a <500 K param FP16 model at 240 Hz target rate is
  unusual — most Core ML deployments run at 30 Hz. ANE workloads
  amortize over batch; calling it 240×/s may keep the engine pegged.
- H.264 hardware encode is on a separate fixed-function block
  (VideoToolbox / AMX adjacent), but shares the SoC thermal budget.

**Degradation ladder** (apply in order as `ProcessInfo.thermalState`
escalates):

| ThermalState | Action |
|---|---|
| `.nominal` / `.fair` | Both paths full quality. |
| `.serious` | **Drop post path entirely.** Stop draining the ring buffer; stop ANE inference. Live continues at 1080p / 2 workers. Operator gets a dashboard chip indicating "post degraded — retune from MOV server-side." |
| `.critical` | **Drop live to 720p / 1 worker.** MOV recording continues so server_post is still recoverable. |
| `.critical` sustained > 30 s | Surface a hard error to the operator — the SoC will throttle capture itself shortly anyway. |

The ladder is an ordered policy; trigger thresholds (specifically
how long to dwell in `.serious` before dropping post, and whether to
preemptively drop on battery <15%) are **TODOs that need real
field measurement** before being committed in code.

## 7. Risks and failure modes

1. **ANE first-inference latency.** 100s of ms cold. Warm at app
   launch with a dummy black 1920×1080 NV12 buffer; never let arm
   hit a cold model.
2. **IOSurface pool exhaustion.** If post path holds CVPixelBuffer
   refs in the ring buffer, AVCaptureSession's pool drains and
   capture silently drops. The Y+UV slab copy in § 4 avoids this
   — confirm by stress-testing with `os_signpost` on
   `AVCaptureVideoDataOutput.didDrop`.
3. **Concurrent ANE + H.264 encode contention.** Both go through
   media-engine adjacent silicon (different blocks but shared
   thermal + memory bus). H.264 must not yield — server_post is
   the recovery path. If ANE pressures encoding, prioritize encode
   and skip ANE inference for that frame; this is naturally what
   the ring-buffer drop policy in § 2 already does.
4. **Core ML model size on disk.** A 500 K param FP16 model = ~1 MB
   compiled, easy. But if distillation produces a larger student
   model, the `.mlmodelc` package can balloon — pin a CI check on
   the compiled model size, fail the build over (say) 5 MB.
5. **HSV→FCN input format mismatch.** FCN trained on RGB, capture
   delivers NV12. Core ML's preprocessing layer can do this if the
   `.mlmodel` declares `imageInputType: ColorSpace.RGB` with a
   `BGR/RGB` conversion descriptor — must verify the BT.601 vs
   BT.709 matrix choice matches what the training pipeline used
   (training on server-decoded MOV → BT.709, capture stack →
   BT.601 — see `docs/architecture.md` § "Color-matrix gap").
   If trained on server-decoded BT.709 frames, the on-device
   inference will see a hue-shifted input. Solve at training time
   by re-rendering the GT through the iOS BT.601 path (`cv2`
   COLOR_YUV2BGR_NV12) before training, **not** by patching at
   inference.
6. **Worker fan-out reordering.** If even/odd frames go to two
   workers, output order is no longer monotonic in `frame_index`.
   The WS dispatcher must serialize on `frame_index` before
   shipping, or the server's `live_pairing` buffer order assumption
   breaks.

## 8. Phased rollout

Each phase has a single judgment criterion to advance.

**Phase 1 — parallelize the existing detector.** Bump
`ConcurrentDetectionPool.nWorkers` to 2; add the frame-index
serializer at the WS dispatch boundary. Measure
`droppedFrameCount` over a 60 s sustained 240 fps recording.
**Advance criterion:** drop rate < 5 % at 240 fps with 2 workers,
or stay at < 10 % at 720p / 2 workers (degraded mode).

**Phase 2 — ring buffer + drain harness, no DL yet.** Add the Y+UV
slab, the post-ring-buffer queue, an instrumented "post worker"
that just memcpy-counts (no detection). Verify no AVCapture drops
under sustained load, measure actual drain latency p50 / p95 / p99.
**Advance criterion:** zero AVCapture drops attributable to
IOSurface pool exhaustion over a 5-minute soak; drain p99 < 100 ms.

**Phase 3 — Tiny FCN distilled from SAM2 GT.** Train a <500 K
param FCN on the existing 931-GT-frame label set. Convert to
Core ML, compile for ANE, warm at app launch. Wire into the post
worker. Add `frames_post_iphone` schema. **Advance criterion:**
on-device post recall ≥ V11 HSV recall + 5 pp on a held-out
session, p99 inference < 30 ms on iPhone 14 ANE.

**Phase 4 — motion verifier as post-stage filter.** Only if
Phase 3 shows false-positive rate that hurts pairing — a stateless
FCN can fire on background distractors that HSV's color gate
naturally excluded. Add ring-buffer-based 3-frame motion
consistency as a post-FCN filter. **Advance criterion:** Phase 3
post path contributes net positive to triangulated point quality
(measure: `SessionResult.fit_rmse_m` distribution shift on
re-fit). If Phase 3 is already net-positive, skip Phase 4.

Each phase ships independently; failure of any phase to meet its
advance criterion is a stop signal — fall back to the prior
phase's behaviour rather than papering over a regression.
