"""Dashboard Quick Sync card body.

Operator UI for the multi-cam quick-sync flow (single emitter plays a
band-A chirp; every cam — emitter included — records and uploads its
WAV; server matched-filters and solves per-cam anchors; operator
applies the run to stamp the device registry).

SSR-paints a usable skeleton with an emitter `<select>` populated from
**currently-online cams** (NOT `expected_camera_ids` — `POST /sync/quick_start`
returns 409 `emitter_offline` for a cam that isn't online, so the
dropdown must reflect live online state to avoid foot-guns). A JS tick
(`tickQuickSync` in `static/dashboard/56_quick_sync.js`) polls
`/sync/quick_state` and re-renders `#quick-sync-dynamic` with progress /
solved anchors / Apply button enablement.

The Apply button is the half that closes BLOCK#1: it POSTs
`/sync/quick_apply/{sync_id}` which stamps the registry AND broadcasts
`quick_sync_applied` back to each stamped cam so iOS adopts the anchor
as `lastSyncAnchor` (preventing the next heartbeat from wiping it)."""
from __future__ import annotations

import html


def _render_quick_sync_body(
    *,
    online_cam_ids: list[str],
) -> str:
    """SSR skeleton. The dynamic block (`#quick-sync-dynamic`) is empty
    at first paint — the first JS poll lands within ~1 s and fills in
    status + solved anchors. Only the emitter selector + buttons are
    server-rendered so the operator can fire a sync immediately without
    waiting for the first poll."""
    options = "".join(
        f'<option value="{html.escape(cam)}">{html.escape(cam)}</option>'
        for cam in online_cam_ids
    )
    if not online_cam_ids:
        # Fail-loud: no online cams → no foot-gun emitter pick. The JS
        # tick re-renders the selector once a phone connects.
        selector = (
            '<select id="quick-sync-emitter" disabled>'
            '<option>(no online cams)</option>'
            '</select>'
        )
        start_disabled = " disabled"
    else:
        selector = (
            f'<select id="quick-sync-emitter">{options}</select>'
        )
        start_disabled = ""
    return (
        '<div class="quick-sync-controls">'
        '<label class="quick-sync-emitter-label">'
        'Emitter: ' + selector +
        '</label>'
        f'<button type="button" class="btn" id="quick-sync-start"{start_disabled}>'
        'Start quick sync</button>'
        '</div>'
        '<div id="quick-sync-dynamic" class="quick-sync-dynamic muted">'
        'Waiting for first poll…'
        '</div>'
    )
