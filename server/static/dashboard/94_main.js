// === main init + setIntervals + keyboard ===

  // (1 s) and is the only high-frequency tick.
  initLiveStream();
  initDetectionConfigControls();
  tickStatus();
  tickCalibration();
  tickEvents();
  tickExtendedMarkers();
  tickIntrinsics();
  // /status polling is now a safety fallback — SSE `device_status` and
  // `device_heartbeat` drive the Devices card in real-time. 5 s covers
  // SSE reconnect gaps without spamming the server at 1 Hz.
  setInterval(tickStatus, 5000);
  setInterval(tickCalibration, 5000);
  // Events: SSE drives freshness now (server_post_progress / done +
  // path_completed all bust _lastEvKey + tickEvents). 15 s polling is
  // a safety net for the rare SSE reconnect gap. Don't lower this — it
  // just adds load without UX gain when SSE is up.
  setInterval(tickEvents, 15000);
  setInterval(tickExtendedMarkers, 5000);
  setInterval(tickIntrinsics, 5000);
  // Re-check the degraded banner without waiting for a new device_status
  // event — the grace window ticks forward even when no events arrive,
  // so the banner needs its own cadence to flip on at the right moment.
  setInterval(updateDegradedBanner, 1000);

  // ------ Keyboard shortcuts --------------------------------------------
  // Deliberately NOT including Space for Arm/Stop — operator typically
  // has a ball in-hand when near the phone and accidentally hitting
  // Space on a tablet keyboard while moving is a real footgun.
  document.addEventListener('keydown', (e) => {
    // Ignore when user is typing in an input / textarea
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'c' || e.key === 'C') {
      // Scroll devices sidebar card into view — closest we have to
      // "open calibration panel" since auto-cal is per-device inline.
      const devices = document.getElementById('devices-body');
      if (devices) { e.preventDefault(); devices.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    } else if (e.key === 'm' || e.key === 'M') {
      // Toggle audio cues. Shown in the nav strip when enabled.
      try {
        const cur = localStorage.getItem('ball_tracker_audio_cues') === '1';
        localStorage.setItem('ball_tracker_audio_cues', cur ? '0' : '1');
      } catch (_) {}
    }
  });
