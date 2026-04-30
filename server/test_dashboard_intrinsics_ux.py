"""Regression guards for the redesigned Intrinsics (ChArUco) card.

The card factors into three sections — Pairing / Records / Upload — and
each section has invariants the JS-side renderer mirrors. If the SSR
output drifts (e.g. one role row gets dropped, the upload optgroup
collapses, manual device_id field disappears) the JS partial-update
won't catch it on first load and the operator gets a half-rendered
state until the first /calibration/intrinsics tick lands."""
from __future__ import annotations

import re
from pathlib import Path

from render_dashboard_intrinsics import _render_intrinsics_body
from routes.calibration_intrinsics import _DEVICE_ID_RE


_INTRINSICS_JS = Path(__file__).parent / "static" / "dashboard" / "84_intrinsics.js"


def test_pairing_section_always_emits_both_roles_even_when_no_devices_online():
    html = _render_intrinsics_body(records=[], online_roles={})
    assert 'class="intrinsics-section"' in html
    assert "Cam A" in html and "Cam B" in html
    assert html.count('class="intrinsics-pair offline"') == 2


def test_pairing_marks_role_as_cal_when_record_exists_for_online_device():
    html = _render_intrinsics_body(
        records=[{
            "device_id": "DEV_A_HAS_CAL",
            "device_model": "iPhone15,3",
            "fx": 1278.0, "fy": 1278.0,
            "rms_reprojection_px": 0.34,
            "n_images": 18,
            "calibrated_at": 1714478520.0,
            "distortion": [0.1, -0.2, 0.0, 0.0, 0.0],
            "source_width_px": 1920, "source_height_px": 1080,
        }],
        online_roles={
            "A": {"device_id": "DEV_A_HAS_CAL", "device_model": "iPhone15,3"},
            "B": {"device_id": "DEV_B_NO_CAL", "device_model": "iPhone14,2"},
        },
    )
    # A is online + has record → "cal ✓"; B is online + no record → "cal ?"
    pair_a_start = html.index("Cam A")
    pair_b_start = html.index("Cam B")
    pair_a_block = html[pair_a_start:pair_b_start]
    pair_b_block = html[pair_b_start:]
    assert "cal ✓" in pair_a_block
    assert "cal ?" in pair_b_block


def test_records_row_carries_used_as_chip_when_device_currently_online():
    html = _render_intrinsics_body(
        records=[{
            "device_id": "DEV_X",
            "device_model": "iPhone15,3",
            "fx": 1280, "fy": 1280,
            "distortion": None,
        }],
        online_roles={"A": {"device_id": "DEV_X", "device_model": "iPhone15,3"}},
    )
    # The record row for DEV_X (the online Cam A occupant) must carry
    # `used as A`. Without this badge the operator cannot tell which
    # record applies to the phone in front of them right now.
    assert "used as A" in html


def test_records_row_omits_used_as_when_device_offline():
    html = _render_intrinsics_body(
        records=[{
            "device_id": "DEV_OFFLINE",
            "device_model": "iPhone14",
            "fx": 1250, "fy": 1250,
            "distortion": None,
        }],
        online_roles={},
    )
    assert "used as" not in html


def test_upload_target_groups_online_then_known_offline():
    """Online roles render under the `Online` optgroup; record-only devices
    that aren't currently online render under `Known (offline)`. A device
    that's both must NOT appear twice (deduped on first occurrence —
    Online wins)."""
    html = _render_intrinsics_body(
        records=[
            {"device_id": "DEV_A_HAS_CAL", "device_model": "iPhone15,3", "fx": 1280, "fy": 1280, "distortion": None},
            {"device_id": "DEV_OFFLINE", "device_model": "iPhone14", "fx": 1250, "fy": 1250, "distortion": None},
        ],
        online_roles={"A": {"device_id": "DEV_A_HAS_CAL", "device_model": "iPhone15,3"}},
    )
    assert '<optgroup label="Online">' in html
    assert '<optgroup label="Known (offline)">' in html
    online_start = html.index('<optgroup label="Online">')
    online_end = html.index("</optgroup>", online_start)
    online_block = html[online_start:online_end]
    offline_start = html.index('<optgroup label="Known (offline)">')
    offline_end = html.index("</optgroup>", offline_start)
    offline_block = html[offline_start:offline_end]
    # Online occupant lives ONLY in the Online block — never duplicated.
    assert online_block.count("DEV_A_HAS_CAL") == 1
    assert offline_block.count("DEV_A_HAS_CAL") == 0
    # Offline device lives only in the Known (offline) block.
    assert offline_block.count("DEV_OFFLINE") == 1
    assert online_block.count("DEV_OFFLINE") == 0


def test_upload_select_disabled_when_no_devices_known():
    html = _render_intrinsics_body(records=[], online_roles={})
    # No optgroups, select disabled with manual-id hint as the only option.
    assert '<select id="intrinsics-target" disabled>' in html
    assert "use manual id below" in html


def test_upload_section_has_manual_device_id_text_field():
    """The manual `device_id` text input is the escape hatch for fully
    new offline devices that have neither a record nor an active heartbeat."""
    html = _render_intrinsics_body(records=[], online_roles={})
    assert 'id="intrinsics-target-manual"' in html
    assert 'type="text"' in html
    # Placeholder hints at intent so the operator knows it's not just
    # another label-free text box.
    assert "identifierForVendor" in html


def test_upload_static_block_preserves_file_input_and_button():
    """The upload <input type=file> + <button id="intrinsics-upload-btn">
    + manual device_id field must NOT live inside #intrinsics-dynamic —
    the JS polling code replaces that block wholesale and a reattached
    file input loses its FileList (browsers don't carry FileList across
    nodes). Split on the upload section's opening tag (it's the first
    sibling after #intrinsics-dynamic closes) for a stable boundary."""
    html = _render_intrinsics_body(records=[], online_roles={})
    split_at = html.index('<div class="intrinsics-upload">')
    dyn_block = html[: split_at]
    static_block = html[split_at:]
    for hook in ('id="intrinsics-file"', 'id="intrinsics-upload-btn"',
                 'id="intrinsics-target-manual"'):
        assert hook in static_block, f"{hook} missing from upload section"
        assert hook not in dyn_block, f"{hook} leaked into #intrinsics-dynamic"


def test_js_manual_device_id_regex_matches_server_regex():
    """The JS pre-flight regex (`_MANUAL_DEVICE_ID_RE` in 84_intrinsics.js)
    must be byte-identical to `_DEVICE_ID_RE.pattern` in the server route.
    A drift here = the client accepts ids the server then rejects with
    400, surfacing as a confusing upload failure instead of a fast-fail."""
    js_text = _INTRINSICS_JS.read_text(encoding="utf-8")
    m = re.search(r"_MANUAL_DEVICE_ID_RE\s*=\s*/(.+?)/\s*;", js_text)
    assert m, "_MANUAL_DEVICE_ID_RE literal not found in 84_intrinsics.js"
    js_pattern = m.group(1)
    assert js_pattern == _DEVICE_ID_RE.pattern, (
        f"JS pattern {js_pattern!r} drifted from server "
        f"{_DEVICE_ID_RE.pattern!r}"
    )


def test_js_renderer_carries_section_label_and_chip_strings():
    """SSR-only Python tests can't reach the JS renderer; a regex-grep
    over `84_intrinsics.js` for the strings the SSR also emits is the
    cheapest way to catch the obvious shape drift without a JS runtime.
    If the JS replaces 'Pairing' with 'Cameras' or drops the optgroup
    labels, this trips before the operator notices."""
    js = _INTRINSICS_JS.read_text(encoding="utf-8")
    for marker in (
        "Pairing", "Records",
        '"Online"', '"Known (offline)"',
        "cal ✓", "cal ?",
        "used as ",
        "intrinsics-pair offline",
        "intrinsics-pair legacy",
        "intrinsics-pair online",
    ):
        assert marker in js, f"JS renderer missing {marker!r}"
