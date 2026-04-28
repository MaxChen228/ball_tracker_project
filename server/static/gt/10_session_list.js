/* Sessions panel (left rail) — render + free-text filter + click-to-select.
 *
 * The SSR pre-paints the list; this module rebinds events on first
 * load and re-renders on every 5 s tick (or after a skip / unskip
 * mutation). We keep the row markup identical to the SSR so the row
 * doesn't visibly reflow when JS takes over.
 *
 * 2026-04-29: dropped the unlabeled-only / show-no-MOV / show-skipped
 * checkboxes — operator confirmed they didn't help workflow. Plain
 * substring filter on session id is the only filter left.
 */
(function () {
  const elList = document.getElementById('gt-session-list');
  const elCount = document.getElementById('gt-session-count');
  const elFilterText = document.getElementById('gt-filter-text');

  function applyFilters(sessions) {
    const text = (elFilterText.value || '').trim().toLowerCase();
    if (!text) return sessions;
    return sessions.filter((s) => s.session_id.toLowerCase().includes(text));
  }

  function renderRow(s) {
    const tint = window.GT.tintFor(s);
    const glyph = window.GT.glyphFor(s);
    const sel = window.GT.selected.sid === s.session_id ? 'gt-row-selected' : '';
    return `<div class="gt-session-row ${tint} ${sel}" role="listitem"
      data-sid="${s.session_id}"
      data-has-gt-a="${s.has_gt.A ? 1 : 0}"
      data-has-gt-b="${s.has_gt.B ? 1 : 0}"
      data-has-mov-a="${s.has_mov.A ? 1 : 0}"
      data-has-mov-b="${s.has_mov.B ? 1 : 0}"
      data-skipped="${s.is_skipped ? 1 : 0}">
      <span class="gt-sid">${s.session_id}</span>
      <span class="gt-glyph">${glyph}</span>
    </div>`;
  }

  function renderList() {
    const filtered = applyFilters(window.GT.sessions);
    if (filtered.length === 0) {
      elList.innerHTML = '<div class="gt-empty">No sessions match filter.</div>';
    } else {
      elList.innerHTML = filtered.map(renderRow).join('');
    }
    elCount.textContent = String(filtered.length);
  }

  // Click-to-select with dirty guard.
  elList.addEventListener('click', (e) => {
    const row = e.target.closest('.gt-session-row');
    if (!row) return;
    const sid = row.dataset.sid;
    if (sid === window.GT.selected.sid) return;
    if (window.GT.editor.dirty) {
      const ok = window.confirm('未加入佇列的修改會丟失，確定切換 session?');
      if (!ok) return;
    }
    window.GT.selected.sid = sid;
    window.GT.selected.cam = 'A';
    window.GT.editor.dirty = false;
    window.GT.editor.click = { x: null, y: null, t: null };
    renderList();
    if (window.GT.render.editor) window.GT.render.editor();
  });

  elFilterText.addEventListener('input', renderList);

  window.GT.render.sessionList = renderList;
})();
