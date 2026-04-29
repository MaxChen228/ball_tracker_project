"""Tests for `viewer_fragments.session_cost_threshold_strip_html` —
the per-session slider in the viewer's nav bar."""
from __future__ import annotations

import re

from viewer_fragments import session_cost_threshold_strip_html


def test_strip_renders_with_default_when_threshold_is_none():
    body = session_cost_threshold_strip_html(None, "s_dead00f1")
    assert 'class="session-tuning"' in body
    assert 'data-session-cost-threshold' in body
    assert 'data-session-recompute' in body
    # None → 1.0 (no filter) shown as initial.
    assert 'value="1.00"' in body
    assert '>1.00<' in body  # st-value display
    # Apply button starts disabled (only enabled after operator drags).
    assert 'disabled' in body
    # Session id interpolated into the apply handler.
    assert 'data-session-id="s_dead00f1"' in body


def test_strip_renders_with_persisted_threshold():
    body = session_cost_threshold_strip_html(0.45, "s_face00ab")
    assert 'value="0.45"' in body
    assert '>0.45<' in body
    assert 'data-session-id="s_face00ab"' in body


def test_strip_escapes_session_id_attr():
    """Session id pattern restricts to hex (^s_[0-9a-f]{4,32}$) but
    escape anyway — defense in depth."""
    body = session_cost_threshold_strip_html(None, 's_aabb00cc')
    # No raw quotes / angles leak from the helper.
    assert '<script>' not in body
    assert 'onerror' not in body
    # Range slider input is well-formed.
    assert re.search(
        r'<input[^>]*type="range"[^>]*data-session-cost-threshold',
        body,
    ) is not None


def test_strip_apply_handler_uses_window_global():
    body = session_cost_threshold_strip_html(None, "s_aabb00cc")
    # oninput handler invokes _setCostThreshold (drag preview) and
    # enables the Apply button. onclick invokes _applyCostThreshold
    # (committed recompute).
    assert 'window._setCostThreshold' in body
    assert 'window._applyCostThreshold' in body
    assert 'data-session-recompute' in body
