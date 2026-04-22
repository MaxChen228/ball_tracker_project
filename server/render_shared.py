"""Shared renderer primitives for the operator web shell.

This module holds the PHYSICS_LAB design tokens/shared CSS and the
top-level app navigation used across dashboard-adjacent pages.
"""
from __future__ import annotations

import html
from typing import Any


_BG = "#F8F7F4"
_SURFACE = "#FCFBFA"
_BORDER_BASE = "#DBD6CD"
_BORDER_L = "#E8E4DB"
_INK = "#2A2520"
_SUB = "#7A756C"
_INK_LIGHT = "#5A5550"
_DEV = "#C0392B"
_CONTRA = "#4A6B8C"
_DUAL = "#D35400"
_ACCENT = "#E6B300"


_CSS = f"""
:root {{
  --bg: {_BG};
  --surface: {_SURFACE};
  --surface-hover: #F3F0EA;
  --border-base: {_BORDER_BASE};
  --border-l: {_BORDER_L};
  --ink: {_INK};
  --sub: {_SUB};
  --ink-light: {_INK_LIGHT};
  --dev: {_DEV};
  --contra: {_CONTRA};
  --dual: {_DUAL};
  --accent: {_ACCENT};
  /* Semantic state washes — mirror kg admin's badge palette so chips
     read as subdued backgrounds rather than saturated pills. */
  --passed:      #256246; --passed-bg:  rgba(37,98,70,.08);
  --warn:        #9B6B16; --warn-bg:    rgba(155,107,22,.08);
  --failed:      #A7372A; --failed-bg:  rgba(167,55,42,.08);
  --idle-bg:     rgba(105,114,125,.06);
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --nav-h: 82px;
  --nav-offset: var(--nav-h);
  --sidebar-w: 440px;
  /* Unified 8px-grid spacing + single border-radius. Use var(--r)
     everywhere; the old 4/12/2 mix collapses to one rhythm. */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px; --s-5: 24px;
  --r: 3px;
}}

* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--ink);
              font-family: var(--sans); font-weight: 300; line-height: 1.8;
              -webkit-font-smoothing: antialiased; }}

/* --- App header --- */
.nav {{ position: fixed; top: 0; left: 0; right: 0; min-height: var(--nav-h);
        background: rgba(252, 251, 250, 0.96); backdrop-filter: blur(10px);
        border-bottom: 1px solid var(--border-base); padding: 12px 24px 10px 24px;
        z-index: 20; display: flex; flex-direction: column; gap: 8px; }}
.nav-main {{ display: grid; grid-template-columns: minmax(0, 1fr) auto;
             align-items: start; gap: 12px 24px; }}
.nav-brand-block {{ display: flex; align-items: center; gap: 18px; min-width: 0; }}
.nav .brand {{ font-family: var(--mono); font-weight: 700; font-size: 14px;
               letter-spacing: 0.16em; color: var(--ink); text-decoration: none;
               line-height: 1.3; white-space: nowrap; }}
.nav .brand .dot {{ display: inline-block; width: 7px; height: 7px; background: var(--ink);
                    margin-right: 10px; vertical-align: middle; border-radius: 0; }}
.nav-page {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
.nav-page-kicker {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
                    text-transform: uppercase; color: var(--sub); }}
.nav-page-title {{ font-family: var(--mono); font-size: 18px; line-height: 1.1;
                   letter-spacing: 0.02em; color: var(--ink); }}
.nav-tabs {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.nav-tab {{ display: inline-flex; align-items: center; min-height: 32px;
            padding: 6px 12px; border: 1px solid var(--border-base); border-radius: var(--r);
            text-decoration: none; background: transparent; color: var(--sub);
            font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
            text-transform: uppercase; transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease; }}
.nav-tab:hover {{ border-color: var(--ink); color: var(--ink); background: var(--surface-hover); }}
.nav-tab.active {{ background: var(--ink); border-color: var(--ink); color: var(--surface); }}
.nav-status-row {{ display: flex; justify-content: flex-end; }}
.nav .status-line {{ display: flex; flex-direction: column; align-items: flex-end;
                     gap: 6px; min-width: min(560px, 100%); }}
.nav .status-main {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
                     font-family: var(--mono); text-transform: uppercase; }}
.nav .status-badge {{ display: inline-flex; align-items: center; padding: 3px 8px;
                      border: 1px solid var(--border-base); border-radius: var(--r);
                      font-size: 10px; letter-spacing: 0.12em; color: var(--sub); }}
.nav .status-badge.ready, .nav .status-badge.recording {{
  color: var(--passed); border-color: var(--passed); background: var(--passed-bg);
}}
.nav .status-badge.blocked, .nav .status-badge.cooldown {{
  color: var(--warn); border-color: var(--warn); background: var(--warn-bg);
}}
.nav .status-badge.syncing {{
  color: var(--ink); border-color: var(--ink); background: rgba(42,37,32,.04);
}}
.nav .status-headline {{ font-size: 12px; letter-spacing: 0.12em; color: var(--ink); }}
.nav .status-context {{ font-size: 10px; letter-spacing: 0.08em; color: var(--sub); }}
.nav .status-checks {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.nav .status-check {{ display: inline-flex; align-items: center; gap: 6px;
                      padding: 2px 8px; border: 1px solid var(--border-base);
                      border-radius: var(--r); font-family: var(--mono); font-size: 10px;
                      letter-spacing: 0.08em; text-transform: uppercase; color: var(--sub); }}
.nav .status-check.ok {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}
.nav .status-check.warn {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
.nav .status-check .k {{ opacity: 0.8; }}
.nav .status-check .v {{ color: currentColor; font-weight: 600; }}

/* --- Main layout: sidebar + canvas --- */
.layout {{ display: flex; min-height: 100vh; padding-top: var(--nav-offset); }}
.sidebar {{ width: var(--sidebar-w); flex-shrink: 0; overflow-y: auto;
            background: var(--surface); border-right: 1px solid var(--border-base);
            padding: var(--s-5) var(--s-4); z-index: 10;
            display: flex; flex-direction: column; gap: var(--s-3); }}
.canvas {{ flex: 1; position: relative; overflow: hidden;
           background: var(--bg); }}
#scene-root {{ position: absolute; inset: 0; }}

/* --- Scrollbar --- */
.sidebar::-webkit-scrollbar {{ width: 4px; }}
.sidebar::-webkit-scrollbar-track {{ background: transparent; }}
.sidebar::-webkit-scrollbar-thumb {{ background: var(--border-base); }}
.sidebar::-webkit-scrollbar-thumb:hover {{ background: var(--sub); }}

/* --- Card --- */
.card {{ background: var(--surface); border: 1px solid var(--border-base);
         border-radius: var(--r); padding: var(--s-4); }}
.card + .card {{ margin-top: 0; }}
.card-title {{ font-family: var(--mono); font-weight: 500; font-size: 11px;
               letter-spacing: 0.12em; text-transform: uppercase; color: var(--sub);
               margin: 0 0 var(--s-3) 0; padding: 0 0 var(--s-2) 0;
               border-bottom: 1px solid var(--border-l); }}
.card-subtitle {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em;
                  text-transform: uppercase; color: var(--sub);
                  margin-top: var(--s-3); margin-bottom: var(--s-1); }}
.card section + section {{ border-top: 1px solid var(--border-l); margin-top: var(--s-3);
                           padding-top: var(--s-3); }}
"""


def _render_nav_status(
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    calibrations: list[str],
    sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
) -> str:
    armed = session is not None and session.get("armed")
    online = len(devices)
    calibrated = len(calibrations)
    synced = sum(1 for d in devices if d.get("time_synced"))
    expected = 2

    if armed:
        badge_cls = "recording"
        badge = "Recording"
        headline = html.escape(session.get("id", "—"))
        context = "session active"
    elif sync is not None:
        badge_cls = "syncing"
        badge = "Sync"
        headline = "sync in progress"
        context = "complete on /sync"
    elif online < expected:
        badge_cls = "blocked"
        badge = "Blocked"
        headline = "bring both devices online"
        context = f"{online}/{expected} devices available"
    elif calibrated < expected:
        badge_cls = "blocked"
        badge = "Blocked"
        headline = "finish calibration"
        context = f"{calibrated}/{expected} cameras calibrated"
    elif synced < expected:
        badge_cls = "blocked"
        badge = "Blocked"
        headline = "run time sync"
        context = f"{synced}/{expected} cameras synced"
    elif sync_cooldown_remaining_s > 0.0:
        badge_cls = "cooldown"
        badge = "Cooldown"
        headline = "sync settling"
        context = f"{sync_cooldown_remaining_s:.0f}s remaining"
    else:
        badge_cls = "ready"
        badge = "Ready"
        headline = "ready to arm"
        context = "all prerequisites satisfied"

    def _check_row(label: str, value: str, ok: bool) -> str:
        cls = "ok" if ok else "warn"
        return (
            f'<span class="status-check {cls}">'
            f'<span class="k">{html.escape(label)}</span>'
            f'<span class="v">{html.escape(value)}</span>'
            f"</span>"
        )

    checks = "".join(
        [
            _check_row("Devices", f"{online}/{expected}", online >= expected),
            _check_row("Cal", f"{calibrated}/{expected}", calibrated >= expected),
            _check_row("Sync", f"{synced}/{expected}", synced >= expected),
        ]
    )
    return (
        '<div class="status-main">'
        f'<span class="status-badge {badge_cls}">{html.escape(badge)}</span>'
        f'<span class="status-headline">{headline}</span>'
        f'<span class="status-context">{html.escape(context)}</span>'
        "</div>"
        f'<div class="status-checks">{checks}</div>'
    )


_PAGE_META: dict[str, tuple[str, str]] = {
    "dashboard": ("Operator Surface", "Dashboard"),
    "setup": ("Calibration", "Setup"),
    "sync": ("Time Sync", "Sync"),
    "markers": ("Registry", "Markers"),
}


def _render_primary_nav(active_page: str) -> str:
    items = [
        ("dashboard", "/", "Dashboard"),
        ("setup", "/setup", "Setup"),
        ("sync", "/sync", "Sync"),
        ("markers", "/markers", "Markers"),
    ]
    return "".join(
        f'<a class="nav-tab{" active" if key == active_page else ""}" href="{href}">{label}</a>'
        for key, href, label in items
    )


def _render_app_nav(
    active_page: str,
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    calibrations: list[str],
    sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
) -> str:
    kicker, title = _PAGE_META.get(active_page, _PAGE_META["dashboard"])
    return (
        '<nav class="nav">'
        '<div class="nav-main">'
        '<div class="nav-brand-block">'
        '<a class="brand" href="/"><span class="dot"></span>BALL_TRACKER</a>'
        '<div class="nav-page">'
        f'<div class="nav-page-kicker">{html.escape(kicker)}</div>'
        f'<div class="nav-page-title">{html.escape(title)}</div>'
        '</div>'
        '</div>'
        f'<div class="nav-tabs">{_render_primary_nav(active_page)}</div>'
        '</div>'
        '<div class="nav-status-row">'
        f'<div class="status-line" id="nav-status">{_render_nav_status(devices, session, calibrations, sync, sync_cooldown_remaining_s)}</div>'
        '</div>'
        '</nav>'
    )
