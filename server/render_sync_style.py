"""Sync/setup page-specific CSS additions."""
from __future__ import annotations

# Sync-page-only additions on top of the shared _CSS: a single-column
# main-area (no sidebar), the trace plot container sizing, and the nav
# link + sync-chip styles introduced here (mirrored into the dashboard's
# nav via render_dashboard.py so the link can be rendered there too).
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
#sync-trace {
  width: 100%; height: 400px;
  background: var(--surface-hover);
  border: 1px solid var(--border-l);
  border-radius: var(--r);
}
.wav-link {
  color: var(--ink); text-decoration: underline; text-decoration-style: dotted;
  text-underline-offset: 2px; font-family: var(--mono); font-size: 11px;
}
.wav-link:hover { color: var(--accent); }
.trace-empty {
  height: 400px; display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--sub);
  background: var(--surface-hover); border: 1px solid var(--border-l);
  border-radius: var(--r);
}
.trace-legend {
  margin-top: var(--s-2); display: flex; flex-wrap: wrap; gap: var(--s-3);
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--sub);
}
.trace-legend .swatch {
  display: inline-block; width: 10px; height: 2px; margin-right: 6px;
  vertical-align: middle;
}
.main-sync .telemetry-panel {
  position: static;
  left: auto;
  right: auto;
  top: auto;
  bottom: auto;
  z-index: auto;
  max-width: none;
}
.main-sync .telemetry-body {
  max-height: 360px;
}
.main-sync .camera-compare .preview-panel .placeholder {
  display: none;
}
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
