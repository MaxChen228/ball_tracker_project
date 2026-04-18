# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System overview

Two-iPhone stereo tracker for a deep-blue baseball. Each phone runs the iOS app in role `A` or `B`, aimed at home plate. The two phones share time by jointly detecting an **audio chirp** (played from a third device) as a common sync anchor; each then independently detects the ball per frame, computes `(θx, θz)` from its optical axis (or undistorts the raw pixel when distortion coefficients are calibrated), and uploads one *cycle* (pitch) payload — **JSON metadata plus an optional H.264 clip** — to a FastAPI server on the LAN. The server recovers each camera's extrinsics from its home-plate homography, pairs A/B frames within an 8 ms window of anchor-relative time, and triangulates 3D positions via ray-midpoint.

- **iOS app** — `ball_tracker/` (Swift, UIKit). Xcode project at `ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`, iOS 26.2 target, Swift 5.0.
- **Server** — `server/` (FastAPI + `python-multipart`). `uv`-managed venv at `server/.venv` (Python 3.13). In-memory state plus `data/` persistence (`pitches/`, `results/`, `videos/`); a restart re-triangulates cached JSON payloads on startup. Clips are stored but not yet consumed by triangulation — Phase-1 raw-video staging for future server-side detection.

## Physical setup (current)

Nominal rig used by the operator — actual per-session pose still comes from the homography solved on-device; these values are just the target the rig is built against:

- **Camera**: iPhone 14-17 series, rear **main (1x wide) camera** only (`builtInWideAngleCamera`). Ultra Wide (0.5x, 120° FOV) is rejected — 5-coefficient distortion can't model it cleanly and the edges lose angular resolution.
- **Resolution**: **1920×1080 (16:9), system-wide fixed**. No 720p option — the Settings UI and all downstream math assume 1080p. ChArUco calibration JSON is auto-scaled from the 4032×3024 source on import.
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
```

The root URL `http://<server>:8765/` is the **dashboard**: a devices panel (A/B online badges), a session panel (ARMED/IDLE + session id), Arm / Cancel buttons, and the events table linking into `/viewer/{session_id}` for each session's 3D scene (plate plane, camera pose, ray bundle, triangulated trajectory). Arm flips the server into an armed session and starts dispatching `{cam: "arm"}` to online devices via `/status`; the first uploaded pitch auto-ends it (one-shot). Purely server-rendered HTML + one short vanilla-JS tick for live-refreshing the top panels; Plotly.js is loaded from CDN so there's no build step.

### iOS
Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

## Architecture

### iOS state machine — `CameraViewController`
`.standby → .timeSyncWaiting → .standby → .syncWaiting → .recording → .uploading → .syncWaiting …`

Driven by a **1 Hz `POST /heartbeat` loop** that runs from app launch and carries the server's per-device `commands[self.camera_id]` **plus the current `session_id`** in every reply. The local "啟動追蹤" button still works as a debug escape hatch (it calls `POST /sessions/arm` to get an id synchronously, then enters sync mode), but the intended flow is dashboard-driven:

- `commands[self] == "arm"` + state `.standby` → cache `response.session.id` as `currentSessionId`, `enterSyncMode()`
- `commands[self] == "disarm"` + state `.syncWaiting`/`.uploading` → `exitSyncMode()`
- `commands[self] == "disarm"` + state `.recording` → `recorder.forceFinishIfRecording()` flushes whatever frames are buffered; the cycle-complete handler routes back to `.standby` (not the usual `.syncWaiting`) via `returnToStandbyAfterCycle`
- `commands[self] == "disarm"` + state `.timeSyncWaiting` → `cancelTimeSync(reason: "disarmed")`
- `lastAppliedCommand` guards against re-triggering on repeated replies during an armed session

iPhones never mint pairing identifiers — every `startRecording` reads `currentSessionId` (set from the heartbeat reply) and stamps it onto the outgoing `PitchPayload.session_id`. Uploads tagged with a superseded session are ignored by `_register_upload_in_session_locked` on the server, so a late flush can't disarm a new session.

The worst-case arm latency is therefore one heartbeat interval (default 1 s, clamped [1, 60]).

State per mode:

- `.timeSyncWaiting` (時間校正): **chirp detector ON, ball detection OFF**. On trigger, saves `lastSyncAnchorFrameIndex/TimestampS` and returns to `.standby`. 15 s timeout.
- `.syncWaiting`: ball detection ON, pre-roll circular buffer (120 frames) filling. First ball-detected frame transitions to `.recording` using the saved chirp anchor for the cycle.
- `.recording`: buffers frames AND writes an H.264 clip via `ClipRecorder`. Stops when: (a) the ball has been absent for 24 consecutive frames AND cycle length ≥ 24 frames, (b) the cycle duration hits `maxCycleDurationS = 5 s` (hard cap — covers "ball stopped in frame"), or (c) `forceFinishIfRecording()` is called by a dashboard disarm. Emits `PitchPayload` via `onCycleComplete`; the clip finishes async before the payload is persisted. **Pre-roll frames are in the JSON payload only** — the clip starts at the first ball-detected frame (Phase-1 scope limit).
- `.uploading`: transient state while the cycle is persisted + enqueued; transitions back to `.syncWaiting` (or `.standby` if a disarm is pending) once handed off.
- Cycles are saved to disk in `Documents/pitch_payloads/` (`PitchPayloadStore`) **before** upload, as paired `<basename>.json` + optional `<basename>.mov`. Upload failures re-insert at queue front with 2 s backoff; `payloadStore.delete` removes the pair atomically on success.

### Frame pipeline (`CameraViewController.captureOutput`)
Runs at 240 fps on `camera.frame.queue`. Advances `frameIndex` every frame (cross-mode coherence). **Never** `DispatchQueue.main.async` per frame — detection results are stashed in `latestCentroidX/Y` and a `CADisplayLink` on main redraws the overlay at 60/120 Hz. Only actual state transitions dispatch to main.

### Audio pipeline (`AudioChirpDetector`)
Runs on `audio.chirp.queue`. `AVCaptureAudioDataOutput` delivers mic samples directly to the detector, which runs a normalized matched filter (cross-correlation against a reference chirp, divided by local window energy) at ~10 Hz. On a peak > `threshold`, parabolic sub-sample interpolation refines the location; the chirp-center session-clock PTS becomes the anchor. Audio sample rate 44.1 kHz → 22 μs per sample; detection precision typically **<100 μs** (40× better than the frame-granularity the earlier flash detector could hit).

The reference chirp is a linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, unit-energy normalized. The server's `/chirp.wav` endpoint emits the same waveform as a playable WAV surrounded by 0.5 s silence — users download it on any third device and play it near the two iPhones during 時間校正.

Threshold (default 0.18, tunable from Settings → Sync → Chirp Threshold) is the normalized matched-filter peak above which a detection fires. Peaks are in roughly 0–1 with 1.0 meaning a perfect reference match on a clean recording; typical field values are 0.2–0.4 on a nearby speaker, <0.05 on stationary ambient. Lower the threshold if the HUD flashes orange ("close") but never triggers; raise it if false-triggers on ambient noise. Hot-reload via `setThreshold(_:)` — no capture-session rebuild.

### Key modules
- `BallDetector.swift` — HSV threshold on a downsampled grid → 8-neighborhood connected components → largest component passing area filter (20–5000 px²) → centroid → `θx = atan2(px - cx, fx)`, `θz = atan2(py - cy, fz)`. Uses calibrated intrinsics if present, else FOV approximation.
- `AudioChirpDetector.swift` — matched-filter chirp detection via `vDSP_dotpr`. Owns the audio ring buffer, reference chirp, cooldown, and emits `ChirpEvent(anchorFrameIndex, anchorTimestampS)` callbacks on its own queue. Threshold is mutable (`setThreshold(_:)`) so Settings changes propagate without session rebuild. Self-contained — no dependencies on other sync helpers.
- `PitchRecorder.swift` — pre-roll buffer + recording assembly. `session_id` and the chirp anchor are both passed in at `startRecording` time — the recorder doesn't mint any pairing identifier. `localRecordingIndex` is a run-of-app debug counter (not reset on state changes) that ships on the payload as `local_recording_index` purely for operator logs.
- `ClipRecorder.swift` — `AVAssetWriter` wrapper that consumes the same `CMSampleBuffer`s the capture queue dispatches, writes H.264 MOV to a tmp URL, and finalises async on cycle-complete. `prepare → append (first append starts the writer session at that PTS) → finish/cancel`. Caller (`CameraViewController`) serialises all calls onto `processingQueue`; `exitSyncMode` tears down via an async dispatch onto the same queue so it can't race with an in-flight append. Failed `prepare` degrades silently — the cycle still uploads, just without a clip.
- `PitchPayloadStore.swift` — local cache for completed cycles. `save(payload, videoURL:)` writes `<basename>.json` atomically and moves any tmp clip to `<basename>.<ext>`. `videoURL(forPayload:)` resolves the companion clip for the uploader; `delete(jsonURL)` removes both. `makeTempVideoURL()` provides fresh `Documents/tmp/clip_<uuid>.mov` URLs for `ClipRecorder`.
- `ServerUploader.swift` — HTTP transport only. `uploadPitch(_, videoURL:, completion:)` posts `/pitch` as `multipart/form-data` (required `payload` JSON field + optional `video` file part; a legacy overload without `videoURL` remains for tests). `sendHeartbeat(cameraId:)` is the 1 Hz liveness + command channel; `armSession(completion:)` is the synchronous shortcut the local tracking button uses to mint a session id before entering sync mode. Multipart body is hand-built — no third-party HTTP lib. Contains no timers or retry logic (those live in the queue / monitor below).
- `ServerHealthMonitor.swift` — 1 Hz `POST /heartbeat` poller with exponential backoff on failure (doubles up to 60 s cap, resets on success) and a generation-token guard that drops stale replies from an outdated host/port. Owns the "last contact: N s ago" 1 Hz tick timer and the `Server` HUD label (`ARMED (s_xxx)` / `IDLE · last: cycle_uploaded` derived from the heartbeat's `session` payload). `onHeartbeatSuccess` is what the camera VC hooks to dispatch arm/disarm commands and cache `session.id`. Hot-reloadable: `updateUploader`, `updateCameraId`, `updateBaseInterval` + `probeNow()` applies a settings change without tearing down the `AVCaptureSession`.
- `PayloadUploadQueue.swift` — serialises cached pitch payloads up to the server, one at a time. `reloadPending()` on entering sync mode rebuilds the in-memory queue from `PitchPayloadStore`'s on-disk files so a restart's cached uploads resume; `enqueue(_:)` appends a freshly saved JSON URL. Upload success deletes the JSON + companion video via the store; failure re-inserts at the head with a 2 s retry. `onStatusTextChanged` / `onLastResultChanged` / `onUploadingChanged` are the hooks the camera VC wires to HUD labels + status-dot colour. All callbacks fire on main.
- `IntrinsicsStore.swift` — single source of truth for the calibration-related `UserDefaults` keys (`intrinsic_fx/fz/cx/cy`, `intrinsic_distortion`, `homography_3x3`, `image_width_px/height_px`, `horizontal_fov_rad`). `loadBallDetectorIntrinsics()` returns the four-parameter `BallDetector.Intrinsics` (nil when any field is missing/zero); `loadIntrinsicsPayload()` returns the shipped shape including the optional 5-coefficient distortion; `loadHomography()` / `loadImageDimensions()` do the same for their respective keys. Setters keep the capture callback's lazy writes (`setHorizontalFov`, `setImageDimensions`) on one well-known schema.
- `CalibrationViewController.swift` — two paths to the same `homography_3x3` (row-major, 9 doubles, h33=1):
  - **Manual**: 5 draggable handles on home-plate pentagon → DLT via 8×8 normal equations with Gaussian elimination.
  - **Auto (ArUco)**: `BTArucoDetector` (OpenCV `cv::aruco`, Obj-C++ wrapper in `ArucoDetector.{h,mm}`) detects DICT_4X4_50 markers IDs 0–5 taped to plate landmarks (FL/FR/RS/LS/BT/MF), then `findHomographyFromWorldPoints:imagePoints:` solves via RANSAC least-squares.
  Also derives `fx/fz/cx/cy` from capture FOV. A Settings toggle can override intrinsics with externally computed ChArUco values including 5-coefficient distortion.
- `SettingsViewController.swift` — persists server IP/port, role A/B, HSV range, chirp detection threshold (`chirp_threshold`, default 0.18), heartbeat interval (`poll_interval_s`, default 1, clamped [1, 60]), capture fps (60/120/240), and optional manual intrinsics (including OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`) to `UserDefaults`. **Capture resolution is system-wide fixed at 1920×1080** (`captureWidthFixed` / `captureHeightFixed` in the file; the `captureWidth`/`captureHeight` struct fields and UserDefaults keys remain so `CameraViewController.selectFormat` stays parameterised, but any stale value is ignored on load). `normalizeServerIP` strips scheme/port/path from pasted URLs. The Intrinsics section accepts `calibrate_intrinsics.py` JSON via `UIDocumentPickerViewController` and auto-crops/scales 4:3 → 1080p 16:9. Save validates port ∈ [1, 65535], heartbeat ∈ [1, 60], chirp ∈ (0, 1], HSV min<max, and fx/fy > 100 before persisting; otherwise shows an alert and refuses to dismiss. `onDismiss` fires in `viewDidDisappear` (covers Save, Cancel, and interactive-swipe dismiss — the last is blocked via `isModalInPresentation = true`) — the presenter re-diffs settings there instead of relying on a polling tick.

### Server
- `schemas.py` — Pydantic wire contracts (`IntrinsicsPayload`, `FramePayload`, `PitchPayload`, `TriangulatedPoint`, `SessionResult`, `HeartbeatBody`) and the in-memory `Device` / `Session` dataclasses. `camera_id` is `^[A-Za-z0-9_-]{1,16}$` and `session_id` is `^s_[0-9a-f]{4,32}$` — both patterns are path-safe so file routes can interpolate them. `_DEFAULT_SESSION_TIMEOUT_S = 60.0` lives here too so tests can import it without pulling in FastAPI. `main.py` re-exports these names for back-compat — new callers should import from `schemas` directly.
- `pairing.py` — `triangulate_cycle(a, b)` does A/B frame pairing within an 8 ms window of anchor-relative time (`timestamp_s − sync_anchor_timestamp_s`) and runs ray-midpoint triangulation. Helpers `_camera_pose` (K + extrinsics from `IntrinsicsPayload` + homography), `_ray_for_frame` (undistort raw pixels when distortion + px/py both present, else fall back to on-device angles), `_valid_frame`, `_frame_items`. Requires intrinsics + homography on both cameras; raises `ValueError` otherwise.
- `triangulate.py` — `recover_extrinsics` decomposes `H = K [r1 r2 t]` (Zhang's planar method), orthonormalizes via SVD, flips sign if `t[2] < 0` (camera must be in front of plate). `triangulate_rays` solves the 2×2 system for the shortest segment between two 3D rays and returns its midpoint + gap. `undistorted_ray_cam` applies OpenCV-compatible 5-coefficient undistortion when raw pixels + distortion are both supplied.
- `reconstruct.py` — pure-geometry scene builder used by the viewer endpoints. `build_scene(session_id, pitches, triangulated)` returns a `Scene` dataclass with `cameras` (position + local RGB-triad axes in world frame), `rays` (one per ball-detected frame: origin = camera center, endpoint = ground-plane intersection or `_RAY_MAX_LEN_M` fallback), and `triangulated` points when both A+B are paired. `Scene.to_dict()` is the stable JSON hand-off for `/reconstruction/{session_id}`.
- `chirp.py` — `chirp_wav_bytes()` builds the reference sync chirp WAV (2→8 kHz linear sweep, 100 ms, Hann-windowed, surrounded by 0.5 s silence) and is `@functools.lru_cache(maxsize=1)`'d so `GET /chirp.wav` reuses the exact bytes across requests. Deterministic — constants only, no RNG.
- `render_scene.py` — `render_scene_html(scene)` returns the Plotly 3D figure for `/viewer/{session_id}`: ground plane (Z=0), world axes, per-camera marker + local RGB triad + forward arrow, ray bundle per camera, triangulated trajectory coloured by time. Plotly.js loaded from CDN so the page is self-contained and build-step-free.
- `render_dashboard.py` — `render_events_index_html(events, devices, session)` produces the `/` dashboard: devices panel (A/B online badges), session panel (ARMED/IDLE + id), Arm/Cancel HTML-form buttons, and the events table with per-row `single`/`dual` mode chip + links into `/viewer/{session_id}`. Owns the `_INDEX_CSS` string and a short vanilla-JS tick that re-polls `/status` at 1 Hz to keep the top panels live without reloading the table. No JS framework, no build step.
- `main.py` — FastAPI app, routes, lifespan, and the `State` class (thread-locked). `State.pitches` dict keyed by `(camera_id, session_id)`; when both A and B for a session arrive, `triangulate_cycle` runs immediately under the state lock and the result is cached in `State.results[session_id]` as a `SessionResult`. `/pitch` accepts multipart (`payload: str` Form + optional `video: UploadFile`); clip bytes are written atomically via tmp-rename to `data/videos/session_{session_id}_{camera_id}.{ext}`. `State.events()` collapses each session into one summary row for the `/events` JSON endpoint and the dashboard. A pitch arriving during an armed session auto-ends the session (one-shot arm) and flips commands to `disarm` for the echo window. Also hosts the `/sessions/arm`, `/sessions/cancel`, `/heartbeat`, `/chirp.wav`, `/status`, `/reset` and `/reconstruction/{session_id}` routes.

### Dashboard control plane
`State` owns three pieces of in-memory state (all reset on server restart):

- **Device registry** (`_devices`) — `{camera_id: Device(last_seen_at)}`, updated by `POST /heartbeat`. `online_devices()` filters to beats within 3 s. Clock is injectable (`time_fn` ctor arg) so tests age devices in microseconds.
- **Session** (`_current_session` + `_last_ended_session`) — at most one armed `Session` at a time with a generated `s_xxxxxxxx` id, a `max_duration_s` auto-timeout (default 60 s), and a list of `camera_id`s uploaded during its armed window (session-scoped, so the list is implicitly `{camera_id, session_id=self.id}`). `arm_session` is idempotent (double-click safe); `cancel_session` is 409 on idle for API callers but always 303 for HTML form callers so the dashboard button never looks broken. `current_session()` lazily applies the timeout on read — no background task.
- **Command dispatch** (`commands_for_devices()`) — derives `{camera_id: "arm"|"disarm"}` from session state: `arm` while armed, `disarm` for `_DISARM_ECHO_S` (5 s) after any end. iPhones poll `/status` (or piggy-back on `POST /heartbeat`'s response) and react to `commands[self.camera_id]`.

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Naming collision**: Swift `BallDetector.Intrinsics.fz` is the image-**vertical** focal length, i.e. OpenCV's `fy`. Server's `build_K(fx, fy, cx, cy)` is invoked with `intr.fz` passed as the `fy` arg. This is intentional — do not "fix" the name on one side without updating the other.
- **iOS persisted intrinsics** (`UserDefaults`): `intrinsic_fx`, `intrinsic_fz`, `intrinsic_cx`, `intrinsic_cy`, optional `intrinsic_distortion` (5-array). `image_width_px` / `image_height_px` are written by the capture callback when dimensions change.

## Payload contract

`POST /pitch` is `multipart/form-data` with two parts:

- **`payload`** (required, `application/json`) — encoded `ServerUploader.PitchPayload` ↔ `main.PitchPayload`:

  ```
  camera_id: str matching ^[A-Za-z0-9_-]{1,16}$   # "A"/"B" in practice; server rejects anything else with 422
  session_id: str matching ^s_[0-9a-f]{4,32}$     # server-minted; sole pairing key for A/B
  sync_anchor_frame_index: int            # placeholder; server ignores and pairs by timestamp
  sync_anchor_timestamp_s: float          # chirp-detected session-clock PTS
  local_recording_index: int?             # device-local debug counter, not used for pairing
  frames: [{frame_index, timestamp_s, theta_x_rad?, theta_z_rad?, px?, py?, ball_detected}]
  intrinsics: {fx, fz, cx, cy, distortion?}?   # fz == OpenCV fy; distortion is [k1,k2,p1,p2,k3]
  homography: [h11..h33]?                 # row-major, h33 normalized to 1
  image_width_px?, image_height_px?
  ```

- **`video`** (optional, typically `video/quicktime`) — H.264 MOV of the recording. When present the server stores it under `data/videos/session_{session_id}_{camera_id}.<ext>` and surfaces `{"clip": {"filename", "bytes"}}` in the response, but triangulation does **not** yet read it.

Pairing is by **`session_id` alone** (server-minted via `POST /sessions/arm`). iPhones never generate pairing identifiers. `local_recording_index` is a device-local debug counter; the server ignores it for pairing.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present — if a phone's calibration was never saved, the server returns `error` on the session but still stores the raw payload (and clip, if any).

## Degraded / fallback modes

- If the user skips 時間校正, `lastSyncAnchor*` is nil and `PitchRecorder` uses the first-ball-frame as the anchor. A/B cycles then won't align across cameras — the 8 ms match window in `triangulate_cycle` will drop most pairs.
- If calibration was never performed, `BallDetector` falls back to FOV-approximation intrinsics (`horizontal_fov_rad` is written from `AVCaptureDevice.activeFormat.videoFieldOfView` during capture setup). Server-side triangulation still fails — intrinsics must be persisted via Calibration screen for a full pipeline.
- Distortion coefficients are optional. When absent, triangulation uses the on-device `θx/θz` angles; when present, it undistorts `(px, py)` per-frame for improved accuracy at the frame edges.

## Connection health + hot-reload

`CameraViewController` runs a 1 Hz `POST /heartbeat` loop (base cadence from Settings → Heartbeat Interval, default 1 s). On failure the interval doubles until a 60 s cap; a success resets to the base. A manual **Test** button on the HUD cancels any in-flight probe and re-probes immediately. The HUD also shows `Last contact: N s ago` updated by a 1 Hz tick timer (paused with the view). A generation token on each probe ensures stale responses from an outdated host/port can't overwrite current state. The HUD's `Server` line shows `ARMED (s_xxx)` / `IDLE · last: cycle_uploaded` derived from the heartbeat's `session` payload.

Settings hot-reload is **not** tied to the heartbeat cadence. `SettingsViewController.onDismiss` fires in `viewDidDisappear` (covers Save, Close, and interactive-swipe dismiss), and the presenter re-diffs `UserDefaults` there. On change: capture format is reconfigured, `ServerUploader` rebuilt, a new `chirpThreshold` is pushed into the live detector, or the heartbeat cadence is updated with backoff reset — none of which require rebuilding the `AVCaptureSession`.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`.
