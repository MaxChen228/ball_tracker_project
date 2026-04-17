# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System overview

Two-iPhone stereo tracker for a deep-blue baseball. Each phone runs the iOS app in role `A` or `B`, aimed at home plate. Both phones see a shared **flash event** (torch/LED) as a common time anchor; each independently detects the ball per frame, computes `(θx, θz)` from its optical axis, and uploads one *cycle* (pitch) payload to a FastAPI server on the LAN. The server recovers each camera's extrinsics from its home-plate homography, pairs A/B frames by time-since-flash (≤8 ms tolerance @ 240 fps), and triangulates 3D positions via ray-midpoint.

- **iOS app** — `ball_tracker/` (Swift, UIKit + SwiftUI shell). Xcode project at `ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`, iOS 26.2 target, Swift 5.0.
- **Server** — `server/` (FastAPI). `uv`-managed venv at `server/.venv` (Python 3.13.5). In-memory state only; a restart loses all pitches.

## Commands

### Server
```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8765   # run (prints LAN IP → paste into iPhone Settings)
uv run pytest                                        # all tests
uv run pytest test_server.py::test_triangulate_sweeps_ball_path   # single test
```

### iOS
Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

## Architecture

### iOS state machine — `CameraViewController`
`.standby → .timeSyncWaiting → .standby → .syncWaiting → .recording → (save+upload) → .syncWaiting …`

- `.timeSyncWaiting` (時間校正): **flash detection ON, ball detection OFF**. On trigger, saves `lastSyncFlashFrameIndex/TimestampS` and returns to `.standby`. 15s timeout.
- `.syncWaiting`: ball detection ON, pre-roll circular buffer (120 frames) filling. First ball-detected frame transitions to `.recording` using the saved flash as the cycle anchor.
- `.recording`: buffers frames until ball has been absent for 24 consecutive frames AND cycle length ≥ 24 frames. Emits `PitchPayload` via `onCycleComplete`.
- Cycles are saved to disk in `Documents/pitch_payloads/` (`PitchPayloadStore`) **before** upload. Upload failures re-insert at queue front with 2s backoff.

### Frame pipeline (`CameraViewController.captureOutput`)
Runs at 240 fps on `camera.frame.queue`. Advances `frameIndex` every frame (cross-mode coherence). **Never** `DispatchQueue.main.async` per frame — detection results are stashed in `latestCentroidX/Y` and a `CADisplayLink` on main redraws the overlay at 60/120 Hz. Only actual state transitions dispatch to main.

### Key modules
- `BallDetector.swift` — HSV threshold on a downsampled grid → 8-neighborhood connected components → largest component passing area filter (20–5000 px²) → centroid → `θx = atan2(px - cx, fx)`, `θz = atan2(py - cy, fz)`. Uses calibrated intrinsics if present, else FOV approximation.
- `FlashDetector.swift` — 30-frame luminance baseline; triggers when `current > baseline * multiplier` (default 2.5); 1s dead time after trigger.
- `PitchRecorder.swift` — pre-roll buffer + cycle assembly. The flash anchor is passed in at `startRecording` time, not discovered by the recorder.
- `CalibrationViewController.swift` — 5 draggable handles on home-plate pentagon → homography (direct-linear-transform via 8×8 normal equations with Gaussian elimination) → persisted as `homography_3x3` (row-major, 9 doubles). Also derives `fx/fz/cx/cy` from capture FOV.
- `SettingsViewController.swift` — persists server IP/port, role A/B, HSV range, flash multiplier, capture resolution/fps to `UserDefaults`. `normalizeServerIP` strips scheme/port/path from pasted URLs.

### Server
- `main.py` — `State` dict keyed by `(camera_id, cycle_number)`. When both A and B for a cycle arrive, `triangulate_cycle` runs immediately under the state lock. `/status` reports received + completed cycles.
- `triangulate.py` — `recover_extrinsics` decomposes `H = K [r1 r2 t]` (Zhang's planar method), orthonormalizes via SVD, flips sign if `t[2] < 0` (camera must be in front of plate). `triangulate_rays` solves the 2×2 system for the shortest segment between two 3D rays and returns its midpoint + gap.

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Naming collision**: Swift `BallDetector.Intrinsics.fz` is the image-**vertical** focal length, i.e. OpenCV's `fy`. Server's `build_K(fx, fy, cx, cy)` is invoked with `intr.fz` passed as the `fy` arg. This is intentional — do not "fix" the name on one side without updating the other.
- **iOS persisted intrinsics** (`UserDefaults`): `intrinsic_fx`, `intrinsic_fz`, `intrinsic_cx`, `intrinsic_cy`. `image_width_px` / `image_height_px` are written by the capture callback when dimensions change.

## Payload contract

`POST /pitch` (JSON, `ServerUploader.PitchPayload` ↔ `main.PitchPayload`):

```
camera_id: "A"|"B"
flash_frame_index, flash_timestamp_s        # shared time anchor (or first-ball fallback)
cycle_number                                # monotonic per device; server pairs A+B by this
frames: [{frame_index, timestamp_s, theta_x_rad?, theta_z_rad?, ball_detected}]
intrinsics: {fx, fz, cx, cy}?               # fz == OpenCV fy (see above)
homography: [h11..h33]?                     # row-major, h33 normalized to 1
image_width_px?, image_height_px?
```

Triangulation requires **both** cameras to have `intrinsics` and `homography` present — if a phone's calibration was never saved, the server returns `error` on the cycle but still stores the raw payload.

## Degraded / fallback modes

- If `lastSyncFlash*` is nil (user skipped 時間校正), `PitchRecorder` uses the first-ball-frame as the anchor. A/B cycles then won't align across cameras — the 8ms match window in `triangulate_cycle` will drop most pairs.
- If calibration was never performed, `BallDetector` falls back to FOV-approximation intrinsics (`horizontal_fov_rad` is written from `AVCaptureDevice.activeFormat.videoFieldOfView` during capture setup). Server-side triangulation still fails — intrinsics must be persisted via Calibration screen for a full pipeline.

## Hot-reload behavior

`CameraViewController.pollServerStatus` runs every 2s and also diffs `UserDefaults` settings, reconfiguring capture format and rebuilding `ServerUploader` when changed. This exists because `.formSheet` Settings dismiss does **not** trigger `viewWillAppear`.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSLocalNetworkUsageDescription`.
