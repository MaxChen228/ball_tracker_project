"""Three-way validation report SSR page (`/report/{sid}`).

Renders the JSON output of `validate_three_way.py` as a self-contained
HTML page in the same PHYSICS_LAB visual language as the dashboard
(JetBrains Mono / Noto Sans TC, warm-neutral palette, 1px borders).

Per-frame timeline is loaded asynchronously by Plotly from the CSV
companion (data/gt/validation/<sid>_<cam>.csv) — the route serves the
raw CSV via FastAPI's StaticFiles mount on data/. For v1 we render the
metrics table + a "Open in viewer" link; the live timeline can be
added once the CSV-loading JS is hooked up.
"""
from __future__ import annotations

import html as _html
from typing import Any


_CSS = """
:root {
    --ink: #1a1a1a;
    --sub: #555;
    --bg: #faf8f3;
    --panel: #ffffff;
    --border: #d6d2c4;
    --passed: #4a8a3f;
    --warn: #c08c1f;
    --failed: #b03c3c;
    --accent: #2c5fa3;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Noto Sans TC", system-ui, sans-serif;
    font-size: 13px;
    line-height: 1.5;
}
header.report-nav {
    height: 52px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
    display: flex;
    align-items: center;
    padding: 0 24px;
    gap: 16px;
}
header .brand {
    font-family: "JetBrains Mono", monospace;
    font-weight: 700;
    letter-spacing: 0.5px;
}
header .links a {
    color: var(--accent);
    text-decoration: none;
    margin-right: 16px;
    font-family: "JetBrains Mono", monospace;
    font-size: 12px;
}
header .links a:hover { text-decoration: underline; }
main {
    padding: 24px;
    max-width: 1400px;
    margin: 0 auto;
}
h1.report-title {
    font-family: "JetBrains Mono", monospace;
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 600;
}
.subtitle { color: var(--sub); margin-bottom: 24px; font-size: 12px; }
.cam-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
}
@media (max-width: 900px) { .cam-grid { grid-template-columns: 1fr; } }
.cam-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
}
.cam-card .card-head {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    font-family: "JetBrains Mono", monospace;
    font-weight: 600;
}
.cam-card .card-body { padding: 16px; }
table.metrics {
    width: 100%;
    border-collapse: collapse;
    font-family: "JetBrains Mono", monospace;
    font-size: 12px;
}
table.metrics th, table.metrics td {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid #ece9dd;
}
table.metrics th { color: var(--sub); font-weight: 500; font-size: 11px; }
table.metrics td.pair { color: var(--ink); font-weight: 600; }
table.metrics .num { text-align: right; font-variant-numeric: tabular-nums; }
.legend {
    display: flex;
    gap: 16px;
    margin-bottom: 16px;
    font-size: 11px;
    color: var(--sub);
    font-family: "JetBrains Mono", monospace;
}
.gate {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 2px;
    font-size: 10px;
    font-family: "JetBrains Mono", monospace;
    margin-left: 6px;
}
.gate.pass { color: #fff; background: var(--passed); }
.gate.warn { color: #fff; background: var(--warn); }
.gate.fail { color: #fff; background: var(--failed); }
.summary-row {
    margin-top: 16px;
    padding: 10px 12px;
    background: #f4f1e6;
    border: 1px solid var(--border);
    border-radius: 3px;
    font-size: 11px;
    color: var(--sub);
}
.empty-cam { padding: 24px; color: var(--sub); text-align: center; }
"""


def _gate(value: float, *, low: float, high: float, higher_is_better: bool = True) -> str:
    """Render a pass/warn/fail badge based on threshold."""
    if higher_is_better:
        if value >= high: return '<span class="gate pass">pass</span>'
        if value >= low: return '<span class="gate warn">warn</span>'
        return '<span class="gate fail">fail</span>'
    else:
        if value <= low: return '<span class="gate pass">pass</span>'
        if value <= high: return '<span class="gate warn">warn</span>'
        return '<span class="gate fail">fail</span>'


def _format_pair(label: str, m: dict) -> str:
    """One row in the per-cam metrics table for one (a, b) pair."""
    n_a = int(m.get("n_a_total", 0))
    n_b = int(m.get("n_b_total", 0))
    n_hits = int(m.get("n_hits", 0))
    recall = float(m.get("recall", 0.0))
    precision = float(m.get("precision", 0.0))
    mae = float(m.get("centroid_mae_px", 0.0))
    p95 = float(m.get("centroid_p95_px", 0.0))
    return (
        f"<tr>"
        f"<td class='pair'>{_html.escape(label)}</td>"
        f"<td class='num'>{n_a}</td>"
        f"<td class='num'>{n_b}</td>"
        f"<td class='num'>{n_hits}</td>"
        f"<td class='num'>{recall:.3f}</td>"
        f"<td class='num'>{precision:.3f}</td>"
        f"<td class='num'>{mae:.2f}</td>"
        f"<td class='num'>{p95:.2f}</td>"
        f"</tr>"
    )


def _render_cam_card(cam_id: str, payload: dict[str, Any]) -> str:
    n_gt = int(payload.get("n_gt_frames", 0))
    n_live = int(payload.get("n_live_frames", 0))
    n_srv = int(payload.get("n_server_frames", 0))
    live_vs_gt = payload.get("live_vs_gt", {})
    server_vs_gt = payload.get("server_vs_gt", {})
    live_vs_server = payload.get("live_vs_server", {})

    # Acceptance gates (per plan):
    #   - live_vs_server p95 ≤ 1 px → algorithms aligned (NV12 success)
    #   - live recall vs GT > 0.90 → distillation worth applying
    live_recall_gate = _gate(float(live_vs_gt.get("recall", 0.0)), low=0.80, high=0.90)
    align_gate = _gate(
        float(live_vs_server.get("centroid_p95_px", 99.0)),
        low=1.0, high=3.0, higher_is_better=False,
    )

    rows = [
        _format_pair("live ↔ GT", live_vs_gt),
        _format_pair("server ↔ GT", server_vs_gt),
        _format_pair("live ↔ server", live_vs_server),
    ]

    return (
        f'<section class="cam-card">'
        f'<div class="card-head">Cam {_html.escape(cam_id)}'
        f'<span class="gate" style="background:transparent;color:var(--sub);">'
        f'gt={n_gt} · live={n_live} · srv={n_srv}</span></div>'
        f'<div class="card-body">'
        f'<table class="metrics">'
        f'<thead><tr>'
        f'<th>pair</th><th class="num">a</th><th class="num">b</th>'
        f'<th class="num">hits</th><th class="num">recall</th>'
        f'<th class="num">prec</th><th class="num">mae px</th>'
        f'<th class="num">p95 px</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
        f'<div class="summary-row">'
        f'  <strong>Live recall vs GT:</strong> '
        f'  {float(live_vs_gt.get("recall", 0.0)):.3f} {live_recall_gate}'
        f'  &nbsp;·&nbsp; '
        f'  <strong>Algorithm alignment p95:</strong> '
        f'  {float(live_vs_server.get("centroid_p95_px", 0.0)):.2f}px {align_gate}'
        f'</div>'
        f'</div>'
        f'</section>'
    )


def render_report_page(session_id: str, cam_payloads: dict[str, dict]) -> str:
    """Render the full /report/{sid} HTML."""
    cards: list[str] = []
    for cam in sorted(cam_payloads):
        cards.append(_render_cam_card(cam, cam_payloads[cam]))
    if not cards:
        cards.append('<div class="empty-cam">No validation report yet — run /sessions/{sid}/run_validation first</div>')
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>Report · {_html.escape(session_id)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;600&display=swap" rel="stylesheet">
  <style>{_CSS}</style>
</head>
<body>
  <header class="report-nav">
    <span class="brand">BALL_TRACKER · REPORT</span>
    <span class="links">
      <a href="/">← dashboard</a>
      <a href="/viewer/{_html.escape(session_id)}">open in viewer →</a>
    </span>
  </header>
  <main>
    <h1 class="report-title">{_html.escape(session_id)}</h1>
    <div class="subtitle">
      Three-way validation: iOS-live vs server_post vs SAM 3 GT.
      Match radius 8 px. <code>pair</code> reads as <code>a ↔ b</code>;
      <code>recall</code> = hits / b-total (b is reference; for *_vs_GT the
      reference is GT).
    </div>
    <div class="legend">
      Gates: live_recall_vs_gt ≥ 0.90 → algorithm fit good ·
      live↔server p95 ≤ 1 px → algorithms aligned (NV12 success)
    </div>
    <div class="cam-grid">{"".join(cards)}</div>
  </main>
</body>
</html>"""
