"""Dashboard Intrinsics (ChArUco) card body.

The card surfaces three orthogonal concerns, each in its own section so
operators don't conflate "which phone is plugged in right now" with
"which phones we've ever measured":

  1. **Pairing** — current Cam A / Cam B → device_id mapping. A role with
     no online device shows `· offline`; a role whose online device has
     no stored record shows `· cal ?`. Always renders both A and B rows
     so the operator immediately sees which slot is unfilled.
  2. **Records** — every stored ChArUco record, keyed by device_id. Each
     row tags itself with `[USED AS A]` / `[USED AS B]` when its device
     is currently the online occupant of that role. Persistent across
     sessions; offline devices keep their record.
  3. **Upload** — pick from a union of (currently-online roles) and
     (devices we already have records for, even if offline). For a brand-
     new offline device, the operator can paste the `device_id` directly
     into a manual text field and upload — no need to wait for the phone
     to connect.

The split mirrors the data model: pairing is ephemeral state from the
heartbeat registry, records are persistent storage on disk, upload is
the bridge between them. Earlier UX collapsed all three into a single
chip strip + dropdown, which left "no phones online" as a confusing
top-line message even when the operator just wanted to update a record
for a phone that happened to be in another room."""
from __future__ import annotations

import datetime as _dt
import html


_ROLES = ("A", "B")


def _fmt_dev_id(device_id: str) -> str:
    """Shorten UUIDs for display. `identifierForVendor` is 36 chars — too
    wide for the sidebar. First 8 is enough to distinguish across a rig of
    a handful of phones."""
    if not device_id:
        return ""
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
    """SSR-paint a usable skeleton even when `records` / `online_roles`
    are `None` (initial page load before the first /calibration/intrinsics
    poll lands) so the card never flashes empty."""
    records = records or []
    online_roles = online_roles or {}
    record_by_device = {str(r.get("device_id") or ""): r for r in records}

    pairing_html = _render_pairing(online_roles, record_by_device)
    records_html = _render_records_list(records, online_roles)
    upload_html = _render_upload_section(online_roles, records)

    # Dynamic block (pairing + records) gets replaced wholesale on each
    # /calibration/intrinsics tick; upload block is SSR-static so the
    # user's picked file isn't wiped mid-selection (DOM file lists can't
    # be reattached to a fresh <input> element).
    return (
        '<div id="intrinsics-dynamic">'
        f'{pairing_html}'
        f'{records_html}'
        '</div>'
        f'{upload_html}'
    )


def _render_pairing(
    online_roles: dict[str, dict[str, object]],
    record_by_device: dict[str, dict[str, object]],
) -> str:
    """Always render both Cam A and Cam B rows. Missing role = offline."""
    rows: list[str] = []
    for role in _ROLES:
        info = online_roles.get(role)
        rows.append(_render_pairing_row(role, info, record_by_device))
    return (
        '<div class="intrinsics-section">'
        '<div class="intrinsics-section-title">Pairing</div>'
        '<div class="intrinsics-pairing">'
        + "".join(rows)
        + '</div>'
        '</div>'
    )


def _render_pairing_row(
    role: str,
    info: dict[str, object] | None,
    record_by_device: dict[str, dict[str, object]],
) -> str:
    role_esc = html.escape(role)
    if info is None:
        return (
            f'<div class="intrinsics-pair offline">'
            f'<span class="pair-role">Cam {role_esc}</span>'
            f'<span class="pair-arrow">·</span>'
            f'<span class="pair-state">offline</span>'
            f'</div>'
        )
    did = str(info.get("device_id") or "")
    model = str(info.get("device_model") or "")
    if not did:
        return (
            f'<div class="intrinsics-pair legacy">'
            f'<span class="pair-role">Cam {role_esc}</span>'
            f'<span class="pair-arrow">→</span>'
            f'<span class="pair-state">legacy client (no device_id)</span>'
            f'</div>'
        )
    has_record = did in record_by_device
    cal_chip = (
        '<span class="chip ok small">cal ✓</span>'
        if has_record
        else '<span class="chip warn small">cal ?</span>'
    )
    model_html = (
        f'<span class="pair-model">({html.escape(model)})</span>' if model else ""
    )
    return (
        f'<div class="intrinsics-pair online">'
        f'<span class="pair-role">Cam {role_esc}</span>'
        f'<span class="pair-arrow">→</span>'
        f'<span class="pair-id" title="{html.escape(did)}">'
        f'{html.escape(_fmt_dev_id(did))}</span>'
        f'{model_html}'
        f'{cal_chip}'
        f'</div>'
    )


def _render_records_list(
    records: list[dict[str, object]],
    online_roles: dict[str, dict[str, object]],
) -> str:
    """device_id-keyed rows; flagged with `[USED AS A/B]` when an online
    role currently points at this device. The flag is the only way for
    the operator to know "this row applies to the phone in front of me
    right now" without cross-referencing the pairing strip above."""
    if not records:
        return (
            '<div class="intrinsics-section">'
            '<div class="intrinsics-section-title">Records</div>'
            '<div class="intrinsics-empty">'
            'No ChArUco records yet. Run <code>calibrate_intrinsics.py</code> '
            "on the phone's shots, then upload the resulting JSON below."
            '</div>'
            '</div>'
        )
    device_to_roles: dict[str, list[str]] = {}
    for role, info in online_roles.items():
        did = str((info or {}).get("device_id") or "")
        if did:
            device_to_roles.setdefault(did, []).append(role)

    row_lines: list[str] = []
    for rec in records:
        did = str(rec.get("device_id") or "")
        model = str(rec.get("device_model") or "unknown")
        fx = rec.get("fx")
        fy = rec.get("fy")
        rms = rec.get("rms_reprojection_px")
        n_img = rec.get("n_images")
        ts = rec.get("calibrated_at")
        distortion = rec.get("distortion")
        has_dist = distortion is not None and len(list(distortion)) == 5
        dist_chip = (
            '<span class="chip ok small">dist ✓</span>'
            if has_dist
            else '<span class="chip warn small">no dist</span>'
        )
        rms_str = f"{float(rms):.2f} px" if isinstance(rms, (int, float)) else "—"
        fx_str = f"{float(fx):.0f}" if isinstance(fx, (int, float)) else "—"
        fy_str = f"{float(fy):.0f}" if isinstance(fy, (int, float)) else "—"
        n_str = f"{int(n_img)}" if isinstance(n_img, int) else "?"
        src_dims = ""
        sw, sh = rec.get("source_width_px"), rec.get("source_height_px")
        if isinstance(sw, int) and isinstance(sh, int):
            src_dims = f'<span class="dim">{sw}×{sh}</span>'
        used_chips = "".join(
            f'<span class="chip ok small used-as">used as {html.escape(r)}</span>'
            for r in sorted(device_to_roles.get(did, []))
        )
        row_lines.append(
            '<div class="intrinsics-row">'
            '<div class="intrinsics-row-top">'
            f'<span class="dev-id" title="{html.escape(did)}">{html.escape(_fmt_dev_id(did))}</span>'
            f'<span class="dev-model">{html.escape(model)}</span>'
            f'{src_dims}'
            f'{dist_chip}'
            f'{used_chips}'
            f'<button type="button" class="btn small danger" '
            f'data-intrinsics-delete="{html.escape(did)}" '
            f'title="Delete ChArUco record for {html.escape(did)}">×</button>'
            '</div>'
            '<div class="intrinsics-row-sub">'
            f'fx={fx_str} · fy={fy_str} · RMS {rms_str} · {n_str} shots · '
            f'{html.escape(_fmt_ts(ts if isinstance(ts, (int, float)) else None))}'
            '</div>'
            '</div>'
        )
    return (
        '<div class="intrinsics-section">'
        '<div class="intrinsics-section-title">Records</div>'
        '<div class="intrinsics-list">'
        + "".join(row_lines)
        + '</div>'
        '</div>'
    )


def _render_upload_section(
    online_roles: dict[str, dict[str, object]],
    records: list[dict[str, object]],
) -> str:
    """Target picker = union of (online roles with device_id) and
    (devices that already have a record). Keeps offline-but-known devices
    selectable. For a fully new offline device, the operator falls back
    to the manual `device_id` text field."""
    options_html = _render_target_options(online_roles, records)
    if options_html:
        device_select = (
            '<select id="intrinsics-target">'
            f'{options_html}'
            '</select>'
        )
    else:
        device_select = (
            '<select id="intrinsics-target" disabled>'
            '<option value="">(no devices yet — use manual id below)</option>'
            '</select>'
        )
    return (
        '<div class="intrinsics-upload">'
        '<div class="intrinsics-section-title">Upload</div>'
        '<div class="intrinsics-upload-row">'
        '<label class="upload-field">'
        '<span class="upload-field-label">Target</span>'
        f'{device_select}'
        '</label>'
        '</div>'
        '<div class="intrinsics-upload-row">'
        '<label class="upload-field">'
        '<span class="upload-field-label">or device_id</span>'
        '<input type="text" id="intrinsics-target-manual" '
        'placeholder="paste identifierForVendor for an offline phone" '
        'autocomplete="off" spellcheck="false">'
        '</label>'
        '</div>'
        '<div class="intrinsics-upload-row">'
        '<input type="file" id="intrinsics-file" accept=".json,application/json">'
        '<button type="button" class="btn small" id="intrinsics-upload-btn">Upload</button>'
        '</div>'
        '<div id="intrinsics-upload-status" class="intrinsics-upload-status"></div>'
        '</div>'
    )


def _render_target_options(
    online_roles: dict[str, dict[str, object]],
    records: list[dict[str, object]],
) -> str:
    """Group <option>s into two <optgroup>s — Online roles, then Known
    records (offline). A device that is both online AND has a record
    appears under its role only, so the operator never sees the same
    device twice in the dropdown."""
    seen: set[str] = set()
    online_opts: list[str] = []
    for role, info in sorted(online_roles.items()):
        did = str((info or {}).get("device_id") or "")
        if not did or did in seen:
            continue
        seen.add(did)
        model = str((info or {}).get("device_model") or "")
        label = f"Cam {html.escape(role)} → {html.escape(_fmt_dev_id(did))}"
        if model:
            label += f" ({html.escape(model)})"
        online_opts.append(
            f'<option value="{html.escape(did)}" '
            f'data-role="{html.escape(role)}">{label}</option>'
        )
    offline_opts: list[str] = []
    for rec in records:
        did = str(rec.get("device_id") or "")
        if not did or did in seen:
            continue
        seen.add(did)
        model = str(rec.get("device_model") or "")
        label = html.escape(_fmt_dev_id(did))
        if model:
            label += f" ({html.escape(model)})"
        offline_opts.append(
            f'<option value="{html.escape(did)}">{label}</option>'
        )
    parts: list[str] = []
    if online_opts:
        parts.append('<optgroup label="Online">')
        parts.extend(online_opts)
        parts.append("</optgroup>")
    if offline_opts:
        parts.append('<optgroup label="Known (offline)">')
        parts.extend(offline_opts)
        parts.append("</optgroup>")
    return "".join(parts)
