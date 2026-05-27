# Operations

## Physical setup (current)

Nominal rig used by the operator — actual per-session pose still comes from the homography solved on-device; these values are just the target the rig is built against:

- **Camera**: iPhone 14-17 series, rear **main (1x wide) camera** only (`builtInWideAngleCamera`). Ultra Wide (0.5x, 120° FOV) is rejected — 5-coefficient distortion can't model it cleanly and the edges lose angular resolution.
- **Resolution**: default 1920×1080 (16:9); runtime capture selection is 1080p or 720p. Calibration always bakes at 1080p and the server rescales intrinsics + homography per-pitch to the MOV's actual pixel grid via `pairing.scale_pitch_to_video_dims`. ChArUco calibration JSON is auto-scaled from the 4032×3024 source on import.
- **Orientation**: **landscape** on both phones. Sensor long-edge aligned with the pitcher→plate horizontal direction. ChArUco intrinsic-calibration shots must be taken in the same orientation.
- **Baseline**: two phones placed ~3 m from home plate, both on the **first-base / third-base line** (i.e. 1B-side phone and 3B-side phone, aimed inward at the plate). This is a wide cross-baseline stereo setup — good depth separation for triangulation.
- **Focus**: lock AF (`setFocusModeLocked`) to the plate distance both during ChArUco capture and during live recording. The main cam has OIS but static mounting keeps its drift negligible.
- **Extrinsics** are NOT assumed from this geometry — homography is recovered per phone via the dashboard's **Auto calibrate** action (server-side ArUco; phone just sends a single frame). The 3 m / 1B-3B numbers are rig targets, not priors fed into code.

For the per-iPhone-model 240 fps capture format breakdown, see
[reference/iphone-camera-formats.md](reference/iphone-camera-formats.md).

## Commands

### Server

```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8765   # run (prints LAN IP → paste into iPhone Settings)
uv run pytest                                        # all tests (server + viewer)
uv run pytest test_triangulation_math.py::test_triangulate_sweeps_ball_path   # single test
uv run python reprocess_sessions.py --since today                 # re-run detection + triangulation with current data/detection_config.json over today's MOVs (also --session s_xxxx / --all / --dry-run; --use-frozen-snapshot replays each pitch's *_used config)
uv run python migrations/strip_prepr93_quarantine.py              # one-shot data migration (2026-05-13): rehydrate `data/_quarantine_2026-05-13/` pitches (PR #93 schema tighten); dry-run by default, `--apply` to write, `--apply --purge-quarantine` to also rm the quarantine folder. After: rerun `reprocess_sessions.py --session <ids> --force-preset blue_ball` (or whichever preset was live for those sessions).
```

### iOS

Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

Agents must NOT run iOS tests via xcodebuild — see [ios.md](ios.md) for the
rule.

## Preset library

Detection presets live as JSON files under `server/data/presets/<slug>.json` (the directory is gitignored alongside the rest of `server/data/`). Two built-in seeds are written by the server on first boot if their files don't exist: `tennis.json` and `blue_ball.json`, both algorithm `v11_hsv_cc`. Restoring a built-in after edit/delete is `rm server/data/presets/<slug>.json` + restart.

Operator workflow lives entirely on the dashboard's **DETECTION CONFIG** card:

- **Switch presets** — click `Manage…` to open the library list, then click `Use` on the target preset (POSTs to `POST /presets/active` with body `{"name": "<slug>", "target": "live" | "server_post"}`; 400 on missing field, 422 on wrong target or v11 mismatch for live). The identity tag updates to the chosen preset.
- **Edit & save** — drag sliders to taste, click `Apply`. Apply opens an **algorithm + preset picker** (d963e4a): pick the target algorithm (form pulls schema from `GET /algorithms`, params editor regenerates on switch), name the slug + label, then POSTs `{name, label, algorithm_id, params}` to `POST /presets`. For a `v11_hsv_cc` preset that becomes the new live target, the server writes the preset file and atomically switches the live config + WS broadcast; non-v11 presets save to disk only (no live change, no WS push — they're server_post-only). The slug doubles as the filename; saving under an existing slug returns 409.
- **Manage** — click `Manage…` to open the library list; per row you get `Use` (snap live config to that preset via `POST /presets/active`), `Duplicate` (prompt for a new slug+label, copy the values), `Delete` (unlink the file — 409 if the slug is currently active). The currently-bound preset is marked `★ current`.

If the live config's `preset` field references a preset that has been deleted, the identity tag turns red and reads `<slug> (preset deleted)`. The dashboard does not silently re-bind; the next Apply records `preset=null` (custom).

For sharing or backup, `cp -r server/data/presets ~/somewhere` is sufficient — each file is self-contained. There is no inter-file dependency.

## Per-session tuning sliders (viewer)

Each session's viewer header has a dedicated tuning row beneath the main nav with a single slider + an Apply button:

- **Gap ≤** (0–200 cm): hides points whose skew-line residual `residual_m` exceeds the cap. Pure client-side mask on the persisted full set.
- **Apply**: persists the stamped value to `SessionResult.gap_threshold_m` and re-runs the segmenter against the stamped subset (`POST /sessions/{sid}/recompute` body `{gap_threshold_m}` only). Pairing does NOT re-run; the persisted point set is invariant. Sub-second on typical sessions.

The legacy **Cost ≤** slider has been retired (b15a611). Cost gating is now **owned by the algorithm** — each runnable entry in `server/algorithms/__init__.py` declares its own `cost_threshold` (via `algorithms.cost_threshold_for_algorithm`), applied server-side in pairing before segmentation. The operator no longer tunes this. `static/viewer/50_canvas.js::_passCostFilterPoint` is a no-op stub kept only so call sites compile; `SessionResult.cost_threshold` has been removed from the schema.

Architecture: pairing emits the full geometrically-plausible set under absolute ceilings (`gap < 5 m`, `cost < 5`) at arm/disarm time. The viewer renders the full set; the Gap slider masks. Apply changes which points the **fit** sees, not which points exist on disk. So you can drag Gap ≤ from 0.05 → 2.0 and see masked points reappear instantly without losing them; the only thing Apply persists is the segmenter's gap input.

The slider never changes pairing emit, so a stamped `gap=0.01` cannot lose data — re-drag to loose values + Apply restores everything. For cases where you actually want fewer emitted points (disk pressure), the absolute ceilings live in `server/pairing.py::_EMIT_*_CEILING` and require a server restart.

## Degraded / fallback modes

- **No 時間校正 before arm**: `sync_anchor_timestamp_s` uploads as `null`. Server skips detection + triangulation and flags the session `error="no time sync"`. Re-run 時間校正 and re-arm.
- **No calibration**: server triangulation fails with "camera X missing calibration". Fix: in the dashboard, enable preview for each cam and click **Auto calibrate** before arming. There is no iOS calibration UI — manual / 5-handle calibration is no longer available.
- **No distortion coefficients**: server detection still runs; triangulation uses zero distortion (equivalent to pinhole projection) — marginally less accurate at frame edges but usable.
- **Low-light room**: FPS stays locked at target via `activeMaxExposureDuration` cap. Image will darken and ISO noise grows rather than the sensor dropping to e.g. 14 fps. If detection fails because the ball is too dim, add light rather than touching FPS.
