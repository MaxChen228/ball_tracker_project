# LAN deployment — snapshot

Point-in-time facts about how this rig is actually deployed on the
user's LAN. **Stale-prone**: re-verify before acting on these values.
Last verified: 2026-04-24.

## Topology

- **Server**: runs on the user's Mac.
  - LAN IP: **`192.168.50.106`** (2026-04-24 confirmed).
  - iPhone Settings → Server IP must point here.
  - Operator typically runs the server in a terminal:
    ```bash
    cd server && uv run uvicorn main:app --host 0.0.0.0 --port 8765
    ```
    stdout log streams directly in that terminal.
- **Device registry**: **Cam A + Cam B both online** (since 2026-04-22).
  Mutual sync / stereo triangulation is the normal path, not a degraded
  fallback.
- **Chirp playback**: from the **Mac itself** (`/chirp.wav` → Mac
  speakers). The two iPhones receive asymmetric SNR (different
  distances to the Mac) — sync tuning must assume non-symmetric SNR.
- **Network**: LAN has occasional drops. Heartbeat timeouts back off
  exponentially (2 s → max 32 s). iOS `PayloadUploadQueue` retries
  flush backlog automatically on recovery.

## Device-log access limitation

Xcode 26 has no remote-iPhone log CLI. Options:

1. Console.app showing connected device.
2. Code-level `os.Logger` so the user can copy-paste from
   Console.app.

`adb`-style remote dump is not available.

## Debugging primers

- **"Events not showing"** → first `curl /status` and check
  `uploads_received`. Empty array ≠ iOS bug; often network drop +
  retry in progress.
- **Sync tuning** must assume the two phones hear the chirp at
  different volumes.
- **Adding iOS-side debug logs** → prefer `log.info / warning / error`
  (operator reads them from Console.app).

## When this file goes stale

Re-verify when:

- The user reports the server IP changed (DHCP lease, router swap,
  wifi handoff).
- A third phone joins (N-view triangulation work — see
  [../architecture.md](../architecture.md)).
- Chirp moves off the Mac onto a separate device.

Update the "Last verified" line at the top of this file in the same
commit.
