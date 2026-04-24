// === SSE live stream init ===

  function initLiveStream() {
    if (!window.EventSource) return;
    const es = new EventSource('/stream');
    es.addEventListener('session_armed', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        currentLiveSession = {
          session_id: data.sid,
          armed: true,
          paths: data.paths || [],
          frame_counts: {},
          frame_samples: { A: [], B: [] },
          frame_fps: {},
          point_count: 0,
          point_depths: [],
          paths_completed: [],
          armed_at_ms: Date.now(),
        };
        livePointStore.set(data.sid, []);
        liveRayStore.set(data.sid, new Map());
        liveTraceIdx = -1;
        // Ghost trail is deliberately preserved across arm — it'll stay
        // rendered until a real point for the new session lands, at which
        // point liveTraces() stops emitting it (the new session trace
        // takes over visually). lastEndedLiveSid is not cleared here so
        // the operator can still see framing drift even on the first
        // moments of the new cycle.
        renderActiveSession(currentLiveSession);
        repaintCanvas();
        playCue('armed');
      } catch (_) {}
    });
    es.addEventListener('frame_count', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!currentLiveSession || currentLiveSession.session_id !== data.sid) return;
        currentLiveSession.frame_counts = currentLiveSession.frame_counts || {};
        currentLiveSession.frame_counts[data.cam] = Number(data.count || 0);
        pushFrameSample(currentLiveSession, data.cam, Number(data.count || 0));
        renderActiveSession(currentLiveSession);
      } catch (_) {}
    });
    es.addEventListener('path_completed', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (currentLiveSession && currentLiveSession.session_id === data.sid) {
          const done = new Set(currentLiveSession.paths_completed || []);
          done.add(data.path);
          currentLiveSession.paths_completed = [...done];
          renderActiveSession(currentLiveSession);
        }
        // Flip the events row's path chip from live→done without waiting
        // for the 5 s /events tick. Invalidate the diff-key memo so the
        // keyed renderer actually sees the new path_status / count.
        if (typeof tickEvents === 'function') {
          _lastEvKey = null;
          tickEvents();
        }
      } catch (_) {}
    });
    es.addEventListener('ray', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const sid = data.sid;
        const cam = data.cam || '?';
        if (!currentLiveSession || currentLiveSession.session_id !== sid) return;
        if (!Array.isArray(data.origin) || !Array.isArray(data.endpoint)) return;
        pushLiveRay(sid, cam, {
          origin: data.origin.map(Number),
          endpoint: data.endpoint.map(Number),
          t_rel_s: Number(data.t_rel_s || 0),
          frame_index: Number(data.frame_index || 0),
        });
        scheduleLiveRayRepaint();
      } catch (_) {}
    });
    es.addEventListener('calibration_changed', () => {
      // Skip the 5s polling tick — repaint canvas immediately so the new
      // pose lands on screen. tickCalibration() still runs on schedule as
      // a safety net if the SSE event arrives before the dashboard has
      // its first paint done.
      if (typeof tickCalibration === 'function') tickCalibration();
    });
    es.addEventListener('point', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const sid = data.sid;
        const pt = {
          x: Number(data.x),
          y: Number(data.y),
          z: Number(data.z),
          t_rel_s: Number(data.t_rel_s || 0),
        };
        const arr = livePointStore.get(sid) || [];
        arr.push(pt);
        livePointStore.set(sid, arr);
        if (currentLiveSession && currentLiveSession.session_id === sid) {
          currentLiveSession.point_count = arr.length;
          currentLiveSession.last_point_at_ms = Date.now();
          if (!currentLiveSession.point_depths) currentLiveSession.point_depths = [];
          currentLiveSession.point_depths.push(pt.z);
          if (currentLiveSession.point_depths.length > 20) {
            currentLiveSession.point_depths.shift();
          }
          renderActiveSession(currentLiveSession);
          // Fast path: append to the already-anchored live trace slot.
          // Falls back to a full repaint if the slot is stale (e.g. first
          // point after an arm, or after a structural change invalidated
          // the cached index).
          if (!extendLivePoint(pt)) repaintCanvas();
        } else {
          repaintCanvas();
        }
        // Telemetry: each `point` SSE arrival is one triangulated pair.
        // Drop samples older than the window so the rolling stats stay
        // bounded regardless of session count or length.
        const nowMs = Date.now();
        pairTimestamps.push(nowMs);
        while (pairTimestamps.length && nowMs - pairTimestamps[0] > TELEMETRY_WINDOW_MS) {
          pairTimestamps.shift();
        }
      } catch (_) {}
    });
    es.addEventListener('session_ended', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (currentLiveSession && currentLiveSession.session_id === data.sid) {
          currentLiveSession.armed = false;
          currentLiveSession.ended_at_ms = Date.now();
          if (Array.isArray(data.paths_completed)) {
            currentLiveSession.paths_completed = data.paths_completed;
          }
          renderActiveSession(currentLiveSession);
          // Retain the trail reference for ghost preview on the next arm.
          // Clear currentLiveSession after a short delay so the active card
          // stays visible briefly with its final counters.
          lastEndedLiveSid = data.sid;
          setTimeout(() => {
            if (currentLiveSession && currentLiveSession.session_id === data.sid && !currentLiveSession.armed) {
              currentLiveSession = null;
              liveTraceIdx = -1;
              renderActiveSession(null);
              repaintCanvas();
            }
          }, 3000);
          playCue('ended');
        }
      } catch (_) {}
    });
    es.addEventListener('device_status', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!data || !data.cam) return;
        const connected = !!data.ws_connected;
        const online = !!data.online;
        const prev = wsStatus.get(data.cam);
        if (!prev || prev.connected !== connected) {
          wsStatus.set(data.cam, { connected, since_ms: Date.now() });
          if (!connected) recordError('ws_disconnect', `Cam ${data.cam} WebSocket dropped`);
        }
        // Authoritative: patch currentDevices in place and repaint.
        // /status polling (now 5 s fallback) no longer needs to be the
        // source of truth for online/offline transitions.
        _applyDeviceStatus(data.cam, { online, ws_connected: connected });
        updateDegradedBanner();
      } catch (_) {}
    });
    es.addEventListener('device_heartbeat', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!data || !data.cam) return;
        // Patch battery / ws_latency / last_seen / time_sync fields.
        // Server emits this on every WS heartbeat (default 1 Hz), so
        // the Devices card's battery + sync LEDs stay fresh without
        // hitting /status.
        _applyDeviceStatus(data.cam, {
          battery_level: data.battery_level,
          battery_state: data.battery_state,
          ws_latency_ms: data.ws_latency_ms,
          last_seen_at: data.last_seen_at,
          time_synced: !!data.time_synced,
          time_sync_id: data.time_sync_id || null,
          ws_connected: true,
        });
        // Telemetry sample: same shape tickStatus previously recorded.
        if (typeof latencySamples === 'object' && typeof data.ws_latency_ms === 'number') {
          const nowMs = Date.now();
          const arr = latencySamples[data.cam] = latencySamples[data.cam] || [];
          arr.push({ t_ms: nowMs, latency: data.ws_latency_ms });
          while (arr.length && nowMs - arr[0].t_ms > TELEMETRY_WINDOW_MS) arr.shift();
        }
      } catch (_) {}
    });
  }

  // Merge a partial device patch into currentDevices, re-render Devices
  // + Session panels, and drop wsStatus entries for offline cams.
  function _applyDeviceStatus(cam, patch) {
    const list = currentDevices || [];
    let found = false;
    for (let i = 0; i < list.length; i++) {
      if (list[i] && list[i].camera_id === cam) {
        list[i] = Object.assign({}, list[i], patch, { camera_id: cam });
        found = true;
        break;
      }
    }
    if (!found) {
      if (patch.online === false) {
        // Offline event for an unknown cam — no row to paint.
        return;
      }
      list.push(Object.assign({ camera_id: cam }, patch));
    }
    // Drop from currentDevices when explicitly offline (mirror the
    // /status derivation that only lists online devices).
    if (patch.online === false) {
      currentDevices = list.filter(d => d && d.camera_id !== cam);
    } else {
      currentDevices = list;
    }
    _lastDevKey = null;
    _lastSessKey = null;
    _lastNavKey = null;
    renderDevices({
      devices: currentDevices,
      calibrations: currentCalibrations || [],
      preview_requested: currentPreviewRequested,
      sync_commands: currentSyncCommands,
      calibration_last_ts: currentCalibrationLastTs || {},
      auto_calibration: currentAutoCalibration,
    });
    renderSession({
      devices: currentDevices,
      session: currentSession,
      calibrations: currentCalibrations || [],
      capture_mode: currentCaptureMode,
    });
  }
