"""Regression guards for the Intrinsics (ChArUco) pairing card.

The card answers exactly one question per role: is the *currently
connected* device's `device_id` known to have a ChArUco record on the
server? Records are deliberately not enumerated — `device_id` is
`identifierForVendor` and rotates on iOS reinstall, so a historical
list would be misleading."""
from __future__ import annotations

from pathlib import Path

from render_dashboard_intrinsics import _render_intrinsics_body


_INTRINSICS_JS = Path(__file__).parent / "static" / "dashboard" / "84_intrinsics.js"


def test_pairing_always_emits_both_roles_even_when_no_devices_online():
    html = _render_intrinsics_body(records=[], online_roles={})
    assert "Cam A" in html and "Cam B" in html
    assert html.count('class="intrinsics-pair offline"') == 2


def test_pairing_marks_role_as_cal_when_record_exists_for_online_device():
    html = _render_intrinsics_body(
        records=[{"device_id": "DEV_A_HAS_CAL", "device_model": "iPhone15,3"}],
        online_roles={
            "A": {"device_id": "DEV_A_HAS_CAL", "device_model": "iPhone15,3"},
            "B": {"device_id": "DEV_B_NO_CAL", "device_model": "iPhone14,2"},
        },
    )
    pair_a_block = html[html.index("Cam A"):html.index("Cam B")]
    pair_b_block = html[html.index("Cam B"):]
    assert "cal ✓" in pair_a_block
    assert "cal ?" in pair_b_block


def test_no_records_list_or_delete_buttons_in_card():
    """Records list, `used as` chips, and per-row delete buttons were
    removed alongside the historical device_id catalog. Lock so a future
    refactor doesn't restore them from git history."""
    html = _render_intrinsics_body(
        records=[{"device_id": "DEV_X", "device_model": "iPhone15,3"}],
        online_roles={"A": {"device_id": "DEV_X", "device_model": "iPhone15,3"}},
    )
    for hook in (
        'class="intrinsics-list"',
        'class="intrinsics-row"',
        "data-intrinsics-delete",
        "used as",
        ">Records<",
    ):
        assert hook not in html, f"{hook!r} leaked back into the intrinsics card"


def test_no_upload_form_in_card():
    """Upload form is dead UI after the iOS-native Calibrate flow took
    over record creation. The POST endpoint
    `/calibration/intrinsics/{device_id}` is preserved (iOS calls it),
    only the dashboard producer is gone."""
    html = _render_intrinsics_body(records=[], online_roles={})
    for hook in (
        'id="intrinsics-file"',
        'id="intrinsics-upload-btn"',
        'id="intrinsics-target"',
        'id="intrinsics-target-manual"',
        'class="intrinsics-upload"',
    ):
        assert hook not in html, f"{hook!r} leaked back into the intrinsics card"


def test_js_renderer_carries_pairing_markers_only():
    js = _INTRINSICS_JS.read_text(encoding="utf-8")
    for marker in (
        "cal ✓", "cal ?",
        "intrinsics-pair offline",
        "intrinsics-pair legacy",
        "intrinsics-pair online",
    ):
        assert marker in js, f"JS renderer missing {marker!r}"
    for forbidden in (
        "intrinsics-list",
        "intrinsics-row",
        "data-intrinsics-delete",
        "intrinsicsDelete",
    ):
        assert forbidden not in js, f"{forbidden!r} should be gone from JS"
