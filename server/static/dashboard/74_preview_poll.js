// === preview image poll ===

  // Preview is a simple server-owned flag: click flips it, server pushes
  // the new state to the phone over WS, WS drop flips it back to false.
  // No client-side keep-alive or TTL refresh — those created race
  // conditions where toggle-off was silently re-armed by a stale beat.

  // Preview image polling. MJPEG streaming via <img> is flaky across
  // browsers (Chrome silently aborts when the server's first multipart
  // boundary doesn't land within a short window), so we bump a
  // cache-busting query-string on every <img data-preview-img> every
  // 200 ms — ~5 fps preview, trivial to debug via the Network tab, and
  // each frame is a normal GET /camera/{id}/preview that returns a
  // single JPEG or 404.
  function tickPreviewImages() {
    const t = Date.now();
    // Legacy 2-pane shape (still used by setup/markers until those phases
    // land). Skipped when the panel is in 'off' state.
    for (const img of document.querySelectorAll('img[data-preview-img]')) {
      const cam = img.dataset.previewImg;
      if (!cam) continue;
      const panel = img.closest('.preview-panel');
      if (!panel || panel.classList.contains('off')) continue;
      img.src = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + t;
      img.style.opacity = 1;
    }
    // New merged cam-view shape — same MJPEG endpoint, gated on the
    // .is-offline class set by the device-card renderer.
    for (const img of document.querySelectorAll('img[data-cam-img]')) {
      const cam = img.dataset.camImg;
      if (!cam) continue;
      const root = img.closest('.cam-view');
      if (!root || root.classList.contains('is-offline')) continue;
      img.src = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + t;
    }
  }
  setInterval(tickPreviewImages, 200);
