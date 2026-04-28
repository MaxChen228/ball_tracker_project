"""SSR for `/gt` — the SAM 3 GT labelling workspace.

Three-zone layout:
  * Left rail  — every session sorted by recency, glyph-coded `(·)`
                 / `(●)` / `(✓)` / `(⊘)` for has-GT progress (mini-plan
                 v4 row tint is secondary).
  * Editor     — single-cam toggle, MOV scrubber, detection-density
                 timeline with two range handles, prompt input,
                 [Add to queue] / [Skip session] / [Validate] /
                 [Report→].
  * Queue rail — pending / running / done / error / canceled rows, mask
                 preview thumbnail per running job, [Run] / [Pause] /
                 [Clear].

Hydration plan:
  * SSR paints the initial sessions list + queue snapshot from `state`
    so the page is usable before any JS runs.
  * `gt_main.js` polls `GET /gt/sessions` (5 s) + `GET /gt/queue` (1 s)
    + the active job's mask thumbnail via `/gt/preview/{id}.jpg`.
  * Editor mutations are POST-then-poll; we never push UI state from
    the client outside of those POSTs.

CSS / JS are concatenated from `static/gt/`. The render function here
just inlines the design-token CSS + reads files from disk + emits the
HTML scaffold; we deliberately don't run a build step (project rule).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from render_shared import _CSS, _render_app_nav

if TYPE_CHECKING:
    from state import State

_STATIC_DIR = Path(__file__).resolve().parent / "static" / "gt"


def _read_static_file(name: str) -> str:
    """Read a /gt-static asset; return empty on missing (dev-friendly).

    We don't care about ordering — main.js wires DOMContentLoaded so any
    file load order is fine, but `<script>` tags are concatenated in
    sorted name order to keep the SSR output deterministic for tests."""
    path = _STATIC_DIR / name
    if not path.is_file():
        return ""
    return path.read_text()


def _concatenate_static(suffix: str) -> str:
    """Join every file in `_STATIC_DIR` matching `suffix` with a `/* … */`
    separator. The parens around the separator string are LOAD-BEARING:
    Python's `.` binds tighter than `+`, so without them only the trailing
    `" concatenated ----- */\\n\\n"` was passed to `.join()` — the `/*`
    opener leaked once at the head, swallowing file 1 until the first
    `*/` and dropping the bundle into a `Unexpected token '--'` parse
    error in the browser."""
    if not _STATIC_DIR.is_dir():
        return ""
    files = sorted(p for p in _STATIC_DIR.iterdir() if p.name.endswith(suffix))
    sep = "\n\n/* ----- " + suffix + " concatenated ----- */\n\n"
    return sep.join(p.read_text() for p in files)


# ----- top-level render -----------------------------------------------


def render_gt_page(state: "State") -> str:
    """Render the /gt page from current State.

    Pulls a fresh GTIndex.get_all() and queue snapshot for SSR; the
    front-end re-fetches via JSON polls thereafter."""
    # Nav strip just needs a devices list shape; we pass an empty list
    # because /gt isn't a session-monitoring page (the dashboard owns
    # that responsibility). Calibrations / current session render as
    # status chips per the shared _render_app_nav contract.
    devices_payload: list[dict] = []
    session = None
    cs = state.current_session()
    if cs is not None:
        session = {"session_id": cs.session_id, "armed": True}
    calibrations = sorted(state.calibrations().keys())

    sessions = state.gt_index.get_all()
    queue_items = state.gt_queue.get_all()

    initial_state = json.dumps(
        {
            "sessions": [s.to_dict() for s in sessions],
            "queue": {
                "items": [it.to_dict() for it in queue_items],
                "paused": state.gt_queue.paused(),
            },
        },
        ensure_ascii=False,
    )

    css = _GT_CSS + "\n" + _concatenate_static(".css")
    js = _concatenate_static(".js")

    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ball_tracker · gt</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap" rel="stylesheet">'
        f"<style>{_CSS}{css}</style>"
        '</head><body data-page="gt">'
        f"{_render_app_nav('gt', devices_payload, session, calibrations)}"
        '<main class="main-gt">'
        f"{_render_sessions_panel(sessions)}"
        f"{_render_editor_panel()}"
        f"{_render_queue_panel(queue_items, state.gt_queue.paused())}"
        "</main>"
        f"<script>window.__GT_INITIAL_STATE__ = {initial_state};</script>"
        f"<script>{js}</script>"
        "</body></html>"
    )


# ----- sub-renderers ---------------------------------------------------


def _render_sessions_panel(sessions) -> str:
    """Left rail. SSR-only static markup; gt_main.js refreshes this on
    the 5 s tick by replacing the inner list.

    Filter checkboxes (`unlabeled only` / `show no-MOV` / `show skipped`)
    were dropped 2026-04-29 — the operator confirmed they didn't help
    the workflow and just added cognitive load. Plain free-text filter
    is enough."""
    rows_html = "".join(_render_session_row(s) for s in sessions) or (
        '<div class="gt-empty">No sessions yet — record some pitches first.</div>'
    )
    return (
        '<aside class="gt-sessions card">'
        '<div class="gt-panel-head">'
        f'<h2>Sessions <span class="gt-count" id="gt-session-count">{len(sessions)}</span></h2>'
        '</div>'
        '<div class="gt-filters">'
        '<input type="text" id="gt-filter-text" placeholder="filter sid…" autocomplete="off">'
        '</div>'
        f'<div class="gt-session-list" id="gt-session-list" role="list">{rows_html}</div>'
        '</aside>'
    )


def _render_session_row(s) -> str:
    glyph = _glyph_for(s)
    tint = _tint_for(s)
    return (
        f'<div class="gt-session-row {tint}" '
        f'role="listitem" '
        f'data-sid="{s.session_id}" '
        f'data-has-gt-a="{int(bool(s.has_gt.get("A")))}" '
        f'data-has-gt-b="{int(bool(s.has_gt.get("B")))}" '
        f'data-has-mov-a="{int(bool(s.has_mov.get("A")))}" '
        f'data-has-mov-b="{int(bool(s.has_mov.get("B")))}" '
        f'data-skipped="{int(s.is_skipped)}" '
        f'data-recency="{s.recency:.0f}">'
        f'<span class="gt-sid">{s.session_id}</span>'
        f'<span class="gt-glyph">{glyph}</span>'
        '</div>'
    )


def _glyph_for(s) -> str:
    if s.is_skipped:
        return "(⊘)"
    a, b = bool(s.has_gt.get("A")), bool(s.has_gt.get("B"))
    if a and b:
        return "(✓)"
    if a or b:
        return "(●)"
    return "(·)"


def _tint_for(s) -> str:
    """Row class for tint. Glyph is primary signal; tint is supportive
    (color-blind operators rely on glyph). Mini-plan v4."""
    if s.is_skipped:
        return "gt-row-skipped"
    a, b = bool(s.has_gt.get("A")), bool(s.has_gt.get("B"))
    if a and b:
        return "gt-row-passed"
    if a or b:
        return "gt-row-warn"
    return "gt-row-neutral"


def _render_editor_panel() -> str:
    """Middle pane. SAM 2 era: drop the prompt input (text prompts don't
    apply to SAM 2), drop the Validate button (Validate / Report were
    SAM 3 era validation flows; CLI / direct URL still works), wrap the
    `<video>` in a positioned div so JS can overlay a click marker."""
    return (
        '<section class="gt-editor card">'
        '<div class="gt-panel-head">'
        '<h2 id="gt-editor-title">← pick a session</h2>'
        '<div class="gt-detail-actions" id="gt-detail-actions" hidden>'
        '<button type="button" class="btn danger" id="gt-skip-btn">Skip permanently</button>'
        '<button type="button" class="btn secondary" id="gt-unskip-btn" hidden>Unskip</button>'
        '</div>'
        '</div>'
        '<div class="gt-cam-toggle" id="gt-cam-toggle" hidden>'
        '<label><input type="radio" name="gt-cam" value="A" checked> Cam A</label>'
        '<label><input type="radio" name="gt-cam" value="B"> Cam B</label>'
        '</div>'
        '<div class="gt-click-hint" id="gt-click-hint" hidden>'
        'Pause at the first frame where the ball is clearly visible, '
        'then click the ball to set the seed point.'
        '</div>'
        '<div class="gt-video-wrap" id="gt-video-wrap" hidden>'
        '<video id="gt-video" preload="metadata"></video>'
        '<div class="gt-video-overlay" id="gt-video-overlay">'
        '<div class="gt-click-marker" id="gt-click-marker" hidden></div>'
        '</div>'
        '<div class="gt-video-controls" id="gt-video-controls">'
        '<button type="button" class="btn small" id="gt-video-play">Play</button>'
        '<button type="button" class="btn small secondary" id="gt-video-step-back" title=", → step −1 frame (~240 fps)">⟨</button>'
        '<button type="button" class="btn small secondary" id="gt-video-step-fwd" title=". → step +1 frame">⟩</button>'
        '<span class="gt-video-time" id="gt-video-time">0.00 / 0.00 s</span>'
        '</div>'
        '<div class="gt-video-meta" id="gt-video-meta">—</div>'
        '</div>'
        '<div class="gt-timeline" id="gt-timeline" hidden>'
        '<svg id="gt-timeline-svg" preserveAspectRatio="none"></svg>'
        '<div class="gt-timeline-hint" id="gt-timeline-hint"></div>'
        '</div>'
        '<div class="gt-range-row" id="gt-range-row" hidden>'
        '<label>start <input type="number" step="0.01" min="0" id="gt-range-start"></label>'
        '<label>end <input type="number" step="0.01" min="0" id="gt-range-end"></label>'
        '<span class="gt-click-readout" id="gt-click-readout">click: —</span>'
        '</div>'
        '<div class="gt-add-row" id="gt-add-row" hidden>'
        '<button type="button" class="btn primary" id="gt-add-btn" disabled>Add to queue</button>'
        '<span class="gt-overwrite-warn" id="gt-overwrite-warn" hidden>⚠ overwrites existing GT</span>'
        '<span class="gt-add-error" id="gt-add-error" hidden></span>'
        '</div>'
        '<div class="gt-empty-hint" id="gt-empty-hint">Pick a session from the left to begin labelling.</div>'
        '</section>'
    )


def _render_queue_panel(items, paused: bool) -> str:
    rows_html = "".join(_render_queue_row(it) for it in items) or (
        '<div class="gt-empty">Queue idle — pick a session and add a range.</div>'
    )
    paused_label = "▸ Run" if paused else "Pause"
    paused_class = "btn primary" if paused else "btn secondary"
    return (
        '<section class="gt-queue card">'
        '<div class="gt-panel-head">'
        '<h2>Queue '
        f'<span class="gt-count" id="gt-queue-summary">{_summary_text(items, paused)}</span>'
        '</h2>'
        '<div class="gt-queue-controls">'
        f'<button type="button" class="{paused_class}" id="gt-queue-toggle" data-paused="{int(paused)}">{paused_label}</button>'
        '<button type="button" class="btn secondary" id="gt-queue-clear-done">Clear done</button>'
        '<button type="button" class="btn secondary" id="gt-queue-clear-errors">Clear errors</button>'
        '</div>'
        '</div>'
        f'<div class="gt-queue-list" id="gt-queue-list" role="list">{rows_html}</div>'
        '</section>'
    )


def _render_queue_row(it) -> str:
    cls = f"gt-queue-row gt-status-{it.status}"
    label = _label_for_queue_item(it)
    return f'<div class="{cls}" role="listitem" data-id="{it.id}">{label}</div>'


def _label_for_queue_item(it) -> str:
    range_str = f"[{it.time_range[0]:.2f}–{it.time_range[1]:.2f}s]"
    click_str = f" click=({it.click_x},{it.click_y})@{it.click_t_video_rel:.2f}"
    base = f"{it.session_id}/{it.camera_id} {range_str}{click_str}"
    if it.status == "running" and it.progress:
        cur = it.progress.get("current_frame", 0)
        total = it.progress.get("total_frames", 0)
        pct = int(100 * cur / total) if total else 0
        return f"▶ {base} · frame {cur}/{total} · {pct}%"
    if it.status == "pending":
        return f"⏳ {base}"
    if it.status == "done":
        n_lab = it.n_labelled if it.n_labelled is not None else 0
        n_dec = it.n_decoded if it.n_decoded is not None else 0
        return f"✓ {base} · {n_lab}/{n_dec} frames"
    if it.status == "error":
        msg = (it.error or "").splitlines()[0][:80] if it.error else "error"
        return f"✗ {base} · {msg}"
    if it.status == "canceled":
        return f"⊘ {base}"
    return base


def _summary_text(items, paused: bool) -> str:
    """One-line summary for the queue panel head. Mirrored by JS in the
    queue tick — keep formats identical so the SSR-then-JS handoff is
    seamless."""
    counts = {"pending": 0, "running": 0, "done": 0, "error": 0, "canceled": 0}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    base = (
        f"total: {len(items)} · running: {counts['running']} · "
        f"queued: {counts['pending']} · done: {counts['done']}"
    )
    if paused:
        base += " · PAUSED"
    return base


# ----- inline CSS (top-level layout) ----------------------------------


_GT_CSS = """
/* Make HTML `hidden` attribute beat author display rules.
   Without this, `.gt-cam-toggle { display: flex }` etc override the
   user-agent `[hidden] { display: none }`, leaving the editor form
   visible before any session is selected (visible bug 2026-04-28). */
[hidden] { display: none !important; }

.main-gt {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr) 360px;
  grid-template-rows: 1fr;
  gap: var(--s-3);
  padding: calc(var(--nav-offset) + var(--s-3)) var(--s-3) var(--s-3) var(--s-3);
  height: 100vh;
  box-sizing: border-box;
  overflow: hidden;
}
.gt-sessions, .gt-queue { overflow-y: auto; min-height: 0; }
.gt-editor { overflow-y: auto; min-height: 0; }
.gt-panel-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: var(--s-3);
}
.gt-panel-head h2 {
  font-family: var(--mono); font-size: 12px; letter-spacing: 0.14em;
  text-transform: uppercase; margin: 0; color: var(--ink);
}
.gt-count {
  font-family: var(--mono); font-size: 10px; color: var(--sub);
  margin-left: var(--s-2); font-weight: 400;
}
.gt-filters {
  display: flex; flex-direction: column; gap: var(--s-2);
  margin-bottom: var(--s-3);
}
.gt-filters input[type=text] {
  font-family: var(--mono); font-size: 12px;
  padding: 6px 8px; border: 1px solid var(--border-base);
  border-radius: var(--r); background: var(--surface);
  color: var(--ink);
}
.gt-filters label {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  display: flex; align-items: center; gap: var(--s-2);
}
.gt-session-list { display: flex; flex-direction: column; gap: 2px; }
.gt-session-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 10px; cursor: pointer; user-select: none;
  border: 1px solid transparent; border-radius: var(--r);
  font-family: var(--mono); font-size: 11px;
  transition: background 0.08s;
}
.gt-session-row:hover { background: var(--surface-hover); }
.gt-session-row.gt-row-selected { border-color: var(--ink); background: var(--surface-hover); }
.gt-row-neutral { background: transparent; }
.gt-row-warn { background: var(--warn-bg); }
.gt-row-passed { background: var(--passed-bg); }
.gt-row-failed { background: var(--failed-bg); }
.gt-row-skipped { background: var(--idle-bg); opacity: 0.55; }
.gt-sid { color: var(--ink); }
.gt-glyph { color: var(--sub); }

.gt-cam-toggle {
  display: flex; gap: var(--s-3); margin-bottom: var(--s-3);
  font-family: var(--mono); font-size: 12px;
}
.gt-video-wrap { position: relative; margin-bottom: var(--s-3); }
.gt-video-wrap video {
  width: 100%; max-height: 50vh; background: #000;
  display: block;
  cursor: crosshair;  /* signals "click to seed" affordance */
}
/* Overlay sits over the video and proxies clicks; the video element
   itself ignores pointer events so our click handler sees the crosshair
   target. Marker is positioned absolutely in CSS-px space relative to
   the wrap, so we don't have to worry about video aspect-fit math. */
.gt-video-overlay {
  position: absolute; inset: 0; pointer-events: none;
}
.gt-click-marker {
  position: absolute; width: 16px; height: 16px;
  border: 2px solid var(--failed); border-radius: 50%;
  transform: translate(-50%, -50%);
  pointer-events: none;
  box-shadow: 0 0 0 1px rgba(255,255,255,0.8);
}
.gt-video-controls {
  display: flex; gap: var(--s-2); align-items: center;
  margin-top: var(--s-2);
}
.gt-video-time {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  margin-left: var(--s-2);
}
.gt-video-meta {
  font-family: var(--mono); font-size: 10px; color: var(--sub);
  margin-top: var(--s-1);
}
.gt-click-hint {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  background: var(--warn-bg); border: 1px solid var(--warn);
  padding: 6px 10px; border-radius: var(--r);
  margin-bottom: var(--s-2);
}
.gt-click-readout {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  align-self: end;
}
.gt-timeline {
  position: relative; margin-bottom: var(--s-3);
  height: 80px; border: 1px solid var(--border-base); background: var(--surface);
}
.gt-timeline svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.gt-timeline-hint {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-family: var(--mono); font-size: 10px; color: var(--sub); pointer-events: none;
}

.gt-range-row {
  display: flex; gap: var(--s-3); flex-wrap: wrap; margin-bottom: var(--s-3);
}
.gt-range-row label {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  display: flex; flex-direction: column; gap: 4px;
}
.gt-range-row input[type=number] {
  width: 100px; font-family: var(--mono); padding: 4px 6px;
  border: 1px solid var(--border-base); border-radius: var(--r);
  background: var(--surface);
}
.gt-add-row {
  display: flex; gap: var(--s-3); align-items: center; flex-wrap: wrap;
}
.gt-overwrite-warn { font-family: var(--mono); font-size: 11px;
  color: var(--warn); }
.gt-add-error { font-family: var(--mono); font-size: 11px;
  color: var(--failed); }

.gt-queue-list { display: flex; flex-direction: column; gap: 4px; }
.gt-queue-row {
  padding: 6px 10px; font-family: var(--mono); font-size: 11px;
  border: 1px solid var(--border-base); border-radius: var(--r);
  background: var(--surface);
}
.gt-status-running { border-color: var(--warn); background: var(--warn-bg); }
.gt-status-done { color: var(--passed); }
.gt-status-error { color: var(--failed); }
.gt-status-canceled { color: var(--sub); opacity: 0.7; }
.gt-empty {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  text-align: center; padding: var(--s-4);
}
.gt-empty-hint {
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  text-align: center; padding: var(--s-3); margin-top: var(--s-3);
}
.gt-detail-actions {
  display: flex; gap: var(--s-2); align-items: center;
}
.gt-queue-controls {
  display: flex; gap: var(--s-2);
}

/* ----- buttons -----
   PHYSICS_LAB family: 1px borders, no shadows, JetBrains Mono.
   Variants stack: `.btn.primary`, `.btn.secondary`, `.btn.danger`,
   `.btn.small`. Mirrors the dashboard's `.ev-btn` look but lives under
   the shorter `.btn` namespace the /gt JS bundle uses. */
.btn {
  display: inline-flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 11px; font-weight: 500;
  letter-spacing: 0.04em;
  padding: 6px 12px; min-height: 26px;
  border: 1px solid var(--border-base); border-radius: var(--r);
  background: var(--surface); color: var(--ink);
  cursor: pointer; user-select: none;
  transition: background 0.08s, border-color 0.08s, color 0.08s;
  text-decoration: none;
}
.btn:hover { background: var(--surface-hover); border-color: var(--ink); }
.btn:active { background: var(--surface-hover); }
.btn:disabled,
.btn[disabled] {
  cursor: not-allowed; opacity: 0.5;
  background: var(--surface); color: var(--sub);
  border-color: var(--border-base);
}
.btn.primary {
  background: var(--ink); color: var(--surface); border-color: var(--ink);
}
.btn.primary:hover { background: var(--accent); border-color: var(--accent); color: var(--surface); }
.btn.secondary { color: var(--sub); }
.btn.secondary:hover { color: var(--ink); }
.btn.danger { color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }
.btn.danger:hover { background: var(--failed); color: var(--surface); border-color: var(--failed); }
.btn.small { font-size: 10px; padding: 3px 8px; min-height: 22px; }
"""
