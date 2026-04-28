"""Dashboard Intrinsics (ChArUco) card body."""
from __future__ import annotations

import datetime as _dt
import html


def _fmt_dev_id(device_id: str) -> str:
    """Shorten UUIDs for display. `identifierForVendor` is 36 chars — too
    wide for the sidebar. First 8 is enough to distinguish across a rig of
    a handful of phones."""
    return device_id[:8] + "…" if len(device_id) > 10 else device_id


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "—"


def _render_intrinsics_body(
    records: list[dict[str, object]] | None = None,
    online_roles: dict[str, dict[str, object]] | None = None,
) -> str:
    """Per-device ChArUco intrinsics card — role→device mapping, stored
    records list, and upload/delete controls.

    SSR-paints a usable skeleton even when `records` / `online_roles` are
    `None` (initial page load before `/calibration/intrinsics` is polled)
    so there's no flash of empty state."""
    records = records or []
    online_roles = online_roles or {}

    # Top strip: which physical device is currently playing each role.
    role_chips_html: list[str] = []
    for role, info in sorted(online_roles.items()):
        did = str(info.get("device_id") or "")
        model = str(info.get("device_model") or "")
        if not did:
            chip = (
                f'<span class="chip idle" title="{html.escape(role)}: no device_id yet">'
                f'{html.escape(role)} · legacy client</span>'
            )
        else:
            label = f"{html.escape(role)} → {html.escape(_fmt_dev_id(did))}"
            if model:
                label += f" ({html.escape(model)})"
            has_rec = any(r.get("device_id") == did for r in records)
            chip_cls = "ok" if has_rec else "warn"
            chip = (
                f'<span class="chip {chip_cls}" title="{html.escape(did)}">'
                f'{label}</span>'
            )
        role_chips_html.append(chip)
    if not role_chips_html:
        role_strip = (
            '<div class="intrinsics-roles-empty">'
            "No phones online — heartbeats populate this when a device connects."
            "</div>"
        )
    else:
        role_strip = (
            '<div class="intrinsics-roles">'
            + "".join(role_chips_html)
            + "</div>"
        )

    # Stored records list.
    if not records:
        records_html = (
            '<div class="intrinsics-empty">'
            "No ChArUco records yet. Run "
            '<code>calibrate_intrinsics.py</code> on the phone\'s shots, '
            "then upload the resulting JSON below."
            "</div>"
        )
    else:
        row_lines: list[str] = []
        for rec in records:
            did = str(rec.get("device_id") or "")
            model = str(rec.get("device_model") or "")
            fx = rec.get("fx")
            fy = rec.get("fy")
            rms = rec.get("rms_reprojection_px")
            n_img = rec.get("n_images")
            ts = rec.get("calibrated_at")
            distortion = rec.get("distortion")
            has_dist = distortion is not None and len(list(distortion)) == 5
            dist_chip = (
                '<span class="chip ok small">dist ✓</span>'
                if has_dist else
                '<span class="chip warn small">no dist</span>'
            )
            rms_str = f"{float(rms):.2f} px" if isinstance(rms, (int, float)) else "—"
            fx_str = f"{float(fx):.0f}" if isinstance(fx, (int, float)) else "—"
            fy_str = f"{float(fy):.0f}" if isinstance(fy, (int, float)) else "—"
            n_str = f"{int(n_img)}" if isinstance(n_img, int) else "?"
            src_dims = ""
            sw, sh = rec.get("source_width_px"), rec.get("source_height_px")
            if isinstance(sw, int) and isinstance(sh, int):
                src_dims = f'<span class="dim">{sw}×{sh}</span>'
            row_lines.append(
                '<div class="intrinsics-row">'
                '<div class="intrinsics-row-top">'
                f'<span class="dev-id" title="{html.escape(did)}">{html.escape(_fmt_dev_id(did))}</span>'
                f'<span class="dev-model">{html.escape(model or "unknown")}</span>'
                f'{src_dims}'
                f'{dist_chip}'
                f'<button type="button" class="btn small danger" '
                f'data-intrinsics-delete="{html.escape(did)}" '
                f'title="Delete ChArUco record for {html.escape(did)}">×</button>'
                '</div>'
                '<div class="intrinsics-row-sub">'
                f'fx={fx_str} · fy={fy_str} · RMS {rms_str} · {n_str} shots · {html.escape(_fmt_ts(ts if isinstance(ts, (int, float)) else None))}'
                '</div>'
                '</div>'
            )
        records_html = (
            '<div class="intrinsics-list">' + "".join(row_lines) + '</div>'
        )

    # Upload controls. Target-device dropdown is populated from online
    # roles so the operator picks a phone by role instead of copy-pasting
    # a UUID — the JS layer resolves role → device_id at submit time.
    role_options: list[str] = []
    for role, info in sorted(online_roles.items()):
        did = str(info.get("device_id") or "")
        model = str(info.get("device_model") or "")
        label = html.escape(role)
        if model:
            label += f" ({html.escape(model)})"
        if not did:
            continue
        role_options.append(
            f'<option value="{html.escape(did)}" data-role="{html.escape(role)}">{label}</option>'
        )
    if role_options:
        device_select = (
            '<select id="intrinsics-target">' + "".join(role_options) + '</select>'
        )
    else:
        device_select = (
            '<select id="intrinsics-target" disabled>'
            '<option>No phones online</option></select>'
        )

    # Split into dynamic block (replaced wholesale on each /calibration/intrinsics
    # tick) and static block (file input + upload button + status — JS only
    # patches the <select> children, never the surrounding nodes). Without
    # this split, polling stomps the user's just-picked file mid-selection.
    return (
        '<div id="intrinsics-dynamic">'
        f'{role_strip}'
        f'{records_html}'
        '</div>'
        '<div class="intrinsics-upload">'
        '<div class="intrinsics-upload-row">'
        f'{device_select}'
        '<input type="file" id="intrinsics-file" accept=".json,application/json">'
        '<button type="button" class="btn small" id="intrinsics-upload-btn">Upload</button>'
        '</div>'
        '<div class="intrinsics-upload-hint">'
        'Accepts <code>calibrate_intrinsics.py</code> output JSON '
        '(<code>fx / fy / cx / cy / distortion_coeffs / image_width / image_height</code>).'
        '</div>'
        '<div id="intrinsics-upload-status" class="intrinsics-upload-status"></div>'
        '</div>'
    )
