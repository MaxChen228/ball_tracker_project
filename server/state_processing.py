"""Server-side post-processing (MOV decode + HSV detect + annotate)
lifecycle + trash state. Split out of `state.py` so `State` focuses on
the in-memory pitch/session data model and routes can reach directly
into a purpose-built coordinator via `state.processing.X`.

Ownership invariants:
- `State.pitches` / `State._video_dir` are read-only to this coordinator.
  Mutations go back through `State.record(...)` / filesystem ops — the
  coordinator only tracks job lifecycle, cancel flags, and per-(session,
  cam) error strings.
- `SessionProcessingState.attach(owner, lock)` wires up the State
  reference + the shared lock used by `State`. All lock-scoped reads
  happen here.
- Persisted trash state still round-trips through `State`: this class
  owns the in-memory `trashed_sessions` dict + serialization via
  `load_trashed` / `trashed_sessions` read; `State` handles the disk JSON.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from schemas import PitchPayload
    from state import State


JobKey = tuple[str, str]  # (camera_id, session_id)


class SessionProcessingState:
    """Trash + server_post-detection job coordinator.

    Constructed before State finishes init (State creates one in its
    ctor). Call `attach(state, lock)` after the owning State is ready
    so the coordinator can read pitches / the video dir / the shared
    lock without circular-import pain.
    """

    def __init__(self) -> None:
        self.trashed_sessions: dict[str, float] = {}
        self.server_post_jobs: dict[JobKey, str] = {}
        self.server_post_active_tasks: set[JobKey] = set()
        # Per-session latest server_post error string:
        # session_id -> {camera_id: error_message}. Populated by
        # routes/pitch.py::_run_server_detection when a stage fails so
        # /events can surface the reason without tailing the log.
        self.server_post_errors: dict[str, dict[str, str]] = {}
        # Owner references wired in by attach(). Separate init-vs-attach
        # so State can instantiate us before its own fields exist.
        self._owner: "State | None" = None
        self._lock: Lock | None = None

    def attach(self, owner: "State", lock: Lock) -> None:
        self._owner = owner
        self._lock = lock

    # ---- Trash (persisted) -------------------------------------------

    def load_trashed(self, trashed: dict[str, float]) -> None:
        self.trashed_sessions.clear()
        self.trashed_sessions.update(trashed)

    def is_trashed(self, session_id: str) -> bool:
        return session_id in self.trashed_sessions

    def trash(self, session_id: str, *, at: float) -> None:
        self.trashed_sessions[session_id] = at
        for key, status in list(self.server_post_jobs.items()):
            _cam, sid = key
            if sid == session_id and status in {"queued", "processing"}:
                self.server_post_jobs[key] = "canceled"

    def restore(self, session_id: str) -> bool:
        if session_id not in self.trashed_sessions:
            return False
        self.trashed_sessions.pop(session_id, None)
        return True

    def trash_count(self) -> int:
        return len(self.trashed_sessions)

    # ---- Low-level job lifecycle -------------------------------------

    def mark_queued(self, key: JobKey) -> None:
        if self.is_trashed(key[1]):
            return
        if key in self.server_post_active_tasks:
            return
        if self.server_post_jobs.get(key) == "processing":
            return
        self.server_post_jobs[key] = "queued"

    def start_job(self, key: JobKey) -> bool:
        if self.is_trashed(key[1]):
            return False
        status = self.server_post_jobs.get(key)
        if status == "canceled":
            return False
        self.server_post_jobs[key] = "processing"
        self.server_post_active_tasks.add(key)
        return True

    def should_cancel(self, key: JobKey) -> bool:
        return self.is_trashed(key[1]) or self.server_post_jobs.get(key) == "canceled"

    def finish_job(self, key: JobKey, *, canceled: bool) -> None:
        self.server_post_active_tasks.discard(key)
        if canceled:
            self.server_post_jobs[key] = "canceled"
        else:
            self.server_post_jobs.pop(key, None)

    def cancel_keys(self, keys: Iterable[JobKey]) -> bool:
        changed = False
        for key in keys:
            if self.server_post_jobs.get(key) != "canceled":
                self.server_post_jobs[key] = "canceled"
                changed = True
        return changed

    def resume_keys(self, keys: Iterable[JobKey]) -> list[JobKey]:
        queued: list[JobKey] = []
        for key in keys:
            if key in self.server_post_active_tasks:
                continue
            self.server_post_jobs[key] = "queued"
            queued.append(key)
        return queued

    # ---- Session-scoped helpers (need State + lock) ------------------
    # These are the methods routes/* now call directly via state.processing.

    def _require_owner(self) -> tuple["State", Lock]:
        if self._owner is None or self._lock is None:
            raise RuntimeError("SessionProcessingState.attach() was never called")
        return self._owner, self._lock

    def find_video_for(self, session_id: str, camera_id: str) -> Path | None:
        owner, _ = self._require_owner()
        matches = sorted(owner._video_dir.glob(f"session_{session_id}_{camera_id}.*"))
        for path in matches:
            if path.name.endswith(".tmp"):
                continue
            return path
        return None

    def session_candidates(
        self, session_id: str
    ) -> list[tuple[str, "PitchPayload", Path]]:
        """Pitches in this session eligible for server-post detection.

        Eligibility: MOV on disk + session not trashed. Previously also
        gated on `frames_server_post` being empty so a completed run
        wouldn't re-queue itself; the viewer's Rerun button now relies
        on this being a re-runnable affordance, so any cam with a MOV
        is fair game. `state.record` overwrites `frames_server_post`
        on each persist — semantics are correct for re-runs."""
        owner, lock = self._require_owner()
        with lock:
            pitches = [
                (cam, pitch)
                for (cam, sid), pitch in owner.pitches.items()
                if sid == session_id
            ]
            trashed = self.is_trashed(session_id)
        if trashed:
            return []
        candidates: list[tuple[str, PitchPayload, Path]] = []
        for cam, pitch in pitches:
            clip_path = self.find_video_for(session_id, cam)
            if clip_path is None:
                continue
            candidates.append((cam, pitch, clip_path))
        return candidates

    # ---- Route-facing API --------------------------------------------

    def mark_server_post_queued(self, session_id: str, camera_id: str) -> None:
        _, lock = self._require_owner()
        with lock:
            self.mark_queued((camera_id, session_id))

    def start_server_post_job(self, session_id: str, camera_id: str) -> bool:
        _, lock = self._require_owner()
        with lock:
            return self.start_job((camera_id, session_id))

    def should_cancel_server_post_job(self, session_id: str, camera_id: str) -> bool:
        _, lock = self._require_owner()
        with lock:
            return self.should_cancel((camera_id, session_id))

    def finish_server_post_job(
        self, session_id: str, camera_id: str, *, canceled: bool
    ) -> None:
        _, lock = self._require_owner()
        with lock:
            self.finish_job((camera_id, session_id), canceled=canceled)

    def record_error(self, session_id: str, camera_id: str, message: str) -> None:
        """Remember a server_post failure reason for display on /events.
        Last write wins per (session, cam) — a retry that succeeds must
        call clear_error for the same key."""
        _, lock = self._require_owner()
        with lock:
            self.server_post_errors.setdefault(session_id, {})[camera_id] = message

    def clear_error(self, session_id: str, camera_id: str) -> None:
        _, lock = self._require_owner()
        with lock:
            cams = self.server_post_errors.get(session_id)
            if cams is None:
                return
            cams.pop(camera_id, None)
            if not cams:
                self.server_post_errors.pop(session_id, None)

    def errors_for(self, session_id: str) -> dict[str, str]:
        _, lock = self._require_owner()
        with lock:
            return dict(self.server_post_errors.get(session_id, {}))

    def cancel_processing(self, session_id: str) -> bool:
        keys = [
            (cam, session_id)
            for cam, _pitch, _clip in self.session_candidates(session_id)
        ]
        _, lock = self._require_owner()
        with lock:
            return self.cancel_keys(keys)

    def resume_processing(
        self, session_id: str
    ) -> list[tuple[Path, "PitchPayload"]]:
        candidates = self.session_candidates(session_id)
        by_key = {
            (cam, session_id): (clip_path, pitch)
            for cam, pitch, clip_path in candidates
        }
        _, lock = self._require_owner()
        with lock:
            queued_keys = self.resume_keys(by_key.keys())
        return [
            (by_key[key][0], by_key[key][1].model_copy(deep=True))
            for key in queued_keys
        ]

    def session_summary(self, session_id: str) -> tuple[str | None, bool]:
        candidates = self.session_candidates(session_id)
        pending_keys = {(cam, session_id) for cam, _pitch, _clip in candidates}
        owner, lock = self._require_owner()
        with lock:
            completed = any(
                sid == session_id and bool(pitch.frames_server_post)
                for (_cam, sid), pitch in owner.pitches.items()
            )
            return self._summary_chip(
                pending_keys=pending_keys,
                completed=completed,
                has_candidates=bool(candidates),
            )

    def _summary_chip(
        self,
        *,
        pending_keys: set[JobKey],
        completed: bool,
        has_candidates: bool,
    ) -> tuple[str | None, bool]:
        """Processing chip state for a single session.

        Post-redesign, server-post detection is opt-in per-session: we
        only show a chip when the operator has actually done something
        (queued/processing/canceled). A session sitting with a MOV but
        no triggered job is the *default* state and shows nothing — the
        "Run srv" action button is the affordance.
        """
        job_states = [
            self.server_post_jobs.get(key)
            for key in pending_keys
            if self.server_post_jobs.get(key) is not None
        ]
        if any(state == "processing" for state in job_states):
            return "processing", True
        if any(state == "queued" for state in job_states):
            return "queued", True
        if any(state == "canceled" for state in job_states):
            return "canceled", bool(pending_keys)
        if not has_candidates and completed:
            return "completed", False
        return None, False

    # ---- Reset / delete cleanup --------------------------------------

    def clear(self) -> None:
        self.trashed_sessions.clear()
        self.server_post_jobs.clear()
        self.server_post_active_tasks.clear()
        self.server_post_errors.clear()

    def remove_session(self, session_id: str) -> None:
        self.trashed_sessions.pop(session_id, None)
        self.server_post_errors.pop(session_id, None)
        for key in list(self.server_post_jobs.keys()):
            if key[1] == session_id:
                self.server_post_jobs.pop(key, None)
                self.server_post_active_tasks.discard(key)
