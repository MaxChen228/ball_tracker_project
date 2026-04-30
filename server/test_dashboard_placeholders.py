"""Regression guards for unresolved `{XXX_JS}` template placeholders.

Background — 2026-04-22 incident
--------------------------------
The dashboard / viewer JS bundles use literal `{NAME}` tokens (e.g.
`{PLATE_WORLD_JS}`) as placeholders that are substituted at import time
by `str.replace` (NOT Python f-strings — the JS body has real braces
everywhere that would blow up `.format()`).

If a new placeholder is added to a `.js` source under
`server/static/dashboard/` or `server/static/viewer/` but the matching
`.replace(...)` call is NOT wired into `_resolve_js_template` /
`_resolve_viewer_js_template`, the literal `{NAME}` survives into the
shipped HTML. Browsers parse it as a top-level reference error, the
IIFE aborts, and every downstream `setInterval` / `addEventListener` /
SSE wiring silently fails — but earlier `onclick` handlers attached
inline still work. The symptom is indistinguishable from a backend
race; on 2026-04-22 it ate four wrong-direction commits before the
real cause surfaced.

These tests are the cheap, offline insurance the post-mortem promised.
"""
from __future__ import annotations

import re

import pytest

# Conservative: SCREAMING_SNAKE_CASE only (`{FOO_BAR}`), >= 2 chars
# starting with letter or underscore. JS object literals (`{ key: ... }`,
# `{x, y}`) and lower-case tokens are intentionally excluded.
_PLACEHOLDER_RE = re.compile(r"\{[A-Z_][A-Z0-9_]+\}")

_FIX_HINT_DASHBOARD = (
    "unresolved placeholder {{{name}}} in dashboard JS template. "
    "Wire up a matching `js = js.replace('{{{name}}}', ...)` call in "
    "`render_dashboard_client._resolve_js_template`."
)
_FIX_HINT_VIEWER = (
    "unresolved placeholder {{{name}}} in viewer JS template. "
    "Wire up a matching `js = js.replace('{{{name}}}', ...)` call in "
    "`viewer_page._resolve_viewer_js_template`."
)
_FIX_HINT_RENDERED = (
    "unresolved placeholder {{{name}}} leaked into rendered dashboard "
    "HTML. A top-level ReferenceError will abort the IIFE in the browser "
    "and silently break every setInterval / addEventListener / SSE wiring. "
    "Wire up the matching `.replace()` in `render_dashboard_client._resolve_js_template` "
    "(or `viewer_page._resolve_viewer_js_template` if it came from the viewer template)."
)


def _find_placeholders(text: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(text)


def test_dashboard_js_template_has_no_unresolved_placeholders() -> None:
    """The resolved dashboard JS bundle must not contain literal
    `{XXX_JS}` placeholders. If this fires, a new shared-helper
    placeholder was added in `static/dashboard/*.js` but no matching
    `.replace()` exists in `_resolve_js_template`."""
    from render_dashboard_client import _JS_TEMPLATE

    leaks = _find_placeholders(_JS_TEMPLATE)
    assert not leaks, _FIX_HINT_DASHBOARD.format(name=leaks[0].strip("{}"))


def test_viewer_js_template_has_no_unresolved_placeholders() -> None:
    """The resolved viewer JS bundle must not contain literal
    `{XXX_JS}` placeholders. If this fires, a new shared-helper
    placeholder was added in `static/viewer/*.js` but no matching
    `.replace()` exists in `_resolve_viewer_js_template`."""
    from viewer_page import _VIEWER_JS_TEMPLATE

    leaks = _find_placeholders(_VIEWER_JS_TEMPLATE)
    assert not leaks, _FIX_HINT_VIEWER.format(name=leaks[0].strip("{}"))


# `/`, `/sync`, `/setup`, `/markers` each go through their own render_*
# function (events_index, render_sync_html, render_setup_html,
# render_markers_html) — a placeholder leak in any one would not be
# caught by testing only `/`. `/viewer/{sid}` needs real session
# fixtures and is covered indirectly by `test_viewer.py`; its JS
# template is already covered above by
# `test_viewer_js_template_has_no_unresolved_placeholders`.
_RENDERED_HTML_ROUTES = ["/", "/sync", "/setup", "/markers"]


@pytest.mark.parametrize("route", _RENDERED_HTML_ROUTES)
def test_rendered_html_route_has_no_placeholder_leaks(route: str) -> None:
    """End-to-end guard per dashboard surface. Each route renders via its
    own template path; this catches unresolved static-bundle tokens AND
    any HTML-template-level `.format()` miss in the per-page renderer
    that left `{FOO}` in the response."""
    from fastapi.testclient import TestClient
    import main
    from main import app

    main.state.reset()
    with TestClient(app) as client:
        resp = client.get(route)
    assert resp.status_code == 200, (
        f"GET {route} returned {resp.status_code} — placeholder check needs "
        f"a 200 response. If the route is gated on state, seed it before this "
        f"test or move the route out of `_RENDERED_HTML_ROUTES`."
    )
    leaks = _find_placeholders(resp.text)
    assert not leaks, _FIX_HINT_RENDERED.format(name=leaks[0].strip("{}"))
