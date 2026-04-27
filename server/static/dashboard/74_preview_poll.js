// === preview image poll ===

  // Preview is a simple server-owned flag: click flips it, server pushes
  // the new state to the phone over WS, WS drop flips it back to false.
  // No client-side keep-alive or TTL refresh — those created race
  // conditions where toggle-off was silently re-armed by a stale beat.

  // Cache-busting <img> polling lives in the cam-view runtime so every
  // page that mounts a cam-view (dashboard, /setup, /markers) gets the
  // same offline gate + per-cam abort handle. EXPECTED is fixed for
  // dashboard, so we kick one poller per cam at boot.
  if (window.BallTrackerCamView) {
    for (const cam of EXPECTED) window.BallTrackerCamView.startPreviewPolling(cam);
  }
