// === auto-cal click + log copy ===

  // Prime both immediately, then stagger polling so the UI stays
  // current without hammering the server. Status carries arming state
  // --- CALIBRATION card (Phase 5) -------------------------------------
  // Click "Auto calibrate" → POST /calibration/auto/start/<cam>.
  // Optimistic:
  // button disables while in flight; toast on failure.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-auto-cal]');
    if (!btn) return;
    if (btn.disabled) return;
    const cam = btn.dataset.autoCal;
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Starting…';
    try {
      const r = await fetch('/calibration/auto/start/' + encodeURIComponent(cam),
                            { method: 'POST' });
      if (!r.ok) {
        let msg = 'Calibration failed';
        try { const body = await r.json(); if (body.detail) msg = body.detail; } catch (_) {}
        alert(msg);
        return;
      }
      tickStatus();
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });

  // Copy a full auto-cal failure log to the clipboard. Surfaces the
  // active + last-run dump plus a /status snapshot so the operator can
  // paste the whole context into an AI / bug report without digging
  // through server logs.
  function autoCalLogCopyFallback(text) {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.style.cssText = 'background:var(--surface,#fff);padding:16px;border:1px solid var(--border,#ccc);border-radius:6px;max-width:80vw;max-height:80vh;display:flex;flex-direction:column;gap:8px;';
    const hdr = document.createElement('div');
    hdr.style.cssText = 'font-family:var(--mono,monospace);font-size:11px;color:var(--sub,#555);letter-spacing:0.08em;text-transform:uppercase;';
    hdr.textContent = 'Auto-copy blocked — press ⌘C / Ctrl+C then Esc';
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.style.cssText = 'flex:1;min-width:60vw;min-height:60vh;font-family:var(--mono,monospace);font-size:11px;padding:8px;';
    panel.appendChild(hdr);
    panel.appendChild(ta);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    ta.focus();
    ta.select();
    const close = () => { document.body.removeChild(overlay); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  }

  function copyPlainTextSync(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (_) { return false; }
  }

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-auto-cal-log]');
    if (!btn) return;
    const cam = btn.dataset.autoCalLog;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Copying…';
    const active = (currentAutoCalibration && currentAutoCalibration.active || {})[cam] || null;
    const last   = (currentAutoCalibration && currentAutoCalibration.last   || {})[cam] || null;
    let serverStatus = null;
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (r.ok) serverStatus = await r.json();
    } catch (_) {}
    const payload = {
      collected_at: new Date().toISOString(),
      camera_id: cam,
      page_url: window.location.href,
      user_agent: navigator.userAgent,
      auto_cal: { active, last },
      server_status: serverStatus,
    };
    const evSource = (last && Array.isArray(last.events)) ? last.events
                     : (active && Array.isArray(active.events)) ? active.events
                     : [];
    const evLines = evSource.map(ev => {
      const t = (typeof ev.t === 'number') ? ev.t.toFixed(3).padStart(7) : '   ?   ';
      const lv = (ev.level || 'info').padEnd(5);
      const data = ev.data ? ' ' + JSON.stringify(ev.data) : '';
      return `[${t}s ${lv}] ${ev.msg}${data}`;
    });
    const header = [
      `# auto-cal log · camera=${cam} · collected ${new Date().toISOString()}`,
      last ? `# run_id=${last.id} status=${last.status} summary=${last.summary || ''} detail=${last.detail || ''}` : '# no last run',
      `# ${evSource.length} event(s):`,
      ...evLines,
      '',
      '# --- full JSON payload ---',
    ].join('\n');
    const text = header + '\n' + JSON.stringify(payload, null, 2);
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        ok = copyPlainTextSync(text);
      }
    } catch (_) {
      ok = copyPlainTextSync(text);
    }
    if (ok) {
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
    } else {
      autoCalLogCopyFallback(text);
      btn.textContent = 'Manual copy';
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2600);
    }
  });
