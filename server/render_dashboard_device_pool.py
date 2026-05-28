"""Dashboard Device Pool card body.

Surfaces the multi-camera rig assignment state shipped in Phase 0 PR1:
  - persistent device_uuid → camera_id assignments (survives restart)
  - observed-online phones whose device_uuid has no persistent record
    yet — the operator's promotion candidates

In PR2 (this card) the assignments are advisory only: the WS handler
still accepts whatever cam_id the phone connects with. PR3 will gate
the WS handshake on these records so that an unassigned device_uuid
sits in pending mode until the dashboard promotes it.

SSR-paints a usable skeleton with whatever the page-render layer hands
us; a JS tick poll (`tickDevicePool` in `render_dashboard_client.py`)
refreshes from /devices/pool and re-renders #device-pool-dynamic.
"""
from __future__ import annotations

import html


def _short_uuid(uuid: str) -> str:
    """First 8 chars + ellipsis. The uuid is identifierForVendor (full
    36-char UUID); cramming it into a sidebar card needs truncation, but
    we keep the full value in a `title=` so hover reveals the rest."""
    if not uuid:
        return ""
    return uuid[:8] + "…" if len(uuid) > 10 else uuid


def _render_device_pool_body(
    *,
    assignments: list[dict[str, object]] | None = None,
    observed_unassigned: list[dict[str, object]] | None = None,
) -> str:
    assignments = assignments or []
    observed_unassigned = observed_unassigned or []
    inner = _render_pool_inner(assignments, observed_unassigned)
    return f'<div id="device-pool-dynamic">{inner}</div>'


def _render_pool_inner(
    assignments: list[dict[str, object]],
    observed_unassigned: list[dict[str, object]],
) -> str:
    parts: list[str] = []

    if not assignments and not observed_unassigned:
        parts.append(
            '<div class="device-pool-empty muted">'
            'No devices yet. Connect a phone or assign a known UUID below.'
            '</div>'
        )
    if assignments:
        rows = "".join(_render_assignment_row(rec) for rec in assignments)
        parts.append(
            '<div class="device-pool-section">'
            '<div class="device-pool-section-title">Assigned</div>'
            f'<div class="device-pool-rows">{rows}</div>'
            '</div>'
        )
    if observed_unassigned:
        rows = "".join(_render_observed_row(rec) for rec in observed_unassigned)
        parts.append(
            '<div class="device-pool-section">'
            '<div class="device-pool-section-title">Observed (unassigned)</div>'
            f'<div class="device-pool-rows">{rows}</div>'
            '</div>'
        )
    return "".join(parts)


def _render_assignment_row(rec: dict[str, object]) -> str:
    cam = html.escape(str(rec.get("camera_id") or ""))
    uuid = str(rec.get("device_uuid") or "")
    model = str(rec.get("device_model") or "")
    online = bool(rec.get("online"))
    online_chip = (
        '<span class="chip ok small">online</span>'
        if online
        else '<span class="chip warn small">offline</span>'
    )
    model_html = (
        f'<span class="pool-model">({html.escape(model)})</span>' if model else ""
    )
    return (
        f'<div class="device-pool-row assigned">'
        f'<span class="pool-cam">Cam {cam}</span>'
        f'<span class="pool-arrow">→</span>'
        f'<span class="pool-uuid" title="{html.escape(uuid)}">'
        f'{html.escape(_short_uuid(uuid))}</span>'
        f'{model_html}'
        f'{online_chip}'
        f'<button type="button" class="pool-action" '
        f'data-device-pool-action="unassign" '
        f'data-camera-id="{cam}">Unassign</button>'
        f'</div>'
    )


def _render_observed_row(rec: dict[str, object]) -> str:
    cam = html.escape(str(rec.get("camera_id") or ""))
    uuid = str(rec.get("device_uuid") or "")
    model = str(rec.get("device_model") or "")
    model_html = (
        f'<span class="pool-model">({html.escape(model)})</span>' if model else ""
    )
    return (
        f'<div class="device-pool-row observed">'
        f'<span class="pool-cam-current">currently Cam {cam}</span>'
        f'<span class="pool-uuid" title="{html.escape(uuid)}">'
        f'{html.escape(_short_uuid(uuid))}</span>'
        f'{model_html}'
        f'<button type="button" class="pool-action" '
        f'data-device-pool-action="assign" '
        f'data-device-uuid="{html.escape(uuid)}" '
        f'data-suggested-camera-id="{cam}" '
        f'data-device-model="{html.escape(model)}">Assign…</button>'
        f'</div>'
    )
