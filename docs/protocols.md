# Protocols

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Intrinsics naming**: server + iOS both use `fy` for the image-vertical focal length. The legacy `fz` field name (a historical collision from early iOS code) has been retired; `IntrinsicsPayload` still accepts `fz` as a read-time alias on `model_validate` so historical `data/calibrations/*.json` and old pitch JSONs still load cleanly. New code writes `fy`.
- **iOS side**: no longer persists intrinsics — ChArUco intrinsics are server-owned per device id under `data/calibrations/<cam>.json` (Phase 1 decoupling). The `intrinsic_*` UserDefaults keys referenced in older docs no longer exist in this codebase.

## Payload contract

`POST /pitch` is `multipart/form-data`. The `payload` part is always required. `video` is technically optional in the handler — `routes/pitch.py` accepts a frame-only payload as long as the JSON ships a non-empty `frames_live` / `frames_server_post` (this is the test path); 422 fires only when **both** `video` and `frames_*` are missing. **In production every real iOS upload ships the MOV** (post-PR61 `ClipRecorder` is unconditional). Calibration data at `data/calibrations/<camera_id>.json` is the **single source of truth** for intrinsics / homography / image dims — iOS no longer echoes them on uploads (Phase 1 decoupling).

- **`payload`** (`application/json`) — encoded `ServerUploader.PitchPayload` ↔ `main.PitchPayload`:

  ```
  camera_id: str matching ^[A-Za-z0-9_-]{1,16}$   # "A"/"B" in practice
  session_id: str matching ^s_[0-9a-f]{4,32}$     # server-minted via secrets.token_hex(4) → "s_" + 8 hex; sole pairing key (32-char upper bound is forward-compat)
  paths: list[DetectionPath]                      # snapshots session.paths at recording start. Post-redesign, session.paths is always {live} at arm time; server_post gets added only after an operator-triggered run.
  sync_anchor_timestamp_s: float | null           # chirp-detected session-clock PTS; null if skipped
  video_start_pts_s: float                        # abs PTS of first MOV frame (session clock)
  video_fps: float
  frames_live: list[FramePayload] | null          # populated only on recovery paths; normally arrives over WS
                                                  # FramePayload.candidates: list[BlobCandidate] is the Phase B wire shape.
                                                  # iOS ships px/py/area/area_score/aspect/fill directly (post-abfa422);
                                                  # server stamps `cost` in live_pairing._resolve_candidates after the
                                                  # shape-prior selector runs.
  local_recording_index: int?                     # device-local debug counter; server ignores
  live_preset_name: str | null                    # active preset filename frozen at arm time (mirrored into SessionResult.live_preset_name). Drives the events list / viewer "Live: <name>" chip; null only on legacy on-disk pitches that predate arm-time preset stamping.
  ```

  `SessionResult` adds the matching `live_preset_name` (A-wins-B-fallback aggregate) plus `server_post_preset_name` (the `preset_name` body field of the most recent `POST /sessions/{sid}/run_server_post`; overwritten on rerun, no history). The events list / viewer chip-render `Live: <name> | Svr: <name|—>` from these two; a preset whose file has been deleted under a session degrades to a faded `(deleted)` suffix.

  Server-side, `/pitch` looks up the matching `CalibrationSnapshot` from `state.calibrations()` and fills in `intrinsics` / `homography` / `image_width_px` / `image_height_px` BEFORE triangulation and on-disk persistence. **No calibration on file → 422**.

- **`video`** (`video/quicktime`) — H.264 MOV. iOS uploads on every recording (PR61); `/pitch` stores it under `data/videos/session_{session_id}_{camera_id}.<ext>` without decoding. Decode + HSV detection (→ `frames_server_post`) happens only when the operator hits `POST /sessions/{sid}/run_server_post`.

- **Live path has no HTTP payload** — the always-on `live` pipeline produces WS `frame` messages on `/ws/device/{cam}` throughout the recording. `state.persist_live_frames(camera_id, session_id)` flushes the in-memory buffer onto the pitch JSON at `path_completed` (or session end) so reloads see the same two-bucket shape as an offline upload.

Pairing is by **`session_id` alone** (server-minted via `POST /sessions/arm`). iPhones never generate pairing identifiers.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present AND `sync_anchor_timestamp_s` non-null — if any is missing, `SessionResult.error` is set and triangulation is skipped (raw payload + MOV are still persisted for forensics).

## WebSocket messages — `/ws/device/{cam}`

The phone holds one WS connection per camera for the lifetime of its app session. Inbound (server→iOS) carries control commands; outbound (iOS→server) carries liveness + the live frame stream. Handler: `server/routes/device_ws.py::ws_device`. Send helper: `server/ws.py::DeviceSocketManager.send` (logs cam id + message type on every drop so silent failures are auditable). Every message is a JSON object with a `type` discriminator.

### Server → iOS

#### `type: "settings"` — full runtime knob snapshot

Pushed on connect, on every `hello` from the phone, and on every dashboard-driven knob change. Source: `main.py::_settings_message_for`. iOS treats this as the authoritative replacement for everything in this list — no merge.

```
type: "settings"
camera_id: str                       # echo of the {cam} in the URL (server-side cross-check)
paths: list[str]                     # default DetectionPath set for newly-armed sessions (always {"live"} post-Phase-1)
hsv_range: {h_min,h_max,s_min,s_max,v_min,v_max}  # from data/detection_config.json (POST /detection/config)
shape_gate: {aspect_min, fill_min}   # from data/detection_config.json (POST /detection/config)
active_preset_name: str | null       # currently-bound preset filename (DetectionConfig.preset). Pushed alongside the HSV+shape values so iOS can log preset identity. Pure metadata — iOS does not act on it. Null only when the live config is custom (slider direct-POST path); never null after a /presets or /presets/active call.
chirp_detect_threshold: float        # matched-filter cutoff for legacy chirp-listener path; data/chirp_detect_threshold.json
mutual_sync_threshold: float         # cutoff for the two-device mutual-sync coordinator; data/mutual_sync_threshold.json
heartbeat_interval_s: float          # cadence iOS uses for upstream {type:"heartbeat"} (state.heartbeat_interval_s)
tracking_exposure_cap: str           # TrackingExposureCapMode enum value (e.g. "auto"/"capped_2ms"); data/tracking_exposure_cap.json
capture_height_px: int               # 1080 / 720 etc. — iOS picks the matching 240 fps format; data/capture_height.json
preview_requested: bool              # True while the dashboard's Preview-on toggle is held for this cam
calibration_frame_requested: bool    # True while /calibration/auto is awaiting a single still from this cam
device_time_synced: bool             # mirrors the gated time_synced bit /status reports for this cam
device_time_sync_id: str | null      # last time_sync_id the server expects from this cam (chirp run id)
```

#### `type: "arm"` — start an armed session

Pushed when an operator hits **Arm** in the dashboard, and re-pushed on reconnect if a session is already armed. Source: `main.py::_arm_message_for`.

```
type: "arm"
sid: str                             # server-minted session id (^s_[0-9a-f]{4,32}$)
paths: list[str]                     # snapshotted Session.paths (sorted) — always ["live"] at arm time
max_duration_s: float                # auto-disarm timeout; iOS displays + uses for its own watchdog
tracking_exposure_cap: str           # exposure cap value at arm-time; iOS applies before the first frame
```

#### `type: "disarm"` — stop the session

Pushed on operator Stop, on `max_duration_s` timeout, or on auto-end after the first MOV upload. Source: `main.py::_disarm_message_for`.

```
type: "disarm"
sid: str                             # matches the sid that was previously armed
```

#### `type: "sync_run"` — late-join a mutual-sync run

Pushed in `routes/device_ws.py::ws_device` only at connect-time, when a mutual-sync run is already active and this cam hasn't reported yet. The standard sync flow uses out-of-band coordination (HTTP `/sync/*` + the dashboard); this push exists so a cam reconnecting mid-run doesn't sit idle until the run times out. Fields are pulled from `state.sync_params()`.

```
type: "sync_run"
sync_id: str                         # the active sync run id
emit_at_s: float                     # session-clock PTS at which this cam should emit its chirp (A vs B)
record_duration_s: float             # how long iOS records audio for matched-filter analysis
```

#### `type: "sync_command"` — chirp time-sync trigger

Pushed by `routes/sync.py::start_sync` (line 225) when an operator triggers a chirp time-sync run from the dashboard. Each cam in the dispatched set gets one push with its own `sync_command_id`. iOS handles it in `ball_tracker/CameraCommandRouter.swift:91` (`case "sync_command"`): the cam latches the id, starts audio capture for matched-filter chirp detection, and reports the detected PTS back via the next `heartbeat` (`time_sync_id` + `sync_anchor_timestamp_s`). Distinct from `sync_run` above, which is the late-join push for the **mutual-sync coordinator** (a separate two-device flow); the chirp single-shot path goes through `sync_command`.

```
type: "sync_command"
command: str                         # currently always "start"
sync_command_id: str                 # server-minted run id; iOS echoes it back as time_sync_id
```

#### `type: "calibration_updated"` — peer cam re-calibrated

Pushed by `routes/calibration.py::_handle_calibration_completed` (line 55) to **every other cam** after a successful auto-calibration of one cam. Lets the remaining cam(s) refresh any cross-cam state (e.g. dashboard preview hints) without polling. Handled in `ball_tracker/CameraCommandRouter.swift:127` (`case "calibration_updated"`).

```
type: "calibration_updated"
cam: str                             # camera_id of the cam that was just (re-)calibrated
```

### iOS → Server

All inbound messages are JSON; `device_ws.note_seen` updates the cam's last-seen timestamp on every recognised message. Unknown `type` values are silently dropped (no `or fallback` — they just skip every branch).

#### `type: "hello"` — connection greeting

Sent right after the WS opens. Server uses it to confirm the cam's identity / battery / sync state. Server replies with a fresh `settings` push (so iOS doesn't have to remember the bootstrap settings if it reconnects).

```
type: "hello"
time_sync_id: str | null             # last chirp run id this cam latched
sync_anchor_timestamp_s: float | null # PTS of the chirp peak on the cam's session clock
battery_level: float | null          # 0..1; null if UIDevice monitoring is off (-1 maps to null)
battery_state: str | null            # "unknown" / "unplugged" / "charging" / "full"
device_id: str | null                # identifierForVendor UUID (or "unknown-<uuid>" fallback); ≤ 64 chars
device_model: str | null             # sysctl hw.machine ("iPhone15,3" etc.); ≤ 32 chars
```

#### `type: "heartbeat"` — periodic liveness + telemetry

Sent every `heartbeat_interval_s` (≈ 1 Hz). Same identity/battery/sync fields as `hello`, plus optional `sync_telemetry` for the mutual-sync coordinator. Server fans out a `device_heartbeat` SSE so the dashboard updates without waiting for the 5 s `/status` poll.

```
type: "heartbeat"
time_sync_id, sync_anchor_timestamp_s, battery_level, battery_state, device_id, device_model
                                     # same shapes as `hello`
sync_telemetry: {…} | absent         # opaque to this doc; consumed by state._sync.record_sync_telemetry
```

#### `type: "frame"` — one detected frame

Sent at capture rate (240 Hz on the binned 240 fps formats) while the session is armed. iOS bypasses pydantic validation server-side (`model_construct`), so missing keys raise loud `KeyError` — the lockstep failure mode for an iOS build that doesn't match the schema. Required keys:

```
type: "frame"
i: int                               # frame_index (monotonic per-camera)
ts: float                            # timestamp_s on iOS session clock
sid: str                             # session id; empty/missing → frame is silently dropped (no fallback)
candidates: list[{
    px: float,                       # blob centroid X in pixels
    py: float,                       # blob centroid Y in pixels
    area: int,                       # CC stat area in pixels
    area_score: float,               # area / max_area_in_batch on the producing side
                                     #   — kept for the viewer BLOBS-overlay sort fallback
                                     #   — NOT a selector-cost input (see candidate_selector.py)
    aspect: float,                   # min(w,h)/max(w,h) of the CC bounding box; required since shape-prior selector landed
    fill: float,                     # area / (w*h) of the CC bounding box; required since shape-prior selector landed
}]
```

`cost` is **server-stamped**, not iOS-shipped: `live_pairing._resolve_candidates` runs the shape-prior selector (`_W_ASPECT·aspect_pen + _W_FILL·fill_pen`, frame-local — no temporal state, no `area_score` term; weights are module constants in `server/candidate_selector.py`, not a runtime tunable) and copies the resulting cost onto every candidate before persistence. Empty `candidates` list → frame is buffered but `ball_detected=False` and no triangulation runs.

#### `type: "cycle_end"` — path completion signal

Sent when iOS finishes its end of the live path (session timeout, user stop, error). Server marks the live path ended for this cam and either persists the WS-buffered frames or rebuilds the SessionResult from already-persisted state.

```
type: "cycle_end"
sid: str                             # session id
reason: str | null                   # free-form ("timeout", "user_stop", etc.) — stored on the path status pill
```

## Server → dashboard / viewer SSE — `/stream`

Server emits SSE events for state-change broadcasts. Listeners in
`server/static/dashboard/86_live_stream.js` handle `session_armed`,
`frame_count`, `ray`, `point`, `path_completed`, `session_ended`,
`device_status`, `device_heartbeat`, `calibration_changed`, and `fit`.

#### `event: fit`

Broadcast on (a) every WS `cycle_end` after the SessionResult rebuild
that updated `result.segments`, and (b) the `POST /sessions/{sid}/recompute`
route after `state.store_result(new_result)` lands. Dashboard auto-selects
the carried sid when nothing is currently selected, patches its trajectory
cache's `segments` array, and repaints the latest-pitch fit visuals (curves
+ release arrows + speed badge).

```
event: fit
data: {
  "sid": str,
  "segments": [SegmentRecord, ...]   # may be empty (1-point sessions, pure noise)
}
```

`SegmentRecord` matches `server/schemas.py` exactly: `(indices, original_indices, p0[3], v0[3], t_anchor, t_start, t_end, rmse_m, speed_kph)`. Sample curves are NOT carried — clients reconstruct via `p0 + v0·τ + ½·G·τ²`.

### Operator audit checklist (when changing wire shapes)

Per project memory `feedback_ws_only_means_check_all_command_paths`, after any WS schema edit grep for every send/receive site:
- Server → iOS sends: `_settings_message_for`, `_arm_message_for`, `_disarm_message_for`, the inline `sync_run` dict in `routes/device_ws.py::ws_device`, the `sync_command` push in `routes/sync.py::start_sync` (line 225), and the `calibration_updated` push in `routes/calibration.py::_handle_calibration_completed` (line 55).
- iOS → server receivers: the four `if mtype == "..."` branches in `routes/device_ws.py::ws_device` (`hello` / `heartbeat` / `frame` / `cycle_end`).
- iOS encoders: `ball_tracker/ServerUploader.swift` Codable structs **and** `ball_tracker/LiveFrameDispatcher.swift` hand-encoded dict (the dispatch-queue path that bypasses Codable — abfa422 was the bug where this was forgotten).
