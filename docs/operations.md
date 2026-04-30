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
[iphone_camera_formats.md](iphone_camera_formats.md).

## Commands

### Server

```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8765   # run (prints LAN IP → paste into iPhone Settings)
uv run pytest                                        # all tests (server + viewer)
uv run pytest test_triangulation_math.py::test_triangulate_sweeps_ball_path   # single test
uv run python reprocess_sessions.py --since today                 # re-run detection + triangulation with current data/detection_config.json over today's MOVs (also --session s_xxxx / --all / --dry-run; --use-frozen-snapshot replays each pitch's *_used config)
```

### iOS

Open `ball_tracker.xcodeproj` in Xcode. The app needs a **physical device** (camera + 240 fps capture + microphone). Unit tests in `ball_trackerTests/`, UI tests in `ball_trackerUITests/` via Xcode's Test action (`⌘U`).

Agents must NOT run iOS tests via xcodebuild — see [ios.md](ios.md) for the
rule.

## Preset library

Detection presets live as JSON files under `server/data/presets/<slug>.json` (the directory is gitignored alongside the rest of `server/data/`). Built-in seeds `tennis.json` and `blue_ball.json` are written by the server on first boot if they don't exist; restoring a built-in after edit/delete is `rm server/data/presets/<slug>.json` + restart.

Operator workflow lives entirely on the dashboard's **DETECTION CONFIG** card:

- **Switch presets** — click a preset button (e.g. `Blue ball`) to load its values into the form, then `APPLY DETECTION CONFIG`. The identity tag updates to the chosen preset.
- **Edit & save as new** — drag sliders to taste, click `+ Save as new`, supply a slug (`[a-z0-9_]{1,32}`) and an operator-facing label. The new preset is on disk before the prompt closes.
- **Manage** — click `Manage…` to open the library list; per row you get `Use` (snap live config to that preset), `Duplicate` (prompt for a new slug+label, copy the values), `Delete` (unlink the file). The currently-bound preset is marked `★ current`.
- **Reset to preset** — appears only when the live config is bound to a preset but has been edited (`identity-modified`); snaps back to the canonical values.

If the live config's `preset` field references a preset that has been deleted, the identity tag turns red and reads `<slug> (preset deleted)`. The dashboard does not silently re-bind; the next Apply records `preset=null` (custom).

For sharing or backup, `cp -r server/data/presets ~/somewhere` is sufficient — each file is self-contained. There is no inter-file dependency.

## Per-session tuning sliders (viewer)

Each session's viewer header has a dedicated tuning row beneath the main nav with two sliders + an Apply button:

- **Cost ≤** (0–1): hides triangulated points whose `max(cost_a, cost_b)` exceeds the cap. The slider operates on the **persisted full set** of points pairing emitted; dragging is a pure client-side mask, no server round-trip.
- **Gap ≤** (0–200 cm): hides points whose skew-line residual `residual_m` exceeds the cap. Same client-side mask.
- **Apply**: persists both stamped values to `SessionResult.cost_threshold` / `gap_threshold_m` and re-runs the segmenter against the stamped subset (`POST /sessions/{sid}/recompute`). Pairing does NOT re-run; the persisted point set is invariant. Sub-second on typical sessions.

Architecture: pairing emits the full geometrically-plausible set under absolute ceilings (`gap < 5 m`, `cost < 5`) at arm/disarm time. The viewer renders the full set; the slider masks. Apply changes which points the **fit** sees, not which points exist on disk. So you can drag Cost ≤ from 0.2 → 1.0 and see masked points reappear instantly without losing them; the only thing Apply persists is the segmenter's filter input.

The sliders never change pairing emit, so a stamped (cost=0.05, gap=0.01) cannot lose data — re-drag to loose values + Apply restores everything. For cases where you actually want fewer emitted points (disk pressure), the absolute ceilings live in `server/pairing.py::_EMIT_*_CEILING` and require a server restart.

## Degraded / fallback modes

- **No 時間校正 before arm**: `sync_anchor_timestamp_s` uploads as `null`. Server skips detection + triangulation and flags the session `error="no time sync"`. Re-run 時間校正 and re-arm.
- **No calibration**: server triangulation fails with "camera X missing calibration". Fix: in the dashboard, enable preview for each cam and click **Auto calibrate** before arming. There is no iOS calibration UI — manual / 5-handle calibration is no longer available.
- **No distortion coefficients**: server detection still runs; triangulation uses zero distortion (equivalent to pinhole projection) — marginally less accurate at frame edges but usable.
- **Low-light room**: FPS stays locked at target via `activeMaxExposureDuration` cap. Image will darken and ISO noise grows rather than the sensor dropping to e.g. 14 fps. If detection fails because the ball is too dim, add light rather than touching FPS.
