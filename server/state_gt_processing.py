"""GT-driven distillation job coordinator.

In-memory state tracking for SAM 3 labelling, three-way validation,
and parameter distillation jobs. Mirrors the `SessionProcessingState`
pattern (lifecycle + cancel flags) but is much simpler — no
persistence, no trash, jobs evaporate on server restart.

Job key: (kind, session_id, camera_id) where:
  - kind ∈ {"label", "validate", "distill"}
  - "global" is used for non-session-scoped jobs (currently just
    distillation, which fits across all GT records at once).

Status transitions:
  none → queued/running → completed | canceled | error

Concurrency: GTProcessingState is accessed from FastAPI request
handlers (event loop) AND from BackgroundTask threads. The state
class uses a non-reentrant Lock for protection — calls are short.
"""
from __future__ import annotations

from threading import Lock
from typing import Tuple

# (kind, session_id, camera_id) — "global" used for non-session jobs.
GTJobKey = Tuple[str, str, str]


class GTProcessingState:
    def __init__(self) -> None:
        self._lock = Lock()
        # job_key → status string. Status values:
        #   "running"   — task started, not yet finished
        #   "completed" — task ended, exit code 0
        #   "canceled"  — operator-triggered termination
        #   "error"     — task ended, non-zero exit / exception
        self._status: dict[GTJobKey, str] = {}
        # job_key → "true" if cancel was requested. Workers poll
        # `is_canceled(key)`; SAM 3 subprocess gets SIGTERM via the
        # caller checking + Popen.terminate(). Distinct from status
        # so a finished job can still know it was canceled.
        self._cancel_requested: set[GTJobKey] = set()
        # job_key → subprocess PID (only for SAM 3 label jobs). Other
        # job kinds run in-process and don't need a PID.
        self._pids: dict[GTJobKey, int] = {}
        # job_key → human-readable error message for the dashboard.
        # Cleared when a job re-enters "running".
        self._errors: dict[GTJobKey, str] = {}

    def start_job(self, key: GTJobKey) -> bool:
        """Returns True if the job was successfully queued (not already
        running). False means the caller should NOT spawn a worker —
        a previous attempt is still pending."""
        with self._lock:
            if self._status.get(key) == "running":
                return False
            self._status[key] = "running"
            self._cancel_requested.discard(key)
            self._errors.pop(key, None)
            self._pids.pop(key, None)
            return True

    def finish_job(self, key: GTJobKey, *, status: str, error: str | None = None) -> None:
        """Mark a job done. Status must be one of completed / canceled
        / error. `error` (optional) is the message the dashboard shows."""
        if status not in ("completed", "canceled", "error"):
            raise ValueError(f"unknown finish status: {status!r}")
        with self._lock:
            self._status[key] = status
            if error is not None:
                self._errors[key] = error
            self._pids.pop(key, None)

    def cancel_session(self, session_id: str) -> int:
        """Mark every running job whose session matches as canceled.
        Returns the number of jobs flagged. Workers see the flag on
        their next poll and bail.

        Special case: distillation is keyed `("distill", "global",
        "global")` because it spans every record, not a single session.
        The dashboard exposes it under the same Cancel surface as
        per-session jobs, so passing `"global"` flags the distill job
        too — without this the operator could trigger a multi-minute
        eval and have no way to abort."""
        n = 0
        with self._lock:
            for key, status in list(self._status.items()):
                if key[1] == session_id and status == "running":
                    self._cancel_requested.add(key)
                    n += 1
        return n

    def cancel_distill(self) -> bool:
        """Flag the global distill job for cancellation. Returns True
        if a running distill job was found and flagged. Routes call
        this from /gt/cancel_distill (and we also surface it through
        cancel_session('global') as a convenience)."""
        key = ("distill", "global", "global")
        with self._lock:
            if self._status.get(key) == "running":
                self._cancel_requested.add(key)
                return True
            return False

    def is_canceled(self, key: GTJobKey) -> bool:
        with self._lock:
            return key in self._cancel_requested

    def set_subprocess_pid(self, key: GTJobKey, pid: int) -> None:
        """SAM 3 subprocess only — others don't need it."""
        with self._lock:
            self._pids[key] = pid

    def status_for(self, key: GTJobKey) -> str | None:
        with self._lock:
            return self._status.get(key)

    def snapshot(self) -> dict:
        """Returns a JSON-friendly dict for /status payload + dashboard
        rendering. Shape:
          {
            "running":   [{"kind", "sid", "cam"}, ...],
            "completed": [{"kind", "sid", "cam"}, ...],
            "errors":    {<key_str>: <error_msg>}
          }
        """
        with self._lock:
            running, completed, errors_out = [], [], {}
            for key, status in self._status.items():
                kind, sid, cam = key
                entry = {"kind": kind, "sid": sid, "cam": cam}
                if status == "running":
                    running.append(entry)
                elif status == "completed":
                    completed.append(entry)
                # canceled / error are surfaced via the errors map for
                # the dashboard chip; running/completed alone is enough
                # for tick rendering.
            for key, msg in self._errors.items():
                errors_out[":".join(key)] = msg
            return {"running": running, "completed": completed, "errors": errors_out}
