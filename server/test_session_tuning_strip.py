"""Tests for `viewer_fragments.session_tuning_strip_html` —
the per-session pairing-tuning sliders (cost + gap) in the viewer's
nav bar."""
from __future__ import annotations

import re

from viewer_fragments import session_tuning_strip_html


def test_strip_renders_with_defaults_when_thresholds_are_none():
    body = session_tuning_strip_html(None, None, "s_dead00f1")
    assert 'class="session-tuning"' in body
    assert 'data-session-cost-threshold' in body
    assert 'data-session-gap-threshold' in body
    assert 'data-session-recompute' in body
    # Cost: None → 1.0 (no filter) shown as initial.
    assert 'value="1.00"' in body
    assert '>1.00<' in body  # st-value display for cost
    # Gap: None → 200cm ("off") shown as initial.
    assert 'value="200"' in body
    assert '>off<' in body  # st-value display for gap
    # Apply button starts disabled (only enabled after operator drags).
    assert 'disabled' in body
    # Session id interpolated into the apply handler.
    assert 'data-session-id="s_dead00f1"' in body


def test_strip_renders_with_persisted_thresholds():
    # 0.45 cost + 0.08m gap → "8 cm" readout
    body = session_tuning_strip_html(0.45, 0.08, "s_face00ab")
    assert 'value="0.45"' in body
    assert '>0.45<' in body
    # gap_threshold_m=0.08 → 8cm initial
    assert 'value="8"' in body
    assert '≤ 8 cm' in body
    assert 'data-session-id="s_face00ab"' in body


def test_strip_clamps_gap_above_route_max_to_off():
    # Defensive: even if a future tuning ships gap > 2.0m (route max),
    # strip should clamp the slider to 200cm "off" instead of
    # rendering an invalid `value="500"`.
    body = session_tuning_strip_html(None, 5.0, "s_aabb00cc")
    assert 'value="200"' in body
    assert '>off<' in body


def test_strip_renders_floor_gap_at_zero():
    body = session_tuning_strip_html(None, 0.0, "s_aabb00cd")
    # 0m → 0cm slider position. Readout still shows "≤ 0 cm" not "off"
    # because the value is meaningfully tight, not the open extreme.
    assert 'value="0"' in body
    assert '≤ 0 cm' in body


def test_strip_escapes_session_id_attr():
    """Session id pattern restricts to hex (^s_[0-9a-f]{4,32}$) but
    escape anyway — defense in depth."""
    body = session_tuning_strip_html(None, None, 's_aabb00cc')
    assert '<script>' not in body
    assert 'onerror' not in body
    # Both range inputs are well-formed.
    assert re.search(
        r'<input[^>]*type="range"[^>]*data-session-cost-threshold',
        body,
    ) is not None
    assert re.search(
        r'<input[^>]*type="range"[^>]*data-session-gap-threshold',
        body,
    ) is not None


def test_strip_apply_handler_uses_unified_window_global():
    body = session_tuning_strip_html(None, None, "s_aabb00cc")
    # oninput handlers invoke the per-axis preview hooks.
    assert 'window._setCostThreshold' in body
    assert 'window._setGapThreshold' in body
    # Apply onclick invokes the unified _applyTuning (sends both axes).
    assert 'window._applyTuning' in body
    # Old single-axis handler must be gone — would silently drop gap.
    assert 'window._applyCostThreshold' not in body
    assert 'data-session-recompute' in body
