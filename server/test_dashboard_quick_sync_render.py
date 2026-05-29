"""SSR rendering for the Quick Sync dashboard card.

Pins the wire-shape contract between server render and the client tick
(`static/dashboard/56_quick_sync.js`):
- emitter `<select>` options come from `online_cam_ids`, NOT
  `expected_camera_ids` (POST /sync/quick_start rejects an offline
  emitter with 409 emitter_offline)
- the dynamic `#quick-sync-dynamic` container exists so the tick can
  re-render into it after the first poll
- the Start button has the id the click handler binds to
- empty online-cams paints a fail-loud disabled state instead of an
  open dropdown with no options

Also runs the dashboard end-to-end via TestClient and asserts no
template placeholders survive (CLAUDE.md cheap-insurance check for
inline JS `{PLACEHOLDER}` regressions).
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

import main
from main import app
from render_dashboard_quick_sync import _render_quick_sync_body


def test_render_with_online_cams_lists_each_as_option():
    out = _render_quick_sync_body(online_cam_ids=["A", "B", "C"])
    assert 'id="quick-sync-emitter"' in out
    for cam in ("A", "B", "C"):
        assert f'<option value="{cam}">{cam}</option>' in out
    assert 'id="quick-sync-start"' in out
    assert 'disabled' not in out.split('id="quick-sync-start"')[0].split('<select')[1]


def test_render_with_no_online_cams_disables_start():
    """Fail-loud: a foot-gun emitter pick (offline cam) returns 409 from
    quick_start. SSR must reflect that there's nothing to pick."""
    out = _render_quick_sync_body(online_cam_ids=[])
    assert 'id="quick-sync-emitter"' in out
    assert "(no online cams)" in out
    # Both controls disabled until a cam comes online and the JS tick re-paints.
    assert 'id="quick-sync-emitter" disabled' in out
    assert 'id="quick-sync-start" disabled' in out


def test_render_dynamic_container_present_for_js_to_replace():
    out = _render_quick_sync_body(online_cam_ids=["A"])
    assert 'id="quick-sync-dynamic"' in out


def test_dashboard_page_includes_quick_sync_card():
    """End-to-end: hitting / serves a page with the Quick Sync card +
    no leftover `{PLACEHOLDER}` from JS template substitution. A
    top-level ReferenceError from an unresolved placeholder would
    silently abort the inline IIFE and freeze every later tick — same
    failure mode CLAUDE.md flags for `{PLATE_WORLD_JS}`."""
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="quick-sync-body"' in body
    assert 'id="quick-sync-emitter"' in body
    assert 'id="quick-sync-start"' in body
    assert 'id="quick-sync-dynamic"' in body
    # Online cams pre-populated server-side.
    assert '<option value="A">A</option>' in body
    assert '<option value="B">B</option>' in body
    # No surviving `{SOMETHING_JS}`-style placeholders inside any <script>.
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', body, flags=re.DOTALL)
    for script in scripts:
        leftovers = re.findall(r'\{[A-Z][A-Z0-9_]*\}', script)
        assert not leftovers, f"unresolved JS template placeholders: {leftovers}"
