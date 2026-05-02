"""Tests for `viewer_fragments.session_tuning_strip_html` — the
per-session gap slider in the viewer's nav bar.

Post cost-absorption refactor: cost is per-algorithm — there is no
operator-tunable cost slider. Only the gap slider remains.
"""
from __future__ import annotations

import re

from viewer_fragments import session_tuning_strip_html


def test_strip_renders_with_default_gap_when_none():
    body = session_tuning_strip_html(None, "s_dead00f1")
    assert 'class="session-tuning"' in body
    # Only the gap slider — no cost slider after Phase 2 cleanup.
    assert 'data-session-gap-threshold' in body
    assert 'data-session-cost-threshold' not in body
    assert 'data-session-recompute' in body
    # Gap: None → PairingTuning.default().gap_threshold_m (0.20m → 20cm).
    assert 'value="20"' in body
    # No more "off" magic word — readout always speaks centimetres.
    assert '>off<' not in body
    assert '≤ 20 cm' in body
    # Apply button starts disabled (only enabled after operator drags).
    assert 'disabled' in body
    # Session id interpolated into the apply handler.
    assert 'data-session-id="s_dead00f1"' in body


def test_strip_renders_with_persisted_gap():
    body = session_tuning_strip_html(0.08, "s_face00ab")
    # gap_threshold_m=0.08 → 8cm initial
    assert 'value="8"' in body
    assert '≤ 8 cm' in body
    assert 'data-session-id="s_face00ab"' in body


def test_strip_clamps_gap_above_route_max_to_slider_max():
    # Defensive: even if a future tuning ships gap > 2.0m (route max),
    # strip should clamp the slider to 200cm instead of rendering an
    # invalid `value="500"`.
    body = session_tuning_strip_html(5.0, "s_aabb00cc")
    assert 'value="200"' in body
    assert '≤ 200 cm' in body
    assert '>off<' not in body


def test_strip_renders_floor_gap_at_zero():
    body = session_tuning_strip_html(0.0, "s_aabb00cd")
    # 0m → 0cm slider position. Readout shows "≤ 0 cm" — same
    # template as every other position, no special-cased word.
    assert 'value="0"' in body
    assert '≤ 0 cm' in body


def test_strip_has_no_pairing_cap_artifacts():
    """Post-Phase-1-5 (pairing emits full set), the slider has NO
    pairing-cap tick / overflow shade / "Apply needed" warn —
    pure client-side mask. Guard against accidental re-introduction."""
    body = session_tuning_strip_html(None, "s_aabb00cc")
    assert 'st-gap-tick' not in body
    assert 'st-gap-track' not in body
    assert 'st-gap-warn' not in body
    assert 'data-session-gap-warn' not in body
    assert 'data-pairing-cap-cm' not in body
    assert '--gap-cap-pct' not in body
    assert 'past pairing cap' not in body


def test_strip_has_no_cost_slider_artifacts():
    """Post cost-absorption refactor: no cost slider, no cost label,
    no cost data attrs. Guard against accidental re-introduction."""
    body = session_tuning_strip_html(None, "s_aabb00cc")
    assert 'data-session-cost-threshold' not in body
    assert 'data-session-cost-value' not in body
    assert 'Cost ≤' not in body
    assert '_setCostThreshold' not in body


def test_strip_escapes_session_id_attr():
    """Session id pattern restricts to hex (^s_[0-9a-f]{4,32}$) but
    escape anyway — defense in depth."""
    body = session_tuning_strip_html(None, 's_aabb00cc')
    assert '<script>' not in body
    assert 'onerror' not in body
    assert re.search(
        r'<input[^>]*type="range"[^>]*data-session-gap-threshold',
        body,
    ) is not None


def test_strip_apply_handler_uses_unified_window_global():
    body = session_tuning_strip_html(None, "s_aabb00cc")
    # oninput handler invokes the gap preview hook.
    assert 'window._setGapThreshold' in body
    # Apply onclick invokes the unified _applyTuning.
    assert 'window._applyTuning' in body
    # Old single-axis handler must be gone — would silently drop gap.
    assert 'window._applyCostThreshold' not in body
    assert 'data-session-recompute' in body
