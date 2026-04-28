/* Queue panel — render + run/pause + cancel/retry/clear actions.
 *
 * Polls /gt/queue at 1 Hz from 99_main.js. Mask preview thumbnail for
 * the running job is loaded from /gt/preview/{id}.jpg with a cache-bust
 * query param keyed on item.progress.current_frame so we re-fetch only
 * when the worker has written a new frame.
 */
(function () {
  const elList = document.getElementById('gt-queue-list');
  const elSummary = document.getElementById('gt-queue-summary');
  const elToggle = document.getElementById('gt-queue-toggle');
  const elClearDone = document.getElementById('gt-queue-clear-done');
  const elClearErrors = document.getElementById('gt-queue-clear-errors');

  function labelFor(it) {
    const range = `[${it.time_range[0].toFixed(2)}–${it.time_range[1].toFixed(2)}s]`;
    const base = `${it.session_id}/${it.camera_id} ${range}`;
    if (it.status === 'running' && it.progress) {
      const cur = it.progress.current_frame || 0;
      const total = it.progress.total_frames || 0;
      const pct = total ? Math.round((100 * cur) / total) : 0;
      return `▶ ${base} · frame ${cur}/${total} · ${pct}%`;
    }
    if (it.status === 'pending') return `⏳ ${base}`;
    if (it.status === 'done') {
      const lab = it.n_labelled || 0;
      const dec = it.n_decoded || 0;
      return `✓ ${base} · ${lab}/${dec} frames`;
    }
    if (it.status === 'error') {
      // Stderr ring is byte-capped from the tail, so the FIRST line of
      // a saved trace is usually a mid-line garbage fragment ("rocessor(
      // videos=video..." instead of "...processor(videos=video..."). The
      // useful line is the last `<Type>Error: ...` row at the bottom of
      // the trace — that's the actual exception. Fall back to last non-
      // empty line if no exception pattern matches, then to first line.
      const raw = (it.error || 'error').trim();
      const lines = raw.split('\n').map((s) => s.trimEnd()).filter(Boolean);
      const exc = [...lines].reverse().find((l) => /^[A-Z][A-Za-z]*Error:/.test(l));
      const msg = (exc || lines[lines.length - 1] || 'error').slice(0, 80);
      return `✗ ${base} · ${msg}`;
    }
    if (it.status === 'canceled') return `⊘ ${base}`;
    return base;
  }

  function previewSrc(it) {
    if (it.status !== 'running' || !it.progress) return null;
    const frame = it.progress.current_frame || 0;
    return `/gt/preview/${it.id}.jpg?f=${frame}`;
  }

  function renderRow(it) {
    const cls = `gt-queue-row gt-status-${it.status}`;
    let inner = `<div class="gt-row-label">${labelFor(it)}</div>`;
    if (it.status === 'running') {
      const cur = (it.progress && it.progress.current_frame) || 0;
      const total = (it.progress && it.progress.total_frames) || 1;
      const pct = total ? Math.round((100 * cur) / total) : 0;
      inner += `<div class="gt-row-progress">
        <div class="gt-progressbar"><div class="gt-progressfill" style="width:${pct}%"></div></div>
      </div>`;
      const src = previewSrc(it);
      if (src) inner += `<img class="gt-row-thumb" src="${src}" alt="mask preview">`;
      inner += `<div class="gt-row-actions"><button class="btn small" data-action="cancel" data-id="${it.id}">⏹ cancel</button></div>`;
    } else if (it.status === 'pending') {
      inner += `<div class="gt-row-actions"><button class="btn small" data-action="cancel" data-id="${it.id}">cancel</button></div>`;
    } else if (it.status === 'error' || it.status === 'canceled') {
      inner += `<div class="gt-row-actions">
        <button class="btn small" data-action="retry" data-id="${it.id}">↻ retry</button>
        ${it.status === 'error' ? `<button class="btn small secondary" data-action="show-error" data-id="${it.id}">…</button>` : ''}
      </div>`;
    }
    return `<div class="${cls}" role="listitem" data-id="${it.id}">${inner}</div>`;
  }

  function renderQueue() {
    const items = window.GT.queue.items || [];
    const paused = !!window.GT.queue.paused;
    elSummary.textContent = window.GT.summaryText(items, paused);
    elToggle.textContent = paused ? '▸ Run' : 'Pause';
    elToggle.dataset.paused = paused ? '1' : '0';
    elToggle.className = paused ? 'btn primary' : 'btn secondary';

    if (items.length === 0) {
      elList.innerHTML = '<div class="gt-empty">Queue idle — pick a session and add a range.</div>';
      return;
    }
    // Show running + next 2 pending + last done + ALL errors/canceled
    // by default. [show all N] toggles below.
    const running = items.filter((it) => it.status === 'running');
    const pendings = items.filter((it) => it.status === 'pending').slice(0, 2);
    const dones = items.filter((it) => it.status === 'done').slice(-1);
    const errs = items.filter((it) => it.status === 'error' || it.status === 'canceled');
    const trimmed = [...running, ...pendings, ...errs, ...dones];
    const totalShown = trimmed.length;
    const hidden = items.length - totalShown;
    const ids = new Set(trimmed.map((it) => it.id));
    const showAll = elList.dataset.showAll === '1';
    const renderedItems = showAll ? items : trimmed;
    let html = renderedItems.map(renderRow).join('');
    if (!showAll && hidden > 0) {
      html += `<div class="gt-empty"><button class="btn small" data-action="show-all">show all ${items.length}</button></div>`;
    }
    if (showAll && items.length > totalShown) {
      html += `<div class="gt-empty"><button class="btn small" data-action="hide-extra">collapse</button></div>`;
    }
    elList.innerHTML = html;
  }

  elList.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (action === 'cancel') {
      await fetch(`/gt/queue/${id}`, { method: 'DELETE' });
    } else if (action === 'retry') {
      await fetch(`/gt/queue/${id}/retry`, { method: 'POST' });
    } else if (action === 'show-error') {
      const it = (window.GT.queue.items || []).find((x) => x.id === id);
      if (it && it.error) window.alert(it.error);
    } else if (action === 'show-all') {
      elList.dataset.showAll = '1';
      renderQueue();
    } else if (action === 'hide-extra') {
      elList.dataset.showAll = '0';
      renderQueue();
    }
    if (window.GT.tickQueue) window.GT.tickQueue();
  });

  elToggle.addEventListener('click', async () => {
    const paused = elToggle.dataset.paused === '1';
    const url = paused ? '/gt/queue/run' : '/gt/queue/pause';
    await fetch(url, { method: 'POST' });
    if (window.GT.tickQueue) window.GT.tickQueue();
  });

  elClearDone.addEventListener('click', async () => {
    await fetch('/gt/queue/done', { method: 'DELETE' });
    if (window.GT.tickQueue) window.GT.tickQueue();
  });

  elClearErrors.addEventListener('click', async () => {
    await fetch('/gt/queue/errors', { method: 'DELETE' });
    if (window.GT.tickQueue) window.GT.tickQueue();
  });

  window.GT.render.queue = renderQueue;
})();
