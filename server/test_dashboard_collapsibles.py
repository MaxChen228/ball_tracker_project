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
    html = TestClient(app).get("/").text
    for key, title in _COLLAPSIBLE_CARDS:
        assert f'data-collapsible-key="{key}"' in html, f"missing key for {title!r}"
    # Header + body sentinels exist (one per card minimum). Count >= 3
    # avoids tying the assertion to events-list internals which also
    # carry these attributes once a session has been recorded.
    assert html.count('data-collapsible-header') >= 3
    assert html.count('data-collapsible-body') >= 3


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
