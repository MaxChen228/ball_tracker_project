"""SSR rendering for the Device Pool dashboard card.

Pins the wire-shape contract between server render and the client tick
(`static/dashboard/85_device_pool.js`): every row carries the data
attributes the click handler needs to round-trip /devices/assign and
/devices/unassign without a re-fetch of /devices/pool.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from render_dashboard_device_pool import _render_device_pool_body


def test_empty_pool_paints_helpful_message():
    out = _render_device_pool_body(assignments=[], observed_unassigned=[])
    assert 'id="device-pool-dynamic"' in out
    assert "No devices yet" in out


def test_assignment_row_includes_unassign_button_with_camera_id():
    out = _render_device_pool_body(
        assignments=[{
            "device_uuid": "abc-12345678-rest-of-uuid",
            "camera_id": "A",
            "device_model": "iPhone15,3",
            "online": True,
        }],
        observed_unassigned=[],
    )
    # Unassign button must carry data-camera-id (the click handler in
    # 85_device_pool.js reads this and posts to /devices/unassign).
    assert 'data-device-pool-action="unassign"' in out
    assert 'data-camera-id="A"' in out
    # Online flag must surface as a chip — operator needs to see at a
    # glance whether the assigned record currently has a live phone.
    assert "chip ok small" in out
    # UUID short-form + full title so hover reveals the rest.
    assert "abc-1234" in out
    assert 'title="abc-12345678-rest-of-uuid"' in out


def test_assignment_row_offline_chip_when_no_live_match():
    out = _render_device_pool_body(
        assignments=[{
            "device_uuid": "u1", "camera_id": "A",
            "device_model": None, "online": False,
        }],
        observed_unassigned=[],
    )
    assert "chip warn small" in out
    assert "offline" in out


def test_observed_row_includes_assign_button_with_all_data_attrs():
    """The Assign button needs all three attrs the JS handler reads:
    data-device-uuid (the target), data-suggested-camera-id (the prompt
    default — current cam_id the phone is connecting under), and
    data-device-model (so the assignment record stores the model)."""
    out = _render_device_pool_body(
        assignments=[],
        observed_unassigned=[{
            "device_uuid": "uuid-fresh",
            "camera_id": "B",
            "device_model": "iPhone15,3",
        }],
    )
    assert 'data-device-pool-action="assign"' in out
    assert 'data-device-uuid="uuid-fresh"' in out
    assert 'data-suggested-camera-id="B"' in out
    assert 'data-device-model="iPhone15,3"' in out


def test_observed_row_html_escapes_hostile_strings():
    """device_model is operator-untrusted (comes from iOS hello). HTML
    injection via `<script>` in device_model must be escaped, otherwise
    a malicious client could pivot to dashboard XSS."""
    out = _render_device_pool_body(
        assignments=[],
        observed_unassigned=[{
            "device_uuid": "uuid-x",
            "camera_id": "A",
            "device_model": '<script>alert(1)</script>',
        }],
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_both_sections_rendered_when_both_present():
    out = _render_device_pool_body(
        assignments=[{"device_uuid": "u1", "camera_id": "A", "online": False}],
        observed_unassigned=[{"device_uuid": "u2", "camera_id": "B"}],
    )
    assert "Assigned" in out
    assert "Observed" in out


# ---- Integration: full dashboard page render -------------------------


def _fresh_main(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    return main


def test_dashboard_page_mounts_device_pool_card(tmp_path, monkeypatch):
    """The dashboard HTML must include the new card shell (collapsible
    header + #device-pool-dynamic mount point) so the client tick has
    somewhere to inject. Without this the 85_device_pool.js tick fails
    silently (querySelector returns null)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/")
    assert r.status_code == 200, r.text
    html = r.text
    assert "Device Pool" in html
    assert 'id="device-pool-body"' in html
    assert 'id="device-pool-dynamic"' in html
    assert 'data-collapsible-key="dash:card:device-pool"' in html


def test_dashboard_page_ssrs_existing_assignments(tmp_path, monkeypatch):
    """Pre-existing assignments must appear in the initial SSR paint —
    operator opening the dashboard with assignments already on disk
    shouldn't see an empty card flash before the first JS tick."""
    main = _fresh_main(tmp_path, monkeypatch)
    main.state.assign_device(
        device_uuid="abc-uuid", camera_id="A", device_model="iPhone15,3",
    )
    client = TestClient(main.app)
    html = client.get("/").text
    # Assigned section with cam A must appear in initial paint.
    assert "Assigned" in html
    assert "abc-uuid" in html
    assert ">Cam A<" in html


def test_dashboard_page_ssrs_observed_unassigned(tmp_path, monkeypatch):
    """A heartbeating device with no assignment must surface under the
    Observed section in initial SSR paint."""
    main = _fresh_main(tmp_path, monkeypatch)
    main.state.heartbeat("A", device_id="uuid-fresh", device_model="iPhone15,3")
    client = TestClient(main.app)
    html = client.get("/").text
    assert "Observed" in html
    assert "uuid-fre" in html  # short-form prefix
