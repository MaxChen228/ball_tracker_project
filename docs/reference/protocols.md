# Protocols

## Coordinate conventions (critical)

- **World frame** (from iPhone calibration): X = plate left/right, Y = plate depth (front→back, pitcher→catcher), Z = plate normal (up). Plate plane is Z=0.
- **Camera frame** (OpenCV pinhole): X = image right, Y = image down, Z = optical axis.
- **Intrinsics naming**: server + iOS both use `fy` for the image-vertical focal length. The legacy `fz` field name (a historical collision from early iOS code) has been **fully retired** (migration script removed 2026-04-29; see comment at `server/schemas.py:50`). `IntrinsicsPayload` no longer accepts `fz` — any on-disk `data/calibrations/*.json` or old pitch JSON that still carries `fz` will **422 / fail to load**. New code must write `fy`.
- **iOS side**: no longer persists intrinsics — ChArUco intrinsics are server-owned per device id under `data/calibrations/<cam>.json` (Phase 1 decoupling). The `intrinsic_*` UserDefaults keys referenced in older docs no longer exist in this codebase.
- **Distortion required on the ChArUco upload wire** (`POST /calibration/intrinsics/{device_id}`, `DeviceIntrinsics`): iOS always solves + ships the full 5-coefficient OpenCV vector `[k1, k2, p1, p2, k3]` alongside K. The upload route (`routes/calibration_intrinsics._validate_intrinsics_payload`) now **422-rejects a missing `distortion`** — a `None` on this boundary is a wire regression (dropped field / schema drift), not a legal pinhole calibration, and would otherwise silently degrade triangulation to a zero-distortion pinhole at frame edges (CLAUDE.md no-silent-fallback). The `IntrinsicsPayload.distortion` field stays typed `list[float] | None` because the **internal FOV-pinhole approximation path** (`calibration_auto._derive_auto_cal_intrinsics` source `"fov"`) legitimately has no lens model; that `None` never traverses the upload route. The ray-path `np.zeros(5)` materialization (`pairing._ray_for_frame` / `reconstruct._world_ray`) is the correct pinhole behaviour for that FOV mode only.

## Payload contract

`POST /pitch` is `multipart/form-data`. The `payload` part is always required. `video` is technically optional in the handler — `routes/pitch.py` accepts a frame-only payload as long as the JSON ships a non-empty `frames_live` / `frames_server_post` (this is the test path); 422 fires only when **both** `video` and `frames_*` are missing. **In production every real iOS upload ships the MOV** (post-PR61 `ClipRecorder` is unconditional). Calibration data at `data/calibrations/<camera_id>.json` is the **single source of truth** for intrinsics / homography / image dims — iOS no longer echoes them on uploads (Phase 1 decoupling).

- **`payload`** (`application/json`) — encoded `ServerUploader.PitchPayload` ↔ `main.PitchPayload`:

  ```
  camera_id: str matching ^[A-Za-z0-9_-]{1,16}$   # "A"/"B" in practice
  session_id: str matching ^s_[0-9a-f]{4,32}$     # server-minted via secrets.token_hex(4) → "s_" + 8 hex; sole pairing key (32-char upper bound is forward-compat)
  paths: list[DetectionPath]                      # snapshots session.paths at recording start. Post-redesign, session.paths is always {live} at arm time; server_post gets added only after an operator-triggered run.
  sync_anchor_timestamp_s: float | null           # chirp-detected session-clock PTS; null if skipped
  video_start_pts_s: float                        # abs PTS of first MOV frame (session clock)
  video_fps: float | null                         # optional sanity-check; iOS no longer ships, server tolerant
  frames_live: list[FramePayload] | null          # populated only on recovery paths; normally arrives over WS
                                                  # FramePayload.candidates: list[BlobCandidate] is the Phase B wire shape.
                                                  # iOS ships px/py/area/area_score/aspect/fill directly (post-abfa422);
                                                  # server stamps `cost` in live_pairing._resolve_candidates after the
                                                  # shape-prior selector runs.
  local_recording_index: int?                     # device-local debug counter; server ignores
  config_used_by_algorithm: dict[str, DetectionConfigSnapshotPayload]
    # key   = algorithm_id (str): "ios_capture_time" for live,
    #         runnable detector ids ("v11_hsv_cc", …) for server_post
    # value = {
    #   algorithm_id: str,
    #   params: dict,            # opaque, algorithm-defined; v11_hsv_cc encodes
    #                            # {hsv:{h_min,...,v_max}, shape_gate:{aspect_min,fill_min}}
    #   preset_name: str | null  # null = custom/ad-hoc; non-null = identity claim
    # }
    # live_config_used / server_post_config_used are server-side computed-field
    # projections (read-only), not iOS upload fields.
  ```

  `SessionResult` carries the same `live_config_used` and `server_post_config_used` fields, but both are `@computed_field` projections over the canonical `config_used_by_algorithm` dict — there is no cross-cam aggregation or A-wins-B-fallback. `live_config_used` returns `config_used_by_algorithm.get(ios_capture_time)`; `server_post_config_used` returns `config_used_by_algorithm.get(active_server_post_algorithm_id)`. Neither field is written to disk: `_PITCH_PERSIST_EXCLUDE` and `_RESULT_PERSIST_EXCLUDE` strip both projections from `.model_dump(mode="json")` so the dict is the only on-disk truth. The events list / viewer CFG strip renders from these projections directly, so custom configs and deleted presets still show the exact frozen HSV + gate values that produced the session.

  **Phase 6b additive fields** (PitchPayload + SessionResult): `frames_by_algorithm: {<algorithm_id>: FramePayload[]}` + `config_used_by_algorithm: {<algorithm_id>: snapshot}` (PitchPayload), and `triangulated_by_algorithm` / `segments_by_algorithm` / `frame_counts_by_algorithm` / `algorithms_completed: string[]` / `config_used_by_algorithm` (SessionResult). These are auto-mirrored from the legacy per-path fields by an after-validator on every load + WS receive (`live` → `ios_capture_time`, `server_post` → the stamped algorithm id, or `_LEGACY_PRE_SNAPSHOT_ALGORITHM_ID = "v11_hsv_cc"` for pre-Phase-2 records). Wire-additive: extra keys, no removal. iOS atomic-drop guards only enforce required-field presence so the additional keys pass through without iOS changes. **Phase 7 (shipped)**: `POST /sessions/{sid}/runs/{algorithm_id}` writes directly into these dicts, including ad-hoc params runs whose snapshot has `preset_name=null`. See the endpoint contract below. **Silent-fallback audit (post-shipped)**: the in-band `algorithm_id_for_path` runtime fallback was removed — a missing `active_server_post_algorithm_id` pointer now raises `ValueError` instead of quietly substituting the legacy bucket id. `_LEGACY_PRE_SNAPSHOT_ALGORITHM_ID` survives ONLY as the one-shot disk-migration anchor (used by the boot-time auto-mirror to assign pre-Phase-2 records into the dict layer) and as the boot drift-guard reference (`algorithms.__init__._check_legacy_bucket_in_registry`).

  Server-side, `/pitch` looks up the matching `CalibrationSnapshot` from `state.calibrations()` and fills in `intrinsics` / `homography` / `image_width_px` / `image_height_px` BEFORE triangulation and on-disk persistence. **No calibration on file → 422**.

- **`video`** (`video/quicktime`) — H.264 MOV. iOS uploads on every recording (PR61); `/pitch` stores it under `data/videos/session_{session_id}_{camera_id}.<ext>` without decoding. Decode + HSV detection (→ `frames_server_post` / `frames_by_algorithm[<algorithm_id>]`) happens only when the operator hits `POST /sessions/{sid}/runs/{algorithm_id}` (or its preset-name alias `POST /sessions/{sid}/run_server_post`).

- **Live path has no HTTP payload** — the always-on `live` pipeline produces WS `frame` messages on `/ws/device/{cam}` throughout the recording. `state.persist_live_frames(camera_id, session_id)` flushes the in-memory buffer onto the pitch JSON at `path_completed` (or session end) so reloads see the same two-bucket shape as an offline upload.

Pairing is by **`session_id` alone** (server-minted via `POST /sessions/arm`). iPhones never generate pairing identifiers.

`POST /sessions/arm` may omit `paths`, in which case the operator's runtime default is used. If a JSON body includes `paths`, it must be a non-empty array of known `DetectionPath` values; unknown values and empty arrays return 422 instead of falling back to defaults.

`POST /sessions/arm` also accepts an optional `max_duration_s: float` (JSON body or form / query) — the auto-disarm timeout the server enforces and re-broadcasts in the WS `arm` push. Omitted → `_DEFAULT_SESSION_TIMEOUT_S` from `server/routes/sessions.py`.

Triangulation requires **both** cameras to have `intrinsics` and `homography` present AND `sync_anchor_timestamp_s` non-null — if any is missing, `SessionResult.error` is set and triangulation is skipped (raw payload + MOV are still persisted for forensics).

### N-camera infra schema (camera_id-keyed dicts)

The legacy per-cam-A/B flat fields on `SessionResult` and `SyncResult` were collapsed into camera_id-keyed dicts so the wire schema scales past the 2-camera rig without further re-cuts. Two-camera deployments stay wire-compatible by carrying `"A"` and `"B"` keys; a future third camera grows the dict.

**`SessionResult`** (`server/schemas.py:506`):
- `cameras_received: dict[str, bool]` — camera_id → "did this session ingest a pitch from this cam". Replaced the pair `camera_a_received: bool` / `camera_b_received: bool`. Today's keys are `{"A","B"}`; downstream "paired" semantics is `bool(received) and all(received.values())`. Sentinel for "session not found in result store" is `cameras_received={}` (empty dict) — explicit "rig configuration unknown" state, distinct from "all roles failed".

**`SyncResult`** (`server/schemas.py:822`) — mutual chirp sync outcome:
- `times_by_role: dict[str, RoleSyncTimes]` — camera_id → `{t_self_s, t_from_other_s}`. Replaced the four flat fields `t_a_self_s` / `t_a_from_b_s` / `t_b_self_s` / `t_b_from_a_s`.
- `traces_by_role: dict[str, RoleSyncTraces]` — camera_id → `{self_trace, other_trace}` matched-filter traces. Replaced the four flat fields `trace_a_self` / `trace_a_other` / `trace_b_self` / `trace_b_other`.
- Mutual sync is intrinsically pair-wise (audio chirp on distinct frequency bands wired per-role in `ball_tracker/MutualSyncAudio.swift`); today only `"A"` + `"B"` ever appear as keys. Missing key = that role never reported (vs. present-with-None inside the inner model = role reported but its scalar was null).
- `sync_analysis._mutual_math` reads via `(times_by_role).get(role).get(key)` with explicit `isinstance(dict, ...)` branching — no `or {}` silent fallback.

**Dashboard wire**: dashboard JS reads `window.__EXPECTED_CAMS__`, SSR-injected by `render_dashboard_html` from `State.expected_camera_ids()` (returns `sorted(known_cameras | calibrated_cameras | _RIG_BASELINE_CAMERAS={"A","B"})`). Adding a third camera to the rig: heartbeat it once OR upload its calibration, and the dashboard's LED strip / device grid / preview tiles all grow automatically.

**iOS wire**: iOS does NOT consume `SessionResult` or `SyncResult` — those are server-internal / dashboard surfaces. `CameraMonitorOverlayView.availableRoles: ["A","B"]` is the iOS-side rig roster (data-driven N-button row); grow by editing that constant. `CameraSyncCoordinator.startMutualSync` keeps the `role == "A" || "B"` guard (mutual sync's pair-wise frequency-band assignment is undefined for `"C"`).

## Server-post detection — `POST /sessions/{sid}/runs/{algorithm_id}` + alias

Operator triggers a server-side detection run against the archived MOVs of every camera in the session. Handler: `server/routes/sessions.py::sessions_run_algorithm`; both endpoints share `_dispatch_server_post`, which validates the session id, gates on `state.processing.session_candidates`, and queues `_run_server_detection` (`server/routes/pitch.py`) as a FastAPI BackgroundTask per camera.

### `POST /sessions/{sid}/runs/{algorithm_id}` (primary)

URL pins the algorithm id (`^[a-z0-9_]{1,32}$`). Body (JSON or form-urlencoded) must carry **exactly one** of:

- `preset_name: str` — load the named preset from disk. Preset's `algorithm_id` must match the URL.
- `params: dict` — ad-hoc one-off run. Validated against the registered detector's `params_schema` (`server/algorithms/<id>.py`); the resulting snapshot's `preset_name` is `null`.

Response (200 on accept):

```json
{
  "ok": true,
  "session_id": "s_xxxxxxxx",
  "queued": 2,
  "algorithm_id": "<URL algorithm_id>",
  "preset_name": "<name>" | null
}
```

Error matrix:

| Status | Trigger |
|---|---|
| 400 | URL `algorithm_id` fails the `[a-z0-9_]{1,32}` slug regex |
| 404 | URL `algorithm_id` is unknown to `algorithms.is_known` |
| 422 | URL `algorithm_id` is a non-runnable data source (`ios_capture_time`) |
| 422 | Body has both `preset_name` and `params` |
| 422 | Body has neither field |
| 404 | `preset_name` does not exist on disk |
| 422 | `preset.algorithm_id` mismatches URL `algorithm_id` |
| 422 | `params` fail the detector's `params_schema.model_validate` |
| 422 | `session_id` fails `^s_[0-9a-f]{4,32}$` (HTML callers 303 to `/`) |
| 409 | Session has no resumable processing candidates (HTML callers 303 to `/`) |

The captured `DetectionConfigSnapshotPayload(algorithm_id, params, preset_name)` is threaded into the BackgroundTask, so a concurrent dashboard slider edit cannot contaminate an in-flight run. Re-running the same `algorithm_id` overwrites that bucket; running a different one leaves prior buckets in place — `PitchPayload.frames_by_algorithm` and `SessionResult.triangulated_by_algorithm` / `segments_by_algorithm` retain the full multi-algorithm history at the dict layer.

### `POST /sessions/{sid}/run_server_post` (deprecation alias)

Kept for the viewer's "Rerun server" HTML form caller which submits `preset_name` only (PR #125 removed the events-row "Run srv" form from the dashboard chip strip — re-runs are viewer-only now). Behaviour is identical to the primary endpoint's preset path: the snapshot's `algorithm_id` is derived from `preset.algorithm_id`, then the same `_dispatch_server_post` runs. 422 on missing `preset_name`, 404 on unknown preset. Response shape matches the primary endpoint.

### `POST /sessions/{sid}/active_run` — flip active server_post pointer

Pure pointer flip — **no detection runs**. Switches `active_server_post_algorithm_id` to a different algorithm that has already been run on this session (i.e. has frames in some cam's `frames_by_algorithm`). After the flip, `server_post_config_used` / `server_post_config_used.preset_name` projections and the viewer history dropdown reflect the newly-active bucket. Handler: `server/routes/sessions.py::sessions_active_run`.

Body (JSON or form-urlencoded):

- `algorithm_id` (required): must already have at least one frame in some cam's `frames_by_algorithm`. The live bucket id (`ios_capture_time`) is rejected.
- `return_to` (optional, HTML form only): viewer redirect after the flip; whitelisted to `/` or `/viewer/{session_id}`.

Response (200):
```json
{"ok": true, "active_algorithm_id": "<algorithm_id>"}
```

Error matrix:

| Status | Trigger |
|---|---|
| 422 | `session_id` fails `^s_[0-9a-f]{4,32}$` slug regex (HTML callers 303 to `/`) |
| 422 | `algorithm_id` missing or empty |
| 422 | `algorithm_id` is the live bucket (`ios_capture_time`) or has no frames in this session |
| 404 | Session not found |

Broadcasts a `fit` SSE with `cause: "active_run_switch"` so every open dashboard / viewer subscriber repaints the scene with the newly-active triangulation bucket without a page reload.

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
chirp_detect_threshold: float        # matched-filter cutoff for legacy chirp-listener path; data/runtime_settings.json key "chirp_detect_threshold"
heartbeat_interval_s: float          # cadence iOS uses for upstream {type:"heartbeat"}; data/runtime_settings.json key "heartbeat_interval_s"
tracking_exposure_cap: str           # TrackingExposureCapMode enum: "frame_duration" (sensor-managed, up to full frame time ≤ 1/fps) | "shutter_500" (1/500 s cap) | "shutter_1000" (1/1000 s cap, 240 fps motion-freeze use case); data/runtime_settings.json key "tracking_exposure_cap"
capture_height_px: int               # 1080 / 720 etc. — iOS picks the matching 240 fps format; data/runtime_settings.json key "capture_height_px"
preview_requested: bool              # True while the dashboard's Preview-on toggle is held for this cam
calibration_frame_requested: bool    # True while /calibration/auto is awaiting a single still from this cam
device_time_synced: bool             # mirrors the gated time_synced bit /status reports for this cam
device_time_sync_id: str | null      # last time_sync_id the server expects from this cam (chirp run id)
```

> **Note — fields not in WS settings:** `active_preset_name`, `algorithm_id`, and `mutual_sync_threshold` are intentionally absent from this payload (iOS has no consumer for them). Active preset identity is available via `GET /status` JSON; not pushed over WS to keep the wire minimal.

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

Pushed by `routes/sync.py::start_sync` (near the per-cam broadcast loop) when an operator triggers a chirp time-sync run from the dashboard. Each cam in the dispatched set gets one push with its own `sync_command_id`. iOS handles it in `ball_tracker/CameraCommandRouter.swift:106` (`case "sync_command"`): the cam latches the id, starts audio capture for matched-filter chirp detection, and reports the detected PTS back via the next `heartbeat` (`time_sync_id` + `sync_anchor_timestamp_s`). Distinct from `sync_run` above, which is the late-join push for the **mutual-sync coordinator** (a separate two-device flow); the chirp single-shot path goes through `sync_command`.

```
type: "sync_command"
command: str                         # currently always "start"
sync_command_id: str                 # server-minted run id; iOS echoes it back as time_sync_id
```

#### `type: "calibration_updated"` — peer cam re-calibrated

Pushed by `routes/calibration.py::_handle_calibration_completed` (near the broadcast call at the end of the function) to **every other cam** after a successful auto-calibration of one cam. Lets the remaining cam(s) refresh any cross-cam state (e.g. dashboard preview hints) without polling. Handled in `ball_tracker/CameraCommandRouter.swift:142` (`case "calibration_updated"`).

```
type: "calibration_updated"
cam: str                             # camera_id of the cam that was just (re-)calibrated
```

### iOS → Server

All inbound messages are JSON; `device_ws.note_seen` updates the cam's last-seen timestamp on every recognised message. Unknown `type` values **fail loud** — `routes/device_ws.py` raises `ValueError` and closes the WS socket so a schema drift is impossible to miss (regression test: `server/test_device_ws_unknown_mtype.py`; invariant also documented in `CLAUDE.md` WS-only checklist §4). Do not reintroduce a silent-drop branch when adding new message types — register the new `type` explicitly in the dispatch table.

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

> **iOS-sent, server-ignored:** iOS also ships `cam` (camera role) and, when armed, `session_id`. The server already knows the cam from the WS URL path and the session id from `state.session_armed`, so both are accepted but unread. Kept for client-side debug visibility only.

#### `type: "heartbeat"` — periodic liveness + telemetry

Sent every `heartbeat_interval_s` (≈ 1 Hz). Same identity/battery/sync fields as `hello`, plus optional `sync_telemetry` for the mutual-sync coordinator. Server fans out a `device_heartbeat` SSE so the dashboard updates without waiting for the 5 s `/status` poll.

```
type: "heartbeat"
time_sync_id, sync_anchor_timestamp_s, battery_level, battery_state, device_id, device_model
                                     # same shapes as `hello`
sync_telemetry: {…} | absent         # opaque to this doc; consumed by state._sync.record_sync_telemetry
```

> **iOS-sent, server-ignored:** iOS also ships `cam` (camera role) and `t_session_s` (`CACurrentMediaTime()` at send time). Both are accepted but unread — the cam is already pinned by the WS URL path and the server stamps its own arrival time. Kept for client-side debug visibility only.

#### `type: "frame"` — one detected frame

Sent at capture rate (240 Hz on the binned 240 fps formats) while the session is armed. iOS bypasses pydantic validation server-side (`model_construct`), so missing keys raise loud `KeyError` — the lockstep failure mode for an iOS build that doesn't match the schema. Required keys:

```
type: "frame"
i: int                               # frame_index (monotonic per-camera)
ts: float                            # timestamp_s on iOS session clock
sid: str                             # session id; empty/missing → handler raises loud (schema bug)
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
`frame_count`, `rays`, `points`, `path_completed`, `session_ended`,
`device_status`, `device_heartbeat`, `calibration_changed`, `fit`,
`server_post_progress`, and `server_post_done`.

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
  "segments": [SegmentRecord, ...],  # may be empty (1-point sessions, pure noise)
  "gap_threshold_m": float,           # carried on all four emit paths. Clients should always patch their per-session slider cache from this field.
  "cause": str                        # "recompute" | "cycle_end" | "server_post" | "active_run_switch"
                                      # viewer 85_sse_fit.js returns early on "recompute" (inline /recompute
                                      # response already patched the scene); all other causes trigger the
                                      # autorefresh / /results refetch path.
}
```

`SegmentRecord` matches `server/schemas.py` exactly: `(indices, original_indices, p0[3], v0[3], t_anchor, t_start, t_end, rmse_m, speed_kph)`. Sample curves are NOT carried — clients reconstruct via `p0 + v0·τ + ½·G·τ²`.

#### `event: server_post_progress`

Emitted by the `run_server_post` BackgroundTask as frames are decoded and
detected. Throttled (not every frame) to avoid saturating the SSE channel.
Events-row and viewer progress indicators consume this to show a running
frame count without polling.

```
event: server_post_progress
data: {
  "sid": str,
  "cam": str,            # "A" or "B" — broadcasts are per-cam
  "frames_done": int,    # frames decoded so far
  "frames_total": int    # total MOV frame count (from probe_frame_count); null if unknown
}
```

#### `event: server_post_done`

Terminal event broadcast once the BackgroundTask finishes (success **or**
cancellation). Dashboard events row hides the in-flight `[Cancel]` button
and reloads the session row (PR #125 removed the events-row "Run srv" form —
re-runs are viewer-only now); viewer "Rerun server" button re-enables.

```
event: server_post_done
data: {
  "sid": str,
  "cam": str,                            # "A" or "B" — broadcasts are per-cam
  "reason": "ok" | "canceled" | "error", # note US spelling "canceled"
  "frames_done": int,                    # frames decoded before terminal event
  "frames_total": int                    # total MOV frame count; null if unknown
}
```

## Preset management — `POST /presets`, `POST /presets/active`, `DELETE /presets/{name}`

(no WS push for these; consumed by dashboard, not iOS)

### `POST /presets/active`

Switches the active preset for one of the two slots. Body (JSON or form):

```
name: str        # preset slug under data/presets/<slug>.json
target: str      # "live" | "server_post"
```

Behaviour:
- `target="live"`: snaps `DetectionConfig`, WS-broadcasts a settings push. Preset's `algorithm_id` must be `v11_hsv_cc`; otherwise 422.
- `target="server_post"`: writes `data/active_server_post_preset.json`; no WS push.

Error matrix:

| Status | Trigger |
|---|---|
| 400 | Missing `name` or `target` |
| 404 | Preset `name` not on disk |
| 409 | Preset deleted concurrently |
| 422 | `target` not in `{"live","server_post"}` |
| 422 | `target="live"` + preset `algorithm_id` ≠ `v11_hsv_cc` |

Response: `{"ok": true, "active": "<name>", "target": "<target>"}`

### `POST /presets`, `GET /presets`, `GET /presets/{name}`, `DELETE /presets/{name}`

Preset CRUD; params validated by `algorithms.get(algorithm_id).detector.params_schema`. See `server/routes/presets.py`.

### Operator audit checklist (when changing wire shapes)

Per project memory `feedback_ws_only_means_check_all_command_paths`, after any WS schema edit grep for every send/receive site:
- Server → iOS sends: `_settings_message_for`, `_arm_message_for`, `_disarm_message_for`, the inline `sync_run` dict in `routes/device_ws.py::ws_device`, the `sync_command` push in `routes/sync.py::start_sync` (near the per-cam broadcast loop), and the `calibration_updated` push in `routes/calibration.py::_handle_calibration_completed` (near the broadcast call at the end of the function).
- iOS → server receivers: the four `if mtype == "..."` branches in `routes/device_ws.py::ws_device` (`hello` / `heartbeat` / `frame` / `cycle_end`).
- iOS encoders: `ball_tracker/ServerUploader.swift` Codable structs **and** `ball_tracker/LiveFrameDispatcher.swift` hand-encoded dict (the dispatch-queue path that bypasses Codable — abfa422 was the bug where this was forgotten).
