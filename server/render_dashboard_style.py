"""Dashboard-specific CSS. Design tokens (`:root` block) are imported
from render_shared so the dashboard and the simpler shells stay in sync
— component CSS (.nav, .device, etc.) legitimately diverges per page,
so only the tokens are shared, not the whole stylesheet."""
from __future__ import annotations

from cam_view_ui import CAM_VIEW_FULL_CSS
from render_shared import _SHARED_LAYOUT_NAV_CSS, _TOKENS_CSS


# Dashboard-specific overrides on top of the shared layout/nav body.
# Four extra status-* rules render the dashboard's editorial nav strip
# (status-main / status-badge / status-headline / status-context); the
# .layout override pins the dashboard to the viewport so the canvas
# can host a fixed-height Plotly scene without document scroll. Both
# arrive AFTER _SHARED_LAYOUT_NAV_CSS in the CSS cascade so they win.
_DASHBOARD_NAV_OVERRIDES_CSS = """
.nav .status-main { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
                    font-family: var(--mono); text-transform: uppercase; }
.nav .status-badge { display: inline-flex; align-items: center; padding: 3px 8px;
                     border: 1px solid var(--border-base); border-radius: var(--r);
                     font-size: 10px; letter-spacing: 0.12em; color: var(--sub); }
.nav .status-badge.ready, .nav .status-badge.recording {
  color: var(--passed); border-color: var(--passed); background: var(--passed-bg);
}
.nav .status-badge.blocked, .nav .status-badge.cooldown {
  color: var(--warn); border-color: var(--warn); background: var(--warn-bg);
}
.nav .status-badge.syncing {
  color: var(--ink); border-color: var(--ink); background: rgba(42,37,32,.04);
}
.nav .status-headline { font-size: 12px; letter-spacing: 0.12em; color: var(--ink); }
.nav .status-context { font-size: 10px; letter-spacing: 0.08em; color: var(--sub); }

/* Dashboard pins the layout to the viewport (no document scroll) so the
   canvas can host the fixed-height Plotly scene. The shared body uses
   `min-height: 100vh` for /markers + /sync where the sidebar grows. */
.layout { height: 100vh; box-sizing: border-box; overflow: hidden; }
"""


_CSS = f"""
{_TOKENS_CSS}
{_SHARED_LAYOUT_NAV_CSS}
{_DASHBOARD_NAV_OVERRIDES_CSS}

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
.device-head .chip-col {{ grid-column: 4; grid-row: 1; justify-self: end;
                          display: flex; flex-direction: row; gap: var(--s-2);
                          align-items: center; }}
.chip.battery {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.04em; }}
.chip.battery.ok {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}
.chip.battery.mid {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
.chip.battery.low {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
.chip.battery.charging {{ color: var(--accent); border-color: var(--accent); background: transparent; }}
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
                      display: flex; align-items: flex-start; gap: var(--s-2);
                      padding: 3px 8px; border-radius: var(--r);
                      white-space: normal; word-break: break-word;
                      line-height: 1.35; }}
.device .sub .item .dot {{ margin-top: 4px; }}
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
.sync-age {{ font-family: var(--mono); font-size: 10px; color: var(--sub);
             margin-right: 4px; letter-spacing: 0.04em; }}
.sidebar .session-actions button.btn {{ padding: 7px 12px; }}
.sidebar .arm-gate {{ margin-top: 8px; font-size: 11px; line-height: 1.45; color: var(--ink); }}
.sidebar .gate-label {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.10em;
                        text-transform: uppercase; color: var(--sub); margin-right: 6px; }}
.sidebar .arm-error {{ margin-top: 6px; font-size: 11px; line-height: 1.45;
                       color: var(--danger, #B14343); font-family: var(--mono); }}
.sidebar .paths-stack {{ gap: 10px; margin-top: 12px; }}
.sidebar .path-option {{ padding: 6px 8px; }}
.sidebar .paths-actions {{ margin-top: 10px; }}
.hsv-form {{ display: flex; flex-direction: column; gap: var(--s-3); }}
.hsv-presets {{ display: flex; gap: var(--s-2); flex-wrap: wrap; }}
.hsv-grid {{ display: flex; flex-direction: column; gap: var(--s-2); }}
.hsv-row {{ display: grid; grid-template-columns: 20px minmax(0, 1fr); gap: var(--s-2); align-items: start; }}
.hsv-label {{ font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--sub); padding-top: 8px; }}
.hsv-pair {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s-2); }}
.hsv-pair label {{ display: grid; grid-template-columns: 30px minmax(0, 1fr) 56px; gap: 6px; align-items: center; }}
.hsv-pair label span {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em; color: var(--sub); text-transform: uppercase; }}
.hsv-pair input[type="range"] {{ width: 100%; margin: 0; accent-color: var(--ink); }}
.hsv-num {{ width: 100%; min-width: 0; padding: 6px 8px; border: 1px solid var(--border-base); border-radius: var(--r); background: var(--surface); color: var(--ink); font-family: var(--mono); font-size: 11px; }}
.hsv-actions {{ display: flex; justify-content: flex-end; }}
.shape-gate-form {{ margin-top: 0; padding-top: 0; border-top: 0; }}
.tune-section {{ border-top: 1px solid var(--border-l); padding: var(--s-2) 0; }}
.tune-section:first-of-type {{ border-top: 0; padding-top: 0; }}
.tune-section > summary {{ cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline;
                            padding: 4px 0; font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em; text-transform: uppercase; color: var(--sub); }}
.tune-section > summary::-webkit-details-marker {{ display: none; }}
.tune-section > summary::after {{ content: '▸'; color: var(--sub); margin-left: var(--s-2); }}
.tune-section[open] > summary::after {{ content: '▾'; color: var(--ink); }}
.tune-section[open] > summary {{ color: var(--ink); }}
.tune-section .tune-name {{ font-weight: 600; }}
.tune-section .tune-summary {{ color: var(--sub); font-size: 10px; letter-spacing: 0.04em; text-transform: none; }}
.tune-section[open] .tune-summary {{ display: none; }}
.tune-section > .hsv-form {{ margin-top: var(--s-2); }}
.hsv-subtitle {{ font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--sub); }}
.detection-identity {{ display: flex; align-items: center; gap: var(--s-2);
    padding: 6px 0 var(--s-2); border-bottom: 1px solid var(--border-l);
    margin-bottom: var(--s-2); }}
.identity-tag {{ font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
    text-transform: uppercase; padding: 4px 10px; border-radius: var(--r);
    border: 1px solid var(--border-base); }}
.identity-pure {{ color: var(--passed, var(--ink)); border-color: var(--passed, var(--ink)); }}
.identity-modified {{ color: var(--ink); border-color: var(--ink);
    background: var(--surface); }}
.identity-deleted {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
.hsv-library-actions {{ display: flex; gap: var(--s-2); margin-top: var(--s-2); padding-top: var(--s-2);
    border-top: 1px solid var(--border-l); }}
.preset-modal {{ width: min(680px, 92vw); padding: var(--s-3);
    border: 1px solid var(--border-base); border-radius: var(--r);
    background: var(--surface); color: var(--ink); }}
.preset-modal::backdrop {{ background: rgba(0,0,0,0.4); }}
.preset-modal-head {{ display: flex; justify-content: space-between; align-items: center;
    margin-bottom: var(--s-2); }}
.preset-modal-head h3 {{ margin: 0; font-family: var(--mono); font-size: 13px;
    text-transform: uppercase; letter-spacing: 0.10em; color: var(--ink); }}
.preset-modal-status {{ font-family: var(--mono); font-size: 10px;
    color: var(--sub); margin-top: var(--s-2); min-height: 1.2em; }}
.preset-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.preset-table th {{ text-align: left; padding: 6px 8px; font-family: var(--mono);
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em; color: var(--sub);
    border-bottom: 1px solid var(--border-l); }}
.preset-table td {{ padding: 6px 8px; border-bottom: 1px solid var(--border-l); vertical-align: middle; }}
.preset-table code {{ font-family: var(--mono); font-size: 11px; color: var(--ink); }}
.preset-current-tag {{ font-family: var(--mono); font-size: 9px; color: var(--accent); margin-left: 4px; }}
.preset-actions {{ display: flex; gap: 4px; justify-content: flex-end; }}
.preset-empty {{ color: var(--sub); padding: 12px; text-align: center; }}
.detection-section {{ border-top: 1px solid var(--border-l); padding-top: var(--s-2);
    margin-top: var(--s-2); }}
.detection-section:first-of-type {{ border-top: 0; padding-top: 0; margin-top: 0; }}
.detection-apply-status {{ font-family: var(--mono); font-size: 10px;
    color: var(--sub); margin-left: var(--s-2); align-self: center; }}
.shape-row {{ display: grid; grid-template-columns: 60px minmax(0, 1fr) 56px; gap: var(--s-2); align-items: center; }}
.shape-label {{ font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em; text-transform: uppercase; color: var(--sub); }}
.shape-row input[type="range"] {{ width: 100%; margin: 0; accent-color: var(--ink); }}
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
.postpass-chip.stopped {{ color:var(--sub); border-style:dashed; }}
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
.path-chip.err {{ color:var(--dev); border-color:var(--dev); background:rgba(192, 57, 43, 0.08); }}
/* Frame count suffix inside a chip: dimmer than the label so the eye still
   parses it as "L 67" (pipeline + number) rather than two equal tokens. */
.path-chip .pc {{ margin-left:4px; padding-left:4px; border-left:1px solid currentColor;
                   font-size:9px; letter-spacing:0; opacity:0.75;
                   font-variant-numeric:tabular-nums; text-transform:none; }}
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
{CAM_VIEW_FULL_CSS}

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

/* --- Events list (redesigned). 3-line card per session, lines 2/3
   collapse when empty. Line 1 = identity + status. Line 2 = pipe chips
   + metrics (wraps cleanly if it overflows the sidebar). Line 3 =
   actions (right-aligned). All flexbox, no grid — the prior CSS-grid
   layout clipped chips when the rightmost column ate horizontal width. */
.events-empty {{ color: var(--sub); font-size: 12px; padding: var(--s-3) 0;
                 font-style: italic; font-family: var(--mono); }}
.events-toolbar {{ display:flex; align-items:center; justify-content:space-between;
                   gap:var(--s-2); margin-bottom:var(--s-2); }}
.events-filters {{ display:flex; gap:6px; }}
.events-filter {{ background:transparent; border:1px solid var(--border-base);
                  color:var(--sub); font-family:var(--mono); font-size:10px;
                  letter-spacing:0.10em; text-transform:uppercase;
                  padding:4px 8px; border-radius:var(--r); cursor:pointer; }}
.events-filter.active {{ background:var(--ink); color:var(--surface); border-color:var(--ink); }}

.event-day {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
              text-transform: uppercase; color: var(--sub);
              padding: 10px var(--s-1) 4px;
              border-bottom: 1px solid var(--border-l);
              margin-top: 8px; }}
.event-day:first-child {{ margin-top: 0; }}

/* Whole row is the dashboard-3D-load click target (40_traj_handlers.js).
   .selected highlights the active overlay; only the explicit "→ viewer"
   link and any in-row form/button escape the row click. */
.event-item {{ padding: 8px var(--s-1);
               border-top: 1px solid var(--border-l);
               transition: background 0.12s ease; min-width: 0;
               cursor: pointer; }}
.event-item:first-child,
.event-day + .event-item {{ border-top: 0; }}
.event-item:hover {{ background: var(--surface-hover); }}
.event-item.selected {{ background: var(--surface-2); }}
.event-item.selected:hover {{ background: var(--surface-hover); }}

.ev-row1, .ev-row2, .ev-row3 {{ display: flex; align-items: center;
                                gap: 8px; min-width: 0; flex-wrap: wrap;
                                row-gap: 4px; }}
.ev-row2, .ev-row3 {{ margin-top: 5px; }}
.ev-row3 {{ justify-content: flex-end; }}
.ev-spacer {{ flex: 1 1 auto; min-width: 4px; }}

.ev-time {{ font-family: var(--mono); font-size: 12px; font-weight: 600;
            color: var(--ink); letter-spacing: 0.02em;
            font-variant-numeric: tabular-nums; flex: 0 0 auto; }}
.ev-sid  {{ font-family: var(--mono); font-size: 11px; color: var(--sub);
            letter-spacing: 0.04em; flex: 0 0 auto;
            white-space: nowrap; }}

.ev-viewer-link {{ font-family: var(--mono); font-size: 10px;
                   letter-spacing: 0.06em; color: var(--sub);
                   text-decoration: none; flex: 0 0 auto;
                   padding: 2px 6px; border: 1px solid var(--border-base);
                   border-radius: var(--r); }}
.ev-viewer-link:hover {{ color: var(--ink); border-color: var(--ink); }}

/* Has-traj indicator. Pure visual — the row itself is the click target
   (40_traj_handlers.js). Selected rows get a filled dot; unselected
   rows with traj show as outline; rows without traj show .swatch-empty. */
.swatch,
.swatch-empty {{ width: 12px; height: 12px; border-radius: 50%;
                 border: 1px solid var(--accent, #C0392B);
                 display: inline-block; flex: 0 0 auto;
                 background: transparent; }}
.swatch-empty {{ border-color: rgba(0,0,0,0.18); opacity: 0.45; }}
.swatch.selected {{ background: var(--accent, #C0392B); }}

.ev-statuses {{ display: flex; gap: 4px; flex: 0 0 auto;
                justify-content: flex-end; }}
.ev-statuses .chip {{ font-size: 9px; padding: 1px 6px;
                       letter-spacing: 0.08em; white-space: nowrap; }}

.ev-pipes {{ display: flex; gap: 5px; flex: 0 0 auto; flex-wrap: wrap;
             row-gap: 4px; }}
.ev-pipe {{ font-family: var(--mono); font-size: 10px; line-height: 1;
            padding: 3px 7px; border: 1px solid var(--border-base);
            border-radius: var(--r); color: var(--sub);
            letter-spacing: 0.04em; white-space: nowrap;
            font-variant-numeric: tabular-nums; }}
.ev-pipe b {{ font-weight: 600; color: var(--sub-strong, var(--ink));
              margin-left: 5px; opacity: 0.85; }}
.ev-pipe.on {{ color: var(--passed); border-color: var(--passed);
                background: var(--passed-bg); }}
.ev-pipe.on b {{ color: var(--passed); opacity: 1; }}
.ev-pipe.err {{ color: var(--failed); border-color: var(--failed);
                 background: var(--failed-bg); }}
.ev-pipe.err b {{ color: var(--failed); opacity: 1; }}
/* In-flight live session: pulsing dot to signal frames are still
   arriving over WS; counter `b` keeps tabular-nums so the changing
   digit doesn't reflow the chip. */
.ev-pipe.streaming {{ color: var(--accent, #2c7be5);
                       border-color: var(--accent, #2c7be5);
                       animation: ev-pipe-pulse 1.4s ease-in-out infinite; }}
.ev-pipe.streaming b {{ color: var(--accent, #2c7be5); opacity: 1; }}
/* Armed but no frame yet — placeholder dim, no animation. Distinct
   from "-" (never ran) so the operator sees the session exists. */
.ev-pipe.armed {{ color: var(--sub); border-style: dashed; opacity: 0.75; }}
@keyframes ev-pipe-pulse {{
  0%,100% {{ opacity: 1; }}
  50%     {{ opacity: 0.55; }}
}}

.ev-action-form {{ margin: 0; }}
.ev-btn {{ background: transparent; border: 1px solid var(--border-base);
            color: var(--sub); font-family: var(--mono); font-size: 9px;
            letter-spacing: 0.08em; text-transform: uppercase;
            line-height: 1; padding: 5px 9px; border-radius: var(--r);
            cursor: pointer; white-space: nowrap; text-decoration: none;
            display: inline-block;
            transition: border-color 0.15s, color 0.15s, background 0.15s; }}
.ev-btn.warn:hover {{ border-color: var(--warn); color: var(--warn); }}
.ev-btn.dev:hover {{ border-color: var(--dev); color: var(--dev); }}
.ev-btn.ok:hover {{ border-color: var(--passed); color: var(--passed); }}
.ev-btn.accent:hover {{ border-color: var(--ink); color: var(--ink); }}

.chip.processing {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
.chip.queued {{ color: var(--sub); border-color: var(--border-base); background: transparent; }}
.chip.canceled {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
.chip.completed {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}

/* Ambient orange pulse while server_post is in-flight; complements the
   inline `processing` chip with a row-level cue visible from across the
   sidebar. Border-left adds a 2 px stripe; padding subtracts the same
   so content doesn't shift on toggle. */
.event-item.processing {{ border-left: 2px solid var(--warn);
                           padding-left: calc(var(--s-1) - 2px);
                           animation: rs-pulse 1.5s ease-in-out infinite; }}
@keyframes rs-pulse {{
  0%, 100% {{ background: transparent; }}
  50%      {{ background: var(--warn-bg); }}
}}
.event-item.flash-done {{ animation: rs-flash 0.6s ease-out; }}
@keyframes rs-flash {{
  0%   {{ background: rgba(34,197,94,0.22); }}
  100% {{ background: transparent; }}
}}

/* --- Intrinsics (ChArUco) card --- */
.intrinsics-roles {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:var(--s-2); }}
.intrinsics-list {{ display:flex; flex-direction:column; gap:6px;
                    margin-bottom:var(--s-2); }}
.intrinsics-row {{ border:1px solid var(--border-base); border-radius:var(--r);
                   padding:var(--s-2); background:var(--surface); }}
.intrinsics-row-top {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.intrinsics-row-top .dev-id {{ font-family:var(--mono); font-size:12px;
                               font-weight:500; color:var(--ink); }}
.intrinsics-row-top .dev-model {{ font-family:var(--mono); font-size:10px;
                                  color:var(--sub); }}
.intrinsics-row-top .dim {{ font-family:var(--mono); font-size:9px;
                            color:var(--sub); letter-spacing:0.06em; }}
.intrinsics-row-top .chip.small {{ font-size:9px; padding:1px 6px; }}
.intrinsics-row-top .btn.danger {{ margin-left:auto; color:var(--failed);
                                    border-color:var(--failed); }}
.intrinsics-row-top .btn.danger:hover {{ background:var(--failed-bg); }}
.intrinsics-row-sub {{ margin-top:4px; font-family:var(--mono); font-size:10px;
                       color:var(--sub); letter-spacing:0.02em; }}
.intrinsics-upload {{ border-top:1px dashed var(--border-base);
                      padding-top:var(--s-2); }}
.intrinsics-upload-row {{ display:flex; gap:6px; align-items:center;
                          flex-wrap:wrap; }}
.intrinsics-upload-row select {{ font-family:var(--mono); font-size:10px;
                                  padding:3px 6px; border:1px solid var(--border-base);
                                  border-radius:var(--r); background:var(--surface);
                                  color:var(--ink); }}
.intrinsics-upload-row input[type=file] {{ font-family:var(--mono); font-size:9px;
                                            color:var(--sub); }}
.intrinsics-upload-status {{ margin-top:6px; font-family:var(--mono); font-size:10px;
                              min-height:14px; }}
.intrinsics-upload-status.ok {{ color:var(--passed); }}
.intrinsics-upload-status.err {{ color:var(--failed); }}

/* --- View-preset toolbar (ISO/CATCH/SIDE/TOP/PITCHER) — pinned top-left
   over the 3D scene. Mirrors viewer's `.scene-col .scene-views` styling
   so the two surfaces read identically. */
.scene-views {{ position: absolute; top: var(--s-4); left: var(--s-4); z-index: 6;
                display: inline-flex; align-items: stretch; flex-wrap: nowrap;
                white-space: nowrap; border: 1px solid var(--border-base);
                border-radius: var(--r); overflow: hidden; background: var(--surface); }}
.scene-views .view-preset {{ padding: 5px 10px; border: none; background: transparent;
                              color: var(--sub); cursor: pointer; min-width: auto;
                              border-radius: 0; font: inherit;
                              font-family: var(--mono); font-size: 10px;
                              letter-spacing: 0.12em; text-transform: uppercase;
                              font-weight: 500; line-height: 1; }}
.scene-views .view-preset + .view-preset {{ border-left: 1px solid var(--border-l); }}
.scene-views .view-preset:hover {{ color: var(--ink); }}
.scene-views .view-preset.active {{ background: var(--ink); color: var(--surface); }}

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
/* --- Latest pitch speed badge — pinned bottom-left of scene-root.
   Top-left is the view-preset toolbar (mirrors viewer layout). */
.latest-pitch-badge {{ position: absolute; left: var(--s-4); bottom: var(--s-4); z-index: 6;
                       display: inline-flex; align-items: baseline; gap: 6px;
                       background: var(--surface); border: 1px solid var(--border-base);
                       border-radius: var(--r); padding: 6px var(--s-3);
                       font-family: var(--mono); color: var(--ink);
                       box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
.latest-pitch-badge[hidden] {{ display: none; }}
.latest-pitch-badge .lpb-speed {{ font-size: 28px; font-weight: 600;
                                  font-variant-numeric: tabular-nums;
                                  letter-spacing: 0.02em; line-height: 1; }}
.latest-pitch-badge .lpb-units {{ font-size: 11px; color: var(--sub);
                                  letter-spacing: 0.12em; text-transform: uppercase; }}
.latest-pitch-badge .lpb-meta {{ font-size: 10px; color: var(--sub);
                                 letter-spacing: 0.06em; padding-left: 6px;
                                 border-left: 1px solid var(--border-base); margin-left: 4px; }}

/* --- Fit filter bar (bottom-right; top-right is Plotly modebar) --- */
.fit-filter-bar {{ position: absolute; right: var(--s-4); bottom: var(--s-4); z-index: 6;
                   display: inline-flex; gap: var(--s-2); font-family: var(--mono); font-size: 10px;
                   letter-spacing: 0.08em; background: var(--surface);
                   border: 1px solid var(--border-base); border-radius: var(--r);
                   padding: var(--s-1) var(--s-2); }}
.fit-filter-bar .ff-cell {{ display: inline-flex; align-items: center; gap: var(--s-2);
                            padding: 0 var(--s-2); }}
.fit-filter-bar .ff-cell + .ff-cell {{ border-left: 1px solid var(--border-base); }}
.fit-filter-bar .ff-name {{ color: var(--ink); text-transform: uppercase; font-weight: 500; }}
.fit-filter-bar input[type="range"] {{ width: 90px; height: 14px; }}
.fit-filter-bar .ff-readout {{ color: var(--sub); min-width: 56px; text-align: right; }}
.fit-filter-bar .ff-src-pill {{ font: inherit; font-size: 10px; letter-spacing: 0.08em;
                                padding: 2px 8px; background: transparent; color: var(--sub);
                                border: 1px solid var(--border-base); border-radius: 2px;
                                cursor: pointer; text-transform: lowercase; }}
.fit-filter-bar .ff-src-pill[aria-pressed="true"] {{ background: var(--ink); color: var(--surface);
                                                    border-color: var(--ink); }}
.fit-filter-bar .ff-src-pill[disabled] {{ opacity: 0.35; cursor: not-allowed; }}
.fit-filter-bar .ff-checkbox {{ display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}
.fit-filter-bar .ff-checkbox input {{ accent-color: var(--ink); cursor: pointer; }}

@media (max-width: 1100px) {{
  .nav {{ padding-left: 16px; padding-right: 16px; }}
  .nav-main {{ grid-template-columns: 1fr; }}
  .nav-tabs {{ justify-content: flex-start; }}
  .nav-status-row {{ justify-content: flex-start; }}
  .nav .status-line {{ align-items: flex-start; min-width: 0; }}
  .nav .status-checks {{ justify-content: flex-start; }}
}}
"""
