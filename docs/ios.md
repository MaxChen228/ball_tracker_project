# iOS app

Source under `ball_tracker/` (Swift + Obj-C++, UIKit). Xcode project at
`ball_tracker.xcodeproj`. Bundle ID `com.Max0228.ball-tracker`,
iOS 26.2 target, Swift 5.0.

**Agent rule: do NOT run iOS tests via xcodebuild.** They take minutes per pass and the operator runs them manually in Xcode. Agents may run `xcodebuild ... build` to verify the iOS source compiles, but never `xcodebuild ... test`. If a refactor invalidates iOS test files, fix the test files for compilation only and let the operator run them.

## State machine — `CameraViewController`

`.standby → .recording → .standby …`

Time-sync is a separate orthogonal flow: the operator taps 時間校正 on the phone OR clicks **Calibrate time** on the dashboard (`POST /sync/trigger` → WS `sync_command: "start"` plus pending flag on the next heartbeat tick → phone enters `.timeSyncWaiting` only if currently `.standby`). Either path flows `.timeSyncWaiting` → (chirp detected) → `.standby`. Not gated by arm; the server guards against dispatching the remote command during an armed session so a mis-click can't disrupt a recording.

Driven by a **1 Hz WS heartbeat tick** that runs from app launch and carries liveness / sync metadata upstream, while server commands come back over WS `settings` / `arm` / `disarm` / `sync_command` messages. Arming is dashboard-only — the iPhone UI has no local start/stop button:

- `commands[self] == "arm"` + state `.standby` → cache `response.session.id` as `currentSessionId`, `enterRecordingMode()` (switch to 240 fps, state = `.recording`, defer ClipRecorder creation to the first captureOutput so pixel dims come from the real sample)
- `commands[self] == "disarm"` + state `.recording` → `recorder.forceFinishIfRecording()` if the recording workflow has kicked off (first sample appended); otherwise just `clipRecorder.cancel()` + `exitRecordingToStandby()`
- `commands[self] == "disarm"` + state `.timeSyncWaiting` → `cancelTimeSync(reason: "disarmed")`
- `lastAppliedCommand` guards against re-triggering on repeated replies during an armed session

iPhones never mint pairing identifiers — every `startRecording` reads `currentSessionId` (set from the WS arm message) and stamps it onto the outgoing `PitchPayload.session_id`. Uploads tagged with a superseded session are ignored by `_register_upload_in_session_locked` on the server, so a late flush can't disarm a new session.

The worst-case arm latency is therefore one heartbeat interval (default 1 s, clamped [1, 60]) **plus the ~300-500 ms fps-swap** cost. There is no `parkCameraInStandby` toggle — `.standby` always keeps the capture session live at `standbyFps` (60) so the preview keeps streaming; arming just swaps fps, never cold-starts the session. (`park_camera_in_standby` is in `AppSettingsStore.legacyKeys` — purged once per launch.)

State per mode:

- `.standby`: capture session is always live at `standbyFps` (60) — preview keeps streaming, sensor stays hot. No `parkCameraInStandby` toggle (retired; key in `AppSettingsStore.legacyKeys`). No recording, no chirp detection.
- `.timeSyncWaiting` (時間校正): session spun up at `standbyFps` so the mic can deliver samples; **chirp detector ON**. On trigger, saves `lastSyncAnchorTimestampS`, stops the session, and returns to `.standby`. 15 s timeout. Can only be entered from `.standby` via the manual 時間校正 button — not from an arm command.
- `.recording`: session spun up at `trackingFps` (240). Captures H.264 MOV via `ClipRecorder`. **Only exit path is `forceFinishIfRecording()`**, triggered when the dashboard sends `disarm` (operator pressed Stop) or the server-side session times out. No on-device detection, no auto-end. Emits `PitchPayload` via `onCycleComplete`; the clip finishes async before the payload is persisted. Session is stopped on the way back to standby. Persist + enqueue runs synchronously before the state transition flips back to `.standby`.
- Cycles are saved to disk in `Documents/pitch_payloads/` (`PitchPayloadStore`) **before** upload, as paired `<basename>.json` + `<basename>.mov`. Upload failures re-insert at queue front with 2 s backoff; `payloadStore.delete` removes the pair atomically on success.

## Frame pipeline (`CameraViewController.captureOutput`)

Runs on `camera.frame.queue` while the session is live — 60 fps during `.standby` / `.timeSyncWaiting` and 240 fps during `.recording`. The capture session is always running (no `parkCameraInStandby` toggle); `switchCaptureFps(_:)` is the mid-session fps swap (stop → reconfigure → start ~300-500 ms) used at every state transition. `startCapture(at:)` and `stopCapture()` remain as the bring-up / tear-down primitives.

FPS is hard-capped by an exposure ceiling: `configureCaptureFormat` sets `activeMaxExposureDuration = frameDuration`, so iOS's auto-exposure can't stretch individual samples past 1/60 s (idle) or 1/240 s (recording). AE compensates with ISO — noisier in low light, but frame rate holds. Without this cap a dim room silently dropped the effective capture rate to ~14 fps.

`captureOutput` advances `frameIndex` for debug logs, updates the HUD FPS estimate, and unconditionally fans the sample out to two sinks (path-set gating was retired alongside the legacy `CaptureMode` — every armed session now does both):

- `clipRecorder` is lazy-bootstrapped from the first sample's dims and appends every sample; the full MOV is uploaded as-is (no trim). Server-side detection (`server_post` path) is run on-demand against this archive.
- `LiveFrameDispatcher` ships each detection result as a WS `frame` message as soon as it's produced; the server pairs + triangulates the `live` path before the session ends.

`dispatchDetectionIfDue` runs on every sample — throttled to 60 Hz on a utility queue so the ~12-18 ms HSV + CC + shape pipeline (1080p full-frame, CPU) can't stall 240 fps capture. The throttle is the current ceiling: to push detection toward 240 Hz, shrink per-frame cost first (ROI tracking around last known blob, 720p capture, Metal/vImage HSV conversion) rather than lifting the throttle. `CameraRecordingWorkflow` is kicked off on the first captured sample using the sample's session-clock PTS as `videoStartPtsS`, regardless of which paths are active.

## Audio pipeline (`AudioChirpDetector`)

Runs on `audio.chirp.queue`. `AVCaptureAudioDataOutput` delivers mic samples directly to the detector, which runs a normalized matched filter (cross-correlation against a reference chirp, divided by local window energy) at ~10 Hz. On a peak > `threshold`, parabolic sub-sample interpolation refines the location; the chirp-center session-clock PTS becomes the anchor. Audio sample rate 44.1 kHz → 22 μs per sample; detection precision typically **<100 μs** (40× better than the frame-granularity the earlier flash detector could hit).

The reference chirp is a linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, unit-energy normalized. The server's `/chirp.wav` endpoint emits the same waveform as a playable WAV surrounded by 0.5 s silence — users download it on any third device and play it near the two iPhones during 時間校正.

Threshold (default 0.18, tunable from Settings → Sync → Chirp Threshold) is the normalized matched-filter peak above which a detection fires. Peaks are in roughly 0–1 with 1.0 meaning a perfect reference match on a clean recording; typical field values are 0.2–0.4 on a nearby speaker, <0.05 on stationary ambient. Lower the threshold if the HUD flashes orange ("close") but never triggers; raise it if false-triggers on ambient noise. Hot-reload via `setThreshold(_:)` — no capture-session rebuild.

## Key Swift modules

- `AudioChirpDetector.swift` — matched-filter chirp detection via `vDSP_dotpr`. Owns the audio ring buffer, reference chirp, cooldown, and emits `ChirpEvent(anchorFrameIndex, anchorTimestampS)` callbacks on its own queue. Threshold is mutable (`setThreshold(_:)`) so Settings changes propagate without session rebuild. Self-contained — no dependencies on other sync helpers.
- `CameraRecordingWorkflow.swift` — owns the recording cycle bookkeeping that used to live in a separate `PitchRecorder` (retired; logic inlined here). Records identity + timing metadata when a sample first arrives, drives `ClipRecorder` lifecycle, and emits `PitchPayload` via `onCycleComplete`. `forceFinishIfRecording()` is the sole exit path (dashboard cancel / session timeout). `localRecordingIndex` is a run-of-app debug counter that ships on the payload as `local_recording_index` purely for operator logs.
- `ClipRecorder.swift` — `AVAssetWriter` wrapper that consumes the same `CMSampleBuffer`s the capture queue dispatches, writes H.264 MOV to a tmp URL, and finalises async on cycle-complete. `prepare → append (first append starts the writer session at that PTS AND records it as `firstSamplePTS`) → finish/cancel`. `firstSamplePTS` is what `CameraRecordingWorkflow` uses as `videoStartPtsS`, so the server can reconstruct absolute session-clock PTS for every decoded frame. Caller serialises all calls onto `processingQueue`; `exitRecordingToStandby` tears down via an async dispatch so it can't race with an in-flight append.
- `PitchPayloadStore.swift` — local cache for completed cycles. `save(payload, videoURL:)` writes `<basename>.json` atomically and moves the tmp clip to `<basename>.<ext>`. `videoURL(forPayload:)` resolves the companion clip for the uploader; `delete(jsonURL)` removes both. `makeTempVideoURL()` provides fresh `Documents/tmp/clip_<uuid>.mov` URLs for `ClipRecorder`.
- `ServerUploader.swift` — HTTP transport only. `uploadPitch(_, videoURL:, completion:)` posts `/pitch` as `multipart/form-data` (required `payload` JSON field + required `video` file part). Multipart body is hand-built — no third-party HTTP lib. Contains no timers or retry logic (those live in the queue / scheduler below).
- `HeartbeatScheduler.swift` — 1 Hz WS heartbeat scheduler with a configurable base cadence (was `ServerHealthMonitor` before the rename). Owns the "last contact: N s ago" 1 Hz tick timer and the `Server` HUD label. The camera VC uses it to emit periodic `{"type":"heartbeat"}` messages upstream; downstream `settings` / `arm` / `disarm` / `sync_command` arrive over the same WS connection (handled by `CameraCommandRouter`). Hot-reloadable: `updateUploader`, `updateCameraId`, `updateBaseInterval` + `probeNow()` applies a settings change without tearing down the `AVCaptureSession`.
- `PayloadUploadQueue.swift` — serialises cached pitch payloads up to the server, one at a time. `reloadPending()` on entering recording mode rebuilds the in-memory queue from `PitchPayloadStore`'s on-disk files so a restart's cached uploads resume. Upload success deletes the JSON + companion video via the store; failure re-inserts at the head with a 2 s retry. Callbacks fire on main.
- `AppSettingsStore.swift` — the only persisted iOS settings store. Reads `server_ip` + `camera_role` from `UserDefaults` at launch. `purgeLegacyKeys()` strips pre-Phase-1 keys (intrinsics, HSV, capture mode, chirp threshold, etc.) once per launch — those are now server-owned and arrive over WS. **There is no in-app settings UI**: operator edits these via the Xcode build config / first-launch defaults, and everything else is pushed by the server. There is no in-app calibration UI either; calibration is fully server-side via the dashboard's **Auto calibrate** ArUco flow (iOS does not POST `/calibration`).

## Connection health + hot-reload

`CameraViewController` emits a 1 Hz WS heartbeat tick (base cadence from the server-owned Heartbeat setting, default 1 s). A manual **Test** action forces an immediate tick. The HUD also shows `Last contact: N s ago` updated by a 1 Hz timer (paused with the view). The HUD's `Server` line reflects the current WS connection / server state; settings and commands arrive back over the same WS channel.

Server-owned settings (HSV / chirp threshold / heartbeat interval / capture height / tracking exposure cap) arrive as WS `settings` messages on the same connection as `arm` / `disarm` / `sync_command`. `CameraCommandRouter` dispatches each one to the relevant collaborator (e.g. `AudioChirpDetector.setThreshold`, `HeartbeatScheduler.updateBaseInterval`, `CameraCaptureRuntime.applyCaptureHeight`). Application is incremental: a knob only triggers an `AVCaptureSession` rebuild if it actually requires one (e.g. capture height); threshold / cadence / HSV apply without restart.

## Info.plist

`NSAllowsArbitraryLoads = true` (plain HTTP to LAN server). Required entries: `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`.
