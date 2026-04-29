"""Sync/setup page-specific CSS additions."""
from __future__ import annotations

# Sync-page-only additions on top of the shared _CSS: a single-column
# main-area (no sidebar), the trace plot container sizing, and the nav
# link + sync-chip styles introduced here (mirrored into the dashboard's
# nav via render_dashboard_page.py so the link can be rendered there too).
_SYNC_CSS = """
.main-sync {
  max-width: 1100px; margin: 0 auto;
  padding: calc(var(--nav-offset) + var(--s-5)) var(--s-4) var(--s-5) var(--s-4);
  display: flex; flex-direction: column; gap: var(--s-3);
}
.page-hero {
  display: flex; align-items: end; justify-content: space-between; gap: var(--s-3);
  flex-wrap: wrap;
}
.page-hero-copy { display: flex; flex-direction: column; gap: 8px; max-width: 720px; }
.page-kicker {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sub);
}
.page-title {
  font-family: var(--mono); font-size: 28px; line-height: 1.05; letter-spacing: 0.02em;
  color: var(--ink); margin: 0;
}
.page-copy { color: var(--ink-light); max-width: 720px; }
.setup-section-title {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sub);
  margin: var(--s-4) 0 calc(-1 * var(--s-2)) var(--s-1);
}
.setup-section-title:first-child { margin-top: 0; }
.wav-link {
  color: var(--ink); text-decoration: underline; text-decoration-style: dotted;
  text-underline-offset: 2px; font-family: var(--mono); font-size: 11px;
}
.wav-link:hover { color: var(--accent); }
.trace-empty {
  padding: var(--s-3) var(--s-2);
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--sub);
}
.main-sync .camera-compare .preview-panel .placeholder {
  display: none;
}
.camera-compare {
  display: flex; flex-direction: column; gap: 12px;
}
.camera-compare-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px;
}
.compare-cell-label {
  position: absolute; left: 12px; top: 12px; z-index: 3;
  padding: 4px 8px; border: 1px solid rgba(255,255,255,0.2);
  background: rgba(0,0,0,0.55); color: rgba(255,255,255,0.85);
  font-family: var(--mono); font-size: 10px;
  letter-spacing: 0.12em; text-transform: uppercase;
}
.devices-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
                 gap: var(--s-3); }
.device { padding: var(--s-2); border: 1px solid var(--border-l);
          border-radius: var(--r); background: var(--surface); }
@media (max-width: 900px) {
  .devices-grid { grid-template-columns: 1fr; }
}
.device-head { display: grid; grid-template-columns: 14px 28px 1fr auto;
               align-items: center; gap: var(--s-2); }
.device-head .sync-led { grid-column: 1; grid-row: 1;
                         width: 10px; height: 10px; border-radius: 50%;
                         background: var(--border-base); }
.device-head .sync-led.synced { background: var(--passed);
                                box-shadow: 0 0 6px rgba(125,255,192,0.5); }
.device-head .sync-led.waiting { background: var(--border-base); }
.device-head .sync-led.listening { background: var(--warn); }
.device-head .sync-led.offline { background: var(--border-base); opacity: 0.45; }
.device-head .id { grid-column: 2; grid-row: 1;
                   font-family: var(--mono); font-size: 14px; font-weight: 600;
                   color: var(--ink); }
.device-head .chip-col { grid-column: 4; grid-row: 1; justify-self: end; }
.device-head .sub { grid-column: 1 / -1; grid-row: 2;
                    display: flex; flex-direction: column; gap: 3px;
                    margin-top: var(--s-1); }
.device .sub .item { font-family: var(--mono); font-size: 11px;
                     letter-spacing: 0.08em; color: var(--sub); }
.device .sub .item .dot { display: inline-block; width: 6px; height: 6px;
                          border-radius: 50%; margin-right: 6px;
                          background: var(--border-base); vertical-align: middle; }
.device .sub .item.ok .dot { background: var(--passed); }
.device .sub .item.warn .dot { background: var(--warn); }
.device .sub .item.bad .dot { background: var(--failed); }
.device-actions { display: flex; gap: var(--s-2); margin-top: var(--s-2);
                  margin-bottom: var(--s-2); flex-wrap: wrap; }
.preview-btn.active { background: var(--ink); color: var(--surface); }
/* Multi-frame accumulation buffer state strip — sits between the
   device-head (status chips) and the device-actions (buttons) so
   operators see "where am I in the calibration sequence" before
   choosing which button to press. */
.buffer-block { display: flex; gap: var(--s-2); flex-wrap: wrap;
                align-items: center; margin: var(--s-2) 0;
                padding: var(--s-1) var(--s-2);
                font-family: var(--mono); font-size: 11px;
                letter-spacing: 0.06em;
                background: var(--surface-deep); border: 1px solid var(--border-base);
                border-radius: 6px; }
.buffer-block .buffer-progress { color: var(--ink); }
.buffer-block .buffer-progress.idle { color: var(--passed); }
.buffer-block .buffer-progress strong { font-weight: 600; }
.buffer-block .reproj-badge { padding: 2px 8px; border-radius: 4px;
                              border: 1px solid; }
.buffer-block .reproj-badge.ok { color: var(--passed); border-color: var(--passed);
                                 background: var(--passed-bg); }
.buffer-block .reproj-badge.warn { color: var(--warn); border-color: var(--warn);
                                   background: var(--warn-bg); }
.buffer-block .reproj-badge.bad { color: var(--failed); border-color: var(--failed);
                                  background: var(--failed-bg); }
.buffer-block .buffer-fail { color: var(--failed); font-weight: 600; }
/* Reset rig — destructive affordance at the bottom of the devices card.
   Same .danger styling as other destructive buttons (delete intrinsics). */
.reset-rig-row { display: flex; justify-content: flex-end;
                 margin-top: var(--s-3); padding-top: var(--s-3);
                 border-top: 1px solid var(--border-base); }
.btn.small.danger { color: var(--failed); border-color: var(--failed);
                    background: transparent; }
.btn.small.danger:hover { background: var(--failed-bg); }
.btn.small.secondary { color: var(--sub); border-color: var(--border-base);
                       background: transparent; }
.btn.small.secondary:hover { background: var(--surface-deep); }
.tuning-status {
  min-height: 18px;
  margin-bottom: var(--s-2);
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--sub);
}
.tuning-status.ok { color: var(--passed); }
.tuning-status.error { color: var(--failed); }
.tuning-row { display: flex; align-items: center; gap: var(--s-2);
              margin-top: var(--s-3); flex-wrap: nowrap; }
.tuning-row:first-child { margin-top: var(--s-2); }
.tuning-label { font-family: var(--mono); font-size: 10px;
                letter-spacing: 0.12em; text-transform: uppercase;
                color: var(--sub); min-width: 120px; }
.tuning-input { font-family: var(--mono); font-size: 11px;
                padding: 4px 8px; border: 1px solid var(--border-base);
                border-radius: var(--r); background: var(--surface);
                color: var(--ink); flex: 1; min-width: 0; }
.tuning-input:focus { outline: none; border-color: var(--ink); }
.tuning-row input[type="number"] { width: 80px; flex: none;
                                   font-family: var(--mono); font-size: 11px;
                                   padding: 4px 6px;
                                   border: 1px solid var(--border-base);
                                   border-radius: var(--r);
                                   background: var(--surface); color: var(--ink); }
.tuning-row input[type="number"]:focus { outline: none; border-color: var(--ink); }
.quick-chirp-telemetry { display: grid; gap: var(--s-3); }
.qct-cam { border: 1px solid var(--border); padding: var(--s-3); }
.qct-head { display: flex; align-items: center; justify-content: space-between;
            font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
            text-transform: uppercase; margin-bottom: var(--s-2); }
.qct-head .clip-chip { padding: 2px 8px; border: 1px solid var(--border);
                       font-size: 10px; color: var(--sub); }
.qct-head .clip-chip.warn { color: var(--failed); border-color: var(--failed); }
.qct-head .clip-chip.ok { color: var(--passed); border-color: var(--passed); }
.qct-row { display: grid; grid-template-columns: 100px 1fr 80px;
           align-items: center; gap: var(--s-2); margin-bottom: var(--s-1);
           font-family: var(--mono); font-size: 11px; }
.qct-row .label { color: var(--sub); letter-spacing: 0.08em;
                  text-transform: uppercase; }
.qct-row .bar { position: relative; height: 10px; background: var(--border); }
.qct-row .bar .fill { position: absolute; left: 0; top: 0; bottom: 0;
                      background: var(--ink); }
.qct-row .bar .fill.warn { background: var(--failed); }
.qct-row .bar .thr-mark { position: absolute; top: -2px; bottom: -2px;
                          width: 2px; background: var(--failed); }
.qct-row .bar .peak-mark { position: absolute; top: -3px; bottom: -3px;
                           width: 2px; background: var(--passed); opacity: 0.7; }
.qct-row .value { text-align: right; font-variant-numeric: tabular-nums; }
.qct-row .value .peak { color: var(--passed); font-size: 10px;
                        margin-left: 4px; opacity: 0.85; }
.qct-head .age { margin-left: 8px; padding: 1px 6px; font-size: 10px;
                 border: 1px solid var(--border); color: var(--sub); }
.qct-head .age.live { color: var(--passed); border-color: var(--passed); }
.qct-head .age.stale { color: var(--sub); border-style: dashed; }
.qct-cam.stale .qct-row .fill { opacity: 0.5; }
.qct-cam.stale { opacity: 0.92; }
.per-cam-sync { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
                gap: var(--s-3); }
.pcs-cam { border: 1px solid var(--border); padding: var(--s-3);
           display: flex; align-items: center; gap: var(--s-3); }
.pcs-cam .led { width: 14px; height: 14px; border-radius: 50%;
                background: var(--border); flex-shrink: 0; }
.pcs-cam.synced .led { background: var(--passed);
                       box-shadow: 0 0 8px rgba(125, 255, 192, 0.5); }
.pcs-cam.offline .led { background: var(--border); }
.pcs-cam.listening .led { background: var(--warn);
                          box-shadow: 0 0 6px rgba(255, 207, 122, 0.5); }
.pcs-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pcs-head { font-family: var(--mono); font-size: 12px; letter-spacing: 0.10em;
            text-transform: uppercase; color: var(--ink); }
.pcs-meta { font-family: var(--mono); font-size: 10px; color: var(--sub);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pcs-meta .sync-id { color: var(--ink); font-weight: 500; }
.pcs-meta .sid-chip { font-family: var(--mono); color: var(--ink);
                      background: var(--panel-alt, rgba(0,0,0,0.04));
                      padding: 1px 4px; border-radius: 3px;
                      letter-spacing: 0.08em; cursor: help; }
.pcs-meta .pair-ok { margin-left: 6px; padding: 1px 5px;
                     font-size: 9px; color: var(--passed);
                     border: 1px solid var(--passed);
                     text-transform: uppercase; letter-spacing: 0.10em; }
"""
