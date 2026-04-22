"""Dashboard renderer for `/` — three-zone layout (top nav + 440px
sidebar + full-bleed 3D canvas) styled after the PHYSICS_LAB design
system. The canvas shows a live 3D scene of the plate plus whichever
cameras have a calibration persisted; the sidebar carries devices,
session controls, and the events list. All three columns tick from
JSON endpoints (`/status`, `/calibration/state`, `/events`) so the page
never has to reload to reflect a new calibration or a new pitch."""
from __future__ import annotations

import datetime as _dt
import html
from typing import Any

from reconstruct import build_calibration_scene
from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    DRAW_PLATE_OVERLAY_JS,
    LIVE_COMPARE_CSS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
    render_live_compare_camera,
)
from render_dashboard_client import _JS_TEMPLATE, _JS_TEMPLATE_RAW, _resolve_js_template
from render_scene import _build_figure
from render_shared import (
    _CSS as _SHARED_CSS,
    _render_app_nav as _shared_render_app_nav,
    _render_nav_status as _shared_render_nav_status,
    _render_primary_nav as _shared_render_primary_nav,
)
from render_tuning import (
    _render_chirp_threshold_body as _shared_render_chirp_threshold_body,
    _render_tuning_body as _shared_render_tuning_body,
)
from schemas import Device, Session


# --- Design-system tokens (mirrored in render_scene.py) ----------------------
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

/* --- Device rows --- */
/* Middle column uses minmax so a wider chip (CALIBRATED vs OFFLINE) can't
   squeeze the sub-row into a second line and make A / B rows different
   heights. `auto` → min-content keeps the chip column tight. */
.device {{ padding: var(--s-2) 0; }}
.device + .device {{ border-top: 1px solid var(--border-l); }}
/* Row 1: id (fixed 28px) | blank stretch | chip (auto). Sub-line gets
   its own full-width row below so long labels like "time sync · not
   synced" + "pose · last 16:13" never collide with the chip. */
.device-head {{ display: grid; grid-template-columns: 14px 28px 1fr auto;
                align-items: center; gap: var(--s-2) var(--s-3); }}
.device-head .sync-led {{ grid-column: 1; grid-row: 1;
                          width: 12px; height: 12px; border-radius: 50%;
                          background: var(--border); justify-self: center; }}
.device-head .sync-led.synced {{ background: var(--passed);
                                 box-shadow: 0 0 8px rgba(125, 255, 192, 0.5); }}
.device-head .sync-led.waiting {{ background: var(--border);
                                  border: 1px dashed var(--sub); }}
.device-head .sync-led.listening {{ background: var(--warn); }}
.device-head .sync-led.offline {{ background: var(--border); opacity: 0.45; }}
.device-head .id {{ grid-column: 2; grid-row: 1; }}
.device-head .chip-col {{ grid-column: 4; grid-row: 1; justify-self: end; }}
.device-head .sub {{ grid-column: 1 / -1; grid-row: 2; }}
.sync-id-chip {{ margin-left: 6px; padding: 1px 5px; font-family: var(--mono);
                 font-size: 9px; border: 1px solid var(--border);
                 color: var(--sub); }}
.device-actions {{ display: flex; gap: var(--s-2); margin-top: var(--s-2); flex-wrap: wrap; }}
.device .id {{ font-family: var(--mono); font-size: 14px; font-weight: 600; color: var(--ink);
               letter-spacing: 0.04em; }}
.device .meta {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                 text-transform: uppercase; color: var(--sub); }}
.device .meta em {{ font-style: normal; color: var(--ink-light); }}
/* Sub-line stacks vertically so long labels ("not synced", "last 16:13")
   never get truncated. One item per line, full card width. Warn/bad
   states get an obvious tinted background so an offline / not-synced
   camera jumps out of the card at a glance. */
.device .sub {{ display: flex; flex-direction: column; gap: 3px;
                margin-top: var(--s-1); }}
.device .sub .item {{ font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em;
                      text-transform: uppercase; color: var(--sub);
                      display: flex; align-items: center; gap: var(--s-2);
                      padding: 3px 8px; border-radius: var(--r);
                      white-space: nowrap; }}
.device .sub .item.ok {{ background: rgba(56, 142, 60, 0.06);
                         color: var(--ink); }}
.device .sub .item.warn {{ background: rgba(230, 145, 40, 0.14);
                           color: #8a4a00; font-weight: 700; }}
.device .sub .item.bad {{ background: rgba(210, 50, 50, 0.14);
                          color: #a6262f; font-weight: 700; }}
.device .sub .dot {{ width: 7px; height: 7px; border-radius: 50%;
                     background: var(--border-base); display: inline-block;
                     flex-shrink: 0; }}
.device .sub .dot.ok {{ background: var(--passed); }}
.device .sub .dot.warn {{ background: var(--warn); }}
.device .sub .dot.bad {{ background: var(--failed); }}

/* --- Chip (pill) — kg-admin badge style: flat, rectangular, subdued bg wash.
   Single rectangle geometry with three semantic variants (passed/warn/failed)
   replacing the former 10+ custom colors. */
.chip {{ display: inline-block; padding: 2px 8px; border-radius: var(--r);
         font-family: var(--mono); font-size: 10px; font-weight: 500;
         letter-spacing: 0.10em; text-transform: uppercase;
         border: 1px solid var(--border-base); color: var(--sub); background: transparent;
         transition: border-color 0.15s ease, color 0.15s ease; }}
/* Green wash — online / calibrated / armed / paired successes */
.chip.online, .chip.calibrated, .chip.armed, .chip.paired
  {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}
/* Amber wash — degraded / partial / paired-no-points / on-device accent */
.chip.partial, .chip.paired_no_points, .chip.on-device
  {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
/* Red wash — explicit errors */
.chip.error {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
/* Neutral (grey) — idle / single / camera-only default */
.chip.idle, .chip.single, .chip.camera-only
  {{ color: var(--sub); border-color: var(--border-base); background: transparent; }}
/* Cam-identity dual chip — retains the B-camera orange tint so per-cam
   rows still read as paired vs single at a glance. */
.chip.dual {{ color: var(--dual); border-color: var(--dual); background: rgba(211,84,0,0.06); }}
/* Fit-quality chips — RMS-bucketed from fitting.py. Drives operator trust
   at a glance: green = ship it, amber = inspect, red = reject / recalibrate. */
.chip.excellent {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}
.chip.good     {{ color: var(--passed); border-color: var(--passed); background: transparent; }}
.chip.fair     {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
.chip.poor     {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
.chip.no-fit   {{ color: var(--sub); border-color: var(--border-base); background: transparent; opacity: 0.7; }}

/* --- Session block --- */
.session-head {{ display: flex; align-items: center; gap: var(--s-2); margin-bottom: var(--s-2); }}
.session-id {{ font-family: var(--mono); font-size: 13px; color: var(--ink);
               letter-spacing: 0.04em; }}
.session-actions {{ display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap;
                     align-items: center; }}
/* Per-cam sync indicator next to the Quick chirp button. Three states:
   off = no device in registry; waiting = online but not time-synced;
   synced = holds a valid sync anchor. Operator reads this at a glance
   to answer "did my last quick chirp actually land on both cams?". */
.sync-led {{ display: inline-flex; align-items: center; gap: 4px;
             padding: 4px 8px; border-radius: 999px;
             border: 1px solid var(--border-l);
             background: var(--surface-hover);
             font-family: var(--mono); font-size: 10px;
             letter-spacing: 0.08em; color: var(--sub);
             line-height: 1; }}
.sync-led::before {{ content: ''; width: 7px; height: 7px;
                     border-radius: 50%; background: var(--border-l); }}
.sync-led.off::before      {{ background: var(--sub); opacity: 0.35; }}
.sync-led.waiting::before  {{ background: var(--partial, #D9A441); }}
.sync-led.synced::before   {{ background: var(--full, #4C7A3F); }}
.sync-led.synced {{ color: var(--ink); border-color: var(--full, #4C7A3F); }}
.sidebar .session-actions button.btn {{ padding: 7px 12px; }}
.sidebar .arm-gate {{ margin-top: 8px; font-size: 11px; line-height: 1.45; color: var(--ink); }}
.sidebar .gate-label {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.10em;
                        text-transform: uppercase; color: var(--sub); margin-right: 6px; }}
.sidebar .paths-stack {{ gap: 10px; margin-top: 12px; }}
.sidebar .path-option {{ padding: 6px 8px; }}
.sidebar .paths-actions {{ margin-top: 10px; }}
.active-head {{ display:flex; align-items:center; gap:var(--s-2); margin-bottom:var(--s-2); }}
.active-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:var(--s-2); margin-top:var(--s-3); }}
.active-grid span {{ display:flex; flex-direction:column; gap:2px; padding:6px 8px;
                      border:1px solid var(--border-l); border-radius:var(--r);
                      background:rgba(42,37,32,0.02); }}
.active-grid .k {{ font-family:var(--mono); font-size:10px; letter-spacing:0.10em;
                   text-transform:uppercase; color:var(--sub); }}
.active-grid .v {{ font-family:var(--mono); font-size:13px; color:var(--ink); }}
.active-empty {{ font-family:var(--mono); font-size:11px; letter-spacing:0.08em; color:var(--sub); }}
.active-head .elapsed {{ margin-left:auto; font-family:var(--mono); font-size:11px; color:var(--sub);
                         letter-spacing:0.04em; }}
.chip.armed.pulse {{ animation: rec-pulse 1.4s ease-in-out infinite; }}
@keyframes rec-pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.45; }}
}}
.cam-row {{ display:grid; grid-template-columns: 80px 18px 1fr auto; align-items:center;
            gap:var(--s-2); padding:6px 8px; margin-top:var(--s-2);
            border:1px solid var(--border-l); border-radius:var(--r);
            background:rgba(42,37,32,0.02); }}
.cam-row .spark {{ width:80px; height:18px; display:block; }}
.cam-row .k {{ font-family:var(--mono); font-size:11px; color:var(--ink); font-weight:600; }}
.cam-row .v {{ font-family:var(--mono); font-size:11px; color:var(--ink); }}
.cam-row .vsub {{ font-family:var(--mono); font-size:10px; color:var(--sub); }}
.live-pairs {{ display:flex; gap:var(--s-2); align-items:center; padding:6px 8px;
               margin-top:var(--s-2); border:1px solid var(--border-l);
               border-radius:var(--r); background:rgba(42,37,32,0.02);
               transition: background 120ms, border-color 120ms; }}
.live-pairs .k {{ font-family:var(--mono); font-size:10px; letter-spacing:0.10em;
                  text-transform:uppercase; color:var(--sub); }}
.live-pairs .v {{ font-family:var(--mono); font-size:12px; color:var(--ink); }}
.live-pairs .vsub {{ font-family:var(--mono); font-size:10px; color:var(--sub); margin-left:auto; }}
.live-pairs.stale {{ border-color:var(--failed); background:var(--failed-bg); }}
.postpass-row {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:var(--s-2); }}
.postpass-chip {{ font-family:var(--mono); font-size:10px; letter-spacing:0.04em;
                  padding:2px 6px; border:1px solid var(--border-base);
                  border-radius:var(--r); color:var(--sub); }}
.postpass-chip.done {{ color:var(--passed); border-color:var(--passed); background:var(--passed-bg); }}
.postpass-chip.pending {{ color:var(--sub); }}
.postpass-chip.running {{ color:var(--ink); border-color:var(--ink); }}
.active-actions {{ display:flex; gap:var(--s-2); margin-top:var(--s-3); }}
.active-actions .btn-stop {{ padding:4px 12px; font:inherit; font-size:11px;
                              background:var(--failed); color:white; border:none;
                              border-radius:var(--r); cursor:pointer; }}
.active-actions .btn-reset {{ padding:4px 12px; font:inherit; font-size:11px;
                               background:transparent; color:var(--sub);
                               border:1px solid var(--border-base);
                               border-radius:var(--r); cursor:pointer; }}
.active-actions .btn-reset:hover {{ color:var(--ink); border-color:var(--ink); }}
.mode-row {{ display: flex; gap: var(--s-2); align-items: center; margin-top: var(--s-3);
             flex-wrap: wrap; }}
.mode-label {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                text-transform: uppercase; color: var(--sub); min-width: 44px; }}
.mode-locked {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em;
                 color: var(--sub); padding-left: var(--s-1); }}
.paths-stack {{ display:flex; flex-direction:column; gap:var(--s-2); margin-top:var(--s-3); }}
.path-option {{ display:flex; gap:var(--s-2); align-items:flex-start; padding:8px;
                border:1px solid var(--border-l); border-radius:var(--r); }}
.path-option input {{ margin-top:3px; }}
.path-option .copy {{ display:flex; flex-direction:column; gap:1px; }}
.path-option .title {{ font-family:var(--mono); font-size:11px; color:var(--ink); letter-spacing:0.06em; }}
.path-option .sub {{ font-family:var(--sans); font-size:11px; color:var(--sub); line-height:1.5; }}
.paths-actions {{ margin-top:var(--s-2); }}
.path-chip-row {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:var(--s-2); }}
.path-chip {{ display:inline-block; padding:2px 8px; border:1px solid var(--border-base);
              border-radius:var(--r); font-family:var(--mono); font-size:10px;
              letter-spacing:0.08em; text-transform:uppercase; color:var(--sub); }}
.path-chip.on {{ color:var(--passed); border-color:var(--passed); background:var(--passed-bg); }}
/* Segmented control: the three mode buttons share one outer border and
   collapse their individual borders/radius so the eye reads them as a
   single exclusive choice, not three separate CTAs. */
.mode-segmented {{ display: inline-flex; border: 1px solid var(--border-base);
                    border-radius: var(--r); overflow: hidden; }}
.mode-segmented form.inline {{ display: inline-flex; margin: 0; }}
.mode-segmented form.inline + form.inline button.btn {{
  border-left: 1px solid var(--border-base); }}
.mode-segmented button.btn,
.mode-segmented button.btn.secondary {{
  border: 0; border-radius: 0; padding: 6px 12px; font-size: 10px;
  letter-spacing: 0.10em; }}
.mode-segmented button.btn.secondary {{
  background: transparent; color: var(--sub); }}
.mode-segmented button.btn.secondary:hover:not(:disabled) {{
  background: var(--surface-hover); color: var(--ink); border: 0; }}

/* --- Buttons — unified geometry, single border-radius. Standard is
   36px tall, mini variant (used in event delete) is 24px. --- */
button.btn {{ font-family: var(--mono); font-size: 11px; font-weight: 500;
              letter-spacing: 0.08em; text-transform: uppercase;
              padding: 8px 14px; border-radius: var(--r); cursor: pointer;
              background: var(--ink); color: var(--surface);
              border: 1px solid var(--ink); transition: border-color 0.15s, background 0.15s, color 0.15s; }}
button.btn:hover:not(:disabled) {{ background: var(--ink-light); }}
button.btn.secondary {{ background: transparent; color: var(--ink);
                        border-color: var(--border-base); }}
button.btn.secondary:hover:not(:disabled) {{ border-color: var(--ink); }}
button.btn.danger {{ background: transparent; color: var(--dev);
                     border-color: var(--dev); }}
button.btn.danger:hover:not(:disabled) {{ background: var(--dev); color: var(--surface); }}
button.btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}
button.btn.small {{ padding: 4px 10px; font-size: 10px; }}
a.btn-link {{ display:inline-flex; align-items:center; justify-content:center;
              font-family: var(--mono); font-size: 11px; font-weight: 500;
              letter-spacing: 0.08em; text-transform: uppercase;
              padding: 8px 14px; border-radius: var(--r); text-decoration: none;
              background: var(--ink); color: var(--surface); border: 1px solid var(--ink);
              transition: border-color 0.15s, background 0.15s, color 0.15s; }}
a.btn-link.secondary {{ background: transparent; color: var(--ink); border-color: var(--border-base); }}
a.btn-link.secondary:hover {{ border-color: var(--ink); }}
form.inline {{ display: inline-block; margin: 0; }}

/* Live-preview toggle + panel (Phase 4a). Mini button sits inline with
   the sub-line dots; the <img> panel sits full-width in the device row
   thanks to grid-column:1/-1 applied inline. 320×180 keeps the 440 px
   sidebar tidy. */
button.btn.preview-btn {{ padding: 3px 8px; font-size: 9px; letter-spacing: 0.10em; }}
button.btn.preview-btn.active {{ background: var(--passed); color: var(--surface);
                                  border-color: var(--passed); }}
/* Devices grid: two equal columns for A and B. Each column stacks
   header → real preview → virtual reprojection canvas. 2x2 grid in
   total, mirrors the viewer's camera/VIRT layout. */
.devices-grid {{ display: grid; grid-template-columns: 1fr 1fr;
                  gap: var(--s-3); width: 100%; align-items: start; }}
.device {{ display: flex; flex-direction: column; gap: var(--s-2); }}
.camera-compare {{ display: flex; flex-direction: column; gap: 8px; }}
.camera-compare-grid {{ display: grid; grid-template-columns: 1fr; gap: 8px; }}
.compare-title {{ margin: 0; font-family: var(--mono); font-size: 11px;
                  letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink); }}
.preview-panel.off img {{ opacity: 0; }}
.preview-panel.off .preview-overlay {{ opacity: 0; }}
.preview-panel.off .placeholder {{ color: rgba(255, 255, 255, 0.6); }}
/* Crosshair at geometric centre of the real preview — reference mark
   for the operator to visually align against the virt canvas's
   principal-point cross below. Hidden when preview is off. */
.preview-panel::before,
.preview-panel::after {{ content: ''; position: absolute;
                          background: rgba(255, 255, 255, 0.55);
                          pointer-events: none; }}
.preview-panel::before {{ left: 50%; top: calc(50% - 8px);
                           width: 1px; height: 16px; transform: translateX(-0.5px); }}
.preview-panel::after {{ top: 50%; left: calc(50% - 8px);
                          width: 16px; height: 1px; transform: translateY(-0.5px); }}
.preview-panel.off::before, .preview-panel.off::after {{ display: none; }}
/* Virtual camera: 2D canvas showing the plate pentagon + principal-point
   cross reprojected through this camera's own K·[R|t]·P. Same idea as
   the viewer's bottom-row virt canvas — if the reprojected outline
   doesn't align with the plate in the real preview above, calibration
   is off. */
{LIVE_COMPARE_CSS}

/* Calibration card (Phase 5). Per-camera auto-calibrate row + an
   extended-markers block. Visually aligned with .device rows so the
   card reads as "calibration plane" beneath Devices/Session. */
.calib-row {{ display: grid; grid-template-columns: 28px minmax(0, 1fr) min-content;
               align-items: center; gap: var(--s-2);
               padding: var(--s-2) 0; }}
.calib-row + .calib-row {{ border-top: 1px solid var(--border-l); }}
.calib-row .id {{ font-family: var(--mono); font-size: 11px; font-weight: 700;
                    color: var(--ink); letter-spacing: 0.08em; }}
.calib-row .meta {{ font-family: var(--sans); font-size: 11px; color: var(--sub); }}
.calib-sub {{ margin-top: var(--s-3); padding-top: var(--s-3);
               border-top: 1px solid var(--border-l); }}
.calib-sub h3 {{ font-family: var(--mono); font-size: 10px; font-weight: 500;
                  letter-spacing: 0.14em; text-transform: uppercase;
                  color: var(--sub); margin: 0 0 var(--s-2) 0; }}
.calib-register-row {{ display: flex; gap: var(--s-2); align-items: center;
                         flex-wrap: wrap; margin-bottom: var(--s-2); }}
.calib-register-row select {{ font-family: var(--mono); font-size: 11px;
                                padding: 6px 8px; border-radius: var(--r);
                                border: 1px solid var(--border-base);
                                background: var(--surface); color: var(--ink); }}
.marker-list {{ display: flex; flex-direction: column; gap: 0;
                 border: 1px solid var(--border-l); border-radius: var(--r);
                 background: var(--surface-hover); }}
.marker-list:empty {{ display: none; }}
.marker-row {{ display: flex; align-items: center; justify-content: space-between;
                padding: 6px var(--s-2); border-top: 1px solid var(--border-l);
                font-family: var(--mono); font-size: 11px; color: var(--ink); }}
.marker-row:first-child {{ border-top: 0; }}
.marker-row .mid {{ font-weight: 700; min-width: 28px; }}
.marker-row .mxy {{ color: var(--sub); flex: 1; padding-left: var(--s-2); }}
.marker-row button {{ background: transparent; border: 0; color: var(--sub);
                       font-family: var(--mono); font-size: 14px; line-height: 1;
                       cursor: pointer; padding: 2px 6px; border-radius: var(--r); }}
.marker-row button:hover {{ color: var(--dev); background: var(--surface); }}
.marker-list-empty {{ color: var(--sub); font-style: italic; font-size: 11px;
                        padding: var(--s-2) 0; font-family: var(--mono); }}
.calib-last {{ font-family: var(--mono); font-size: 10px; color: var(--sub);
                 letter-spacing: 0.06em; }}

/* Runtime tunables card — two slider + number-input rows. Server owns
   the persisted value; sliders POST on `change` (keystroke commits on
   blur). Matches the segmented / button family visually. */
.tuning-row {{ display: flex; align-items: center; gap: var(--s-2);
                margin-top: var(--s-3); flex-wrap: nowrap; }}
.tuning-row:first-child {{ margin-top: var(--s-2); }}
.tuning-label {{ font-family: var(--mono); font-size: 10px;
                  letter-spacing: 0.12em; text-transform: uppercase;
                  color: var(--sub); min-width: 96px; }}
.tuning-row input[type="range"] {{ flex: 1; accent-color: var(--ink);
                                     min-width: 0; }}
.tuning-row input[type="number"] {{ width: 64px; font-family: var(--mono);
                                     font-size: 11px; padding: 4px 6px;
                                     border: 1px solid var(--border-base);
                                     border-radius: var(--r);
                                     background: var(--surface); color: var(--ink); }}
.tuning-row input[type="number"]:focus {{ outline: none; border-color: var(--ink); }}
.tuning-unit {{ font-family: var(--mono); font-size: 10px; color: var(--sub);
                 letter-spacing: 0.08em; min-width: 14px; }}

/* Time Sync diagnostic log panel — fixed-height scrollable <pre> with a
   Copy button that writes the visible text to the clipboard. Lines are
   server/A/B event traces; the operator copies and pastes back into the
   chat when a run misbehaves. */
.sync-log-head {{ display: flex; align-items: center; gap: var(--s-2);
                   margin-top: var(--s-3); }}
.sync-log-label {{ font-family: var(--mono); font-size: 10px;
                    letter-spacing: 0.12em; text-transform: uppercase;
                    color: var(--sub); flex: 1; }}
.sync-log {{ margin: var(--s-2) 0 0 0; padding: var(--s-2);
              background: var(--surface-hover); border: 1px solid var(--border-l);
              border-radius: var(--r); font-family: var(--mono);
              font-size: 10px; line-height: 1.4; color: var(--ink);
              max-height: 240px; overflow-y: auto;
              white-space: pre; word-break: normal; }}

/* --- Events list — dense, hover-highlighted rows inspired by kg admin
   tables. Row-level hover wash replaces the former negative-margin hack. */
.events-empty {{ color: var(--sub); font-size: 12px; padding: var(--s-3) 0;
                 font-style: italic; font-family: var(--mono); }}
.event-item {{ display: flex; align-items: flex-start;
               border-top: 1px solid var(--border-l);
               transition: background 0.12s ease; }}
.event-item:first-child {{ border-top: 0; }}
.event-item:hover {{ background: var(--surface-hover); }}
.event-row {{ flex: 1; min-width: 0; display: block; text-decoration: none;
              color: inherit; padding: var(--s-2) var(--s-2); }}
/* Trajectory overlay toggle — only visible on sessions with 3D pts. The
   coloured dot mirrors the trace tint Plotly uses in the canvas so the
   operator can match checkbox → line without guessing. Clicking the
   checkbox does NOT navigate (it's outside the <a class="event-row">). */
.traj-toggle {{ flex: 0 0 auto; padding: var(--s-2) 0 0 var(--s-2);
                display: flex; align-items: center; gap: 4px;
                cursor: pointer; user-select: none; }}
.traj-toggle input[type=checkbox] {{ accent-color: var(--ink);
                                      width: 13px; height: 13px; margin: 0;
                                      cursor: pointer; }}
.traj-toggle .swatch {{ width: 10px; height: 10px; border-radius: 50%;
                         border: 1px solid rgba(0,0,0,0.12);
                         display: inline-block; }}
.traj-toggle-placeholder {{ flex: 0 0 auto;
                             width: calc(13px + 10px + var(--s-2) + 8px); }}
.event-top {{ display: flex; align-items: center; gap: var(--s-2); margin-bottom: var(--s-1);
              flex-wrap: wrap; }}
.event-top .sid {{ font-family: var(--mono); font-size: 12px; font-weight: 500;
                   color: var(--ink); letter-spacing: 0.04em; margin-right: var(--s-1); }}
/* Capture-mode annotation next to sid — metadata, not a state badge, so
   it's rendered as subdued inline text instead of a chip. Avoids the
   prior double-"DUAL" collision with the cam-identity chip. */
.event-top .capmode {{ font-family: var(--mono); font-size: 10px;
                        letter-spacing: 0.10em; text-transform: uppercase;
                        color: var(--sub); }}
.event-top .capmode::before {{ content: "· "; opacity: 0.5; }}
.event-top .event-paths {{ display:flex; gap:4px; flex-wrap:wrap; margin-left:auto; }}
.event-top .event-paths .path-chip {{ font-size:9px; padding:1px 5px; }}
.event-stats {{ display: grid; grid-template-columns: repeat(3, 1fr);
                gap: var(--s-1) var(--s-3);
                font-family: var(--mono); font-size: 11px; color: var(--ink-light); }}
.event-stats .k {{ color: var(--sub); letter-spacing: 0.10em; text-transform: uppercase;
                   font-size: 9px; display: block; margin-bottom: 1px; }}
.event-stats .v {{ font-variant-numeric: tabular-nums; color: var(--ink); }}
.event-delete-form {{ flex: 0 0 auto; margin: var(--s-2) var(--s-1) 0 0; }}
.event-delete {{ background: transparent; border: 1px solid var(--border-base);
                 color: var(--sub); font-family: var(--mono); font-size: 13px;
                 line-height: 1; padding: 2px 8px 3px; border-radius: var(--r);
                 cursor: pointer; transition: border-color 0.15s, color 0.15s, background 0.15s; }}
.event-delete:hover {{ border-color: var(--dev); color: var(--dev);
                       background: var(--surface); }}
.events-toolbar {{ display:flex; align-items:center; justify-content:space-between;
                   gap:var(--s-2); margin-bottom:var(--s-2); }}
.events-filters {{ display:flex; gap:6px; }}
.events-filter {{ background:transparent; border:1px solid var(--border-base);
                  color:var(--sub); font-family:var(--mono); font-size:10px;
                  letter-spacing:0.10em; text-transform:uppercase;
                  padding:4px 8px; border-radius:var(--r); cursor:pointer; }}
.events-filter.active {{ background:var(--ink); color:var(--surface); border-color:var(--ink); }}
.event-actions {{ display:flex; flex-direction:column; gap:6px; margin:var(--s-2) var(--s-1) 0 0; }}
.event-action-form {{ margin:0; }}
.event-action {{ background:transparent; border:1px solid var(--border-base);
                 color:var(--sub); font-family:var(--mono); font-size:10px;
                 letter-spacing:0.08em; text-transform:uppercase;
                 line-height:1; padding:5px 8px; border-radius:var(--r);
                 cursor:pointer; transition:border-color 0.15s,color 0.15s,background 0.15s; }}
.event-action.warn:hover {{ border-color:var(--warn); color:var(--warn); background:var(--surface); }}
.event-action.dev:hover {{ border-color:var(--dev); color:var(--dev); background:var(--surface); }}
.event-action.ok:hover {{ border-color:var(--passed); color:var(--passed); background:var(--surface); }}
.chip.processing {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
.chip.queued {{ color: var(--sub); border-color: var(--border-base); background: transparent; }}
.chip.canceled {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
.chip.completed {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}

/* --- Canvas overlay hint --- moved to bottom-left to free the top row for
   the mode toggle + Plotly's modebar. */
.canvas-hint {{ position: absolute; left: var(--s-4); bottom: var(--s-4); z-index: 5;
                font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                text-transform: uppercase; color: var(--sub);
                background: var(--surface); border: 1px solid var(--border-l);
                border-radius: var(--r); padding: var(--s-1) var(--s-2); pointer-events: none; }}

/* --- Canvas mode toggle — top-left so it can't collide with Plotly's
   modebar (camera/home/reset axes buttons), which always sits top-right
   and can't be moved without reconstructing Plotly's config. */
.degraded-banner {{ position: absolute; top: var(--s-3); left: 50%; transform: translateX(-50%);
                    z-index: 8; display: flex; align-items: center; gap: var(--s-2);
                    padding: var(--s-2) var(--s-4); background: var(--failed-bg);
                    border: 1px solid var(--failed); border-radius: var(--r);
                    font-family: var(--mono); font-size: 11px; color: var(--failed);
                    letter-spacing: 0.04em; max-width: 80%; }}
.degraded-banner .degraded-icon {{ font-size: 14px; }}
.telemetry-panel {{ position: absolute; left: var(--s-4);
                    top: calc(var(--s-4) + 42px); z-index: 7;
                    background: var(--surface); border: 1px solid var(--border-base);
                    border-radius: var(--r); max-width: 320px; font-family: var(--mono);
                    font-size: 11px; color: var(--ink); }}
.telemetry-panel summary {{ cursor: pointer; padding: var(--s-2) var(--s-3);
                             letter-spacing: 0.12em; color: var(--sub);
                             user-select: none; list-style: none; }}
.telemetry-panel summary::-webkit-details-marker {{ display: none; }}
.telemetry-panel summary::after {{ content: ' ▸'; color: var(--sub); }}
.telemetry-panel[open] summary::after {{ content: ' ▾'; color: var(--ink); }}
.telemetry-panel[open] summary {{ color: var(--ink); border-bottom: 1px solid var(--border-l); }}
.telemetry-body {{ padding: var(--s-2) var(--s-3); display: flex; flex-direction: column;
                   gap: var(--s-2); max-height: min(340px, calc(100vh - var(--nav-h) - 120px));
                   overflow-y: auto; }}
.tel-row {{ display: grid; grid-template-columns: 60px 80px 1fr; align-items: center;
            gap: var(--s-2); }}
.tel-row .k {{ font-size: 10px; color: var(--sub); letter-spacing: 0.08em; }}
.tel-row .v {{ font-size: 10px; color: var(--ink); }}
.tel-row .tel-spark {{ width: 80px; height: 16px; display: block; }}
.tel-block {{ display: flex; flex-direction: column; gap: 4px; }}
.tel-block .k {{ font-size: 10px; color: var(--sub); letter-spacing: 0.08em; }}
.tel-matrix {{ display: flex; gap: 4px; flex-wrap: wrap; }}
.tel-cell {{ font-size: 9px; padding: 2px 4px; border: 1px solid var(--border-l);
             border-radius: var(--r); font-family: var(--mono); color: var(--ink); }}
.tel-errors {{ display: flex; flex-direction: column; gap: 2px; }}
.tel-err {{ font-size: 10px; color: var(--failed); display: flex; gap: var(--s-2); }}
.tel-err .t {{ color: var(--sub); }}
.tel-none {{ font-size: 10px; color: var(--sub); font-style: italic; }}
.canvas-mode-toggle {{ position: absolute; left: var(--s-4); top: var(--s-4); z-index: 6;
                       display: inline-flex; gap: 0; font-family: var(--mono); font-size: 10px;
                       letter-spacing: 0.12em; text-transform: uppercase;
                       border: 1px solid var(--border-base); border-radius: var(--r);
                       overflow: hidden; background: var(--surface); }}
.canvas-mode-toggle button {{ background: transparent; color: var(--sub); border: 0;
                              padding: var(--s-1) var(--s-3); cursor: pointer;
                              font: inherit; letter-spacing: inherit; text-transform: inherit;
                              transition: color 0.15s, background 0.15s; }}
.canvas-mode-toggle button + button {{ border-left: 1px solid var(--border-base); }}
.canvas-mode-toggle button:hover {{ color: var(--ink); }}
.canvas-mode-toggle button.active {{ background: var(--ink); color: var(--surface); }}

/* --- Replay playback bar (bottom-center, hidden when mode=inspect) --- */
.playback-bar {{ position: absolute; left: 50%; bottom: var(--s-4); transform: translateX(-50%);
                 z-index: 6; display: none; align-items: center; gap: var(--s-3);
                 background: var(--surface); border: 1px solid var(--border-base);
                 border-radius: var(--r); padding: var(--s-2) var(--s-3);
                 font-family: var(--mono); font-size: 11px; color: var(--ink);
                 min-width: 480px; max-width: 70%; }}
.playback-bar.show {{ display: inline-flex; }}
.playback-bar .playpause {{ background: var(--ink); color: var(--surface); border: 0;
                            width: 28px; height: 22px; border-radius: var(--r);
                            cursor: pointer; font: inherit; font-size: 10px; }}
.playback-bar .playpause:hover {{ opacity: 0.85; }}
.playback-bar input[type="range"] {{ flex: 1; accent-color: var(--ink); }}
.playback-bar .time {{ color: var(--sub); font-size: 10px; letter-spacing: 0.08em;
                       min-width: 88px; text-align: right; }}
.playback-bar .speed {{ display: inline-flex; border: 1px solid var(--border-base);
                        border-radius: var(--r); overflow: hidden; }}
.playback-bar .speed button {{ background: transparent; border: 0; padding: 2px 8px;
                               font: inherit; font-size: 10px; color: var(--sub); cursor: pointer; }}
.playback-bar .speed button + button {{ border-left: 1px solid var(--border-base); }}
.playback-bar .speed button.active {{ background: var(--ink); color: var(--surface); }}
.playback-bar .empty {{ color: var(--sub); font-size: 10px; letter-spacing: 0.10em;
                        text-transform: uppercase; }}
@media (max-width: 1100px) {{
  .nav {{ padding-left: 16px; padding-right: 16px; }}
  .nav-main {{ grid-template-columns: 1fr; }}
  .nav-tabs {{ justify-content: flex-start; }}
  .nav-status-row {{ justify-content: flex-start; }}
  .nav .status-line {{ align-items: flex-start; min-width: 0; }}
  .nav .status-checks {{ justify-content: flex-start; }}
}}
"""


def _render_device_rows(
    devices: list[dict[str, Any]],
    calibrations: list[str],
    calibration_last_ts: dict[str, float] | None = None,
    preview_requested: dict[str, bool] | None = None,
    compare_mode: str = "toggle",
) -> str:
    """Merged Devices card row — status + per-cam calibration actions +
    per-cam preview toggle + inline MJPEG panel. JS will replace within
    1 s; SSR paints usable buttons so there's no flash of empty state."""
    device_by_id = {d["camera_id"]: d for d in devices}
    calibrated = set(calibrations)
    calibration_last_ts = calibration_last_ts or {}
    preview_requested = preview_requested or {}

    def render_row(cam_id: str) -> str:
        dev = device_by_id.get(cam_id)
        online = dev is not None
        time_synced = bool(dev.get("time_synced")) if dev else False
        is_cal = cam_id in calibrated
        always_on = compare_mode == "always_on"
        preview_on = always_on or bool(preview_requested.get(cam_id))
        last_ts = calibration_last_ts.get(cam_id) if is_cal else None
        if not online:
            chip_cls, chip_label = "idle", "offline"
        elif is_cal:
            chip_cls, chip_label = "calibrated", "calibrated"
        else:
            chip_cls, chip_label = "online", "online"
        cal_dot = "ok" if is_cal else ("warn" if online else "bad")
        sync_dot = "ok" if time_synced else ("warn" if online else "bad")
        sync_label = "synced" if time_synced else ("not synced" if online else "offline")
        cal_label = (
            f"last {html.escape(_fmt_hhmm(last_ts))}" if (is_cal and last_ts)
            else ("pending" if online else "offline")
        )
        disabled_attr = "" if online else " disabled"
        auto_cal_btn = (
            f'<button type="button" class="btn small" '
            f'data-auto-cal="{html.escape(cam_id)}"{disabled_attr}>Run auto-cal</button>'
        )
        preview_btn = (
            f'<button type="button" class="btn small preview-btn{" active" if preview_on else ""}" '
            f'data-preview-cam="{html.escape(cam_id)}" '
            f'data-preview-enabled="{1 if preview_on else 0}"{disabled_attr}>'
            f'{"PREVIEW ON" if preview_on else "PREVIEW"}</button>'
        ) if not always_on else ""
        compare_block = render_live_compare_camera(
            cam_id,
            preview_src=f"/camera/{html.escape(cam_id)}/preview?t=0",
            preview_placeholder=("" if always_on else ("…" if preview_on else "Preview off")),
            virt_placeholder=("loading…" if is_cal else "not calibrated"),
            preview_off=(not always_on and not preview_on),
        )
        sync_led_cls = "offline" if not online else ("synced" if time_synced else "waiting")
        return (
            f'<div class="device">'
            f'<div class="device-head">'
            f'<span class="sync-led {sync_led_cls}" title="time sync · {sync_label}"></span>'
            f'<div class="id">{html.escape(cam_id)}</div>'
            f'<div class="sub">'
            f'<span class="item {sync_dot}"><span class="dot {sync_dot}"></span>time sync · {sync_label}</span>'
            f'<span class="item {cal_dot}"><span class="dot {cal_dot}"></span>pose · {cal_label}</span>'
            f'<span class="item {"warn" if online else "bad"}"><span class="dot {"warn" if online else "bad"}"></span>auto-cal · {"idle" if online else "offline"}</span>'
            f'</div>'
            f'<div class="chip-col"><span class="chip {chip_cls}">{chip_label}</span></div>'
            f'</div>'
            f'<div class="device-actions">{preview_btn}{auto_cal_btn}</div>'
            f'{compare_block}'
            f'</div>'
        )

    rows = [render_row(cam) for cam in ("A", "B")]
    rows.extend(render_row(d["camera_id"]) for d in devices if d["camera_id"] not in ("A", "B"))
    return f'<div class="devices-grid">{"".join(rows)}</div>'


def _render_extended_markers_body(
    device_ids: list[str],
    extended_markers: list[dict[str, Any]] | None = None,
) -> str:
    """Extended-markers subsection — register new markers by auto-projecting
    them through the plate homography from a selected camera's preview
    frame. Sits at the bottom of the merged Devices card."""
    extended_markers = extended_markers or []
    cam_options = "".join(
        f'<option value="{html.escape(cam)}">{html.escape(cam)}</option>'
        for cam in device_ids
    )
    if extended_markers:
        list_items = "".join(
            f'<div class="marker-row">'
            f'<span class="mid">#{int(row["id"])}</span>'
            f'<span class="mxy">({float(row["wx"]):+0.3f}, {float(row["wy"]):+0.3f}) m</span>'
            f'<button type="button" data-marker-remove="{int(row["id"])}" '
            f'title="Remove marker {int(row["id"])}">&times;</button>'
            f'</div>'
            for row in extended_markers
        )
        list_html = f'<div class="marker-list">{list_items}</div>'
    else:
        list_html = '<div class="marker-list-empty">No extended markers registered.</div>'
    return (
        '<div class="calib-sub">'
        '<h3>Extended markers</h3>'
        '<div class="calib-register-row">'
        f'<select id="marker-register-cam">{cam_options}</select>'
        '<button type="button" class="btn small" id="marker-register-btn">Register from this camera</button>'
        '<button type="button" class="btn small secondary" id="marker-clear-btn">Clear all</button>'
        '</div>'
        f'<div id="marker-list">{list_html}</div>'
        '</div>'
    )


def _fmt_hhmm(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


_MODE_LABELS = {
    "camera_only": "Camera-only",
    "on_device": "On-device",
    "dual": "Dual",
}

_PATH_LABELS = {
    "live": ("Live stream", "iOS → WS"),
    "ios_post": ("iOS post-pass", "on-device analyzer"),
    "server_post": ("Server post-pass", "PyAV + OpenCV"),
}


def _render_detection_paths_body(
    default_paths: list[str] | None,
    session: dict[str, Any] | None = None,
) -> str:
    armed = bool(session and session.get("armed"))
    active = set(default_paths or ["server_post"])
    if armed:
        active = set(session.get("paths") or active)
        chips = "".join(
            f'<span class="path-chip on">{html.escape(_PATH_LABELS.get(path, (path, ""))[0])}</span>'
            for path in ("live", "ios_post", "server_post")
            if path in active
        ) or '<span class="path-chip">none</span>'
        return (
            '<div class="path-lock">'
            '<span class="mode-label">Paths</span>'
            f'<div class="path-chip-row">{chips}</div>'
            "</div>"
        )

    rows: list[str] = []
    for path in ("live", "ios_post", "server_post"):
        title, subtitle = _PATH_LABELS.get(path, (path, ""))
        checked = " checked" if path in active else ""
        rows.append(
            '<label class="path-option">'
            f'<input type="checkbox" name="paths" value="{path}"{checked}>'
            '<span class="copy">'
            f'<span class="title">{html.escape(title)}</span>'
            f'<span class="sub">{html.escape(subtitle)}</span>'
            "</span>"
            "</label>"
        )
    return (
        '<form method="POST" action="/detection/paths" id="paths-form">'
        '<div class="paths-stack">'
        f'{"".join(rows)}'
        '</div>'
        '<div class="paths-actions">'
        '<button class="btn" type="submit">Apply</button>'
        '</div>'
        '</form>'
    )


def _render_active_session_body(live_session: dict[str, Any] | None) -> str:
    if not live_session:
        return '<div class="active-empty">No active live stream.</div>'
    sid = html.escape(live_session.get("session_id", "—"))
    frame_counts = live_session.get("frame_counts") or {}
    paths = live_session.get("paths") or []
    path_chips = "".join(
        f'<span class="path-chip on">{html.escape(_PATH_LABELS.get(path, (path, ""))[0])}</span>'
        for path in paths
    ) or '<span class="path-chip">none</span>'
    point_count = int(live_session.get("point_count") or 0)
    a_frames = int(frame_counts.get("A") or 0)
    b_frames = int(frame_counts.get("B") or 0)
    return (
        '<div class="active-head">'
        '<span class="chip armed">live</span>'
        f'<span class="session-id">{sid}</span>'
        '</div>'
        f'<div class="path-chip-row">{path_chips}</div>'
        '<div class="active-grid">'
        f'<span><span class="k">A frames</span><span class="v">{a_frames}</span></span>'
        f'<span><span class="k">B frames</span><span class="v">{b_frames}</span></span>'
        f'<span><span class="k">Live 3D pts</span><span class="v">{point_count}</span></span>'
        '</div>'
    )


def _render_session_body(
    session: dict[str, Any] | None,
    capture_mode: str = "camera_only",
    default_paths: list[str] | None = None,
    devices: list[dict[str, Any]] | None = None,
    calibrations: list[str] | None = None,
) -> str:
    armed = session is not None and session.get("armed")
    devices = devices or []
    calibrated = set(calibrations or [])
    online = {d["camera_id"] for d in devices}
    synced = {d["camera_id"] for d in devices if d.get("time_synced")}
    # Demo-safety preconditions: both expected cams (A, B) must be online,
    # calibrated, AND time-synced before Arm is allowed. Server accepts the
    # POST regardless — this is UI-level safety so the operator doesn't
    # silently arm a session that will flag error="no time sync" or similar.
    missing: list[str] = []
    for cam in ("A", "B"):
        if cam not in online:
            missing.append(f"{cam} offline")
        elif cam not in calibrated:
            missing.append(f"{cam} not calibrated")
        elif cam not in synced:
            missing.append(f"{cam} not time-synced")
    arm_ok = not missing
    chip_html = (
        '<span class="chip armed">armed</span>'
        if armed
        else '<span class="chip idle">idle</span>'
    )
    sid_html = (
        f'<span class="session-id">{html.escape(session["id"])}</span>'
        if session and session.get("id")
        else ""
    )
    arm_disabled = armed or not arm_ok
    arm_title = "; ".join(missing) if missing else "Ready to record"
    arm_btn = (
        '<form class="inline" method="POST" action="/sessions/arm">'
        f'<button class="btn" type="submit"{" disabled" if arm_disabled else ""} '
        f'title="{html.escape(arm_title)}">Arm session</button>'
        "</form>"
    )
    stop_btn = (
        '<form class="inline" method="POST" action="/sessions/stop">'
        f'<button class="btn danger" type="submit"{"" if armed else " disabled"}>Stop</button>'
        "</form>"
    )
    # CALIBRATE TIME broadcasts a single-listener chirp-listen command to
    # every online camera on its next WS heartbeat tick. Disabled while armed —
    # firing a time-sync mid-recording would disrupt the armed clip.
    # Dashboard-level Time Sync exposes only the Quick chirp fallback.
    # Mutual sync lives on `/sync` — it's the primary path and needs its
    # own surface for trace plots, WAV replay, and tuning. Keeping it off
    # the dashboard stops operators from firing it without the
    # visualisations that make it diagnosable.
    sync_trigger_btn = (
        '<form class="inline" method="POST" action="/sync/trigger">'
        f'<button class="btn secondary" type="submit"{" disabled" if armed else ""}>Quick chirp</button>'
        "</form>"
    )

    # Per-cam sync LEDs rendered server-side for initial paint. JS
    # `renderSession` repaints them on each /status tick from the same
    # `devices[*].time_synced` state, so the three visual states
    # (off / waiting / synced) stay in sync with server truth within
    # one heartbeat interval.
    def _sync_led_html(cam: str) -> str:
        dev = next((d for d in devices if d.get("camera_id") == cam), None)
        if dev is None:
            cls, tip = "off", f"{cam}: offline"
        elif dev.get("time_synced"):
            age = dev.get("time_sync_age_s")
            age_txt = f" · {age:.0f}s ago" if isinstance(age, (int, float)) else ""
            cls, tip = "synced", f"{cam}: synced{age_txt}"
        else:
            cls, tip = "waiting", f"{cam}: waiting"
        return (
            f'<span class="sync-led {cls}" title="{html.escape(tip)}">{cam}</span>'
        )
    sync_leds = _sync_led_html("A") + _sync_led_html("B")

    clear_btn = ""
    if not armed and session and session.get("id"):
        clear_btn = (
            '<form class="inline" method="POST" action="/sessions/clear">'
            '<button class="btn" type="submit">Clear</button>'
            "</form>"
        )

    gate_row = ""
    if not armed and missing:
        gate_row = (
            '<div class="arm-gate">'
            f'<span class="gate-label">Need:</span> {html.escape(", ".join(missing))}'
            "</div>"
        )
    return (
        f'<div class="session-head">{chip_html}{sid_html}</div>'
        f'<div class="session-actions">{arm_btn}{stop_btn}{clear_btn}</div>'
        f'{gate_row}'
        '<div class="card-subtitle">Time Sync</div>'
        f'<div class="session-actions">{sync_trigger_btn}{sync_leds}</div>'
        f'{_render_detection_paths_body(default_paths, session)}'
    )


def _render_events_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<div class="events-empty">No sessions received yet.</div>'
    parts: list[str] = []
    for e in events:
        sid = html.escape(e["session_id"])
        cams = " · ".join(html.escape(c) for c in e.get("cameras", [])) or "—"
        status = html.escape(e.get("status", ""))
        stat_label = status.replace("_", " ")
        mode_val = e.get("mode")
        capture_mode = (
            "on-device" if mode_val == "on_device"
            else "dual" if mode_val == "dual"
            else "camera-only"
        )
        path_status = e.get("path_status") or {}
        path_html = ''.join(
            f'<span class="path-chip{" on" if path_status.get(path) == "done" else ""}">{label}</span>'
            for path, label in (("live", "L"), ("ios_post", "I"), ("server_post", "S"))
        )
        mean = "—" if e.get("mean_residual_m") is None else format(e["mean_residual_m"], ".4f")
        peak_z = "—" if e.get("peak_z_m") is None else format(e["peak_z_m"], ".2f")
        duration = "—" if e.get("duration_s") is None else format(e["duration_s"], ".2f")
        # Collapse the stats row when the session produced no usable
        # metrics — saves two rows of "—" clutter for error / single-cam
        # sessions. Mirror of the JS-tick guard in renderEvents().
        has_metrics = (
            (e.get("n_triangulated") or 0) > 0
            or mean != "—" or peak_z != "—" or duration != "—"
        )
        stats_html = (
            f'<div class="event-stats">'
            f'<span><span class="k">Cams</span><span class="v">{cams}</span></span>'
            f'<span><span class="k">3D pts</span><span class="v">{e.get("n_triangulated", 0)}</span></span>'
            f'<span><span class="k">Mean resid (m)</span><span class="v">{mean}</span></span>'
            f'<span><span class="k">Peak Z (m)</span><span class="v">{peak_z}</span></span>'
            f'<span><span class="k">Duration (s)</span><span class="v">{duration}</span></span>'
            f"</div>"
        ) if has_metrics else ""
        # SSR renders the checkbox placeholder unchecked (localStorage is
        # browser-side); the first tickEvents round-trip rehydrates the
        # persisted selection within ~one paint. Column width matches the
        # JS-rendered variant so there's no layout shift on rehydrate.
        has_traj = (e.get("n_triangulated") or 0) > 0
        if has_traj:
            toggle_html = (
                '<label class="traj-toggle" title="Overlay trajectory on canvas">'
                f'<input type="checkbox" data-traj-sid="{sid}">'
                '<span class="swatch"></span>'
                "</label>"
            )
        else:
            toggle_html = '<span class="traj-toggle-placeholder" aria-hidden="true"></span>'
        processing_state = e.get("processing_state")
        processing_chip = (
            f'<span class="chip {html.escape(processing_state)}">{html.escape(processing_state)}</span>'
            if processing_state else ""
        )
        if e.get("trashed"):
            lifecycle_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/restore">'
                f'<button class="event-action ok" type="submit">Restore</button>'
                f"</form>"
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/delete" '
                f'onsubmit="return confirm(\'刪除 session {sid}？此動作無法復原。\');">'
                f'<button class="event-action dev" type="submit">Delete</button>'
                f"</form>"
            )
        else:
            lifecycle_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/trash" '
                f'onsubmit="return confirm(\'移動 session {sid} 到垃圾桶？\');">'
                f'<button class="event-action dev" type="submit">Trash</button>'
                f"</form>"
            )
        processing_html = ""
        if processing_state in {"queued", "processing"}:
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/cancel_processing">'
                f'<button class="event-action warn" type="submit">Cancel Proc</button>'
                f"</form>"
            )
        elif processing_state == "canceled" and e.get("processing_resumable"):
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/resume_processing">'
                f'<button class="event-action ok" type="submit">Resume</button>'
                f"</form>"
            )
        parts.append(
            # event-row is a link into the viewer; the delete form + traj
            # toggle are siblings (not descendants) so their clicks don't
            # navigate via the wrapping anchor.
            f'<div class="event-item">'
            f"{toggle_html}"
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-top">'
            f'<span class="sid">{sid}</span>'
            f'<span class="capmode">{capture_mode}</span>'
            f'<span class="event-paths">{path_html}</span>'
            f"{processing_chip}"
            f'<span class="chip {status}">{stat_label}</span>'
            f"</div>"
            f"{stats_html}"
            f"</a>"
            f'<div class="event-actions">{processing_html}{lifecycle_html}</div>'
            f"</div>"
        )
    return "".join(parts)


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
    "dashboard": (
        "Operator Surface",
        "Dashboard",
    ),
    "setup": (
        "Calibration",
        "Setup",
    ),
    "sync": (
        "Time Sync",
        "Sync",
    ),
    "markers": (
        "Registry",
        "Markers",
    ),
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


# Shared-shell façade: keep the historical render_dashboard imports stable
# while routing callers to the extracted shared module.
_CSS = _SHARED_CSS
_render_nav_status = _shared_render_nav_status
_render_primary_nav = _shared_render_primary_nav
_render_app_nav = _shared_render_app_nav
_render_chirp_threshold_body = _shared_render_chirp_threshold_body
_render_tuning_body = _shared_render_tuning_body


def _render_chirp_threshold_body(
    chirp_detect_threshold: float,
    mutual_sync_threshold: float = 0.10,
) -> str:
    """Two independent threshold rows — quick-chirp (third-device up+down
    sweep; strong signal) vs mutual-sync (two-phone cross-detection; the
    far phone's chirp can land much quieter). Shared slider in the
    original design forced operators to tune for the weaker modality,
    losing false-positive margin on the stronger one."""
    q = f"{chirp_detect_threshold:.2f}"
    m = f"{mutual_sync_threshold:.2f}"
    return (
        '<form class="tuning-row" method="POST" '
        'action="/settings/chirp_threshold" id="tuning-chirp-form">'
        '<span class="tuning-label">Quick chirp thr</span>'
        f'<input type="range" name="threshold" min="0.02" max="0.60" step="0.01" '
        f'value="{q}" '
        'oninput="document.getElementById(\'tuning-chirp-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-chirp-num" name="threshold" '
        f'min="0.02" max="0.60" step="0.01" value="{q}" '
        'form="tuning-chirp-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '</form>'
        '<form class="tuning-row" method="POST" '
        'action="/settings/mutual_sync_threshold" id="tuning-mutual-form">'
        '<span class="tuning-label">Mutual sync thr</span>'
        f'<input type="range" name="threshold" min="0.02" max="0.60" step="0.01" '
        f'value="{m}" '
        'oninput="document.getElementById(\'tuning-mutual-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-mutual-num" name="threshold" '
        f'min="0.02" max="0.60" step="0.01" value="{m}" '
        'form="tuning-mutual-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '</form>'
    )


def _render_tuning_body(
    heartbeat_interval_s: float,
    tracking_exposure_cap: str = "frame_duration",
    capture_height_px: int = 1080,
) -> str:
    """Linked slider + segmented-control rows. Each form posts on
    submit — the `<input>`s share a `form` attribute and an `oninput`
    handler that mirrors slider <-> number, so the operator sees the
    number update as they drag. Submit fires on the change event after
    release (slider) or blur / Enter (number)."""
    ivl = f"{heartbeat_interval_s:g}"
    return (
        # Heartbeat interval row.
        '<form class="tuning-row" method="POST" '
        'action="/settings/heartbeat_interval" id="tuning-hb-form">'
        '<span class="tuning-label">Heartbeat</span>'
        f'<input type="range" name="interval_s" min="1" max="10" step="0.5" '
        f'value="{ivl}" '
        'oninput="document.getElementById(\'tuning-hb-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-hb-num" name="interval_s" '
        f'min="1" max="10" step="0.5" value="{ivl}" '
        'form="tuning-hb-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '<span class="tuning-unit">s</span>'
        '</form>'
        # Tracking exposure-cap row. Server-owned policy; iOS hot-applies
        # it on WS settings messages and armed sessions snapshot it at arm time.
        + ''.join(
            '<div class="tuning-row">'
            '<span class="tuning-label">Tracking exp</span>'
            '<div class="res-segmented" role="radiogroup" aria-label="Tracking exposure cap">'
            + ''.join(
                f'<form class="inline" method="POST" action="/settings/tracking_exposure_cap">'
                f'<input type="hidden" name="mode" value="{mode}">'
                f'<button class="btn{"" if mode == tracking_exposure_cap else " secondary"} small" '
                f'type="submit">{label}</button>'
                f'</form>'
                for mode, label in (
                    ("frame_duration", "1/240"),
                    ("shutter_500", "1/500"),
                    ("shutter_1000", "1/1000"),
                )
            )
            + '</div>'
            '</div>'
            for _ in (0,)
        )
        # Capture-resolution row. Three-button segmented picker — only
        # applied by iOS when state == .standby so an armed clip isn't
        # disrupted mid-recording. FPS stays locked at 240 (the whole
        # point of the rig).
        + ''.join(
            '<div class="tuning-row">'
            '<span class="tuning-label">Capture</span>'
            '<div class="res-segmented" role="radiogroup" aria-label="Capture resolution">'
            + ''.join(
                f'<form class="inline" method="POST" action="/settings/capture_height">'
                f'<input type="hidden" name="height" value="{h}">'
                f'<button class="btn{"" if h == capture_height_px else " secondary"} small" '
                f'type="submit">{h}p</button>'
                f'</form>'
                for h in (720, 1080)
            )
            + '</div>'
            '</div>'
            for _ in (0,)  # run the wrapper exactly once
        )
    )


_render_chirp_threshold_body = _shared_render_chirp_threshold_body
_render_tuning_body = _shared_render_tuning_body


def render_events_index_html(
    events: list[dict[str, Any]],
    trash_count: int = 0,
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    capture_mode: str = "camera_only",
    default_paths: list[str] | None = None,
    live_session: dict[str, Any] | None = None,
    sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    chirp_detect_threshold: float = 0.18,
    heartbeat_interval_s: float = 1.0,
    tracking_exposure_cap: str = "frame_duration",
    capture_height_px: int = 1080,
    calibration_last_ts: dict[str, float] | None = None,
    extended_markers: list[dict[str, Any]] | None = None,
    preview_requested: dict[str, bool] | None = None,
) -> str:
    """Render the dashboard: top nav + sidebar (devices / session / events)
    + a canvas showing the current calibration scene. All three panels
    hydrate from JSON ticks after first paint — the initial SSR avoids a
    flash of empty content while the first fetch is in flight."""
    devices = devices or []
    calibrations = calibrations or []

    from main import state  # local import: avoid circular at module load time

    scene = build_calibration_scene(state.calibrations())
    fig = _build_figure(scene)
    # Dashboard tweaks vs viewer defaults:
    #  - title=None: corner pill + nav already say what this is
    #  - fixed bbox + manual aspect ratio: with aspectmode="data" a single
    #    3m-distant camera blows up the bounding box and shrinks the
    #    50 cm plate to a dot. Pinning ±3.5 m XY / 2 m Z to the rig
    #    geometry keeps the plate readable whether 0, 1, or 2 cams are
    #    calibrated. Viewer leaves "data" so the ball trajectory still
    #    fits naturally.
    fig.update_layout(
        title=None, margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-6.0, 6.0],
        scene_yaxis_range=[-6.0, 6.0],
        scene_zaxis_range=[-0.2, 3.5],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
        # Pin scene uirevision to the SAME string both at first SSR paint
        # and on every /calibration/state tick. Without this override,
        # _build_figure's default ("viewer-scene") disagrees with whatever
        # the dashboard JS sends via Plotly.react, and each mismatch
        # triggers Plotly to treat UI state (camera/zoom) as "stale" and
        # snap it back to the default eye position. Same-string across
        # all paints = camera stays wherever the user dragged it.
        scene_uirevision="dashboard-canvas",
    )
    scene_div = fig.to_html(include_plotlyjs=False, full_html=False, div_id="scene-root")

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}</style>"
        "</head><body data-page=\"dashboard\">"
        f'{_render_app_nav("dashboard", devices, session, calibrations, sync, sync_cooldown_remaining_s)}'
        '<div class="layout">'
        '<aside class="sidebar">'
        '<div class="card">'
        '<h2 class="card-title">Active Session</h2>'
        f'<div id="active-body">{_render_active_session_body(live_session)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Session</h2>'
        f'<div id="session-body">{_render_session_body(session, capture_mode, default_paths, devices, calibrations)}</div>'
        "</div>"
        '<div class="card">'
        '<div class="events-toolbar">'
        '<h2 class="card-title" style="margin:0;border-bottom:0;padding:0;">Events</h2>'
        '<div class="events-filters">'
        '<button type="button" class="events-filter active" data-events-bucket="active">Active</button>'
        f'<button type="button" class="events-filter" data-events-bucket="trash">Trash {trash_count}</button>'
        '</div>'
        '</div>'
        f'<div id="events-body">{_render_events_body(events)}</div>'
        "</div>"
        "</aside>"
        '<section class="canvas">'
        '<div id="degraded-banner" class="degraded-banner" role="alert" style="display:none">'
        '  <span class="degraded-icon">⚠</span>'
        '  <span data-degraded-body>Live stream degraded.</span>'
        '</div>'
        '<div class="canvas-hint">Drag to rotate</div>'
        '<div class="canvas-mode-toggle" role="radiogroup" aria-label="Canvas mode">'
        '  <button type="button" data-canvas-mode="inspect" class="active">INSPECT</button>'
        '  <button type="button" data-canvas-mode="replay">REPLAY</button>'
        '</div>'
        f"{scene_div}"
        '<div class="playback-bar" id="playback-bar">'
        '  <button type="button" class="playpause" id="playpause">▶</button>'
        '  <input type="range" id="scrub" min="0" max="1000" step="1" value="0">'
        '  <span class="time" id="time-readout">0.00 / 0.00 s</span>'
        '  <span class="speed" role="radiogroup" aria-label="Playback speed">'
        '    <button type="button" data-speed="0.25">0.25×</button>'
        '    <button type="button" data-speed="0.5">0.5×</button>'
        '    <button type="button" data-speed="1" class="active">1×</button>'
        '    <button type="button" data-speed="2">2×</button>'
        '  </span>'
        '</div>'
        "</section>"
        "</div>"
        f"<script>{_JS_TEMPLATE}</script>"
        "</body></html>"
    )
