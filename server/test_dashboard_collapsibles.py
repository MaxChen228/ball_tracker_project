"""Regression guards for the dashboard collapsible markup.

Three sidebar cards (Detection HSV / Capture Tuning / Intrinsics) and
each `event-day` group must ship the trio of data attributes that
`static/dashboard/05_collapsibles.js` reads. A refactor that drops the
attributes silently de-features the UI — this test is the fast tripwire."""
from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from render_dashboard_events import _render_events_body


_COLLAPSIBLE_CARDS = (
    ("dash:card:hsv", "Detection HSV"),
    ("dash:card:tuning", "Capture Tuning"),
    ("dash:card:intrinsics", "Intrinsics (ChArUco)"),
)


def test_dashboard_sidebar_cards_carry_collapsible_attrs():
    """Each card must carry the key on the OUTER `.card` (not the inner
    body that polling replaces) AND have header + body sentinels nested
    within. Anchoring on the exact outer-wrapper substring catches both
    classes of regression — a refactor that drops the attribute and one
    that moves it to the inner body div."""
    html = TestClient(app).get("/").text
    for key, title in _COLLAPSIBLE_CARDS:
        outer = f'<div class="card" data-collapsible-key="{key}">'
        assert html.count(outer) == 1, f"missing outer card wrapper for {title!r}"
        block_start = html.index(outer)
        next_card = html.find('<div class="card"', block_start + len(outer))
        block_end = next_card if next_card >= 0 else html.find("</aside>", block_start)
        block = html[block_start:block_end]
        assert "data-collapsible-header" in block, f"header missing inside {title!r} card"
        assert "data-collapsible-body" in block, f"body missing inside {title!r} card"


def test_events_body_groups_each_day_with_collapsible_attrs():
    events = [
        {"session_id": "s_one", "created_day": "2026-04-30", "created_hm": "10:00"},
        {"session_id": "s_two", "created_day": "2026-04-30", "created_hm": "11:00"},
        {"session_id": "s_three", "created_day": "2026-04-29", "created_hm": "20:00"},
    ]
    html = _render_events_body(events)
    # Two distinct days → two collapsible groups.
    assert html.count('class="event-day-group"') == 2
    assert 'data-collapsible-key="dash:event-day:2026-04-30"' in html
    assert 'data-collapsible-key="dash:event-day:2026-04-29"' in html
    # Each group has exactly one body wrapper (the items live inside).
    assert html.count('class="event-day-body"') == 2
    # Both items for 2026-04-30 land inside the same body — sanity check
    # by ordering. (If grouping broke, s_two would land in its own body.)
    apr30_start = html.index('data-collapsible-key="dash:event-day:2026-04-30"')
    apr30_end = html.index('data-collapsible-key="dash:event-day:2026-04-29"')
    apr30_block = html[apr30_start:apr30_end]
    assert 's_one' in apr30_block and 's_two' in apr30_block


def test_events_body_empty_state_omits_groups():
    html = _render_events_body([])
    assert 'event-day-group' not in html
    assert 'events-empty' in html


def _snapshot_v11(preset="tennis"):
    """Wire shape `_render_card -> _cfg_strip_html` consumes — must
    match `DetectionConfigSnapshotPayload.model_dump()` (canonical
    `{algorithm_id, params, preset_name}`, no top-level `hsv`)."""
    return {
        "algorithm_id": "v11_hsv_cc",
        "params": {
            "hsv": {"h_min": 25, "h_max": 55, "s_min": 90, "s_max": 255, "v_min": 90, "v_max": 255},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        "preset_name": preset,
    }


def _snapshot_hybrid(preset="hybrid_28d_blue_ball"):
    return {
        "algorithm_id": "hybrid_28d",
        "params": {
            "prod_hsv": {"h_min": 105, "h_max": 112, "s_min": 140, "s_max": 255, "v_min": 40, "v_max": 255},
            "prod_shape": {"aspect_min": 0.75, "fill_min": 0.55},
            "prod_area_min": 20,
            "v11_hsv": {"h_min": 103, "h_max": 118, "s_min": 120, "s_max": 255, "v_min": 30, "v_max": 255},
            "v11_shape": {"aspect_min": 0.40, "fill_min": 0.35},
            "v11_area_min": 3,
            "v11_close_kernel": 3,
            "neigh_half": 6,
            "match_px": 5.0,
        },
        "preset_name": preset,
    }


def test_events_card_renders_v11_snapshot_without_keyerror():
    """Regression for the cfg-strip tip reading top-level `hsv` —
    canonical snapshot shape is `{algorithm_id, params: {hsv, shape_gate}, preset_name}`
    so any `cfg["hsv"]` access KeyErrors on the events page."""
    e = {
        "session_id": "s_v11", "created_day": "2026-05-04", "created_hm": "10:00",
        "live_config_used": _snapshot_v11(),
        "server_post_config_used": _snapshot_v11(),
    }
    html = _render_events_body([e])
    assert "ev-cfg-chip" in html
    assert "tennis" in html


def test_events_card_renders_hybrid_snapshot_without_keyerror():
    """Hybrid snapshot has no top-level `hsv` / `shape_gate` — it
    carries `prod_hsv` / `v11_hsv`. The cfg-strip tip dispatch on
    algorithm_id, with a hybrid-specific layout."""
    e = {
        "session_id": "s_hyb", "created_day": "2026-05-04", "created_hm": "10:00",
        "live_config_used": _snapshot_v11(),  # live always v11
        "server_post_config_used": _snapshot_hybrid(),
    }
    html = _render_events_body([e])
    assert "hybrid_28d_blue_ball" in html
    assert "tennis" in html  # live chip
