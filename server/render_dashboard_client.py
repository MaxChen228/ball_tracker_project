from __future__ import annotations

import datetime as _dt
from pathlib import Path

from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    DRAW_PLATE_OVERLAY_JS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
)

_STATIC_DIR = Path(__file__).parent / "static"


def _resolve_js_template() -> str:
    """Substitute the shared virt-canvas helpers into the dashboard JS.
    The template uses `{PLATE_WORLD_JS}` / `{PROJECTION_JS}` / etc. as
    literal placeholders (NOT Python f-string fields — the rest of the
    template is full of JS braces that would explode `.format()`), so
    resolve them with plain `str.replace` before embedding."""
    js = _JS_TEMPLATE_RAW
    js = js.replace("{PLATE_WORLD_JS}", PLATE_WORLD_JS)
    js = js.replace("{PROJECTION_JS}", PROJECTION_JS)
    js = js.replace("{DRAW_VIRTUAL_BASE_JS}", DRAW_VIRTUAL_BASE_JS)
    js = js.replace("{DRAW_PLATE_OVERLAY_JS}", DRAW_PLATE_OVERLAY_JS)
    return js


_JS_TEMPLATE_RAW: str = (_STATIC_DIR / "dashboard_client.js").read_text(encoding="utf-8")

_JS_TEMPLATE = _resolve_js_template()


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
