"""Dashboard Intrinsics (ChArUco) card body.

Only surfaces the **pairing** view: for each expected camera role, is the
currently-connected device's `device_id` known to have a ChArUco record?
That's the single question an operator standing at the rig needs answered.

Why no records list:
  - `device_id` is `identifierForVendor`, which rotates on app reinstall.
    A historical list of device_ids is misleading — the same physical
    phone can appear as several rows, and an entry for a long-gone
    install carries no operational meaning.
  - The dashboard cares about the *connected* device, not the catalog of
    every device_id ever calibrated. Records still live on disk (the
    server uses them to fill in `/pitch` payloads) and DELETE
    `/calibration/intrinsics/{device_id}` is preserved for housekeeping.
"""
from __future__ import annotations

import html


def _roles() -> tuple[str, ...]:
    """Camera roles the intrinsics card should render rows for. Single
    source of truth is `State.expected_camera_ids()` — adding a third
    camera to the rig grows the pairing table without code changes."""
    from main import state  # local import: avoid circular at module load
    return tuple(state.expected_camera_ids())


def _fmt_dev_id(device_id: str) -> str:
    if not device_id:
        return ""
    return device_id[:8] + "…" if len(device_id) > 10 else device_id


def _render_intrinsics_body(
    records: list[dict[str, object]] | None = None,
    online_roles: dict[str, dict[str, object]] | None = None,
) -> str:
    """SSR-paint a usable skeleton even when `records` / `online_roles`
    are `None` (initial page load before the first /calibration/intrinsics
    poll lands) so the card never flashes empty."""
    records = records or []
    online_roles = online_roles or {}
    record_ids = {str(r.get("device_id") or "") for r in records}
    pairing_html = _render_pairing(online_roles, record_ids)
    return f'<div id="intrinsics-dynamic">{pairing_html}</div>'


def _render_pairing(
    online_roles: dict[str, dict[str, object]],
    record_ids: set[str],
) -> str:
    """Always render every expected role. Missing role = offline."""
    rows = "".join(
        _render_pairing_row(role, online_roles.get(role), record_ids)
        for role in _roles()
    )
    return f'<div class="intrinsics-pairing">{rows}</div>'


def _render_pairing_row(
    role: str,
    info: dict[str, object] | None,
    record_ids: set[str],
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
    cal_chip = (
        '<span class="chip ok small">cal ✓</span>'
        if did in record_ids
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
