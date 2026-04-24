// === main init + setIntervals + keyboard ===

  // (1 s) and is the only high-frequency tick.
  initLiveStream();
  initHSVControls();
  tickStatus();
  tickCalibration();
  tickEvents();
  tickExtendedMarkers();
  tickIntrinsics();
  setInterval(tickStatus, 1000);
  setInterval(tickCalibration, 5000);
  setInterval(tickEvents, 5000);
  setInterval(tickExtendedMarkers, 5000);
  setInterval(tickIntrinsics, 5000);
  setInterval(tickActiveSession, 100);
  // Re-check the degraded banner without waiting for a new device_status
  // event — the grace window ticks forward even when no events arrive,
  // so the banner needs its own cadence to flip on at the right moment.
  setInterval(updateDegradedBanner, 1000);
  // Telemetry panel re-renders at 1Hz when open; closed <details> gets
  // display:none for its body so the innerHTML rewrite is a no-op visually.
  setInterval(() => {
    const panel = document.getElementById('telemetry-panel');
    if (panel && panel.open) renderTelemetry();
  }, 1000);

  // ------ Keyboard shortcuts --------------------------------------------
  // Deliberately NOT including Space for Arm/Stop — operator typically
  // has a ball in-hand when near the phone and accidentally hitting
  // Space on a tablet keyboard while moving is a real footgun. Space
  // stays bound to replay play/pause (existing behavior).
  document.addEventListener('keydown', (e) => {
    // Ignore when user is typing in an input / textarea
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'r' || e.key === 'R') {
      const btn = activeBox && activeBox.querySelector('[data-reset-trail]');
      if (btn) { e.preventDefault(); btn.click(); }
    } else if (e.key === 'c' || e.key === 'C') {
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
