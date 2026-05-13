"""Per-device WS command derivation.

`commands_for_devices` derives the next command each online phone should
receive (`arm` / `disarm` / `sync_run` / absent) from the in-memory
session + sync state. The iPhone receives its slot via WS push
(`/ws/device/{cam}` on hello and on every settings broadcast);
`/status` mirrors the same map for dashboard observability.

Mirrors the free-function + State-as-facade pattern (see state_events.py,
session_results.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state import State


# When a session ends, server keeps advertising `disarm` on /status for a
# brief window so the phone that didn't fire the cycle still gets the signal
# on its next poll. Long enough to cover any sensible poll cadence.
_DISARM_ECHO_S = 5.0


def commands_for_devices(state: "State") -> dict[str, str]:
    """Derive per-device commands from the current session state. The
    iPhone receives its slot via WS push (`/ws/device/{cam}` on hello
    and on every settings broadcast); `/status` mirrors the same map
    for dashboard observability:
      - "sync_run" if a mutual-sync run is active AND this phone has
        not yet posted its report for that run (preempts arm/disarm —
        guarded by `start_sync` to be mutually exclusive with an
        armed session anyway)
      - "arm"    if a session is currently armed
      - "disarm" if a session ended within _DISARM_ECHO_S ago
      - absent   otherwise (steady state, no action required)

    Once a phone has reported for the current sync, we stop re-
    advertising `sync_run` to it so the phone doesn't re-trigger on
    the next heartbeat tick while the peer's report is still in
    flight — `lastAppliedCommand` de-dupe on the phone dedupes on
    `(command, sync_id)`, but dropping the command here is an extra
    defense and keeps the command dict's semantics clean."""
    now = state._time_fn()
    current = state.current_session()  # applies timeout
    online_ids = [d.camera_id for d in state.online_devices()]
    with state._lock:
        state._sync.check_sync_timeout_locked(now)
        sync_run = state._sync.current_sync_locked()
        last_ended = state._most_recent_ended_session_locked()
    cmds: dict[str, str] = {}
    if sync_run is not None:
        for cam in online_ids:
            role = cam  # rig convention: camera_id == role ("A" | "B")
            if role in sync_run.reports:
                continue  # already reported for this run
            cmds[cam] = "sync_run"
        return cmds
    if current is not None:
        for cam in online_ids:
            cmds[cam] = "arm"
    elif last_ended is not None and last_ended.ended_at is not None:
        if now - last_ended.ended_at <= _DISARM_ECHO_S:
            for cam in online_ids:
                cmds[cam] = "disarm"
    return cmds
