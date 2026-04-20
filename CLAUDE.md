# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System overview

Two-iPhone stereo tracker for a yellow-green tennis ball (the default HSV range; `tennis` / `baseball` presets + custom range available from the dashboard's DETECTION · HSV card). Each phone runs the iOS app in role `A` or `B`, aimed at home plate. Both phones share time by jointly detecting an **audio chirp** (played from a third device) as a common sync anchor. The server pairs A/B frames within an 8 ms window of anchor-relative time and triangulates 3D positions via ray-midpoint.

### Two capture modes

The dashboard has a mode toggle that picks how detection is done; every `POST /sessions/arm` snapshots the current global mode into `Session.mode`, so a dashboard flip mid-cycle doesn't disturb an armed session.

- **Mode one — `camera_only`** (default, safe path): iPhone is a pure camera. Records H.264 MOV, uploads MOV + minimal JSON; server decodes with PyAV, runs HSV + MOG2 + shape-gate detection per frame, triangulates. 20-60 MB per camera per cycle, 8-20 s end-to-end latency.
- **Mode two — `on_device`**: iPhone runs the same HSV + MOG2 + shape-gate pipeline locally via `BTDetectionSession`, uploads only the per-frame detection results (no MOV). Server skips decode + detection and only pairs + triangulates. ~10 KB per camera, <0.5 s end-to-end latency. Detection algorithm constants are kept lock-step with `server/detection.py` + `server/pipeline.py` — see the header comment in `ball_tracker/BallDetector.mm`.

### Components

- **iOS app** — `ball_tracker/` (Swift + Obj-C++, UIKit). Xcode project at `ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`, iOS 26.2 target, Swift 5.0. Reads effective mode from `/heartbeat` replies (session snapshot if armed, else dashboard global); HUD shows a `MODE · …` chip top-right. In mode-one the clip writer + detection queue both run (detection only drives HUD/debug; the full MOV is uploaded untrimmed); in mode-two the clip writer is skipped entirely and detection output goes directly into the upload payload's `frames` list.
- **Server** — `server/` (FastAPI + `python-multipart` + PyAV + OpenCV). `uv`-managed venv at `server/.venv` (Python 3.13). In-memory state plus `data/` persistence (`pitches/`, `results/`, `videos/`); a restart reloads enriched pitch JSONs (frames already detected) and re-triangulates. `POST /pitch` accepts either a multipart `video` (mode-one, server runs detection) or a `frames` list in the JSON payload (mode-two, server skips decode); 422 when neither is supplied. Events + viewer tag each historical session by looking for a MOV on disk under `data/videos/session_<sid>_*`.

## Physical setup (current)

Nominal rig used by the operator — actual per-session pose still comes from the homography solved on-device; these values are just the target the rig is built against:

- **Camera**: iPhone 14-17 series, rear **main (1x wide) camera** only (`builtInWideAngleCamera`). Ultra Wide (0.5x, 120° FOV) is rejected — 5-coefficient distortion can't model it cleanly and the edges lose angular resolution.
- **Resolution**: default 1920×1080 (16:9); Settings → Camera → Resolution picks between 1080p / 720p / 540p (240 fps capped; 540p not supported on every device). Calibration always bakes at 1080p and the server rescales intrinsics + homography per-pitch to the MOV's actual pixel grid via `pairing.scale_pitch_to_video_dims`. ChArUco calibration JSON is auto-scaled from the 4032×3024 source on import.
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

- 52 px top nav: `BALL_TRACKER` brand + live status strip (`Devices n/2 · Calibrated n/2 · Session …`)
- 440 px left sidebar: three cards — **Devices** (per-camera row with `offline` / `online` / `calibrated` chip), **Session** (`armed`/`idle` chip + `Arm` / `Stop` buttons), **Events** (stacked event rows linking to `/viewer/{session_id}`)
- full-bleed right canvas: live Plotly 3D scene showing the plate mesh and whichever cameras have a calibration persisted — even before any pitch is uploaded. Drag to orbit.

Hydration: initial SSR paints every panel + canvas, then three JS ticks keep everything fresh — `/status` every 1 s (devices/session/nav strip), `/calibration/state` every 5 s (canvas repaint via `Plotly.react`), `/events` every 5 s (sidebar event list). Plotly.js is loaded once from CDN at the top of the document and shared across the canvas and `/viewer/{session_id}`; there is no build step. Arm flips the server into an armed session and starts dispatching `{cam: "arm"}` to online devices via `/status`; the first uploaded pitch auto-ends it (one-shot).

### iOS
Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

## Architecture

### iOS state machine — `CameraViewController`
`.standby → .recording → .uploading → .standby …`

Time-sync is a separate orthogonal flow: the operator taps 時間校正 on the phone OR clicks **Calibrate time** on the dashboard (`POST /sync/trigger` → heartbeat `sync_command: "start"` → phone enters `.timeSyncWaiting` only if currently `.standby`). Either path flows `.timeSyncWaiting` → (chirp detected) → `.standby`. Not gated by arm; the server guards against dispatching the remote command during an armed session so a mis-click can't disrupt a recording.

Driven by a **1 Hz `POST /heartbeat` loop** that runs from app launch and carries the server's per-device `commands[self.camera_id]` **plus the current `session_id`** in every reply. Arming is dashboard-only — the iPhone UI has no local start/stop button:

- `commands[self] == "arm"` + state `.standby` → cache `response.session.id` as `currentSessionId`, `enterRecordingMode()` (switch to 240 fps, state = `.recording`, defer ClipRecorder creation to the first captureOutput so pixel dims come from the real sample)
- `commands[self] == "disarm"` + state `.recording` → `recorder.forceFinishIfRecording()` if the PitchRecorder kicked off (first sample appended); otherwise just `clipRecorder.cancel()` + `exitRecordingToStandby()`
- `commands[self] == "disarm"` + state `.timeSyncWaiting` → `cancelTimeSync(reason: "disarmed")`
- `commands[self] == "disarm"` + state `.uploading` → no-op; the upload queue drives itself back to standby
- `lastAppliedCommand` guards against re-triggering on repeated replies during an armed session

iPhones never mint pairing identifiers — every `startRecording` reads `currentSessionId` (set from the heartbeat reply) and stamps it onto the outgoing `PitchPayload.session_id`. Uploads tagged with a superseded session are ignored by `_register_upload_in_session_locked` on the server, so a late flush can't disarm a new session.

The worst-case arm latency is therefore one heartbeat interval (default 1 s, clamped [1, 60]) **plus ~500 ms** for the cold-start of `AVCaptureSession` when `parkCameraInStandby` is ON (standby parks the camera, so a `startCapture(at:)` call is needed to bring it up); with the toggle OFF the session stays live at `standbyFps` and arming only pays the fps-swap (~300-500 ms) cost.

State per mode:

- `.standby`: behaviour gated on the `parkCameraInStandby` Settings toggle (nav-bar quick-flip exposes it on the HUD). **When ON** (the cool-running default for long idle): capture session stopped — camera + mic hardware idle, preview goes dark, only the heartbeat keeps running. **When OFF**: capture session stays live but FPS drops to `standbyFps` (60) so the operator can keep framing the plate; the sensor is still hot. Either way: no recording, no chirp detection.
- `.timeSyncWaiting` (時間校正): session spun up at `standbyFps` so the mic can deliver samples; **chirp detector ON**. On trigger, saves `lastSyncAnchorTimestampS`, stops the session, and returns to `.standby`. 15 s timeout. Can only be entered from `.standby` via the manual 時間校正 button — not from an arm command.
- `.recording`: session spun up at `trackingFps` (240). Captures H.264 MOV via `ClipRecorder`. **Only exit path is `forceFinishIfRecording()`**, triggered when the dashboard sends `disarm` (operator pressed Stop) or the server-side session times out. No on-device detection, no auto-end. Emits `PitchPayload` via `onCycleComplete`; the clip finishes async before the payload is persisted. Session is stopped on the way back to standby.
- `.uploading`: transient state while the cycle is persisted + enqueued; transitions back to `.standby` once handed off.
- Cycles are saved to disk in `Documents/pitch_payloads/` (`PitchPayloadStore`) **before** upload, as paired `<basename>.json` + `<basename>.mov`. Upload failures re-insert at queue front with 2 s backoff; `payloadStore.delete` removes the pair atomically on success.

### Frame pipeline (`CameraViewController.captureOutput`)
Runs on `camera.frame.queue` **only while the session is live** — 60 fps during `.timeSyncWaiting` / `.uploading` and 240 fps during `.recording`. `.standby` keeps the session stopped **only when `parkCameraInStandby` is ON** (default); with the toggle OFF the session stays live at `standbyFps` (60) so the preview keeps streaming. Transitions are driven by two helpers: `startCapture(at:)` (configure format + `startRunning`, used to leave standby) and `stopCapture()` (`stopRunning`, used to return to standby in the parked variant). `switchCaptureFps(_:)` is the mid-session fps swap (stop → reconfigure → start ~300-500 ms) — used both for the recording → standby drop in the live-preview variant and historically for fps changes mid-session.

FPS is hard-capped by an exposure ceiling: `configureCaptureFormat` sets `activeMaxExposureDuration = frameDuration`, so iOS's auto-exposure can't stretch individual samples past 1/60 s (idle) or 1/240 s (recording). AE compensates with ISO — noisier in low light, but frame rate holds. Without this cap a dim room silently dropped the effective capture rate to ~14 fps.

`captureOutput` advances `frameIndex` for debug logs, updates the HUD FPS estimate, and branches on `currentCaptureMode`:

- In mode-one: lazy-bootstraps `clipRecorder` from the first sample's dims and appends every sample to it. The full recorded MOV is uploaded as-is (no trim); server-side detection is authoritative.
- In mode-two: skips `clipRecorder` entirely (no tmp MOV written), relies on `BTDetectionSession`'s accumulated `FramePayload` list, and ships it via `PitchPayload.frames` with `videoURL: nil`.

Both modes run `dispatchDetectionIfDue` on every sample — throttled to 60 Hz on a utility queue so the ~12-18 ms HSV + CC + shape + MOG2 pipeline can't stall 240 fps capture. `PitchRecorder` is kicked off on the first captured sample in both modes using the sample's session-clock PTS as `videoStartPtsS`.

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
- `ServerHealthMonitor.swift` — 1 Hz `POST /heartbeat` poller with exponential backoff on failure (doubles up to 60 s cap, resets on success) and a generation-token guard that drops stale replies from an outdated host/port. Owns the "last contact: N s ago" 1 Hz tick timer and the `Server` HUD label. `onHeartbeatSuccess` is what the camera VC hooks to dispatch arm/disarm commands and cache `session.id`. Hot-reloadable: `updateUploader`, `updateCameraId`, `updateBaseInterval` + `probeNow()` applies a settings change without tearing down the `AVCaptureSession`.
- `PayloadUploadQueue.swift` — serialises cached pitch payloads up to the server, one at a time. `reloadPending()` on entering recording mode rebuilds the in-memory queue from `PitchPayloadStore`'s on-disk files so a restart's cached uploads resume. Upload success deletes the JSON + companion video via the store; failure re-inserts at the head with a 2 s retry. Callbacks fire on main.
- `IntrinsicsStore.swift` — single source of truth for the calibration-related `UserDefaults` keys (`intrinsic_fx/fz/cx/cy`, `intrinsic_distortion`, `homography_3x3`, `image_width_px/height_px`, `horizontal_fov_rad`). `loadIntrinsicsPayload()` returns the shipped shape including optional 5-coefficient distortion; `loadHomography()` / `loadImageDimensions()` cover their respective keys. Setters keep the capture callback's lazy writes (`setHorizontalFov`, `setImageDimensions`) on one well-known schema.
- `CalibrationViewController.swift` — two paths to the same `homography_3x3` (row-major, 9 doubles, h33=1):
  - **Manual**: 5 draggable handles on home-plate pentagon → DLT via 8×8 normal equations with Gaussian elimination.
  - **Auto (ArUco)**: `BTArucoDetector` (OpenCV `cv::aruco`, Obj-C++ wrapper in `ArucoDetector.{h,mm}`) detects DICT_4X4_50 markers IDs 0–5 taped to plate landmarks (FL/FR/RS/LS/BT/MF), then `findHomographyFromWorldPoints:imagePoints:` solves via RANSAC least-squares.
  Also derives `fx/fz/cx/cy` from capture FOV. A Settings toggle can override intrinsics with externally computed ChArUco values including 5-coefficient distortion. Both save paths (manual Save + Auto ArUco Save) also POST the freshly-persisted `{camera_id, intrinsics, homography, image_{width,height}_px}` to the server's `/calibration` endpoint via `ServerUploader.postCalibration` — fire-and-forget, failures log only. This feeds the dashboard's calibration-preview canvas so each phone's pose appears immediately after Save.
- `SettingsViewController.swift` — persists server IP/port, role A/B, chirp detection threshold (`chirp_threshold`, default 0.18), heartbeat interval (`poll_interval_s`, default 1, clamped [1, 60]), and optional manual intrinsics (including OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`) to `UserDefaults`. No HSV / ball-detection settings — the HSV range is pushed from the server (dashboard DETECTION · HSV card) via each `/heartbeat` reply and hot-applied to `BTBallDetector` with no capture-session rebuild. **Capture resolution is fixed at 1920×1080**; **capture FPS is not a user setting** — owned by `CameraViewController` as `standbyFps = 60` / `trackingFps = 240` constants with `activeMaxExposureDuration` capped to the target frame duration so AE can't drop the effective rate in low light. The Intrinsics section accepts `calibrate_intrinsics.py` JSON via `UIDocumentPickerViewController` and auto-crops/scales 4:3 → 1080p 16:9. `onDismiss` fires in `viewDidDisappear` (covers Save, Cancel, and interactive-swipe dismiss — the last is blocked via `isModalInPresentation = true`) — the presenter re-diffs settings there instead of polling.

### Server
- `schemas.py` — Pydantic types (`IntrinsicsPayload`, `FramePayload`, `PitchPayload`, `CalibrationSnapshot`, `TriangulatedPoint`, `SessionResult`, `HeartbeatBody`) and the in-memory `Device` / `Session` dataclasses. **Wire `PitchPayload` carries no per-frame data** — just `camera_id`, `session_id`, `sync_anchor_timestamp_s` (optional), `video_start_pts_s`, `video_fps`, `intrinsics`, `homography`, `image_{width,height}_px`. `FramePayload` is an internal shape server detection writes into `PitchPayload.frames` before triangulation; on-disk enriched pitch JSONs carry those frames so a restart can re-pair without re-decoding. `CalibrationSnapshot` is the body of `POST /calibration`. `camera_id` is `^[A-Za-z0-9_-]{1,16}$`; `session_id` regex is `^s_[0-9a-f]{4,32}$` (server actually mints `s_` + 8 hex chars via `secrets.token_hex(4)` — the 32-char upper bound is forward-compat slack, and the iOS-side `ServerUploader.PitchPayload` doc-comment that says "4–16 hex chars" is stale). Both path-safe.
- `detection.py` — OpenCV-backed HSV ball detector. `HSVRange.default()` is `25,55,90,255,90,255` (fluorescent yellow-green tennis ball); the dashboard persists the live range in `data/hsv_range.json` via `POST /detection/hsv` (presets `tennis` / `baseball`, or six explicit ints). `HSVRange.from_env()` still reads `BALL_TRACKER_HSV_RANGE="hMin,hMax,sMin,sMax,vMin,vMax"` as a last-resort override for headless test runs, but the server main path passes `state.hsv_range()` into `detect_pitch` explicitly. Hue is OpenCV's 8-bit convention (0-179, i.e. standard 0-360° halved). `detect_ball(bgr, hsv_range)` runs `cv2.inRange` + `cv2.connectedComponentsWithStats` and returns `(px, py)` of the largest blob whose area ∈ [20, 150_000] px² (30× looser than the early `5000` cap; close-range balls can occupy a large area), else `None`. No morphology, no temporal smoothing — keep the detector simple; ML-based detectors are a follow-up.
- `video.py` — PyAV decoder. `iter_frames(mov_path, video_start_pts_s) → Iterator[(absolute_pts_s, bgr)]`. Reconstructs absolute session-clock PTS: `absolute_pts_s = video_start_pts_s + (frame.pts − first_pts) * time_base`, so every decoded frame's timestamp sits on the same clock as `sync_anchor_timestamp_s`. `count_frames(path)` is a cheap second-pass counter used by tests.
- `pipeline.py` — glue. `detect_pitch(mov_path, video_start_pts_s, hsv_range?, frame_iter?)` decodes the MOV and synthesises one `FramePayload` per sample (always pixel-path: `theta_*` stays None). Tests inject a stub iterator to avoid real decoding. Output is what `pairing.triangulate_cycle` already consumes.
- `pairing.py` — `triangulate_cycle(a, b)` does A/B frame pairing within an 8 ms window of anchor-relative time (`timestamp_s − sync_anchor_timestamp_s`) and runs ray-midpoint triangulation. `_ray_for_frame` prefers the pixel path whenever `px`/`py` are present (server detection always produces them); zero-distortion is the default when `intrinsics.distortion` is absent, which is numerically equivalent to the legacy angle ray. Requires intrinsics + homography on both cameras.
- `triangulate.py` — `recover_extrinsics` decomposes `H = K [r1 r2 t]` (Zhang's planar method), orthonormalizes via SVD, flips sign if `t[2] < 0`. `triangulate_rays` solves the 2×2 system for the shortest segment between two 3D rays. `undistorted_ray_cam` applies OpenCV 5-coefficient undistortion.
- `reconstruct.py` — pure-geometry scene builder used by the viewer endpoints. `build_scene(session_id, pitches, triangulated)` returns a `Scene` with cameras (position + RGB triad axes in world frame), rays (one per ball-detected frame), and triangulated points. `build_calibration_scene(calibrations)` builds the dashboard-canvas scene without rays. Both paths share `_camera_view_from_intrinsics_and_homography` so poses are byte-for-byte identical across `CalibrationSnapshot` and post-triangulation `PitchPayload`.
- `chirp.py` — `chirp_wav_bytes()` builds the reference sync chirp WAV (2→8 kHz linear sweep, 100 ms, Hann-windowed, surrounded by 0.5 s silence) and is `@functools.lru_cache(maxsize=1)`'d so `GET /chirp.wav` reuses the exact bytes across requests.
- `render_scene.py` / `render_dashboard.py` — Plotly viewer + dashboard HTML. Same PHYSICS_LAB palette. Plotly.js loaded once from CDN; SSR + JS ticks handle hydration.
- `main.py` — FastAPI app, routes, and the `State` class (thread-locked). `State.pitches` dict keyed by `(camera_id, session_id)`; when both A and B for a session arrive, `triangulate_cycle` runs on the enriched (post-detection) payloads and the result is cached in `State.results[session_id]`. `/pitch` requires multipart (`payload: str` Form + `video: UploadFile` — **both mandatory now**); the handler saves the clip, runs `detect_pitch` OUTSIDE the state lock, populates `payload.frames`, then calls `state.record(payload)`. Uploads with `sync_anchor_timestamp_s=None` skip detection and triangulation; the session is flagged `error="no time sync"`. Also hosts `/sessions/arm`, `/sessions/cancel`, `/heartbeat`, `/chirp.wav`, `/status`, `/reset`, `/calibration`, `/calibration/state`, `/detection/hsv`, `/sync/trigger`, `/events`, `/viewer/{session_id}`, `/reconstruction/{session_id}`.

### Dashboard control plane
`State` owns three pieces of in-memory state (all reset on server restart):

- **Device registry** (`_devices`) — `{camera_id: Device(last_seen_at)}`, updated by `POST /heartbeat`. `online_devices()` filters to beats within 3 s. Clock is injectable (`time_fn` ctor arg) so tests age devices in microseconds.
- **Session** (`_current_session` + `_last_ended_session`) — at most one armed `Session` at a time with a generated `s_xxxxxxxx` id, a `max_duration_s` auto-timeout (default 60 s), and a list of `camera_id`s uploaded during its armed window (session-scoped, so the list is implicitly `{camera_id, session_id=self.id}`). `arm_session` is idempotent (double-click safe); `cancel_session` is 409 on idle for API callers but always 303 for HTML form callers so the dashboard button never looks broken. `current_session()` lazily applies the timeout on read — no background task.
- **Command dispatch** (`commands_for_devices()`) — derives `{camera_id: "arm"|"disarm"}` from session state: `arm` while armed, `disarm` for `_DISARM_ECHO_S` (5 s) after any end. iPhones poll `/status` (or piggy-back on `POST /heartbeat`'s response) and react to `commands[self.camera_id]`.

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Naming collision**: iOS persisted `intrinsic_fz` is the image-**vertical** focal length, i.e. OpenCV's `fy`. Server's `build_K(fx, fy, cx, cy)` is invoked with `intr.fz` passed as the `fy` arg. This is intentional — do not "fix" the name on one side without updating the other.
- **iOS persisted intrinsics** (`UserDefaults`): `intrinsic_fx`, `intrinsic_fz`, `intrinsic_cx`, `intrinsic_cy`, optional `intrinsic_distortion` (5-array). `image_width_px` / `image_height_px` are written by the capture callback when dimensions change.

## Payload contract

`POST /pitch` is `multipart/form-data` with **both parts required**:

- **`payload`** (`application/json`) — encoded `ServerUploader.PitchPayload` ↔ `main.PitchPayload`. The calibration DB at `data/calibrations/<camera_id>.json` is the **single source of truth** for intrinsics / homography / image dims — iOS no longer echoes them on pitch uploads (Phase 1 decoupling):

  ```
  camera_id: str matching ^[A-Za-z0-9_-]{1,16}$   # "A"/"B" in practice
  session_id: str matching ^s_[0-9a-f]{4,32}$     # server-minted via secrets.token_hex(4) → "s_" + 8 hex; sole pairing key (32-char upper bound is forward-compat)
  sync_anchor_timestamp_s: float | null           # chirp-detected session-clock PTS; null if skipped
  video_start_pts_s: float                        # abs PTS of first MOV frame (session clock)
  local_recording_index: int?                     # device-local debug counter; server ignores
  ```

  Server-side, `/pitch` looks up the matching `CalibrationSnapshot` from `state.calibrations()` and fills in `intrinsics` / `homography` / `image_width_px` / `image_height_px` BEFORE triangulation and on-disk persistence. **No calibration on file → 422**. Older on-disk pitch JSONs that still carry the fields load fine (Pydantic keeps them Optional). **No `frames` field on the wire.** Server detection populates it post-ingest.

- **`video`** (`video/quicktime`) — H.264 MOV of the recording. Stored under `data/videos/session_{session_id}_{camera_id}.<ext>`, decoded by PyAV, and fed into `detect_pitch` → `FramePayload` list before triangulation. Upload without a video → 422.

Pairing is by **`session_id` alone** (server-minted via `POST /sessions/arm`). iPhones never generate pairing identifiers.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present AND `sync_anchor_timestamp_s` non-null — if any is missing, `SessionResult.error` is set and triangulation is skipped (raw payload + MOV are still persisted for forensics).

## Degraded / fallback modes

- **No 時間校正 before arm**: `sync_anchor_timestamp_s` uploads as `null`. Server skips detection + triangulation and flags the session `error="no time sync"`. Re-run 時間校正 and re-arm.
- **No calibration**: server triangulation fails with "camera X missing calibration". Fix: run the Calibration screen (Auto ArUco or manual 5-handle) per phone before arming.
- **No distortion coefficients**: server detection still runs; triangulation uses zero distortion (equivalent to pinhole projection) — marginally less accurate at frame edges but usable.
- **Low-light room**: FPS stays locked at target via `activeMaxExposureDuration` cap. Image will darken and ISO noise grows rather than the sensor dropping to e.g. 14 fps. If detection fails because the ball is too dim, add light rather than touching FPS.

## Connection health + hot-reload

`CameraViewController` runs a 1 Hz `POST /heartbeat` loop (base cadence from Settings → Heartbeat Interval, default 1 s). On failure the interval doubles until a 60 s cap; a success resets to the base. A manual **Test** button on the HUD cancels any in-flight probe and re-probes immediately. The HUD also shows `Last contact: N s ago` updated by a 1 Hz tick timer (paused with the view). A generation token on each probe ensures stale responses from an outdated host/port can't overwrite current state. The HUD's `Server` line shows `ARMED (s_xxx)` / `IDLE` derived from the heartbeat's `session` payload.

Settings hot-reload is **not** tied to the heartbeat cadence. `SettingsViewController.onDismiss` fires in `viewDidDisappear` (covers Save, Close, and interactive-swipe dismiss), and the presenter re-diffs `UserDefaults` there. On change: `ServerUploader` rebuilt, a new `chirpThreshold` is pushed into the live detector, camera role propagated to `PitchRecorder` / `ServerHealthMonitor`, or the heartbeat cadence is updated with backoff reset — none of which require rebuilding the `AVCaptureSession`.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`.
