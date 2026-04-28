/* Mini-plan v4 /gt page — global state object.
 *
 * Single namespace `window.GT` holds:
 *   - sessions: latest /gt/sessions response (refreshed every 5s)
 *   - queue:    latest /gt/queue response (refreshed every 1s)
 *   - selected: { sid, cam } currently in editor
 *   - editor:   { range_start, range_end, prompt, dirty } — uncommitted edits
 *   - heatmap:  cached /gt/timeline/{sid}/{cam}.json by key
 *   - lastPrompt: persisted prompt across navigations
 *
 * No frameworks. Tick handlers live in 99_main.js; modules below 99
 * register render functions on `GT.render` so `99_main.js` can call
 * them in the right order on each tick.
 */
(function () {
  const initial = window.__GT_INITIAL_STATE__ || {};
  window.GT = {
    sessions: initial.sessions || [],
    queue: initial.queue || { items: [], paused: false },
    selected: { sid: null, cam: 'A' },
    editor: {
      rangeStart: null,
      rangeEnd: null,
      prompt: initial.lastPrompt || 'blue ball',
      dirty: false,
    },
    heatmap: {},
    lastPrompt: initial.lastPrompt || 'blue ball',
    render: {},
  };

  // Helper: format a session for the row (mirrors the SSR helpers in
  // render_gt_page.py — we keep them in sync so SSR-then-JS handoff
  // doesn't reflow rows).
  window.GT.glyphFor = function (s) {
    if (s.is_skipped) return '(⊘)';
    const a = !!(s.has_gt && s.has_gt.A);
    const b = !!(s.has_gt && s.has_gt.B);
    if (a && b) return '(✓)';
    if (a || b) return '(●)';
    return '(·)';
  };
  window.GT.tintFor = function (s) {
    if (s.is_skipped) return 'gt-row-skipped';
    const a = !!(s.has_gt && s.has_gt.A);
    const b = !!(s.has_gt && s.has_gt.B);
    if (a && b) return 'gt-row-passed';
    if (a || b) return 'gt-row-warn';
    return 'gt-row-neutral';
  };

  window.GT.summaryText = function (items, paused) {
    const counts = { pending: 0, running: 0, done: 0, error: 0, canceled: 0 };
    items.forEach((it) => { counts[it.status] = (counts[it.status] || 0) + 1; });
    let s = `total: ${items.length} · running: ${counts.running} · queued: ${counts.pending} · done: ${counts.done}`;
    if (paused) s += ' · PAUSED';
    return s;
  };
})();
