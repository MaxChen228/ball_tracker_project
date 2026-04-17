# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System overview

Two-iPhone stereo tracker for a deep-blue baseball. Each phone runs the iOS app in role `A` or `B`, aimed at home plate. The two phones share time by jointly detecting an **audio chirp** (played from a third device) as a common sync anchor; each then independently detects the ball per frame, computes `(θx, θz)` from its optical axis (or undistorts the raw pixel when distortion coefficients are calibrated), and uploads one *cycle* (pitch) payload — **JSON metadata plus an optional H.264 clip** — to a FastAPI server on the LAN. The server recovers each camera's extrinsics from its home-plate homography, pairs A/B frames within an 8 ms window of anchor-relative time, and triangulates 3D positions via ray-midpoint.

- **iOS app** — `ball_tracker/` (Swift, UIKit). Xcode project at `ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`, iOS 26.2 target, Swift 5.0.
- **Server** — `server/` (FastAPI + `python-multipart`). `uv`-managed venv at `server/.venv` (Python 3.13). In-memory state plus `data/` persistence (`pitches/`, `results/`, `videos/`); a restart re-triangulates cached JSON payloads on startup. Clips are stored but not yet consumed by triangulation — Phase-1 raw-video staging for future server-side detection.

## Physical setup (current)

Nominal rig used by the operator — actual per-session pose still comes from the homography solved on-device; these values are just the target the rig is built against:

- **Camera**: iPhone 14-17 series, rear **main (1x wide) camera** only (`builtInWideAngleCamera`). Ultra Wide (0.5x, 120° FOV) is rejected — 5-coefficient distortion can't model it cleanly and the edges lose angular resolution.
- **Orientation**: **landscape** on both phones. Sensor long-edge aligned with the pitcher→plate horizontal direction. ChArUco intrinsic-calibration shots must be taken in the same orientation.
- **Baseline**: two phones placed ~3 m from home plate, both on the **first-base / third-base line** (i.e. 1B-side phone and 3B-side phone, aimed inward at the plate). This is a wide cross-baseline stereo setup — good depth separation for triangulation.
- **Focus**: lock AF (`setFocusModeLocked`) to the plate distance both during ChArUco capture and during live recording. The main cam has OIS but static mounting keeps its drift negligible.
- **Extrinsics** are NOT assumed from this geometry — every session still runs the Calibration screen (Auto ArUco or manual 5-handle) per phone to recover the real homography. The 3 m / 1B-3B numbers are rig targets, not priors fed into code.

## Commands

### Server
```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8765   # run (prints LAN IP → paste into iPhone Settings)
uv run pytest                                        # all tests
uv run pytest test_server.py::test_triangulate_sweeps_ball_path   # single test
```

### iOS
Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

## Architecture

### iOS state machine — `CameraViewController`
`.standby → .timeSyncWaiting → .standby → .syncWaiting → .recording → .uploading → .syncWaiting …`

- `.timeSyncWaiting` (時間校正): **chirp detector ON, ball detection OFF**. On trigger, saves `lastSyncAnchorFrameIndex/TimestampS` and returns to `.standby`. 15 s timeout.
- `.syncWaiting`: ball detection ON, pre-roll circular buffer (120 frames) filling. First ball-detected frame transitions to `.recording` using the saved chirp anchor for the cycle.
- `.recording`: buffers frames AND writes an H.264 clip via `ClipRecorder`. Stops when the ball has been absent for 24 consecutive frames AND cycle length ≥ 24 frames. Emits `PitchPayload` via `onCycleComplete`; the clip finishes async before the payload is persisted. **Pre-roll frames are in the JSON payload only** — the clip starts at the first ball-detected frame (Phase-1 scope limit).
- `.uploading`: transient state while the cycle is persisted + enqueued; transitions back to `.syncWaiting` once handed off.
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
- `PitchRecorder.swift` — pre-roll buffer + cycle assembly. The chirp anchor is passed in at `startRecording` time, not discovered by the recorder.
- `ClipRecorder.swift` — `AVAssetWriter` wrapper that consumes the same `CMSampleBuffer`s the capture queue dispatches, writes H.264 MOV to a tmp URL, and finalises async on cycle-complete. `prepare → append (first append starts the writer session at that PTS) → finish/cancel`. Caller (`CameraViewController`) serialises all calls onto `processingQueue`; `exitSyncMode` tears down via an async dispatch onto the same queue so it can't race with an in-flight append. Failed `prepare` degrades silently — the cycle still uploads, just without a clip.
- `PitchPayloadStore.swift` — local cache for completed cycles. `save(payload, videoURL:)` writes `<basename>.json` atomically and moves any tmp clip to `<basename>.<ext>`. `videoURL(forPayload:)` resolves the companion clip for the uploader; `delete(jsonURL)` removes both. `makeTempVideoURL()` provides fresh `Documents/tmp/clip_<uuid>.mov` URLs for `ClipRecorder`.
- `ServerUploader.swift` — `POST /pitch` as `multipart/form-data` (required `payload` JSON field + optional `video` file part). `uploadPitch(_, videoURL:, completion:)` is the primary API; a legacy overload without `videoURL` remains for tests and early-migration callers. Multipart body is hand-built — no third-party HTTP lib. `fetchStatus(completion:)` hits `/status` and is what the connection-health HUD polls.
- `CalibrationViewController.swift` — two paths to the same `homography_3x3` (row-major, 9 doubles, h33=1):
  - **Manual**: 5 draggable handles on home-plate pentagon → DLT via 8×8 normal equations with Gaussian elimination.
  - **Auto (ArUco)**: `BTArucoDetector` (OpenCV `cv::aruco`, Obj-C++ wrapper in `ArucoDetector.{h,mm}`) detects DICT_4X4_50 markers IDs 0–5 taped to plate landmarks (FL/FR/RS/LS/BT/MF), then `findHomographyFromWorldPoints:imagePoints:` solves via RANSAC least-squares.
  Also derives `fx/fz/cx/cy` from capture FOV. A Settings toggle can override intrinsics with externally computed ChArUco values including 5-coefficient distortion.
- `SettingsViewController.swift` — persists server IP/port, role A/B, HSV range, chirp detection threshold (`chirp_threshold`, default 0.18), `/status` poll interval (`poll_interval_s`, default 10, clamped [2, 300]), capture resolution/fps, and optional manual intrinsics (including OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`) to `UserDefaults`. `normalizeServerIP` strips scheme/port/path from pasted URLs. `onDismiss` fires in `viewDidDisappear` (covers Save, Close, and interactive-swipe dismiss) — the presenter re-diffs settings there instead of relying on a polling tick.

### Server
- `main.py` — `State` dict keyed by `(camera_id, cycle_number)`. When both A and B for a cycle arrive, `triangulate_cycle` runs immediately under the state lock. `/pitch` accepts multipart (`payload: str` Form + optional `video: UploadFile`); `camera_id` is Pydantic-constrained to `^[A-Za-z0-9_-]{1,16}$` so it can't escape the data dir when interpolated into file paths. Clip bytes are written atomically via tmp-rename to `data/videos/cycle_{cycle:06d}_{camera_id}.{ext}`. `/status` reports received + completed cycles. `/chirp.wav` returns the reference sync chirp.
- `triangulate.py` — `recover_extrinsics` decomposes `H = K [r1 r2 t]` (Zhang's planar method), orthonormalizes via SVD, flips sign if `t[2] < 0` (camera must be in front of plate). `triangulate_rays` solves the 2×2 system for the shortest segment between two 3D rays and returns its midpoint + gap. `undistorted_ray_cam` applies OpenCV-compatible 5-coefficient undistortion when raw pixels + distortion are both supplied.

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
  sync_anchor_frame_index: int            # placeholder; server ignores and pairs by timestamp
  sync_anchor_timestamp_s: float          # chirp-detected session-clock PTS
  cycle_number: int                       # monotonic per device; server pairs A+B by this
  frames: [{frame_index, timestamp_s, theta_x_rad?, theta_z_rad?, px?, py?, ball_detected}]
  intrinsics: {fx, fz, cx, cy, distortion?}?   # fz == OpenCV fy; distortion is [k1,k2,p1,p2,k3]
  homography: [h11..h33]?                 # row-major, h33 normalized to 1
  image_width_px?, image_height_px?
  ```

- **`video`** (optional, typically `video/quicktime`) — H.264 MOV of the cycle. When present the server stores it under `data/videos/cycle_{cycle:06d}_{camera_id}.<ext>` and surfaces `{"clip": {"filename", "bytes"}}` in the response, but triangulation does **not** yet read it.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present — if a phone's calibration was never saved, the server returns `error` on the cycle but still stores the raw payload (and clip, if any).

## Degraded / fallback modes

- If the user skips 時間校正, `lastSyncAnchor*` is nil and `PitchRecorder` uses the first-ball-frame as the anchor. A/B cycles then won't align across cameras — the 8 ms match window in `triangulate_cycle` will drop most pairs.
- If calibration was never performed, `BallDetector` falls back to FOV-approximation intrinsics (`horizontal_fov_rad` is written from `AVCaptureDevice.activeFormat.videoFieldOfView` during capture setup). Server-side triangulation still fails — intrinsics must be persisted via Calibration screen for a full pipeline.
- Distortion coefficients are optional. When absent, triangulation uses the on-device `θx/θz` angles; when present, it undistorts `(px, py)` per-frame for improved accuracy at the frame edges.

## Connection health + hot-reload

`CameraViewController` runs a scheduled `/status` probe (base cadence from Settings → Poll Interval, default 10 s). On failure the interval doubles until a 60 s cap (`10 → 20 → 40 → 60`); a success resets to the base. A manual **Test** button on the HUD cancels any in-flight probe and re-probes immediately. The HUD also shows `Last contact: N s ago` updated by a 1 Hz tick timer (paused with the view). A generation token on each probe ensures stale responses from an outdated host/port can't overwrite current state.

Settings hot-reload is **not** tied to the probe cadence. `SettingsViewController.onDismiss` fires in `viewDidDisappear` (covers Save, Close, and interactive-swipe dismiss), and the presenter re-diffs `UserDefaults` there. On change: capture format is reconfigured, `ServerUploader` rebuilt, a new `chirpThreshold` is pushed into the live detector, or the probe cadence is updated with backoff reset — none of which require rebuilding the `AVCaptureSession`.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`.
