# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System overview

**iOS-decoupling status (Phase 1-5 complete)**: the dashboard at `/` is the sole control surface. Per-session operator flow requires zero iPhone touches: enable preview per cam → **Auto calibrate** (server-side ArUco) → either dashboard **Calibrate time** (legacy single-listener chirp trigger) or `/setup` **Run mutual sync** (two-device chirp exchange) → **Arm session** → throw ball → event auto-captured. iOS UI is display-only status (`AutoCalibrationViewController` kept as field fallback; no local time-sync button; no Manual 5-handle path). Per-camera intrinsics + homography live in `data/calibrations/<cam>.json` (authoritative — iOS no longer echoes on pitch uploads). HSV / chirp threshold / heartbeat interval / extended markers are server-persisted and pushed over the WS settings path.

Two-iPhone stereo tracker. The default HSV targets a yellow-green tennis ball; the rig actually runs a deep-blue ball via the `blue_ball` preset (`data/hsv_range.json`). Presets: `tennis` / `blue_ball` + custom range available from the dashboard's DETECTION · HSV card. Each phone runs the iOS app in role `A` or `B`, aimed at home plate. Both phones share time by jointly detecting an **audio chirp** (played from a third device) as a common sync anchor. The server pairs A/B frames within an 8 ms window of anchor-relative time and triangulates 3D positions via ray-midpoint.

### Two detection paths (live always on, server_post on-demand)

Every armed session runs the `live` path unconditionally and archives the MOV for every camera — `Session.paths` snapshots `{live}` at arm time; `server_post` is triggered post-hoc on whichever session the operator cares about via the events-row **Run server** button. The legacy `CaptureMode` enum + `capture_mode` wire field are both gone (#85 phase 1).

- **`live`** (always on) — iOS runs HSV + connectedComponents + shape-gate per-frame on raw BGRA and streams each `{type: "frame", ...}` over `/ws/device/{cam}`. `live_pairing.py` buffers A/B arrivals and triangulates incrementally; viewer / dashboard see points before the session has ended.
- **`server_post`** (on-demand) — iOS *always* records an H.264 MOV (PR61 made this unconditional; `CameraViewController.captureOutput` bootstraps `ClipRecorder` on first sample regardless of the arm message's `paths`). The MOV is uploaded to `data/videos/session_{sid}_{cam}.mov` via `POST /pitch`. Server-side HSV detection only runs when the operator hits `POST /sessions/{sid}/run_server_post` from the events list (or the CLI `reprocess_sessions.py --session s_xxxx`). 20–60 MB per camera on disk; 8–20 s detection cost paid on-demand.

The `ios_post` path (iOS re-decoded MOV locally, uploaded via `/pitch_analysis`) was removed — `live` fully subsumes it with lower latency and no tmp MOV churn.

HSV / area / shape constants are kept lock-step across both — see the header comment in `ball_tracker/BallDetector.mm` and the shared `server/detection.py` + `server/pipeline.py`. **Algorithms are byte-aligned** (HSV → connectedComponents → shape gate → temporal selector); the legacy MOG2 + 3×3 CLOSE prepass on `server_post` was removed so distillation against SAM 3 GT can fit a single set of HSV / shape_gate parameters that applies to both paths uniformly. The remaining divergence is **input asymmetry only**: BGRA 4:4:4 (iOS sensor direct) vs H.264-decoded BGR with chroma 4:2:0 + DCT quantization (server_post sees the encoded MOV). This is a physical-layer gap, not an algorithm gap. Viewer overlays each path independently (two pill sets, two A/B strip rows), so a path that found the ball where the other missed reads as a visible delta — and that delta now isolates the input gap rather than mixing algorithm + input differences.

### Components

- **iOS app** — `ball_tracker/` (Swift + Obj-C++, UIKit). Xcode project at `ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`, iOS 26.2 target, Swift 5.0. `LiveFrameDispatcher` streams per-frame detections over WS and `ClipRecorder` writes the MOV — both run unconditionally on every armed session. `currentSessionPaths` is updated dynamically from the server's WS arm message via `CameraCommandRouter.applyPushedPaths`; `CameraRecordingWorkflow` reads it to gate the `waitForDetectionDrain` step (only fires when paths contains `.live`), so it is real behaviour state, not vestige.
- **Server** — `server/` (FastAPI + `python-multipart` + PyAV + OpenCV). `uv`-managed venv at `server/.venv` (Python 3.13). In-memory `State` plus `data/` persistence (`pitches/`, `results/`, `videos/`); a restart reloads enriched pitch JSONs (both frame buckets preserved) and re-triangulates. `POST /pitch` is multipart; the `video` part arrives on every real iOS upload (post-PR61 ClipRecorder is unconditional), though tests can omit it when supplying pre-computed `frames_server_post` for synthetic scenes. `/pitch` just archives the MOV; server-side HSV detection is on-demand via `POST /sessions/{sid}/run_server_post`. Live frames arrive independently over `WS /ws/device/{cam}` and are paired by `live_pairing.LivePairingSession` without touching `/pitch`. Events + viewer tag each historical session by looking for a MOV on disk under `data/videos/session_<sid>_*`.

## Physical setup (current)

Nominal rig used by the operator — actual per-session pose still comes from the homography solved on-device; these values are just the target the rig is built against:

- **Camera**: iPhone 14-17 series, rear **main (1x wide) camera** only (`builtInWideAngleCamera`). Ultra Wide (0.5x, 120° FOV) is rejected — 5-coefficient distortion can't model it cleanly and the edges lose angular resolution.
- **Resolution**: default 1920×1080 (16:9); runtime capture selection is 1080p or 720p. Calibration always bakes at 1080p and the server rescales intrinsics + homography per-pitch to the MOV's actual pixel grid via `pairing.scale_pitch_to_video_dims`. ChArUco calibration JSON is auto-scaled from the 4032×3024 source on import.
- **Orientation**: **landscape** on both phones. Sensor long-edge aligned with the pitcher→plate horizontal direction. ChArUco intrinsic-calibration shots must be taken in the same orientation.
- **Baseline**: two phones placed ~3 m from home plate, both on the **first-base / third-base line** (i.e. 1B-side phone and 3B-side phone, aimed inward at the plate). This is a wide cross-baseline stereo setup — good depth separation for triangulation.
- **Focus**: lock AF (`setFocusModeLocked`) to the plate distance both during ChArUco capture and during live recording. The main cam has OIS but static mounting keeps its drift negligible.
- **Extrinsics** are NOT assumed from this geometry — every session still runs the Calibration screen (Auto ArUco or manual 5-handle) per phone to recover the real homography. The 3 m / 1B-3B numbers are rig targets, not priors fed into code.

## Commands

### Server
```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8765   # run (prints LAN IP → paste into iPhone Settings)
uv run pytest                                        # all tests (server + viewer)
uv run pytest test_server.py::test_triangulate_sweeps_ball_path   # single test
uv run python reprocess_sessions.py --since today                 # re-run detection + triangulation with current hsv_range.json over today's MOVs (also --session s_xxxx / --all / --dry-run)
```

The root URL `http://<server>:8765/` is the **dashboard** — a three-zone layout styled after the `PHYSICS_LAB` design system (warm-neutral palette, JetBrains Mono + Noto Sans TC, 1 px borders replacing shadows):

- 52 px top nav: `BALL_TRACKER` brand + live status strip (`Devices n · Cal n · Sync n/n`). Three chips only; no editorial headline.
- 440 px left sidebar: **Session** (`armed`/`idle` chip + `Arm` / `Stop` + `Quick chirp` + per-cam sync LEDs), **Detection HSV**, **Capture Tuning**, **Events**. The old Session Monitor card (per-cam fps / frames / live-pairs panel) was retired — during a live stream the operator only watches the 3D canvas; fps now surfaces per-path in the `/viewer/{sid}` cam cards as `L|n/m N fps` (effective fps = frames / duration).
- full-bleed right canvas: live Plotly 3D scene showing the plate mesh and whichever cameras have a calibration persisted — even before any pitch is uploaded. Drag to orbit.

Events rows are single-line: `sid · L|n · S|n · [status chip] · [Run srv | Cancel] · [Trash]`. "Run srv" POSTs to `/sessions/{sid}/run_server_post` which fires the same background detection as the legacy auto-path (MOVs are always archived now, so any session with a clip is eligible). The same button also lives in the viewer's top-right nav when the session hasn't had server detection run yet — displays `[server done]` chip instead once frames_server_post is populated.

Hydration: initial SSR paints every panel + canvas, then three JS ticks keep everything fresh — `/status` every 1 s (devices/session/nav strip), `/calibration/state` every 5 s (canvas repaint via `Plotly.react`), `/events` every 5 s (sidebar event list). Plotly.js is loaded once from CDN at the top of the document and shared across the canvas and `/viewer/{session_id}`; there is no build step. Arm flips the server into an armed session and starts dispatching `{cam: "arm"}` to online devices via `/status`; the first uploaded pitch auto-ends it (one-shot).

### iOS
Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

**Agent rule: do NOT run iOS tests via xcodebuild.** They take minutes per pass and the operator runs them manually in Xcode. Agents may run `xcodebuild ... build` to verify the iOS source compiles, but never `xcodebuild ... test`. If a refactor invalidates iOS test files, fix the test files for compilation only and let the operator run them.

## Architecture

### iOS state machine — `CameraViewController`
`.standby → .recording → .standby …`

Time-sync is a separate orthogonal flow: the operator taps 時間校正 on the phone OR clicks **Calibrate time** on the dashboard (`POST /sync/trigger` → WS `sync_command: "start"` plus pending flag on the next heartbeat tick → phone enters `.timeSyncWaiting` only if currently `.standby`). Either path flows `.timeSyncWaiting` → (chirp detected) → `.standby`. Not gated by arm; the server guards against dispatching the remote command during an armed session so a mis-click can't disrupt a recording.

Driven by a **1 Hz WS heartbeat tick** that runs from app launch and carries liveness / sync metadata upstream, while server commands come back over WS `settings` / `arm` / `disarm` / `sync_command` messages. Arming is dashboard-only — the iPhone UI has no local start/stop button:

- `commands[self] == "arm"` + state `.standby` → cache `response.session.id` as `currentSessionId`, `enterRecordingMode()` (switch to 240 fps, state = `.recording`, defer ClipRecorder creation to the first captureOutput so pixel dims come from the real sample)
- `commands[self] == "disarm"` + state `.recording` → `recorder.forceFinishIfRecording()` if the PitchRecorder kicked off (first sample appended); otherwise just `clipRecorder.cancel()` + `exitRecordingToStandby()`
- `commands[self] == "disarm"` + state `.timeSyncWaiting` → `cancelTimeSync(reason: "disarmed")`
- `lastAppliedCommand` guards against re-triggering on repeated replies during an armed session

iPhones never mint pairing identifiers — every `startRecording` reads `currentSessionId` (set from the WS arm message) and stamps it onto the outgoing `PitchPayload.session_id`. Uploads tagged with a superseded session are ignored by `_register_upload_in_session_locked` on the server, so a late flush can't disarm a new session.

The worst-case arm latency is therefore one heartbeat interval (default 1 s, clamped [1, 60]) **plus the ~300-500 ms fps-swap** cost. There is no `parkCameraInStandby` toggle — `.standby` always keeps the capture session live at `standbyFps` (60) so the preview keeps streaming; arming just swaps fps, never cold-starts the session. (`park_camera_in_standby` is in `AppSettingsStore.legacyKeys` — purged once per launch.)

State per mode:

- `.standby`: capture session is always live at `standbyFps` (60) — preview keeps streaming, sensor stays hot. No `parkCameraInStandby` toggle (retired; key in `AppSettingsStore.legacyKeys`). No recording, no chirp detection.
- `.timeSyncWaiting` (時間校正): session spun up at `standbyFps` so the mic can deliver samples; **chirp detector ON**. On trigger, saves `lastSyncAnchorTimestampS`, stops the session, and returns to `.standby`. 15 s timeout. Can only be entered from `.standby` via the manual 時間校正 button — not from an arm command.
- `.recording`: session spun up at `trackingFps` (240). Captures H.264 MOV via `ClipRecorder`. **Only exit path is `forceFinishIfRecording()`**, triggered when the dashboard sends `disarm` (operator pressed Stop) or the server-side session times out. No on-device detection, no auto-end. Emits `PitchPayload` via `onCycleComplete`; the clip finishes async before the payload is persisted. Session is stopped on the way back to standby. Persist + enqueue runs synchronously before the state transition flips back to `.standby`.
- Cycles are saved to disk in `Documents/pitch_payloads/` (`PitchPayloadStore`) **before** upload, as paired `<basename>.json` + `<basename>.mov`. Upload failures re-insert at queue front with 2 s backoff; `payloadStore.delete` removes the pair atomically on success.

### Frame pipeline (`CameraViewController.captureOutput`)
Runs on `camera.frame.queue` while the session is live — 60 fps during `.standby` / `.timeSyncWaiting` and 240 fps during `.recording`. The capture session is always running (no `parkCameraInStandby` toggle); `switchCaptureFps(_:)` is the mid-session fps swap (stop → reconfigure → start ~300-500 ms) used at every state transition. `startCapture(at:)` and `stopCapture()` remain as the bring-up / tear-down primitives.

FPS is hard-capped by an exposure ceiling: `configureCaptureFormat` sets `activeMaxExposureDuration = frameDuration`, so iOS's auto-exposure can't stretch individual samples past 1/60 s (idle) or 1/240 s (recording). AE compensates with ISO — noisier in low light, but frame rate holds. Without this cap a dim room silently dropped the effective capture rate to ~14 fps.

`captureOutput` advances `frameIndex` for debug logs, updates the HUD FPS estimate, and unconditionally fans the sample out to two sinks (path-set gating was retired alongside the legacy `CaptureMode` — every armed session now does both):

- `clipRecorder` is lazy-bootstrapped from the first sample's dims and appends every sample; the full MOV is uploaded as-is (no trim). Server-side detection (`server_post` path) is run on-demand against this archive.
- `LiveFrameDispatcher` ships each detection result as a WS `frame` message as soon as it's produced; the server pairs + triangulates the `live` path before the session ends.

`dispatchDetectionIfDue` runs on every sample — throttled to 60 Hz on a utility queue so the ~12-18 ms HSV + CC + shape pipeline (1080p full-frame, CPU) can't stall 240 fps capture. The throttle is the current ceiling: to push detection toward 240 Hz, shrink per-frame cost first (ROI tracking around last known blob, 720p capture, Metal/vImage HSV conversion) rather than lifting the throttle. `PitchRecorder` is kicked off on the first captured sample using the sample's session-clock PTS as `videoStartPtsS`, regardless of which paths are active.

### Audio pipeline (`AudioChirpDetector`)
Runs on `audio.chirp.queue`. `AVCaptureAudioDataOutput` delivers mic samples directly to the detector, which runs a normalized matched filter (cross-correlation against a reference chirp, divided by local window energy) at ~10 Hz. On a peak > `threshold`, parabolic sub-sample interpolation refines the location; the chirp-center session-clock PTS becomes the anchor. Audio sample rate 44.1 kHz → 22 μs per sample; detection precision typically **<100 μs** (40× better than the frame-granularity the earlier flash detector could hit).

The reference chirp is a linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, unit-energy normalized. The server's `/chirp.wav` endpoint emits the same waveform as a playable WAV surrounded by 0.5 s silence — users download it on any third device and play it near the two iPhones during 時間校正.

Threshold (default 0.18, tunable from Settings → Sync → Chirp Threshold) is the normalized matched-filter peak above which a detection fires. Peaks are in roughly 0–1 with 1.0 meaning a perfect reference match on a clean recording; typical field values are 0.2–0.4 on a nearby speaker, <0.05 on stationary ambient. Lower the threshold if the HUD flashes orange ("close") but never triggers; raise it if false-triggers on ambient noise. Hot-reload via `setThreshold(_:)` — no capture-session rebuild.

### Key modules
- `AudioChirpDetector.swift` — matched-filter chirp detection via `vDSP_dotpr`. Owns the audio ring buffer, reference chirp, cooldown, and emits `ChirpEvent(anchorFrameIndex, anchorTimestampS)` callbacks on its own queue. Threshold is mutable (`setThreshold(_:)`) so Settings changes propagate without session rebuild. Self-contained — no dependencies on other sync helpers.
- `PitchRecorder.swift` — thin bookkeeping between the camera VC's state machine and the upload queue. `startRecording(sessionId:, anchorTimestampS:, videoStartPtsS:, videoFps:)` records identity + timing metadata; `forceFinishIfRecording()` is the sole exit path (dashboard cancel / session timeout) and emits `PitchPayload` via `onCycleComplete`. No frames, no pre-roll — the phone uploads the MOV and server detection fills in per-frame data. `localRecordingIndex` is a run-of-app debug counter that ships on the payload as `local_recording_index` purely for operator logs.
- `ClipRecorder.swift` — `AVAssetWriter` wrapper that consumes the same `CMSampleBuffer`s the capture queue dispatches, writes H.264 MOV to a tmp URL, and finalises async on cycle-complete. `prepare → append (first append starts the writer session at that PTS AND records it as `firstSamplePTS`) → finish/cancel`. `firstSamplePTS` is what CameraViewController feeds into `PitchRecorder.startRecording` as `videoStartPtsS`, so the server can reconstruct absolute session-clock PTS for every decoded frame. Caller serialises all calls onto `processingQueue`; `exitRecordingToStandby` tears down via an async dispatch so it can't race with an in-flight append.
- `PitchPayloadStore.swift` — local cache for completed cycles. `save(payload, videoURL:)` writes `<basename>.json` atomically and moves the tmp clip to `<basename>.<ext>`. `videoURL(forPayload:)` resolves the companion clip for the uploader; `delete(jsonURL)` removes both. `makeTempVideoURL()` provides fresh `Documents/tmp/clip_<uuid>.mov` URLs for `ClipRecorder`.
- `ServerUploader.swift` — HTTP transport only. `uploadPitch(_, videoURL:, completion:)` posts `/pitch` as `multipart/form-data` (required `payload` JSON field + required `video` file part). `sendHeartbeat(cameraId:)` is the 1 Hz liveness + command channel that also carries arm/disarm commands and `session.id` in its reply. Multipart body is hand-built — no third-party HTTP lib. Contains no timers or retry logic (those live in the queue / monitor below).
- `ServerHealthMonitor.swift` — 1 Hz WS heartbeat scheduler with a configurable base cadence. Owns the "last contact: N s ago" 1 Hz tick timer and the `Server` HUD label. The camera VC uses it to emit periodic `{"type":"heartbeat"}` messages upstream; downstream settings / arm / disarm arrive over the same WS connection. Hot-reloadable: `updateUploader`, `updateCameraId`, `updateBaseInterval` + `probeNow()` applies a settings change without tearing down the `AVCaptureSession`.
- `PayloadUploadQueue.swift` — serialises cached pitch payloads up to the server, one at a time. `reloadPending()` on entering recording mode rebuilds the in-memory queue from `PitchPayloadStore`'s on-disk files so a restart's cached uploads resume. Upload success deletes the JSON + companion video via the store; failure re-inserts at the head with a 2 s retry. Callbacks fire on main.
- `CalibrationViewController.swift` — two paths to the same `homography_3x3` (row-major, 9 doubles, h33=1):
  - **Manual**: 5 draggable handles on home-plate pentagon → DLT via 8×8 normal equations with Gaussian elimination.
  - **Auto (ArUco)**: `BTArucoDetector` (OpenCV `cv::aruco`, Obj-C++ wrapper in `ArucoDetector.{h,mm}`) detects DICT_4X4_50 markers IDs 0–5 taped to plate landmarks (FL/FR/RS/LS/BT/MF), then `findHomographyFromWorldPoints:imagePoints:` solves via RANSAC least-squares.
  Also derives `fx/fy/cx/cy` from capture FOV. A Settings toggle can override intrinsics with externally computed ChArUco values including 5-coefficient distortion. Both save paths (manual Save + Auto ArUco Save) also POST the freshly-persisted `{camera_id, intrinsics, homography, image_{width,height}_px}` to the server's `/calibration` endpoint via `ServerUploader.postCalibration` — fire-and-forget, failures log only. This feeds the dashboard's calibration-preview canvas so each phone's pose appears immediately after Save.
- `SettingsViewController.swift` — persists only bootstrap settings (server IP/port, role A/B, plus optional manual intrinsics including OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`) to `UserDefaults`. Runtime tuning such as chirp threshold, heartbeat interval, capture resolution, tracking exposure cap, HSV, and capture mode is server-owned and pushed over WS `settings` messages with no capture-session rebuild unless the specific knob requires one. The Intrinsics section accepts `calibrate_intrinsics.py` JSON via `UIDocumentPickerViewController` and auto-crops/scales 4:3 → 1080p 16:9. `onDismiss` fires in `viewDidDisappear` (covers Save, Cancel, and interactive-swipe dismiss — the last is blocked via `isModalInPresentation = true`) — the presenter re-diffs settings there instead of polling.

### Server
- `schemas.py` — Pydantic types (`IntrinsicsPayload`, `BlobCandidate`, `FramePayload`, `PitchPayload`, `CalibrationSnapshot`, `TriangulatedPoint`, `BallisticSummary`, `SessionResult`, `DeviceIntrinsics`, `MarkerRecord` + draft/batch variants, `SyncReport` / `SyncResult` / `SyncRun` / `SyncLogEntry`) plus the `DetectionPath` / `TrackingExposureCapMode` enums and in-memory `Device` / `Session` / `StoredPitch` dataclasses. `CaptureMode` is gone; `paths_for_mode` / `mode_for_paths` were removed with it. `PitchPayload` carries two parallel frame buckets — `frames_live`, `frames_server_post` — each independently populated by the matching path. Server-populated `frames_server_post` is written after PyAV decode; `frames_live` arrives over WS and is persisted onto the pitch JSON by `state.persist_live_frames`. `FramePayload.candidates: list[BlobCandidate]` is the Phase B wire shape: iOS ships top-K HSV blobs per frame; the server runs `select_best_candidate` against a temporal prior to pick the winner that fills `px` / `py` / `area`. `CalibrationSnapshot` is the body of `POST /calibration`. `camera_id` is `^[A-Za-z0-9_-]{1,16}$`; `session_id` regex is `^s_[0-9a-f]{4,32}$` (server mints `s_` + 8 hex chars via `secrets.token_hex(4)`; 32-char upper bound is forward-compat slack). Both path-safe.
- `detection.py` — OpenCV-backed HSV blob extractor + multi-candidate scorer. `HSVRange.default()` is `25,55,90,255,90,255` (fluorescent yellow-green tennis ball — fallback for headless tests / first boot); the dashboard persists the live range in `data/hsv_range.json` via `POST /detection/hsv` (presets `tennis` / `blue_ball`, or six explicit ints). `HSVRange.from_env()` still reads `BALL_TRACKER_HSV_RANGE="hMin,hMax,sMin,sMax,vMin,vMax"` as a last-resort override for headless test runs. Hue is OpenCV's 8-bit convention (0-179, standard 0-360° halved). `detect_ball(bgr, hsv_range, ...)` runs `cv2.inRange` + `cv2.connectedComponentsWithStats`, applies the operator-tunable `ShapeGate` (aspect / fill thresholds, `data/shape_gate.json`), and feeds the surviving blobs to `candidate_selector.select_best_candidate` together with the prior position/velocity. The selector minimises `w_area·(1 - area_score) + w_dist·dist_cost` (weights / saturation / expected radius come from `data/candidate_selector_tuning.json`); the same selector is used by the live path in `live_pairing._resolve_candidates`. After detection, `chain_filter.annotate` walks each pitch's frame sequence and tags `filter_status ∈ {kept, rejected_flicker, rejected_jump}` per `data/chain_filter_params.json` (`max_jump_px`, `min_run_len`, `max_frame_gap`).
- `video.py` — PyAV decoder. `iter_frames(mov_path, video_start_pts_s) → Iterator[(absolute_pts_s, bgr)]`. Reconstructs absolute session-clock PTS: `absolute_pts_s = video_start_pts_s + (frame.pts − first_pts) * time_base`, so every decoded frame's timestamp sits on the same clock as `sync_anchor_timestamp_s`. `count_frames(path)` is a cheap second-pass counter used by tests.
- `pipeline.py` — glue. `detect_pitch(mov_path, video_start_pts_s, hsv_range?, frame_iter?, *, should_cancel?, shape_gate?, selector_tuning?, chain_filter_params?, progress?)` decodes the MOV and runs the same byte-aligned HSV → CC → shape gate → temporal selector pipeline as iOS-live (no MOG2, no morphology, no `expected_radius_px` — the geometry-derived radius prior was removed Apr 2026 because it silently rejected real balls when distance differed from the calibration assumption). Synthesises one `FramePayload` per decoded sample on the iOS session clock; runs `chain_filter.annotate` once at the end. Tests inject a stub iterator to avoid real decoding.
- `pairing.py` — `triangulate_cycle(a, b)` does A/B frame pairing within an 8 ms window of anchor-relative time (`timestamp_s − sync_anchor_timestamp_s`) and runs ray-midpoint triangulation. `_ray_for_frame` prefers the pixel path whenever `px`/`py` are present (server detection always produces them); zero-distortion is the default when `intrinsics.distortion` is absent, which is numerically equivalent to the legacy angle ray. Requires intrinsics + homography on both cameras.
- `triangulate.py` — `recover_extrinsics` decomposes `H = K [r1 r2 t]` (Zhang's planar method), orthonormalizes via SVD, flips sign if `t[2] < 0`. `triangulate_rays` solves the 2×2 system for the shortest segment between two 3D rays. `undistorted_ray_cam` applies OpenCV 5-coefficient undistortion.
- `reconstruct.py` — pure-geometry scene builder used by the viewer endpoints. `build_scene(session_id, pitches, triangulated)` returns a `Scene` with cameras, rays (tagged with `source` = `live` / `server` so the viewer can split them), ground traces per path (`ground_traces`, `ground_traces_live`), and triangulated points per path (`triangulated`). Viewer JS keeps the two pipelines independent via these per-path collections; they are never merged. `build_calibration_scene(calibrations)` builds the dashboard-canvas scene without rays. Both paths share `_camera_view_from_intrinsics_and_homography` so poses are byte-for-byte identical across `CalibrationSnapshot` and post-triangulation `PitchPayload`. **`t_rel_s` on rays and ground traces MUST share the same clock as `frames[*].t_rel_s` produced by `routes/viewer.py::_videos_for_session`** — both fall back to `video_start_pts_s` when `sync_anchor_timestamp_s` is None. A stale `or 0.0` fallback here would emit absolute PTS (~14681 s) while frames and the video element stay at anchor-relative (0–5 s), breaking viewer playback's scrubber→ray lookup.
- `chirp.py` — `chirp_wav_bytes()` builds the reference sync chirp WAV (2→8 kHz linear sweep, 100 ms, Hann-windowed, surrounded by 0.5 s silence) and is `@functools.lru_cache(maxsize=1)`'d so `GET /chirp.wav` reuses the exact bytes across requests.
- `render_scene.py` / `render_dashboard.py` — Plotly viewer + dashboard HTML. Same PHYSICS_LAB palette. Plotly.js loaded once from CDN; SSR + JS ticks handle hydration.
- `main.py` — FastAPI app + `State` class (thread-locked). WS handler `ws_device` lives in `routes/device_ws.py`. `State.pitches` keyed by `(camera_id, session_id)`; when both A and B for a session arrive, per-path triangulation runs (see `session_results.py`) and the `SessionResult` is cached in `State.results[session_id]`. The `/pitch` handler lives in `routes/pitch.py`: multipart `payload` is always required, `video` arrives on every real upload (iOS records unconditionally post-PR61) but the handler accepts frame-only payloads so tests stay slim. `/pitch` no longer kicks off server-side detection; it just archives the MOV. Operator-triggered `POST /sessions/{sid}/run_server_post` schedules HSV detection as a FastAPI BackgroundTask; candidate gate is "MOV on disk + `frames_server_post` empty + session not trashed", so historical live-only sessions can be re-run at any time. Live `frame` / `path_completed` messages on `WS /ws/device/{cam}` go through `state.ingest_live_frame` + `state.live_ray_for_frame`; `state.persist_live_frames` flushes the WS buffer onto the pitch JSON so a restart can replay. Other endpoints live under `routes/{sessions,calibration,markers,settings,sync,camera,viewer,pitch}.py` — grep `@router\.(get|post|delete)` there for the canonical list. Notable groups: `/detection/{hsv,shape_gate,candidate_selector,chain_filter}` (operator-tunable detection knobs, persisted under `data/`), `/settings/{chirp_threshold,mutual_sync_threshold,heartbeat_interval,tracking_exposure_cap,capture_height,sync_params}`, `/sessions/{sid}/run_server_post` (operator-triggered server-post detection), `/sync/*` (mutual sync coordinator), `/calibration/intrinsics/*` (per-device ChArUco upload). Removed: `/sessions/set_mode`, `/detection/paths` (legacy path-selection UI gone).
- `state_events.py` / `session_results.py` / `detection_paths.py` (extracted from `state.py` in commit `60c3a95`) — `state_events.py` builds the `/events` dashboard rows (per-session, per-pipeline counts + path status pills); `session_results.py::rebuild_result_for_session` is the authoritative constructor for `SessionResult`, coordinating per-path triangulation; `detection_paths.py` resolves which frame bucket (`frames_live` / `frames_server_post`) is authoritative for a given `(pitch, path)` pair. All three are pure read against `State` — no mutation.
- `routes/device_ws.py` — PR #85 phase 2 abstraction: contains the `ws_device` handler that processes `hello` / `heartbeat` / `frame` / `cycle_end` WS messages on `/ws/device/{cam}`. Pulled out of `main.py` so the FastAPI app file stays the wiring root.
- `live_pairing.py` — `LivePairingSession` buffers WS-streamed frame messages (`{cam, t_rel_s, frame_index, candidates: [{px, py, area, area_score, ...}], ...}` — Phase B wire shape) and pairs A/B within the 8 ms window on the fly. `_resolve_candidates` runs the shared `candidate_selector` against the per-cam temporal prior (last winner + velocity) so each cam's chosen blob is consistent with the server-side knobs in `data/candidate_selector_tuning.json`. The session's `tuning` field is hot-swapped by `state.ingest_live_frame` so dashboard slider updates apply on the next frame, no re-arm. Triangulated points are emitted as soon as the second cam's matching frame lands; the server broadcasts `path_completed` / triangulated-point events on the same WS channel so the dashboard can draw before the session ends.

### Dashboard control plane
**Single-lock model**: `state._lock` is a non-reentrant `Lock` that serialises all store access (~40 `with self._lock` blocks). Per-store facades on `State` (settings / calibration / runtime) are synchronisation boundaries, not vestigial pass-through — the underlying stores are deliberately not thread-safe in isolation, so deleting a facade and pointing callers at the inner store would silently lose synchronisation. `_marker_registry` / `_preview` carry their own internal locks separate from `_lock`. See the `class State` docstring in `state.py`.

`State` owns three pieces of in-memory state (all reset on server restart):

- **Device registry** (`_devices`) — `{camera_id: Device(last_seen_at)}`, updated by WS `hello` / `heartbeat` messages. `online_devices()` filters to beats within 3 s. Clock is injectable (`time_fn` ctor arg) so tests age devices in microseconds.
- **Session** (`_current_session` + `_last_ended_session`) — at most one armed `Session` at a time with a generated `s_xxxxxxxx` id, a `max_duration_s` auto-timeout (default 60 s), a snapshotted `paths: set[DetectionPath]` (taken at `POST /sessions/arm`, immune to mid-session dashboard edits), and a list of `camera_id`s uploaded during its armed window. `arm_session` is idempotent (double-click safe); `cancel_session` is 409 on idle for API callers but always 303 for HTML form callers so the dashboard button never looks broken. `current_session()` lazily applies the timeout on read — no background task.
- **Command dispatch** (`commands_for_devices()`) — derives `{camera_id: "arm"|"disarm"}` from session state: `arm` while armed, `disarm` for `_DISARM_ECHO_S` (5 s) after any end. The live iPhone path consumes these via WS `arm` / `disarm`; `/status` mirrors them for dashboard observability.

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Intrinsics naming**: server + iOS both use `fy` for the image-vertical focal length. The legacy `fz` field name (a historical collision from early iOS code) has been retired; `IntrinsicsPayload` still accepts `fz` as a read-time alias on `model_validate` so historical `data/calibrations/*.json` and old pitch JSONs still load cleanly. New code writes `fy`.
- **iOS side**: no longer persists intrinsics — ChArUco intrinsics are server-owned per device id under `data/calibrations/<cam>.json` (Phase 1 decoupling). The `intrinsic_*` UserDefaults keys referenced in older docs no longer exist in this codebase.

## Payload contract

`POST /pitch` is `multipart/form-data`. The `payload` part is always required; the `video` part is required only when the payload declares `server_post` in its `paths` (see `routes/pitch.py::_requires_video`). Calibration data at `data/calibrations/<camera_id>.json` is the **single source of truth** for intrinsics / homography / image dims — iOS no longer echoes them on uploads (Phase 1 decoupling).

- **`payload`** (`application/json`) — encoded `ServerUploader.PitchPayload` ↔ `main.PitchPayload`:

  ```
  camera_id: str matching ^[A-Za-z0-9_-]{1,16}$   # "A"/"B" in practice
  session_id: str matching ^s_[0-9a-f]{4,32}$     # server-minted via secrets.token_hex(4) → "s_" + 8 hex; sole pairing key (32-char upper bound is forward-compat)
  paths: list[DetectionPath]                      # snapshots session.paths at recording start. Post-redesign, session.paths is always {live} at arm time; server_post gets added only after an operator-triggered run.
  sync_anchor_timestamp_s: float | null           # chirp-detected session-clock PTS; null if skipped
  video_start_pts_s: float                        # abs PTS of first MOV frame (session clock)
  video_fps: float
  frames_live: list[FramePayload] | null          # populated only on recovery paths; normally arrives over WS
                                                  # FramePayload.candidates: list[BlobCandidate] is the Phase B wire shape;
                                                  # px/py/area get filled in by the server's candidate selector, not by iOS.
  local_recording_index: int?                     # device-local debug counter; server ignores
  ```

  Server-side, `/pitch` looks up the matching `CalibrationSnapshot` from `state.calibrations()` and fills in `intrinsics` / `homography` / `image_width_px` / `image_height_px` BEFORE triangulation and on-disk persistence. **No calibration on file → 422**.

- **`video`** (`video/quicktime`) — H.264 MOV. iOS uploads on every recording (PR61); `/pitch` stores it under `data/videos/session_{session_id}_{camera_id}.<ext>` without decoding. Decode + HSV detection (→ `frames_server_post`) happens only when the operator hits `POST /sessions/{sid}/run_server_post`.

- **Live path has no HTTP payload** — the always-on `live` pipeline produces WS `frame` messages on `/ws/device/{cam}` throughout the recording. `state.persist_live_frames(camera_id, session_id)` flushes the in-memory buffer onto the pitch JSON at `path_completed` (or session end) so reloads see the same two-bucket shape as an offline upload.

Pairing is by **`session_id` alone** (server-minted via `POST /sessions/arm`). iPhones never generate pairing identifiers.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present AND `sync_anchor_timestamp_s` non-null — if any is missing, `SessionResult.error` is set and triangulation is skipped (raw payload + MOV are still persisted for forensics).

## Degraded / fallback modes

- **No 時間校正 before arm**: `sync_anchor_timestamp_s` uploads as `null`. Server skips detection + triangulation and flags the session `error="no time sync"`. Re-run 時間校正 and re-arm.
- **No calibration**: server triangulation fails with "camera X missing calibration". Fix: run the Calibration screen (Auto ArUco or manual 5-handle) per phone before arming.
- **No distortion coefficients**: server detection still runs; triangulation uses zero distortion (equivalent to pinhole projection) — marginally less accurate at frame edges but usable.
- **Low-light room**: FPS stays locked at target via `activeMaxExposureDuration` cap. Image will darken and ISO noise grows rather than the sensor dropping to e.g. 14 fps. If detection fails because the ball is too dim, add light rather than touching FPS.

## Connection health + hot-reload

`CameraViewController` emits a 1 Hz WS heartbeat tick (base cadence from the server-owned Heartbeat setting, default 1 s). A manual **Test** action forces an immediate tick. The HUD also shows `Last contact: N s ago` updated by a 1 Hz timer (paused with the view). The HUD's `Server` line reflects the current WS connection / server state; settings and commands arrive back over the same WS channel.

Settings hot-reload is **not** tied to the WS heartbeat cadence. `SettingsViewController.onDismiss` fires in `viewDidDisappear` (covers Save, Close, and interactive-swipe dismiss), and the presenter re-diffs `UserDefaults` there. On change: `ServerUploader` rebuilt, camera role propagated to `PitchRecorder` / `ServerHealthMonitor`, and the WS connection is re-established if needed — none of which require rebuilding the `AVCaptureSession` unless the pushed runtime setting itself demands it.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`.
