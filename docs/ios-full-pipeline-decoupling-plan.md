# iOS Full-Pipeline Parity And Capture-Decoupling Plan

## Context

The current iOS on-device detection path does not match the server's authoritative `detect_pitch()` path.

Today there are two independent mismatches:

1. Input mismatch
   - iOS detection runs on live `CVPixelBuffer` BGRA samples inside `CameraViewController.captureOutput`.
   - Server detection runs on the finalized H.264 MOV after PyAV decode.
2. Execution mismatch
   - iOS currently calls stateless `BTBallDetector.detect(...)` from `dispatchDetectionIfDue(...)`.
   - Server runs HSV + MOG2 + warmup + morphology close + connected-components + shape gate in `server/pipeline.py`.

Those two mismatches produce both count drift and identity drift:

- `s_50e743fc`
  - A: server `492` ball frames vs on-device `175`
  - B: server `524` ball frames vs on-device `103`
  - dominant cause: iOS detection dispatch under-samples the recording
- `s_a95bfcdd`
  - A: server `468` ball frames vs on-device `331`
  - B: server `455` ball frames vs on-device `24`
  - causes: severe under-sampling on B, plus A-side false positives from the stateless HSV-only path

Historical data in `server/data` shows this is systemic rather than a one-off:

- A-camera median `frames_on_device / server_frames` ratio: `0.492`
- B-camera median `frames_on_device / server_frames` ratio: `0.091`
- `s_a95bfcdd B` is the 2nd worst frame-sampling ratio in the sampled dual-mode history (`57 / 1190 = 0.048`)

## Decision

The authoritative on-device result must move to a post-recording analysis job over the finalized local MOV, not the live capture callback.

This is the only design that satisfies all three requirements at once:

1. Match the server pipeline as closely as possible
2. Keep recording isolated from analysis back-pressure
3. Preserve a low-latency operator preview

## First-Principles Constraints

### 1. Capture is a real-time deadline system

At 240 fps, the camera budget is about `4.17 ms/frame`.

Any path that allows detection latency to feed back into capture will eventually force one of these outcomes:

- dropped analysis samples
- capture stalls
- unstable frame cadence
- thermal collapse under sustained load

Recording must therefore be treated as a protected plane with a hard rule:

> No authoritative per-frame detection result may depend on synchronous progress inside the live capture callback.

### 2. "Same algorithm" is not enough

If iOS runs the same math over a different image source, the result still diverges.

Server truth is defined over:

- MOV bitstream
- PyAV decode order
- decoded BGR frames
- `frame_index` based on decode order

If iOS wants parity, the iOS authority path must also operate on:

- the finalized local MOV
- deterministic local decode order
- BGR frames derived from that MOV

### 3. Preview and authority have different optimization targets

Preview wants:

- low latency
- acceptable quality
- latest-frame semantics
- aggressive frame dropping under back-pressure

Authority wants:

- deterministic coverage
- reproducible indexing
- stable timestamps
- full pass over the clip

Those goals conflict. They must not share the same execution contract.

## Target Architecture

Split the iPhone pipeline into three planes.

### 1. Capture Plane

Responsibilities:

- acquire camera frames
- write H.264 MOV
- maintain session clock
- stamp `video_start_pts_s`
- stay isolated from analysis latency

Rules:

- no authoritative ball detection in `captureOutput`
- no dependency on detector completion for recording lifecycle
- no frame queue growth from downstream analysis

### 2. Preview Plane

Responsibilities:

- drive HUD/debug overlays
- provide low-latency remote preview to dashboard
- support operator framing / exposure / HSV tuning

Rules:

- advisory only
- may downsample aggressively
- may skip frames freely
- "latest frame wins", never queue stale preview work

Recommended implementation:

- keep the existing preview upload concept
- continue to prefer dropped frames over queueing
- optionally add lightweight live ball overlay later, but keep it separate from authoritative output

### 3. Analysis Plane

Responsibilities:

- decode the finalized local MOV
- run the full server-equivalent detection pipeline
- produce authoritative `frames_on_device`
- optionally produce a local annotated MOV for diagnostics

Rules:

- runs only after clip finalization
- independent worker / queue from capture
- deterministic decode order
- `frame_index` defined by decode order, not dispatch order

This plane becomes the sole source of truth for on-device detection.

## Detector Parity Requirements

The analysis-plane detector must match server semantics, not current iOS semantics.

Required features:

1. Same HSV threshold defaults and runtime overrides
2. Same area bounds
3. Same aspect and fill gates
4. Same MOG2 configuration
5. Same warmup behavior
6. Same morphology-close stage
7. Same output shape: one `FramePayload` per decoded frame
8. Same null-detection behavior for frames with no accepted blob

The existing `BTDetectionSession` is not sufficient on its own because it still runs on live `CVPixelBuffer` input and is bounded by capture-time scheduling. The right long-term shape is a shared detector core reused by:

- server decode pipeline
- iOS post-recording analysis pipeline

## Shared-Core Strategy

The cleanest implementation is to move detector logic into a shared native core.

### Preferred direction

Create a shared C++ / Obj-C++ detector module that owns:

- HSV mask build
- MOG2 apply
- morphology close
- connected-components
- shape gating

Server options:

- near term: keep Python orchestration and call into the shared native module for frame-level detection
- transitional option: keep server pipeline in Python but add parity tests against the shared core

iOS options:

- decode MOV locally with AVAssetReader or VideoToolbox-backed path
- convert decoded frames to BGR
- call the same shared detector core

If full shared-core adoption is too large for one PR, phase it:

1. Build iOS analysis plane first using an iOS-native detector that matches `server/pipeline.py`
2. Add parity fixtures and golden tests
3. Collapse to a shared native detector after behavior is proven

## Data-Flow Changes

### Current

`captureOutput` -> detection dispatch -> `frames_on_device` buffer -> cycle complete -> upload

### Target

`captureOutput` -> MOV finalize -> persist cycle metadata -> enqueue analysis job -> local MOV decode -> full pipeline -> authoritative `frames_on_device` -> upload or patch session

This has one important consequence:

`on-device` can no longer assume that authoritative frame data exists at the exact moment recording stops.

That is a feature, not a bug. The system should explicitly model "video persisted, analysis pending".

## Wire / API Evolution

The current payload contract assumes a cycle is uploaded in one shot. That is too rigid for a decoupled authority path.

Recommended API evolution:

### Option A: Two-step session ingest

1. Upload raw MOV + session metadata immediately after recording
2. Upload on-device analysis results later as a second request keyed by `(camera_id, session_id)`

Suggested endpoint:

- `POST /pitch_analysis`

Body:

- `camera_id`
- `session_id`
- `video_start_pts_s`
- `frames_on_device`
- optional diagnostics metadata:
  - decode fps
  - analysis duration
  - detector version

Server behavior:

- store late-arriving `frames_on_device`
- recompute `points_on_device` when both A and B analyses exist
- never block raw MOV ingest on analysis completion

### Option B: Local defer, single upload

1. Record locally
2. Run local analysis
3. Upload MOV + `frames_on_device` together only after analysis finishes

This is simpler on the server but weaker operationally:

- slower time-to-first-upload
- more fragile if analysis is interrupted
- larger loss window if the app crashes after recording

Recommendation: choose Option A.

## iOS Module Changes

### New components

1. `LocalVideoAnalysisJob`
   - immutable job spec for one completed recording
   - points to local MOV and payload metadata
2. `LocalVideoAnalyzer`
   - decodes finalized MOV
   - runs full detector pipeline
   - returns authoritative `FramePayload[]`
3. `AnalysisQueue`
   - durable queue for pending analysis jobs
   - independent retry / resume behavior
4. `AnalysisResultStore`
   - persists completed on-device analysis until acknowledged by server
5. `PreviewDetector` or `PreviewOverlayEngine`
   - optional lightweight live detector for HUD only

### Existing code to simplify

`CameraViewController`

- remove authoritative meaning from `detectionFramesBuffer`
- remove `detectionCallIndex` as a source of truth
- stop treating capture-time detector output as upload-ready analysis data
- keep only lightweight preview work inside capture callback

`PitchRecorder`

- continue owning session-level timing metadata only
- do not grow frame-level responsibilities back into this class

`ServerUploader`

- add explicit upload path for late analysis attachment
- keep MOV upload independent from analysis completion

## Server Changes

1. Accept late `frames_on_device`
2. Recompute `points_on_device` independently of `points`
3. Distinguish these lifecycle states in UI / API:
   - raw video uploaded
   - server detection complete
   - on-device analysis pending
   - on-device analysis complete
4. Preserve source labels:
   - `server`
   - `on_device_postpass`
   - optionally `preview_advisory` if surfaced for diagnostics

## Rollout Plan

### Phase 0: Observability

Add measurements before changing behavior:

- capture fps
- MOV writer drops
- analysis enqueue latency
- analysis wall-clock duration
- local decode fps
- preview fps
- detector parity counters versus server

Success criterion:

- enough telemetry to distinguish capture issues from analysis issues

### Phase 1: Harden capture / preview separation

- strip authoritative semantics from live detection buffer
- keep or simplify preview-only detector
- ensure preview never queues stale work

Success criterion:

- capture remains stable with preview enabled

### Phase 2: Add post-recording analysis plane on iOS

- finalize clip
- persist job
- decode local MOV
- run full pipeline locally
- emit authoritative `frames_on_device`

Success criterion:

- on-device frame coverage is decode-order complete
- no dependence on live detection dispatch rate

### Phase 3: Add late-analysis upload path

- upload raw MOV first
- attach `frames_on_device` later
- recompute `points_on_device` server-side

Success criterion:

- recording/upload path no longer waits on local analysis

### Phase 4: Parity validation

For a fixed set of archived sessions, compare:

- total decoded frames
- detected-ball frame count
- per-frame centroid deltas
- paired-frame counts within 8 ms window
- triangulated count and residual distribution

Required gold sessions should include:

- `s_50e743fc`
- `s_a95bfcdd`
- at least one clean high-count session
- at least one clutter-heavy false-positive session

Success criterion:

- parity close enough that remaining drift is explainable by decode-stack differences, not architecture differences

### Phase 5: Shared detector core

- extract common native detector
- reuse across server and iOS
- retain golden parity tests

Success criterion:

- behavior changes land once and are testable from both sides

## Validation Matrix

### Functional

- iOS capture still records full MOV under sustained use
- local analysis resumes after app restart
- late analysis upload rehydrates server state correctly
- viewer overlays both sources correctly

### Performance

- no regression to capture cadence
- preview remains low latency under recording-adjacent load
- analysis throughput acceptable for expected clip lengths

### Accuracy

- frame-count parity versus server improves materially over current dual-mode results
- false-positive clusters like `s_a95bfcdd A` are suppressed by the full pipeline
- under-sampling failures like `s_a95bfcdd B` disappear because authority no longer depends on capture-time dispatch

## Non-Goals

- replacing the HSV pipeline with ML
- changing triangulation math
- changing sync-anchor semantics
- making preview authoritative

## Risks

1. Local decode differences versus PyAV
   - mitigation: parity fixtures and golden-session comparison
2. Analysis backlog growth on-device
   - mitigation: durable queue, explicit status, retry policy, optional thermal gating
3. Server complexity from late-arriving analysis
   - mitigation: additive endpoint, source-tagged recomputation
4. Implementation sprawl across Swift, Obj-C++, and Python
   - mitigation: phased rollout and early shared-core boundary definition

## Recommended PR Sequence

Do not ship this as one giant refactor. Use a staged PR stack:

1. telemetry and status plumbing
2. preview-only cleanup in capture path
3. local post-recording analysis plane
4. late-analysis server ingest
5. parity test corpus
6. shared detector core consolidation

## Final Recommendation

Adopt a three-plane design:

- capture plane for recording integrity
- preview plane for operator immediacy
- analysis plane for authoritative on-device parity

The key architectural decision is:

> authoritative on-device detection must be generated from the finalized local MOV in a post-recording analysis plane, then attached to the session asynchronously.

That is the highest-leverage change because it removes both major root causes at once:

- live-callback under-sampling
- mismatch between live BGRA analysis and server MOV-decode analysis
