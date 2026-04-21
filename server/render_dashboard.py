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
from render_scene import _build_figure
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
  --nav-h: 52px;
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

/* --- Top nav --- */
.nav {{ position: fixed; top: 0; left: 0; right: 0; height: var(--nav-h);
        background: var(--surface); border-bottom: 1px solid var(--border-base);
        display: flex; align-items: center; padding: 0 24px;
        z-index: 20; gap: 24px; }}
.nav .brand {{ font-family: var(--mono); font-weight: 700; font-size: 14px;
               letter-spacing: 0.16em; color: var(--ink); }}
.nav .brand .dot {{ display: inline-block; width: 7px; height: 7px; background: var(--ink);
                    margin-right: 10px; vertical-align: middle; border-radius: 0; }}
.nav .status-line {{ margin-left: auto; font-family: var(--mono); font-size: 11px;
                     letter-spacing: 0.08em; text-transform: uppercase; color: var(--sub);
                     display: flex; gap: 20px; align-items: center; }}
.nav .status-line .pair {{ display: flex; gap: 6px; align-items: center; }}
.nav .status-line .label {{ color: var(--sub); }}
.nav .status-line .val {{ color: var(--ink); font-weight: 500; }}
.nav .status-line .val.armed {{ color: var(--passed); }}
.nav .status-line .val.idle {{ color: var(--sub); }}
/* Warn-tinted count when < 2 cams report — makes "0/2 devices" jump out
   as actionable instead of reading as a flat label next to "1/2 cal". */
.nav .status-line .val.partial {{ color: var(--warn); }}
.nav .status-line .val.full    {{ color: var(--passed); }}
/* Standalone nav link (points at /sync). Monospace, uppercase, subtle
   underline on hover — same rhythm as the surrounding label/val pairs. */
.nav .status-line .nav-link {{ color: var(--ink); text-decoration: none;
                                font-family: var(--mono); font-size: 11px;
                                letter-spacing: 0.10em; text-transform: uppercase;
                                border-bottom: 1px solid transparent;
                                padding-bottom: 1px; }}
.nav .status-line .nav-link:hover {{ border-bottom-color: var(--ink); }}

/* --- Main layout: sidebar + canvas --- */
.layout {{ display: flex; height: 100vh; padding-top: var(--nav-h); }}
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
.device-head {{ display: grid; grid-template-columns: 28px 1fr auto;
                align-items: center; gap: var(--s-2) var(--s-3); }}
.device-head .id {{ grid-column: 1; grid-row: 1; }}
.device-head .chip-col {{ grid-column: 3; grid-row: 1; justify-self: end; }}
.device-head .sub {{ grid-column: 1 / -1; grid-row: 2; }}
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
.session-actions {{ display: flex; gap: var(--s-2); margin-top: var(--s-3); }}
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
.preview-panel.off img {{ display: none; }}
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
.telemetry-panel {{ position: absolute; left: var(--s-4); bottom: var(--s-4); z-index: 7;
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
                   gap: var(--s-2); max-height: 340px; overflow-y: auto; }}
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
"""


_JS_TEMPLATE = r"""
(function () {
  const EXPECTED = ['A', 'B'];

  const sceneRoot = document.getElementById('scene-root');
  const devicesBox = document.getElementById('devices-body');
  const activeBox = document.getElementById('active-body');
  const sessionBox = document.getElementById('session-body');
  const eventsBox = document.getElementById('events-body');
  const navStatus = document.getElementById('nav-status');
  let currentDefaultPaths = ['server_post'];
  let currentLiveSession = null;
  const livePointStore = new Map();   // sid -> [{x,y,z,t_rel_s}]
  let lastEndedLiveSid = null;        // For ghost-preview on the next arm
  // Per-cam WS connection state from SSE device_status events. Keyed by
  // camera id; value shape: {connected: bool, since_ms: number}. The
  // degraded banner fires when an armed session has any cam that's been
  // disconnected for more than the grace window.
  const WS_GRACE_MS = 10_000;
  const wsStatus = new Map();
  // Telemetry panel state. All arrays are rolling windows; entries get
  // timestamped with Date.now() so the 60s window can be filtered by
  // wall-clock rather than insertion order. `pairTimestamps` holds the
  // arrival ms of each `point` SSE event; pair rate (pts/s) is the count
  // of entries within the trailing 1s window. `latencySamples` tracks
  // per-cam ws_latency_ms pulls from /status (1Hz).
  const TELEMETRY_WINDOW_MS = 60_000;
  const pairTimestamps = [];
  const latencySamples = { A: [], B: [] };
  const errorLog = [];  // {t_ms, kind, message}
  function recordError(kind, message) {
    errorLog.unshift({ t_ms: Date.now(), kind, message });
    if (errorLog.length > 10) errorLog.pop();
  }

  // --- Trajectory overlay state --------------------------------------------
  // Persisted set of session_ids whose triangulated trajectory is currently
  // painted on top of the calibration canvas. Cached /results/{sid} payloads
  // avoid refetching across ticks; basePlot is the last /calibration/state
  // fig spec so checkbox toggles can repaint without waiting for the 5 s
  // tick. Palette is deliberately disjoint from the A/B camera colours in
  // render_scene.py so the trajectory lines don't get confused with cams.
  const TRAJ_STORAGE_KEY = 'ball_tracker_dashboard_selected_trajectories';
  const TRAJ_PALETTE = ['#256246', '#9B6B16', '#A7372A', '#4A6B8C', '#5A5550', '#C97A2B'];
  const selectedTrajIds = (() => {
    try {
      const raw = localStorage.getItem(TRAJ_STORAGE_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch { return new Set(); }
  })();
  const trajCache = new Map();       // sid -> {points_on_device, fit_on_device}
  let basePlot = null;               // last /calibration/state .plot payload

  function persistTrajSelection() {
    try { localStorage.setItem(TRAJ_STORAGE_KEY, JSON.stringify([...selectedTrajIds])); }
    catch { /* storage full / private mode — ignore, selection stays in-memory */ }
  }

  // Stable hash → palette index so the same session always gets the same
  // colour across reloads even though the Set iteration order is random.
  function trajColorFor(sid) {
    let h = 0;
    for (let i = 0; i < sid.length; ++i) h = ((h << 5) - h + sid.charCodeAt(i)) | 0;
    return TRAJ_PALETTE[Math.abs(h) % TRAJ_PALETTE.length];
  }

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      // Dashboard displays on-device (mode-two) only. Server (mode-one) data
      // is forensic-only — it's still in the SessionResult payload but
      // intentionally ignored here.
      const entry = {
        points_on_device: data.points_on_device || [],
        fit_on_device: data.fit_on_device || null,
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  function evalQuadratic(coeffs, t) {
    return coeffs[0] * t * t + coeffs[1] * t + coeffs[2];
  }

  function densifyFit(fit, n) {
    const t0 = fit.t_min_s;
    const t1 = (fit.plate_t_s !== null && fit.plate_t_s !== undefined) ? fit.plate_t_s : fit.t_max_s;
    const xs = new Array(n), ys = new Array(n), zs = new Array(n);
    for (let i = 0; i < n; ++i) {
      const t = t0 + (t1 - t0) * (i / (n - 1));
      xs[i] = evalQuadratic(fit.coeffs_x, t);
      ys[i] = evalQuadratic(fit.coeffs_y, t);
      zs[i] = evalQuadratic(fit.coeffs_z, t);
    }
    return { xs, ys, zs };
  }

  // --- Canvas mode + playback state ---------------------------------------
  const CANVAS_MODE_KEY = 'ball_tracker_canvas_mode';
  let canvasMode = (() => {
    try { return localStorage.getItem(CANVAS_MODE_KEY) === 'replay' ? 'replay' : 'inspect'; }
    catch { return 'inspect'; }
  })();
  // Playback state — single global progress in [0,1] mapped to each selected
  // session's own [t_min, t_max]. This lets the scrubber stay coherent when
  // multiple sessions are overlaid without caring that their durations
  // differ; the UX reads as "show me all selected pitches synchronized to
  // the same fraction of their flight".
  let playheadFrac = 0.0;
  let playbackSpeed = 1.0;
  let isPlaying = false;
  let lastFrameTs = null;

  const playbackBar = document.getElementById('playback-bar');
  const playpauseBtn = document.getElementById('playpause');
  const scrubSlider = document.getElementById('scrub');
  const timeReadout = document.getElementById('time-readout');

  function activeReplaySid() {
    // Most recently added selected session is the "active" one — its
    // absolute time drives the readout while others animate at the same
    // fraction of their own flight.
    const arr = [...selectedTrajIds];
    return arr.length ? arr[arr.length - 1] : null;
  }

  function activeFitDuration() {
    const sid = activeReplaySid();
    if (!sid) return 0;
    const r = trajCache.get(sid);
    if (!r || !r.fit_on_device) return 0;
    return r.fit_on_device.t_max_s - r.fit_on_device.t_min_s;
  }

  function updateTimeReadout() {
    if (!timeReadout || !scrubSlider) return;
    const dur = activeFitDuration();
    const now = dur * playheadFrac;
    timeReadout.textContent = `${now.toFixed(2)} / ${dur.toFixed(2)} s`;
    scrubSlider.value = Math.round(playheadFrac * 1000);
  }

  // --- Strike zone geometry: MLB-standard 17" wide at plate, Z in 0.5-1.2 m
  // for a demo rig (no batter present). Drawn as a dashed wireframe so it
  // reads as reference grid, not a solid obstacle.
  const STRIKE_ZONE_HALF_W = 0.216;  // 17" / 2
  const STRIKE_ZONE_Z_LO = 0.5;
  const STRIKE_ZONE_Z_HI = 1.2;
  function strikeZoneTrace() {
    const hw = STRIKE_ZONE_HALF_W;
    return {
      type: 'scatter3d', mode: 'lines',
      x: [-hw, +hw, +hw, -hw, -hw],
      y: [0, 0, 0, 0, 0],
      z: [STRIKE_ZONE_Z_LO, STRIKE_ZONE_Z_LO, STRIKE_ZONE_Z_HI, STRIKE_ZONE_Z_HI, STRIKE_ZONE_Z_LO],
      line: { color: 'rgba(80,80,80,0.55)', width: 3, dash: 'dash' },
      name: 'strike zone',
      hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function inspectTracesFor(sid, result, color) {
    // Inspect mode: dense fitted quadratic + inlier dots + outlier X markers.
    // Lets operator judge RANSAC decisions at a glance and spot sessions
    // where the fit chose the wrong cluster.
    const fit = result.fit_on_device;
    const raw = result.points_on_device || [];
    if (fit) {
      const { xs, ys, zs } = densifyFit(fit, 64);
      const inlierSet = new Set(fit.inlier_indices);
      const inliers = raw.filter((_, i) => inlierSet.has(i));
      const outliers = raw.filter((_, i) => !inlierSet.has(i));
      const traces = [{
        type: 'scatter3d',
        mode: 'lines',
        x: xs, y: ys, z: zs,
        line: { color, width: 5 },
        name: `${sid} · fit`,
        hovertemplate: `${sid}<br>rms=${fit.rms_m.toFixed(3)}m<extra></extra>`,
        showlegend: true,
      }, {
        type: 'scatter3d',
        mode: 'markers',
        x: inliers.map(p => p.x_m),
        y: inliers.map(p => p.y_m),
        z: inliers.map(p => p.z_m),
        marker: { color, size: 3, opacity: 0.55 },
        name: `${sid} · inliers`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
        customdata: inliers.map(p => p.t_rel_s),
        showlegend: false,
      }];
      if (outliers.length) {
        traces.push({
          type: 'scatter3d',
          mode: 'markers',
          x: outliers.map(p => p.x_m),
          y: outliers.map(p => p.y_m),
          z: outliers.map(p => p.z_m),
          marker: { color: '#C03A2B', size: 5, symbol: 'x', opacity: 0.9 },
          name: `${sid} · outliers`,
          hovertemplate: `${sid} OUTLIER<br>t=%{customdata:.3f}s<extra></extra>`,
          customdata: outliers.map(p => p.t_rel_s),
          showlegend: false,
        });
      }
      return traces;
    }
    if (!raw.length) return [];
    return [{
      type: 'scatter3d',
      mode: 'lines+markers',
      x: raw.map(p => p.x_m),
      y: raw.map(p => p.y_m),
      z: raw.map(p => p.z_m),
      line: { color, width: 3, dash: 'dot' },
      marker: { color, size: 2, opacity: 0.6 },
      name: `${sid} · raw`,
      hovertemplate: `${sid} (unfit)<br>t=%{customdata:.3f}s<extra></extra>`,
      customdata: raw.map(p => p.t_rel_s),
      showlegend: true,
    }];
  }

  function replayTracesFor(sid, result, color) {
    // Replay mode: clean trajectory line + animated ball sphere + short
    // motion trail. Inlier/outlier markers are suppressed — those are an
    // inspect-mode concern, not a broadcast/demo concern.
    const fit = result.fit_on_device;
    if (!fit) return [];
    const { xs, ys, zs } = densifyFit(fit, 80);
    const tActive = fit.t_min_s + playheadFrac * (fit.t_max_s - fit.t_min_s);
    const bx = evalQuadratic(fit.coeffs_x, tActive);
    const by = evalQuadratic(fit.coeffs_y, tActive);
    const bz = evalQuadratic(fit.coeffs_z, tActive);
    // Short fading trail: 12 samples behind the ball, ~0.1 s worth.
    const trailN = 12;
    const trailDt = 0.01;
    const trailX = [], trailY = [], trailZ = [];
    for (let i = trailN; i >= 1; --i) {
      const tt = tActive - i * trailDt;
      if (tt < fit.t_min_s) continue;
      trailX.push(evalQuadratic(fit.coeffs_x, tt));
      trailY.push(evalQuadratic(fit.coeffs_y, tt));
      trailZ.push(evalQuadratic(fit.coeffs_z, tt));
    }
    return [
      {
        type: 'scatter3d', mode: 'lines',
        x: xs, y: ys, z: zs,
        line: { color, width: 4 },
        name: `${sid} · path`,
        hovertemplate: `${sid}<br>rms=${fit.rms_m.toFixed(3)}m<extra></extra>`,
        showlegend: true,
        opacity: 0.45,
      },
      {
        type: 'scatter3d', mode: 'lines',
        x: trailX, y: trailY, z: trailZ,
        line: { color, width: 6 },
        name: `${sid} · trail`,
        hoverinfo: 'skip',
        showlegend: false,
        opacity: 0.8,
      },
      {
        type: 'scatter3d', mode: 'markers',
        x: [bx], y: [by], z: [bz],
        marker: {
          color: '#D9A441', size: 9, symbol: 'circle',
          line: { color: '#4A3E24', width: 1.5 },
        },
        name: `${sid} · ball`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
        customdata: [tActive - fit.t_min_s],
        showlegend: false,
      },
    ];
  }

  function trajTracesFor(sid, result, color) {
    return canvasMode === 'replay'
      ? replayTracesFor(sid, result, color)
      : inspectTracesFor(sid, result, color);
  }

  function ghostTrace(pts, sid) {
    // Rendered before the active-session trace so the active one paints
    // on top. Alpha kept low — this is a "camera framing hasn't moved"
    // visual cue, not a thing to compare against.
    return {
      type: 'scatter3d',
      mode: 'lines',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      line: { color: 'rgba(192,57,43,0.20)', width: 2 },
      name: `${sid} · ghost`,
      hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function liveTraces() {
    const traces = [];
    // Ghost preview of the previous live session — shown BETWEEN arm
    // cycles (no current session armed) so the operator can confirm
    // camera framing still matches the last pitch's trail before
    // throwing again. Suppressed once a new session arms to avoid
    // clutter on the active canvas.
    if (
      (!currentLiveSession || !currentLiveSession.session_id) &&
      lastEndedLiveSid
    ) {
      const ghostPts = livePointStore.get(lastEndedLiveSid) || [];
      if (ghostPts.length) traces.push(ghostTrace(ghostPts, lastEndedLiveSid));
    }
    if (!currentLiveSession || !currentLiveSession.session_id) return traces;
    const sid = currentLiveSession.session_id;
    const pts = livePointStore.get(sid) || [];
    if (!pts.length) return traces;
    traces.push({
      type: 'scatter3d',
      mode: 'lines+markers',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      marker: {
        size: 4,
        color: pts.map(p => p.t_rel_s),
        colorscale: 'YlOrRd',
        opacity: 0.95,
      },
      line: { color: '#C0392B', width: 4 },
      name: `${sid} · live`,
      hovertemplate: `${sid}<br>t=%{marker.color:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
      showlegend: true,
    });
    return traces;
  }

  // Layout is effectively static across the dashboard's lifetime (axes,
  // aspect, uirevision never change — only trace data does). Cache the
  // first layout we see and reuse the SAME object reference on every
  // Plotly.react. Passing the identical reference is the most reliable
  // way to tell Plotly "layout hasn't changed, don't touch the camera or
  // recompute anything scene-related" — stronger than relying solely on
  // uirevision heuristics, and cheap.
  let cachedLayout = null;
  let canvasFirstPaintDone = false;
  // Index of the live-trace inside the plot's data array after the most
  // recent Plotly.react. -1 = not painted yet / stale. extendLivePoint()
  // uses Plotly.extendTraces to append a single point without walking the
  // full trace tree — the per-point append cost drops from ~5-20ms
  // (Plotly.react with full trace rebuild) to <1ms. Any structural change
  // (session flip, mode switch, trajectory toggle) must reset this to -1
  // so the next point event falls back to a full repaint and the slot
  // re-anchors.
  let liveTraceIdx = -1;

  function extendLivePoint(pt) {
    if (liveTraceIdx < 0 || !sceneRoot || !window.Plotly) return false;
    try {
      Plotly.extendTraces(
        sceneRoot,
        {
          x: [[pt.x]],
          y: [[pt.y]],
          z: [[pt.z]],
          'marker.color': [[pt.t_rel_s]],
        },
        [liveTraceIdx],
      );
      return true;
    } catch (_) {
      liveTraceIdx = -1;  // slot invalid — force repaint next time
      return false;
    }
  }

  async function repaintCanvas() {
    if (!basePlot || !window.Plotly) return;
    const extraTraces = [];
    // Load any missing trajectories in parallel — checkbox clicks before
    // the first tick should still paint immediately.
    await Promise.all([...selectedTrajIds].map(sid => ensureTrajLoaded(sid)));
    // Strike zone shown only in replay mode — serves as a reference target
    // for where the pitch is going, irrelevant for outlier-inspection.
    if (canvasMode === 'replay' && selectedTrajIds.size > 0) {
      extraTraces.push(strikeZoneTrace());
    }
    for (const sid of selectedTrajIds) {
      const result = trajCache.get(sid);
      if (!result) continue;
      extraTraces.push(...trajTracesFor(sid, result, trajColorFor(sid)));
    }
    extraTraces.push(...liveTraces());
    if (cachedLayout === null) {
      // One-time build from the first basePlot.layout we see. The server
      // sets scene.uirevision='dashboard-canvas' in both SSR and tick
      // responses — matching the value already embedded by fig.to_html
      // means Plotly never sees a uirevision transition and UI state
      // stays under user control from frame zero.
      cachedLayout = JSON.parse(JSON.stringify(basePlot.layout || {}));
      if (!cachedLayout.scene) cachedLayout.scene = {};
      cachedLayout.scene.uirevision = 'dashboard-canvas';
    }
    const finalTraces = [...(basePlot.data || []), ...extraTraces];
    Plotly.react(
      sceneRoot,
      finalTraces,
      cachedLayout,
      // doubleClick:false — Plotly 3D ships a built-in "reset camera on
      // double-click anywhere in the scene" gesture. Users bump into it
      // accidentally (especially on trackpads where a firm tap registers
      // as dblclick) and it overrides uirevision preservation. Kill it.
      // scrollZoom stays true so the native + our wheel handler both
      // work for panning the eye distance.
      { responsive: true, scrollZoom: true, doubleClick: false },
    );
    // Anchor the live-trace slot for subsequent extendTraces calls. The
    // live trace (when present) is the last one liveTraces() appends.
    liveTraceIdx = -1;
    if (currentLiveSession && currentLiveSession.session_id) {
      for (let i = finalTraces.length - 1; i >= 0; i--) {
        const t = finalTraces[i];
        if (t && typeof t.name === 'string' && t.name.endsWith(' · live')) {
          liveTraceIdx = i;
          break;
        }
      }
    }
    canvasFirstPaintDone = true;
  }

  // Plotly's built-in 3D wheel-zoom is tuned for mouse wheels and feels
  // sluggish on trackpads (especially pinch-to-zoom which arrives as
  // ctrl+wheel with tiny deltas). Replace it with a direct camera.eye
  // scale so each wheel tick = ~10 % distance change and trackpad
  // gestures get the same per-event treatment as a mouse wheel click.
  if (sceneRoot) {
    sceneRoot.addEventListener('wheel', (e) => {
      if (!sceneRoot._fullLayout || !sceneRoot._fullLayout.scene) return;
      const cam = sceneRoot._fullLayout.scene.camera;
      if (!cam || !cam.eye) return;
      e.preventDefault();
      // Wheel-down (positive deltaY) = zoom out, wheel-up = zoom in.
      // Magnitude scaled by sqrt so trackpad's many-tiny-events feels
      // continuous instead of jittery; mouse wheel's chunky events
      // still produce a noticeable but bounded jump per click.
      const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
      const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
      Plotly.relayout(sceneRoot, {
        'scene.camera.eye': {
          x: cam.eye.x * factor,
          y: cam.eye.y * factor,
          z: cam.eye.z * factor,
        },
      });
    }, { passive: false });
  }

  // Delegated change handler — event list re-renders on every tick, so we
  // can't rebind per-checkbox. Capture click on the wrapping <label> to
  // prevent the event-row <a> from swallowing the toggle.
  if (eventsBox) eventsBox.addEventListener('click', (e) => {
    if (e.target.closest('.traj-toggle')) e.stopPropagation();
  });
  if (eventsBox) eventsBox.addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-traj-sid]');
    if (!cb) return;
    const sid = cb.dataset.trajSid;
    // Single-select preview: clicking one row always replaces the
    // selection (clicking again on the same row deselects). Multi-select
    // was confusing when replays had different durations + made the fit
    // outlier inspector busy when several sessions overlapped in space.
    if (cb.checked) {
      selectedTrajIds.clear();
      selectedTrajIds.add(sid);
      // Uncheck every other checkbox in the events list so the DOM
      // reflects the one-at-a-time invariant without waiting for the
      // next events tick to re-render.
      eventsBox.querySelectorAll('input[data-traj-sid]').forEach(other => {
        if (other !== cb) other.checked = false;
      });
      // Reset playhead so the new selection starts from t=0 rather
      // than wherever the previous pitch was mid-animation.
      playheadFrac = 0.0;
    } else {
      selectedTrajIds.delete(sid);
    }
    persistTrajSelection();
    if (canvasMode === 'replay') updateTimeReadout();
    repaintCanvas();
  });

  // --- Canvas mode toggle: INSPECT vs REPLAY -------------------------------
  function applyCanvasMode(nextMode) {
    if (nextMode !== 'inspect' && nextMode !== 'replay') return;
    canvasMode = nextMode;
    try { localStorage.setItem(CANVAS_MODE_KEY, canvasMode); } catch {}
    document.querySelectorAll('.canvas-mode-toggle button').forEach(b => {
      b.classList.toggle('active', b.dataset.canvasMode === canvasMode);
    });
    // Playback bar only makes sense in replay mode; pause + reset the
    // scrubber when leaving so we don't keep the animation loop running
    // invisibly (wasted frames + broken readout on return).
    if (canvasMode === 'replay') {
      if (playbackBar) playbackBar.classList.add('show');
      updateTimeReadout();
    } else {
      if (playbackBar) playbackBar.classList.remove('show');
      setPlaying(false);
    }
    repaintCanvas();
  }
  document.querySelectorAll('.canvas-mode-toggle button').forEach(btn => {
    btn.addEventListener('click', () => applyCanvasMode(btn.dataset.canvasMode));
  });
  // Initial mode sync (localStorage value may already be 'replay').
  applyCanvasMode(canvasMode);

  // --- Playback controls ---------------------------------------------------
  // Track whether the user is currently mid-drag on the canvas. Plotly
  // 3D orbit/pan rely on a continuous pointer-down gesture with no
  // DOM-level repaint interruptions between mousedown and mouseup —
  // every Plotly.react during that window wipes the drag state before
  // the next mousemove can extend it. During replay playback we issue
  // Plotly.react every frame for the ball's new position, which stomps
  // on any orbit attempt and manifests as "only wheel zoom works".
  // Suppress visual repaints (not the playhead logic) while dragging;
  // the ball will catch up on mouseup.
  let isUserInteracting = false;
  if (sceneRoot) {
    sceneRoot.addEventListener('pointerdown', () => { isUserInteracting = true; });
    // mouseup/pointerup can fire OUTSIDE the canvas if the user releases
    // after dragging away — bind to window, not sceneRoot, so we never
    // miss the release and leave the flag stuck true.
    window.addEventListener('pointerup', () => { isUserInteracting = false; });
    window.addEventListener('pointercancel', () => { isUserInteracting = false; });
  }

  function setPlaying(flag) {
    isPlaying = !!flag;
    if (playpauseBtn) playpauseBtn.textContent = isPlaying ? '❚❚' : '▶';
    if (isPlaying) {
      lastFrameTs = null;
      requestAnimationFrame(animationTick);
    }
  }
  function animationTick(ts) {
    if (!isPlaying) return;
    if (lastFrameTs !== null) {
      const dur = activeFitDuration();
      if (dur > 0) {
        const dt = (ts - lastFrameTs) / 1000.0;
        playheadFrac += (dt * playbackSpeed) / dur;
        if (playheadFrac >= 1.0) {
          // Loop back to start so the operator can keep playing without
          // clicking ▶ after every pitch. If single-shot is ever desired,
          // gate on a `loop` flag from a future UI element.
          playheadFrac = 0.0;
        }
        updateTimeReadout();
        // Skip the heavy repaint while the user is mid-drag — playhead
        // still advances silently so playback resumes at the correct
        // time on pointerup.
        if (!isUserInteracting) repaintCanvas();
      }
    }
    lastFrameTs = ts;
    if (isPlaying) requestAnimationFrame(animationTick);
  }
  if (playpauseBtn) playpauseBtn.addEventListener('click', () => {
    if (activeFitDuration() <= 0) return;  // nothing to play
    setPlaying(!isPlaying);
  });
  if (scrubSlider) scrubSlider.addEventListener('input', () => {
    playheadFrac = Math.max(0, Math.min(1, parseInt(scrubSlider.value, 10) / 1000.0));
    setPlaying(false);  // user scrub pauses playback
    updateTimeReadout();
    repaintCanvas();
  });
  document.querySelectorAll('.playback-bar .speed button').forEach(btn => {
    btn.addEventListener('click', () => {
      playbackSpeed = parseFloat(btn.dataset.speed);
      document.querySelectorAll('.playback-bar .speed button').forEach(b =>
        b.classList.toggle('active', b === btn)
      );
    });
  });
  // Spacebar: play/pause when replay visible and user isn't typing in a form.
  window.addEventListener('keydown', (e) => {
    if (canvasMode !== 'replay') return;
    if (e.target.matches('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); playpauseBtn.click(); }
  });

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  function statusChip(cam, online, calibrated) {
    if (calibrated) return `<span class="chip calibrated">calibrated</span>`;
    if (online)     return `<span class="chip online">online</span>`;
    return `<span class="chip idle">offline</span>`;
  }

  function renderDevices(state) {
    if (!devicesBox) return;
    const devByCam = new Map((state.devices || []).map(d => [d.camera_id, d]));
    const calibrated = new Set(state.calibrations || []);
    const syncPending = state.sync_commands || {};
    const previewReq = state.preview_requested || {};
    const calLastTs = state.calibration_last_ts || {};
    const autoCalActive = (state.auto_calibration && state.auto_calibration.active) || {};
    const autoCalLast = (state.auto_calibration && state.auto_calibration.last) || {};
    function hhmm(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      return d.toTimeString().slice(0, 5);
    }

    function row(cam, deviceRecord) {
      const online = !!deviceRecord;
      const timeSynced = !!(deviceRecord && deviceRecord.time_synced);
      const pending = !!syncPending[cam];
      const isCal = calibrated.has(cam);
      const previewOn = !!previewReq[cam];
      const lastTs = calLastTs[cam];
      const autoRun = autoCalActive[cam] || null;
      const autoLast = autoCalLast[cam] || null;
      const calDot = isCal ? 'ok' : (online ? 'warn' : 'bad');
      const syncDot = !online ? 'bad' : (pending ? 'warn' : (timeSynced ? 'ok' : 'warn'));
      const autoDot = autoRun ? 'warn'
                    : (autoLast && autoLast.status === 'completed' ? 'ok'
                    : (autoLast && autoLast.status === 'failed' ? 'bad' : (online ? 'warn' : 'bad')));
      const syncLabel = !online ? 'offline' : (pending ? 'pending…' : (timeSynced ? 'synced' : 'not synced'));
      const calLabel = (isCal && lastTs) ? ('last ' + hhmm(lastTs))
                     : (!online ? 'offline' : (isCal ? 'calibrated' : 'pending'));
      const autoLabel = autoRun
        ? `${autoRun.status} · ${autoRun.stable_frames || 0} stable`
        : (autoLast
          ? `${autoLast.status}${autoLast.result && autoLast.result.reprojection_px != null ? ' · ' + Number(autoLast.result.reprojection_px).toFixed(1) + 'px' : ''}`
          : (online ? 'idle' : 'offline'));
      const previewBtn = `<button type="button" class="btn small preview-btn${previewOn ? ' active' : ''}" ` +
        `data-preview-cam="${esc(cam)}" data-preview-enabled="${previewOn ? 1 : 0}">` +
        `${previewOn ? 'PREVIEW ON' : 'PREVIEW'}</button>`;
      const autoCalBtn = `<button type="button" class="btn small" data-auto-cal="${esc(cam)}" ${autoRun ? 'disabled' : ''}>` +
        `${autoRun ? 'Auto-cal…' : 'Run auto-cal'}</button>`;
      // Always render the panel so the row height stays stable; off
      // state shows a black placeholder. When on, the tickPreviewImages
      // loop (see below) cache-busts the <img src>.
      const previewPanel = `<div class="preview-panel${previewOn ? '' : ' off'}" data-preview-panel="${esc(cam)}">` +
        `<img data-preview-img="${esc(cam)}" src="${previewOn ? ('/camera/' + encodeURIComponent(cam) + '/preview?annotate=1&t=' + Date.now()) : ''}" alt="preview ${esc(cam)}">` +
        `<svg class="plate-overlay" data-preview-overlay="${esc(cam)}" aria-hidden="true"><polygon></polygon></svg>` +
        `<div class="placeholder">${previewOn ? '…' : 'Preview off'}</div>` +
        `</div>`;
      const virtCell = `<div class="virt-cell" data-virt-cell="${esc(cam)}">` +
        `<canvas data-virt-canvas="${esc(cam)}"></canvas>` +
        `<div class="virt-label">VIRT · ${esc(cam)}</div>` +
        `<div class="placeholder">${isCal ? 'loading…' : 'not calibrated'}</div>` +
        `</div>`;
      return `
        <div class="device">
          <div class="device-head">
            <div class="id">${esc(cam)}</div>
            <div class="sub">
              <span class="item ${syncDot}"><span class="dot ${syncDot}"></span>time sync · ${esc(syncLabel)}</span>
              <span class="item ${calDot}"><span class="dot ${calDot}"></span>pose · ${esc(calLabel)}</span>
              <span class="item ${autoDot}"><span class="dot ${autoDot}"></span>auto-cal · ${esc(autoLabel)}</span>
            </div>
            <div class="chip-col">${statusChip(cam, online, isCal)}</div>
          </div>
          <div class="device-actions">${previewBtn}${autoCalBtn}</div>
          ${previewPanel}
          ${virtCell}
        </div>`;
    }

    const rows = EXPECTED.map(cam => row(cam, devByCam.get(cam))).join('');
    const extras = (state.devices || [])
      .filter(d => !EXPECTED.includes(d.camera_id))
      .map(d => row(d.camera_id, d)).join('');
    devicesBox.innerHTML = `<div class="devices-grid">${rows + extras}</div>`;
    // The innerHTML rebuild above destroys any existing canvases inside
    // the virt cells and preview overlays — redraw them on the fresh DOM.
    if (typeof redrawAllVirtCanvases === 'function') redrawAllVirtCanvases();
    if (typeof redrawAllPreviewPlateOverlays === 'function') redrawAllPreviewPlateOverlays();
  }

  const MODE_LABELS = { camera_only: 'Camera-only', on_device: 'On-device', dual: 'Dual' };
  const PATH_LABELS = {
    live: ['Live stream', 'iOS → WS'],
    ios_post: ['iOS post-pass', 'on-device analyzer'],
    server_post: ['Server post-pass', 'PyAV + OpenCV'],
  };

  // Instantaneous fps derived from the most recent pair of frame_count
  // samples. Returns 0 when <2 samples or the window is too short to be
  // meaningful. Keeps the sparkline-per-cam history bounded to 60 entries
  // (~60s at 1Hz frame_count emission) so arbitrary-long sessions don't
  // grow unbounded.
  const FPS_HISTORY_CAP = 60;
  function pushFrameSample(liveSession, cam, count) {
    liveSession.frame_samples = liveSession.frame_samples || { A: [], B: [] };
    const arr = liveSession.frame_samples[cam] = liveSession.frame_samples[cam] || [];
    const now = Date.now();
    const prev = arr.length ? arr[arr.length - 1] : null;
    arr.push({ t: now, count });
    if (arr.length > FPS_HISTORY_CAP) arr.shift();
    // fps from most recent two samples
    if (arr.length >= 2) {
      const a = arr[arr.length - 2];
      const b = arr[arr.length - 1];
      const dtS = Math.max(0.001, (b.t - a.t) / 1000);
      liveSession.frame_fps = liveSession.frame_fps || {};
      liveSession.frame_fps[cam] = Math.max(0, (b.count - a.count) / dtS);
    }
    return prev;
  }

  function drawSparkline(canvas, samples) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!samples || samples.length < 2) return;
    // Derive per-sample fps on the fly
    const fps = [];
    for (let i = 1; i < samples.length; i++) {
      const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
      fps.push((samples[i].count - samples[i - 1].count) / dtS);
    }
    const maxFps = Math.max(240, ...fps);  // keep 240 as visual cap
    ctx.strokeStyle = '#C0392B';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    fps.forEach((f, i) => {
      const x = (i / (fps.length - 1 || 1)) * W;
      const y = H - (f / maxFps) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function fmtElapsed(ms) {
    if (!ms || ms < 0) return '00:00.0';
    const total = ms / 1000;
    const m = Math.floor(total / 60);
    const s = Math.floor(total % 60);
    const ds = Math.floor((total * 10) % 10);
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${ds}`;
  }

  function renderActiveSession(liveSession) {
    if (!activeBox) return;
    if (!liveSession || !liveSession.session_id) {
      activeBox.innerHTML = `<div class="active-empty">No active live stream.</div>`;
      return;
    }
    const sid = esc(liveSession.session_id);
    const frameCounts = liveSession.frame_counts || {};
    const fps = liveSession.frame_fps || {};
    const armed = liveSession.armed !== false;
    const chips = (liveSession.paths || []).map(path =>
      `<span class="path-chip on">${esc((PATH_LABELS[path] || [path])[0])}</span>`
    ).join('') || `<span class="path-chip">none</span>`;
    const elapsedMs = liveSession.armed_at_ms
      ? (armed ? Date.now() : (liveSession.ended_at_ms || Date.now())) - liveSession.armed_at_ms
      : 0;
    // Last-point-age flips red after 200ms of silence during an armed
    // session — operator signal that triangulation has stalled (lost
    // sync, ball left frame, or a stream is dropping frames).
    const lastPtMs = liveSession.last_point_at_ms
      ? Date.now() - liveSession.last_point_at_ms
      : null;
    const lastPtClass = (armed && lastPtMs !== null && lastPtMs > 200) ? 'stale' : '';
    const lastPtTxt = lastPtMs === null
      ? '—'
      : (lastPtMs < 1000 ? `${lastPtMs}ms ago` : `${(lastPtMs/1000).toFixed(1)}s ago`);
    const depths = liveSession.point_depths || [];
    let depthTxt = '—';
    if (depths.length) {
      const mean = depths.reduce((a,b)=>a+b,0) / depths.length;
      const variance = depths.reduce((a,b)=>a+(b-mean)*(b-mean),0) / depths.length;
      const std = Math.sqrt(variance);
      depthTxt = `${mean.toFixed(2)}m ± ${std.toFixed(2)}`;
    }
    // Post-pass rows: which paths are part of the session and their status
    const pathsOn = new Set(liveSession.paths || []);
    const completed = new Set(liveSession.paths_completed || []);
    const postPassRow = (path, label) => {
      if (!pathsOn.has(path)) return '';
      const state = completed.has(path) ? 'done' : (armed ? 'pending' : 'running');
      return `<span class="postpass-chip ${state}">${esc(label)}: ${state}</span>`;
    };
    const postPassChips = [
      postPassRow('ios_post', 'iOS'),
      postPassRow('server_post', 'srv'),
    ].filter(Boolean).join('');
    activeBox.innerHTML = `
      <div class="active-head">
        <span class="chip armed ${armed ? 'pulse' : ''}">${armed ? '●REC' : 'ended'}</span>
        <span class="session-id">${sid}</span>
        <span class="elapsed" data-elapsed>${fmtElapsed(elapsedMs)}</span>
      </div>
      <div class="path-chip-row">${chips}</div>
      <div class="cam-row" data-cam="A">
        <canvas class="spark" data-spark="A"></canvas>
        <span class="k">A</span>
        <span class="v">${(fps.A || 0).toFixed(0)} fps</span>
        <span class="vsub">${Number(frameCounts.A || 0)} frames</span>
      </div>
      <div class="cam-row" data-cam="B">
        <canvas class="spark" data-spark="B"></canvas>
        <span class="k">B</span>
        <span class="v">${(fps.B || 0).toFixed(0)} fps</span>
        <span class="vsub">${Number(frameCounts.B || 0)} frames</span>
      </div>
      <div class="live-pairs ${lastPtClass}">
        <span class="k">Live pairs</span>
        <span class="v">${Number(liveSession.point_count || 0)} pts</span>
        <span class="vsub">last ${lastPtTxt} · ${depthTxt}</span>
      </div>
      ${postPassChips ? `<div class="postpass-row">${postPassChips}</div>` : ''}
      <div class="active-actions">
        ${armed ? `<form method="post" action="/sessions/cancel" style="display:inline"><button type="submit" class="btn-stop">Stop</button></form>` : ''}
        <button type="button" class="btn-reset" data-reset-trail>Reset trail</button>
      </div>`;
    // Redraw sparklines after DOM replacement (canvas clears on innerHTML).
    ['A','B'].forEach(cam => {
      const canvas = activeBox.querySelector(`[data-spark="${cam}"]`);
      const samples = ((liveSession.frame_samples || {})[cam]) || [];
      drawSparkline(canvas, samples);
    });
    const resetBtn = activeBox.querySelector('[data-reset-trail]');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        if (!currentLiveSession) return;
        livePointStore.set(currentLiveSession.session_id, []);
        currentLiveSession.point_count = 0;
        currentLiveSession.point_depths = [];
        currentLiveSession.last_point_at_ms = null;
        liveTraceIdx = -1;
        renderActiveSession(currentLiveSession);
        repaintCanvas();
      });
    }
  }

  function renderDetectionPaths(session) {
    const armed = !!(session && session.armed);
    const active = new Set(armed ? (session.paths || currentDefaultPaths || []) : (currentDefaultPaths || []));
    if (armed) {
      const chips = [...active].map(path =>
        `<span class="path-chip on">${esc((PATH_LABELS[path] || [path])[0])}</span>`
      ).join('') || `<span class="path-chip">none</span>`;
      return `<div class="path-lock"><span class="mode-label">Paths</span><div class="path-chip-row">${chips}</div></div>`;
    }
    const options = ['live', 'ios_post', 'server_post'].map(path => {
      const [title, sub] = PATH_LABELS[path] || [path, ''];
      return `<label class="path-option">
          <input type="checkbox" name="paths" value="${path}" ${active.has(path) ? 'checked' : ''}>
          <span class="copy">
            <span class="title">${esc(title)}</span>
            <span class="sub">${esc(sub)}</span>
          </span>
        </label>`;
    }).join('');
    return `<form method="POST" action="/detection/paths" id="paths-form">
      <div class="paths-stack">${options}</div>
      <div class="paths-actions"><button class="btn" type="submit">Apply</button></div>
    </form>`;
  }

  function renderSession(state) {
    if (!sessionBox) { /* nav-only render still executes below */ }
    const s = state.session;
    const armed = !!(s && s.armed);
    currentDefaultPaths = state.default_paths || currentDefaultPaths || ['server_post'];
    currentLiveSession = state.live_session || currentLiveSession;
    const chip = armed ? `<span class="chip armed">armed</span>` : `<span class="chip idle">idle</span>`;
    const sid = s && s.id ? `<span class="session-id">${esc(s.id)}</span>` : '';
    const clearBtn = (!armed && s && s.id)
      ? `<form class="inline" method="POST" action="/sessions/clear">
           <button class="btn" type="submit">Clear</button>
         </form>`
      : '';
    const sessHtml = `
      <div class="session-head">${chip}${sid}</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm">
          <button class="btn" type="submit" ${armed ? 'disabled' : ''}>Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" ${armed ? '' : 'disabled'}>Stop</button>
        </form>
        <form class="inline" method="POST" action="/sync/trigger">
          <button class="btn secondary" type="submit" ${armed ? 'disabled' : ''}>Calibrate time</button>
        </form>
        ${clearBtn}
      </div>
      ${renderDetectionPaths(s)}`;
    if (sessionBox) sessionBox.innerHTML = sessHtml;
    renderActiveSession(currentLiveSession);

    // Mirror into the nav's tiny status strip. Also surface a tiny Sync
    // chip (syncing / cooldown / idle) + a link to /setup so the operator
    // sees sync state at a glance from the main dashboard. Suppressed on
    // /setup where render_sync.py's renderNav owns the nav instead (its
    // link says "← Dashboard" and it also tracks matched-filter state).
    if (navStatus && document.body.dataset.page !== 'setup') {
      const online = (state.devices || []).length;
      const cal = (state.calibrations || []).length;
      const countCls = n => (n >= 2 ? 'full' : 'partial');
      const cooldown = Number(state.sync_cooldown_remaining_s || 0);
      const syncLabel = state.sync ? 'syncing'
                                   : (cooldown > 0 ? 'cooldown' : 'idle');
      const syncCls = state.sync ? 'armed'
                                : (cooldown > 0 ? 'partial' : 'idle');
      const live = state.live_session || {};
      const frameCounts = live.frame_counts || {};
      const liveRate = `${Number(frameCounts.A || 0)}/${Number(frameCounts.B || 0)} frames`;
      const ws = state.ws_devices || {};
      const wsA = ws.A && ws.A.connected ? '●' : '○';
      const wsB = ws.B && ws.B.connected ? '●' : '○';
      const rttVals = [ws.A && ws.A.last_latency_ms, ws.B && ws.B.last_latency_ms].filter(v => v != null).map(Number);
      const rtt = rttVals.length ? `${(rttVals.reduce((a, b) => a + b, 0) / rttVals.length).toFixed(0)}ms` : '—';
      const navHtml = `
        <span class="pair"><span class="label">Devices</span><span class="val ${countCls(online)}">${online}/2</span></span>
        <span class="pair"><span class="label">Calibrated</span><span class="val ${countCls(cal)}">${cal}/2</span></span>
        <span class="pair"><span class="label">Session</span>` +
        (armed
          ? `<span class="val armed">${esc(s.id || '—')}</span>`
          : `<span class="val idle">idle</span>`) +
        `</span>` +
        `<span class="pair"><span class="label">Stream</span><span class="val ${armed ? 'armed' : 'idle'}">${wsA}${wsB} ${liveRate}</span></span>` +
        `<span class="pair"><span class="label">RTT</span><span class="val ${rttVals.length ? 'full' : 'idle'}">${rtt}</span></span>` +
        `<span class="pair"><span class="label">Sync</span><span class="val ${syncCls}">${syncLabel}</span></span>` +
        `<a class="nav-link" href="/setup">Setup</a>` +
        `<a class="nav-link" href="/markers">Markers</a>`;
      navStatus.innerHTML = navHtml;
    }
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  // (Time Sync rendering lives on /sync now — render_sync.py. The
  // dashboard still surfaces a tiny "Sync · syncing/cooldown/idle" chip
  // in the nav status strip, populated by renderSession off /status.)

  function renderEvents(events) {
    if (!eventsBox) return;
    let evHtml;
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      return;
    }
    evHtml = events.map(e => {
      const sid = esc(e.session_id);
      const stat = (e.status || '').replace(/_/g, ' ');
      const speedKmh = e.speed_mps != null ? (e.speed_mps * 3.6).toFixed(1) : null;
      const duration = fmtNum(e.fit_duration_s != null ? e.fit_duration_s : e.duration_s, 2);
      const rms = fmtNum(e.rms_m, 3);
      const plateX = e.plate_xz_m ? e.plate_xz_m[0].toFixed(2) : null;
      const plateZ = e.plate_xz_m ? e.plate_xz_m[1].toFixed(2) : null;
      const pathStatus = e.path_status || {};
      const pathChips = [['live', 'L'], ['ios_post', 'I'], ['server_post', 'S']]
        .map(([path, label]) => `<span class="path-chip${pathStatus[path] === 'done' ? ' on' : ''}">${label}</span>`)
        .join('');
      // Quality chip from fit RMS: <10mm excellent, <30mm good, <80mm fair, else poor.
      // Sessions without a fit get a neutral `no-fit` chip — they still list
      // (the operator may want to forensic them) but signal loudly.
      let qualityClass = 'no-fit', qualityLabel = 'no fit';
      if (e.rms_m != null) {
        if (e.rms_m < 0.010)      { qualityClass = 'excellent'; qualityLabel = 'excellent'; }
        else if (e.rms_m < 0.030) { qualityClass = 'good';      qualityLabel = 'good'; }
        else if (e.rms_m < 0.080) { qualityClass = 'fair';      qualityLabel = 'fair'; }
        else                      { qualityClass = 'poor';      qualityLabel = 'poor'; }
      }
      const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
      // Trajectory overlay toggle: only sessions with on-device points qualify.
      // Mode-one (camera_only) sessions are intentionally not overlayable on
      // the LIVE dashboard — use the forensic viewer for those.
      const hasTraj = (e.n_triangulated_on_device || 0) > 0;
      const color = hasTraj ? trajColorFor(e.session_id) : '';
      const checked = selectedTrajIds.has(e.session_id) ? 'checked' : '';
      const toggle = hasTraj
        ? `<label class="traj-toggle" title="Overlay trajectory on canvas">
             <input type="checkbox" data-traj-sid="${sid}" ${checked}>
             <span class="swatch" style="background:${color}"></span>
           </label>`
        : `<span class="traj-toggle-placeholder" aria-hidden="true"></span>`;
      const metricsRow = e.has_fit ? `
          <div class="event-stats">
            ${speedKmh != null ? `<span><span class="k">Speed</span><span class="v">${speedKmh} km/h</span></span>` : ''}
            ${plateX != null ? `<span><span class="k">Plate (x,z)</span><span class="v">${plateX}, ${plateZ} m</span></span>` : ''}
            <span><span class="k">Dur</span><span class="v">${duration} s</span></span>
            <span><span class="k">RMS</span><span class="v">${rms} m</span></span>
          </div>` : '';
      return `
        <div class="event-item">
          ${toggle}
          <a class="event-row" href="/viewer/${sid}">
            <div class="event-top">
              <span class="sid">${sid}</span>
              <span class="event-paths">${pathChips}</span>
              <span class="quality chip ${qualityClass}" title="fit RMS quality">${qualityLabel}</span>
              <span class="chip ${esc(e.status || '')}">${esc(stat)}</span>
            </div>
            ${metricsRow}
          </a>
          <form class="event-delete-form" method="POST"
                action="/sessions/${sid}/delete"
                onsubmit="return confirm(${JSON.stringify(confirmMsg)});">
            <button class="event-delete" type="submit"
                    aria-label="Delete session ${sid}">&times;</button>
          </form>
        </div>`;
    }).join('');
    eventsBox.innerHTML = evHtml;
  }

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentCaptureMode = 'camera_only';
  let currentPreviewRequested = {};
  let currentCalibrationLastTs = {};

  // Keys used to skip re-renders when nothing changed. We compare serialised
  // state data rather than innerHTML strings because the browser re-serialises
  // HTML differently from the raw template literals we build.
  let _lastDevKey = null;
  let _lastSessKey = null;
  let _lastNavKey = null;
  let _lastEvKey = null;

  const _origRenderDevices = renderDevices;
  renderDevices = function(state) {
    const key = JSON.stringify({
      devices: (state.devices || []).map(d => ({ id: d.camera_id, ts: d.time_synced })),
      calibrations: (state.calibrations || []).slice().sort(),
      preview: state.preview_requested || {},
      last_ts: state.calibration_last_ts || {},
      sync_pending: Object.keys(state.sync_commands || {}).sort(),
    });
    if (key === _lastDevKey) return;
    _lastDevKey = key;
    _origRenderDevices(state);
  };

  const _origRenderSession = renderSession;
  renderSession = function(state) {
    const s = state.session;
    const sessKey = JSON.stringify({
      armed: !!(s && s.armed), id: s && s.id, mode: s && s.mode,
      capture_mode: state.capture_mode,
      paths: state.default_paths || [],
      live_session: state.live_session || null,
    });
    const cooldownBucket = Number(state.sync_cooldown_remaining_s || 0) > 0 ? 1 : 0;
    const navKey = JSON.stringify({
      online: (state.devices || []).length,
      cal: (state.calibrations || []).length,
      armed: !!(s && s.armed), id: s && s.id,
      syncing: !!state.sync, cooling: cooldownBucket,
    });
    if (sessKey === _lastSessKey && navKey === _lastNavKey) return;
    _lastSessKey = sessKey;
    _lastNavKey = navKey;
    _origRenderSession(state);
  };

  const _origRenderEvents = renderEvents;
  renderEvents = function(events) {
    const key = JSON.stringify((events || []).map(e => ({
      id: e.session_id, status: e.status, n: e.n_triangulated,
    })));
    if (key === _lastEvKey) return;
    _lastEvKey = key;
    _origRenderEvents(events);
  };

  async function tickStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      // /status does not include calibrations; merge the last-known set so
      // the devices card shows "calibrated" chips between calibration ticks.
      s.calibrations = currentCalibrations || [];
      currentDevices = s.devices || [];
      currentSession = s.session || null;
      currentCaptureMode = s.capture_mode || 'camera_only';
      currentPreviewRequested = s.preview_requested || {};
      renderDevices({
        devices: s.devices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        sync_commands: s.sync_commands || {},
        calibration_last_ts: currentCalibrationLastTs || {},
      });
      renderSession(s);
      // Telemetry: record per-cam WS latency sampled from /status.
      // Server-side ws_latency_ms reflects the last heartbeat round-trip
      // per the DeviceSocketManager snapshot.
      const nowMs = Date.now();
      for (const dev of (s.devices || [])) {
        if (!dev || !dev.camera_id) continue;
        const lat = dev.ws_latency_ms;
        if (typeof lat !== 'number') continue;
        const arr = latencySamples[dev.camera_id] = latencySamples[dev.camera_id] || [];
        arr.push({ t_ms: nowMs, latency: lat });
        while (arr.length && nowMs - arr[0].t_ms > TELEMETRY_WINDOW_MS) arr.shift();
      }
    } catch (e) { /* silent retry next tick */ }
  }

  // Digest of the last basePlot we actually repainted from. Calibrations
  // rarely change between 5 s ticks; skipping the Plotly.react call when
  // the payload is identical (same cameras, same poses) eliminates the
  // most-frequent opportunity for an accidental camera snap-back and
  // avoids ~ms of churn per tick.
  let lastBasePlotDigest = null;
  async function tickCalibration() {
    try {
      const r = await fetch('/calibration/state', { cache: 'no-store' });
      if (!r.ok) return;
      const payload = await r.json();
      currentCalibrations = (payload.calibrations || []).map(c => c.camera_id);
      currentCalibrationLastTs = {};
      for (const c of (payload.calibrations || [])) {
        if (c.last_ts != null) currentCalibrationLastTs[c.camera_id] = c.last_ts;
      }
      renderDevices({
        devices: currentDevices || [],
        calibrations: currentCalibrations,
        preview_requested: currentPreviewRequested,
        sync_commands: {},
        calibration_last_ts: currentCalibrationLastTs,
      });
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations, capture_mode: currentCaptureMode });
      // Update per-camera virt reprojection metadata from scene.cameras
      // (carries fx/fy/cx/cy/R_wc/t_wc/distortion/dims).
      virtCamMeta.clear();
      for (const c of ((payload.scene || {}).cameras || [])) {
        virtCamMeta.set(c.camera_id, c);
      }
      redrawAllVirtCanvases();
      redrawAllPreviewPlateOverlays();
      // Main 3D canvas lives only on `/`. Don't gate the metadata update
      // above on sceneRoot — `/setup` still needs virt canvases drawn.
      if (payload.plot && sceneRoot && window.Plotly) {
        const digest = JSON.stringify(payload.plot);
        if (digest !== lastBasePlotDigest || basePlot === null) {
          lastBasePlotDigest = digest;
          basePlot = payload.plot;
          repaintCanvas();
        }
      }
    } catch (e) { /* silent */ }
  }

  let currentEvents = [];
  async function tickEvents() {
    try {
      const r = await fetch('/events', { cache: 'no-store' });
      if (!r.ok) return;
      const events = await r.json();
      currentEvents = events;
      // Prune selection for sessions the user deleted server-side so the
      // canvas doesn't keep painting a phantom trajectory whose checkbox
      // no longer exists.
      const liveIds = new Set(events.map(e => e.session_id));
      let pruned = false;
      for (const sid of [...selectedTrajIds]) {
        if (!liveIds.has(sid)) {
          selectedTrajIds.delete(sid);
          trajCache.delete(sid);
          pruned = true;
        }
      }
      if (pruned) { persistTrajSelection(); repaintCanvas(); }
      renderEvents(events);
    } catch (e) { /* silent */ }
  }

  // Mode toggle: intercept form submit via fetch + optimistic update so
  // the button state never bounces back to the previous value between the
  // POST and the next tickStatus round-trip.
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (form.action && form.action.endsWith('/sessions/set_mode')) {
      e.preventDefault();
      const mode = (form.querySelector('input[name="mode"]') || {}).value;
      if (!mode) return;
      currentCaptureMode = mode;
      // Invalidate key so the next renderSession call repaints.
      _lastSessKey = null;
      renderSession({ devices: currentDevices || [], session: currentSession,
                      calibrations: currentCalibrations || [], capture_mode: currentCaptureMode });
      try { await fetch('/sessions/set_mode', { method: 'POST', body: new FormData(form) }); }
      catch (_) {}
      return;
    }
    // (Mutual-sync kickoff is handled on /sync now.)
  });

  // Live-preview toggle (Phase 4a). Click flips the server-side flag;
  // optimistic update paints immediately so there's no round-trip stall.
  const previewOn = new Set();
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-preview-cam]');
    if (!btn) return;
    const cam = btn.dataset.previewCam;
    const enabled = btn.dataset.previewEnabled !== '1';
    if (enabled) previewOn.add(cam); else previewOn.delete(cam);
    _lastDevKey = null;
    const merged = Object.fromEntries([...previewOn].map(c => [c, true]));
    currentPreviewRequested = merged;
    renderDevices({
      devices: currentDevices || [],
      calibrations: currentCalibrations || [],
      preview_requested: merged,
      sync_commands: {},
      calibration_last_ts: currentCalibrationLastTs || {},
    });
    try {
      await fetch('/camera/' + encodeURIComponent(cam) + '/preview_request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
    } catch (_) { /* next tick surfaces failure via /status */ }
  });

  // Keep server-side TTL alive. Server flag lapses in 5 s; 2 s refresh
  // absorbs one missed tick.
  async function tickPreviewRefresh() {
    for (const cam of previewOn) {
      try {
        await fetch('/camera/' + encodeURIComponent(cam) + '/preview_request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: true }),
        });
      } catch (_) { /* swallow; next tick retries */ }
    }
  }
  setInterval(tickPreviewRefresh, 2000);

  // Preview image polling. MJPEG streaming via <img> is flaky across
  // browsers (Chrome silently aborts when the server's first multipart
  // boundary doesn't land within a short window), so we bump a
  // cache-busting query-string on every <img data-preview-img> every
  // 200 ms — ~5 fps preview, trivial to debug via the Network tab, and
  // each frame is a normal GET /camera/{id}/preview that returns a
  // single JPEG or 404.
  function tickPreviewImages() {
    const t = Date.now();
    for (const img of document.querySelectorAll('img[data-preview-img]')) {
      const cam = img.dataset.previewImg;
      if (!cam) continue;
      // Panel is always present; only poll the <img> src when the
      // camera is actually preview-enabled. Off state stays on the
      // black placeholder.
      const panel = img.closest('.preview-panel');
      if (!panel || panel.classList.contains('off')) continue;
      img.src = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + t;
      img.style.opacity = 1;
    }
  }
  setInterval(tickPreviewImages, 200);

  // Per-camera mini 3D pose canvas — renders beside each preview panel.
  // Reuses `basePlot` (from /calibration/state) by keeping traces with
  // meta.camera_id == this cam PLUS shared world traces (no meta/camera_id).
  // Tiny Plotly react on each calibration tick; layout cached per host.
  // Per-camera 2D reprojection (K·[R|t]·P). Ported from the viewer's
  // drawVirtCanvas: project the home-plate pentagon through THIS camera's
  // own calibration so the dashed outline lands where the camera sees the
  // plate. If the reprojected outline doesn't sit on top of the plate in
  // the real preview above, calibration is off.
  {PLATE_WORLD_JS}
  // Populated by tickCalibration from /calibration/state `scene.cameras`.
  const virtCamMeta = new Map();
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
  {DRAW_PLATE_OVERLAY_JS}
  function drawVirtCanvas(canvas, cam) {
    return !!drawVirtualBase(canvas, cam);
  }
  function redrawAllVirtCanvases() {
    for (const canvas of document.querySelectorAll('[data-virt-canvas]')) {
      const cam = canvas.dataset.virtCanvas;
      const meta = virtCamMeta.get(cam);
      const cell = canvas.closest('.virt-cell');
      const ok = drawVirtCanvas(canvas, meta);
      if (cell) cell.classList.toggle('ready', ok);
    }
  }
  function redrawAllPreviewPlateOverlays() {
    for (const svg of document.querySelectorAll('[data-preview-overlay]')) {
      const cam = svg.dataset.previewOverlay;
      const meta = virtCamMeta.get(cam);
      redrawPlateOverlay(svg, meta);
    }
  }
  window.addEventListener('resize', () => {
    redrawAllVirtCanvases();
    redrawAllPreviewPlateOverlays();
  });

  // Prime all three immediately, then stagger polling so the UI stays
  // current without hammering the server. Status carries arming state
  // --- CALIBRATION card (Phase 5) -------------------------------------
  // Click "Auto calibrate" → POST /calibration/auto/<cam>. Optimistic:
  // button disables while in flight; toast on failure.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-auto-cal]');
    if (!btn) return;
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

  // Register extended markers from the picked camera.
  document.addEventListener('click', async (e) => {
    if (e.target && e.target.id === 'marker-register-btn') {
      const sel = document.getElementById('marker-register-cam');
      const cam = sel && sel.value;
      if (!cam) return;
      e.target.disabled = true;
      try {
        const r = await fetch('/calibration/markers/register/' + encodeURIComponent(cam),
                              { method: 'POST' });
        if (!r.ok) {
          let msg = 'Register failed';
          try { const body = await r.json(); if (body.detail) msg = body.detail; } catch (_) {}
          alert(msg);
        }
        tickExtendedMarkers();
      } finally {
        e.target.disabled = false;
      }
      return;
    }
    if (e.target && e.target.id === 'marker-clear-btn') {
      if (!confirm('Clear all extended markers?')) return;
      try {
        await fetch('/calibration/markers/clear', { method: 'POST',
          headers: { 'Content-Type': 'application/json' } });
      } catch (_) {}
      tickExtendedMarkers();
      return;
    }
    const remBtn = e.target.closest('[data-marker-remove]');
    if (remBtn) {
      const mid = remBtn.dataset.markerRemove;
      try {
        await fetch('/calibration/markers/' + encodeURIComponent(mid),
                    { method: 'DELETE' });
      } catch (_) {}
      tickExtendedMarkers();
    }
  });

  function renderExtendedMarkers(markers) {
    const listEl = document.getElementById('marker-list');
    if (!listEl) return;
    if (!markers || markers.length === 0) {
      listEl.innerHTML = '<div class="marker-list-empty">No extended markers registered.</div>';
      return;
    }
    const rows = markers.map(row => {
      const id = Number(row.id);
      const wx = Number(row.wx);
      const wy = Number(row.wy);
      const fmt = v => (v >= 0 ? '+' : '') + v.toFixed(3);
      return '<div class="marker-row">' +
             '<span class="mid">#' + id + '</span>' +
             '<span class="mxy">(' + fmt(wx) + ', ' + fmt(wy) + ') m</span>' +
             '<button type="button" data-marker-remove="' + id +
             '" title="Remove marker ' + id + '">&times;</button>' +
             '</div>';
    }).join('');
    listEl.innerHTML = '<div class="marker-list">' + rows + '</div>';
  }

  async function tickExtendedMarkers() {
    try {
      const r = await fetch('/calibration/markers', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderExtendedMarkers(body.markers || []);
    } catch (e) { /* silent */ }
  }

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
        if (!currentLiveSession || currentLiveSession.session_id !== data.sid) return;
        const done = new Set(currentLiveSession.paths_completed || []);
        done.add(data.path);
        currentLiveSession.paths_completed = [...done];
        renderActiveSession(currentLiveSession);
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
        const prev = wsStatus.get(data.cam);
        const connected = !!data.ws_connected;
        if (!prev || prev.connected !== connected) {
          wsStatus.set(data.cam, { connected, since_ms: Date.now() });
          if (!connected) recordError('ws_disconnect', `Cam ${data.cam} WebSocket dropped`);
        }
        updateDegradedBanner();
      } catch (_) {}
    });
  }

  // ------ Audio cues (opt-in via localStorage toggle) --------------------
  let audioCtx = null;
  function audioEnabled() {
    try { return localStorage.getItem('ball_tracker_audio_cues') === '1'; } catch { return false; }
  }
  function playCue(kind) {
    if (!audioEnabled()) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const freq = kind === 'armed' ? 220 : kind === 'ended' ? 440 : 150;
      const durS = kind === 'degraded' ? 0.2 : 0.08;
      osc.frequency.value = freq;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0.12, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + durS);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + durS);
    } catch (_) {}
  }

  // ------ Degraded banner: WS lost > grace window on an armed cam ---------
  let lastDegradedState = false;
  function updateDegradedBanner() {
    const banner = document.getElementById('degraded-banner');
    if (!banner) return;
    const now = Date.now();
    const armed = currentLiveSession && currentLiveSession.armed;
    const stale = [];
    for (const [cam, st] of wsStatus) {
      if (!st.connected && now - st.since_ms > WS_GRACE_MS) stale.push(cam);
    }
    const degraded = armed && stale.length > 0;
    if (degraded) {
      banner.style.display = 'flex';
      banner.querySelector('[data-degraded-body]').textContent =
        `Cam ${stale.join(', ')} WebSocket lost — falling back to post-pass. Next session will be 2-8s latency.`;
    } else {
      banner.style.display = 'none';
    }
    if (degraded && !lastDegradedState) playCue('degraded');
    lastDegradedState = degraded;
  }

  // ------ Telemetry panel -------------------------------------------------
  // Collapsible debug overlay bottom-left of canvas. Operator rarely looks
  // at it — it's a diagnostic when "feels slow" needs an evidence trail.
  // All metrics are derived client-side from existing SSE + /status signals;
  // no new server endpoints required.
  function percentile(arr, p) {
    if (!arr.length) return null;
    const sorted = [...arr].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(sorted.length * p)));
    return sorted[idx];
  }

  function drawTelemetrySpark(canvas, values, maxVal) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!values || values.length < 2) return;
    const maxY = maxVal !== undefined ? maxVal : Math.max(1, ...values);
    ctx.strokeStyle = '#4A6B8C';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - (Math.max(0, v) / maxY) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function sessionPathMatrix() {
    // Derived from the events list (most recent 10). Each cell shows
    // whether a given path completed for that session.
    const cells = [];
    const list = (currentEvents || []).slice(0, 10);
    for (const ev of list) {
      const paths = new Set(ev.paths_completed || []);
      cells.push({
        sid: ev.session_id,
        live: paths.has('live'),
        ios: paths.has('ios_post'),
        srv: paths.has('server_post'),
      });
    }
    return cells;
  }

  function renderTelemetry() {
    const box = document.getElementById('telemetry-body');
    if (!box) return;
    // Per-cam fps sparkline — reuse frame_samples on currentLiveSession
    const camRow = (cam) => {
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      const avg = fps.length ? fps.reduce((a,b)=>a+b,0) / fps.length : 0;
      const min = fps.length ? Math.min(...fps) : 0;
      return `
        <div class="tel-row">
          <span class="k">${cam} fps</span>
          <canvas class="tel-spark" data-tel-spark="${cam}"></canvas>
          <span class="v">avg ${avg.toFixed(0)} · min ${min.toFixed(0)}</span>
        </div>`;
    };
    // Pair rate: trailing-window count of pair timestamps over 1s
    const nowMs = Date.now();
    const pairsLast1s = pairTimestamps.filter(t => nowMs - t <= 1000).length;
    const pairsAvg = pairTimestamps.length / Math.max(1, TELEMETRY_WINDOW_MS / 1000);
    // Latency stats aggregated across cams
    const allLat = [];
    for (const cam of ['A','B']) {
      for (const s of (latencySamples[cam] || [])) allLat.push(s.latency);
    }
    const p50 = percentile(allLat, 0.50);
    const p95 = percentile(allLat, 0.95);
    const maxLat = allLat.length ? Math.max(...allLat) : null;
    const latTxt = p50 === null
      ? '—'
      : `p50 ${p50.toFixed(0)}ms · p95 ${p95.toFixed(0)}ms · max ${maxLat.toFixed(0)}ms`;
    // Path completion matrix
    const matrix = sessionPathMatrix();
    const matrixHtml = matrix.length
      ? matrix.map(c => `<span class="tel-cell" title="${esc(c.sid)}">${c.live?'L':'·'}${c.ios?'i':'·'}${c.srv?'s':'·'}</span>`).join('')
      : '<span class="tel-none">no sessions yet</span>';
    // Errors
    const errHtml = errorLog.length
      ? errorLog.map(e => {
          const ts = new Date(e.t_ms).toLocaleTimeString();
          return `<div class="tel-err"><span class="t">${ts}</span> <span class="msg">${esc(e.message)}</span></div>`;
        }).join('')
      : '<span class="tel-none">none</span>';
    box.innerHTML = `
      ${camRow('A')}
      ${camRow('B')}
      <div class="tel-row">
        <span class="k">Pairs</span>
        <span class="v">${pairsLast1s}/s · avg ${pairsAvg.toFixed(1)}/s</span>
      </div>
      <div class="tel-row">
        <span class="k">WS latency</span>
        <span class="v">${latTxt}</span>
      </div>
      <div class="tel-block">
        <span class="k">Last 10 sessions (L/i/s)</span>
        <div class="tel-matrix">${matrixHtml}</div>
      </div>
      <div class="tel-block">
        <span class="k">Errors</span>
        <div class="tel-errors">${errHtml}</div>
      </div>`;
    // Draw sparklines after DOM replacement
    ['A','B'].forEach(cam => {
      const canvas = box.querySelector(`[data-tel-spark="${cam}"]`);
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      drawTelemetrySpark(canvas, fps, 240);
    });
  }

  // 10 Hz tick for the time-sensitive active-session fields (elapsed
  // counter + last-point-age). Cheaper than re-rendering the whole card
  // on every SSE event, and ensures the "stale" flag trips within 100 ms
  // of the 200 ms threshold being crossed.
  function tickActiveSession() {
    if (!currentLiveSession || !currentLiveSession.armed) return;
    const elapsedEl = activeBox && activeBox.querySelector('[data-elapsed]');
    if (elapsedEl && currentLiveSession.armed_at_ms) {
      elapsedEl.textContent = fmtElapsed(Date.now() - currentLiveSession.armed_at_ms);
    }
    // Re-evaluate stale flag without a full re-render
    const pairsEl = activeBox && activeBox.querySelector('.live-pairs');
    if (pairsEl && currentLiveSession.last_point_at_ms) {
      const age = Date.now() - currentLiveSession.last_point_at_ms;
      pairsEl.classList.toggle('stale', age > 200);
    }
  }

  // (1 s) and is the only high-frequency tick.
  initLiveStream();
  tickStatus();
  tickCalibration();
  tickEvents();
  tickExtendedMarkers();
  setInterval(tickStatus, 1000);
  setInterval(tickCalibration, 5000);
  setInterval(tickEvents, 5000);
  setInterval(tickExtendedMarkers, 5000);
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
})();
"""


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render_device_rows(
    devices: list[dict[str, Any]],
    calibrations: list[str],
    calibration_last_ts: dict[str, float] | None = None,
    preview_requested: dict[str, bool] | None = None,
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
        preview_on = bool(preview_requested.get(cam_id))
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
        preview_btn = (
            f'<button type="button" class="btn small preview-btn{" active" if preview_on else ""}" '
            f'data-preview-cam="{html.escape(cam_id)}" '
            f'data-preview-enabled="{1 if preview_on else 0}">'
            f'{"PREVIEW ON" if preview_on else "PREVIEW"}</button>'
        )
        auto_cal_btn = (
            f'<button type="button" class="btn small" '
            f'data-auto-cal="{html.escape(cam_id)}">Run auto-cal</button>'
        )
        compare_block = render_live_compare_camera(
            cam_id,
            preview_src=(f"/camera/{html.escape(cam_id)}/preview" if preview_on else ""),
            preview_placeholder=("…" if preview_on else "Preview off"),
            virt_placeholder=("loading…" if is_cal else "not calibrated"),
            preview_off=not preview_on,
        )
        return (
            f'<div class="device">'
            f'<div class="device-head">'
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
    sync_btn = (
        '<form class="inline" method="POST" action="/sync/trigger">'
        f'<button class="btn secondary" type="submit"{" disabled" if armed else ""}>Calibrate time</button>'
        "</form>"
    )
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
        f'<div class="session-actions">{arm_btn}{stop_btn}{sync_btn}{clear_btn}</div>'
        f'{gate_row}'
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
            f'<span class="chip {status}">{stat_label}</span>'
            f"</div>"
            f"{stats_html}"
            f"</a>"
            f'<form class="event-delete-form" method="POST" '
            f'action="/sessions/{sid}/delete" '
            f'onsubmit="return confirm(\'刪除 session {sid}？此動作無法復原。\');">'
            f'<button class="event-delete" type="submit" '
            f'aria-label="Delete session {sid}">&times;</button>'
            f"</form>"
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
    session_html = (
        f'<span class="val armed">{html.escape(session.get("id", "—"))}</span>'
        if armed
        else '<span class="val idle">idle</span>'
    )
    def _count_cls(n: int) -> str:
        return "full" if n >= 2 else "partial"
    dev_cls = _count_cls(len(devices))
    cal_cls = _count_cls(len(calibrations))
    if sync is not None:
        sync_label, sync_cls = "syncing", "armed"
    elif sync_cooldown_remaining_s > 0.0:
        sync_label, sync_cls = "cooldown", "partial"
    else:
        sync_label, sync_cls = "idle", "idle"
    return (
        f'<span class="pair"><span class="label">Devices</span><span class="val {dev_cls}">{len(devices)}/2</span></span>'
        f'<span class="pair"><span class="label">Calibrated</span><span class="val {cal_cls}">{len(calibrations)}/2</span></span>'
        f'<span class="pair"><span class="label">Session</span>{session_html}</span>'
        f'<span class="pair"><span class="label">Sync</span><span class="val {sync_cls}">{sync_label}</span></span>'
        f'<a class="nav-link" href="/setup">Setup</a>'
        f'<a class="nav-link" href="/markers">Markers</a>'
    )


def _render_tuning_body(
    chirp_detect_threshold: float,
    heartbeat_interval_s: float,
    tracking_exposure_cap: str = "frame_duration",
    capture_height_px: int = 1080,
) -> str:
    """Two linked slider + number-input rows. Each form posts on
    submit — the `<input>`s share a `form` attribute and an `oninput`
    handler that mirrors slider <-> number, so the operator sees the
    number update as they drag. Submit fires on the change event after
    release (slider) or blur / Enter (number)."""
    thr = f"{chirp_detect_threshold:.2f}"
    ivl = f"{heartbeat_interval_s:g}"
    return (
        # Chirp threshold row.
        '<form class="tuning-row" method="POST" '
        'action="/settings/chirp_threshold" id="tuning-chirp-form">'
        '<span class="tuning-label">Chirp thr</span>'
        f'<input type="range" name="threshold" min="0.05" max="0.60" step="0.01" '
        f'value="{thr}" '
        'oninput="document.getElementById(\'tuning-chirp-num\').value=this.value" '
        'onchange="this.form.submit()">'
        f'<input type="number" id="tuning-chirp-num" name="threshold" '
        f'min="0.05" max="0.60" step="0.01" value="{thr}" '
        'form="tuning-chirp-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.submit()">'
        '</form>'
        # Heartbeat interval row.
        '<form class="tuning-row" method="POST" '
        'action="/settings/heartbeat_interval" id="tuning-hb-form">'
        '<span class="tuning-label">Heartbeat</span>'
        f'<input type="range" name="interval_s" min="1" max="10" step="0.5" '
        f'value="{ivl}" '
        'oninput="document.getElementById(\'tuning-hb-num\').value=this.value" '
        'onchange="this.form.submit()">'
        f'<input type="number" id="tuning-hb-num" name="interval_s" '
        f'min="1" max="10" step="0.5" value="{ivl}" '
        'form="tuning-hb-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.submit()">'
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


def render_events_index_html(
    events: list[dict[str, Any]],
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
        "</head><body>"
        '<nav class="nav">'
        '<span class="brand"><span class="dot"></span>BALL_TRACKER</span>'
        f'<div class="status-line" id="nav-status">{_render_nav_status(devices, session, calibrations, sync, sync_cooldown_remaining_s)}</div>'
        "</nav>"
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
        '<h2 class="card-title">Events</h2>'
        f'<div id="events-body">{_render_events_body(events)}</div>'
        "</div>"
        "</aside>"
        '<section class="canvas">'
        '<div id="degraded-banner" class="degraded-banner" role="alert" style="display:none">'
        '  <span class="degraded-icon">⚠</span>'
        '  <span data-degraded-body>Live stream degraded.</span>'
        '</div>'
        '<details id="telemetry-panel" class="telemetry-panel">'
        '  <summary>TELEMETRY</summary>'
        '  <div id="telemetry-body" class="telemetry-body"></div>'
        '</details>'
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
