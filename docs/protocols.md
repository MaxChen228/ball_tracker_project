# Protocols

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
