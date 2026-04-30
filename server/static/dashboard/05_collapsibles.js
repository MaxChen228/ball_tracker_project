// === collapsible primitive ===
//
// Generic show/hide for any node carrying:
//   data-collapsible-key="..."   (unique per logical group; localStorage key)
//   data-collapsible-header      (the clickable handle — chevron auto-injected via CSS)
//   data-collapsible-body        (the section that hides when collapsed)
//
// State is persisted as a JSON-encoded array of collapsed keys under
// 'dashboard:collapsed'. Default = expanded; only collapsed entries get
// stored (so brand-new keys auto-show).
//
// Cards keep `data-collapsible-key` on the OUTER `.card`, not on the body
// the polling code replaces (intrinsics-body, hsv-body) — otherwise a
// /calibration/intrinsics tick would wipe the data attribute and forget
// the toggle. CSS collapses via descendant selector so any nesting works.
//
// Event-day groups are created on the fly by 60_events_render.js when
// SSE/poll feeds new days; they call window.applyCollapsibleState(node)
// so freshly-added groups inherit the persisted state instantly.

  const COLLAPSE_STORE = 'dashboard:collapsed';
  const _collapsedKeys = (() => {
    try {
      const raw = localStorage.getItem(COLLAPSE_STORE);
      if (!raw) return new Set();
      const arr = JSON.parse(raw);
      return new Set(Array.isArray(arr) ? arr : []);
    } catch { return new Set(); }
  })();
  function _persistCollapsed() {
    try { localStorage.setItem(COLLAPSE_STORE, JSON.stringify([..._collapsedKeys])); }
    catch {}
  }
  function applyCollapsibleState(node) {
    if (!node || !node.dataset) return;
    const key = node.dataset.collapsibleKey;
    if (!key) return;
    node.dataset.collapsed = _collapsedKeys.has(key) ? 'true' : 'false';
  }
  function applyCollapsibleStateAll(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-collapsible-key]').forEach(applyCollapsibleState);
  }
  // Delegated click — covers SSR-rendered AND dynamically-inserted headers.
  // Buttons / inputs / forms / links inside the header escape the toggle so
  // controls (e.g. an Active/Trash filter sitting next to a card title) keep
  // their own click semantics.
  document.addEventListener('click', (e) => {
    const header = e.target.closest('[data-collapsible-header]');
    if (!header) return;
    if (e.target.closest('button, input, select, textarea, a, form, label')) return;
    const root = header.closest('[data-collapsible-key]');
    if (!root) return;
    const key = root.dataset.collapsibleKey;
    if (!key) return;
    if (_collapsedKeys.has(key)) _collapsedKeys.delete(key);
    else _collapsedKeys.add(key);
    _persistCollapsed();
    applyCollapsibleState(root);
  });
  applyCollapsibleStateAll();
  // Expose for renderers that build collapsible nodes after DOMContentLoaded
  // (60_events_render.js when a new event-day arrives via poll/SSE).
  window.applyCollapsibleState = applyCollapsibleState;
  window.applyCollapsibleStateAll = applyCollapsibleStateAll;

