from __future__ import annotations

from typing import Iterable


JobKey = tuple[str, str]  # (camera_id, session_id)


class SessionProcessingState:
    """Trash state and server post-processing job status."""

    def __init__(self) -> None:
        self.trashed_sessions: dict[str, float] = {}
        self.server_post_jobs: dict[JobKey, str] = {}
        self.server_post_active_tasks: set[JobKey] = set()

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

    def cancel(self, keys: Iterable[JobKey]) -> bool:
        changed = False
        for key in keys:
            if self.server_post_jobs.get(key) != "canceled":
                self.server_post_jobs[key] = "canceled"
                changed = True
        return changed

    def resume(self, keys: Iterable[JobKey]) -> list[JobKey]:
        queued: list[JobKey] = []
        for key in keys:
            if key in self.server_post_active_tasks:
                continue
            self.server_post_jobs[key] = "queued"
            queued.append(key)
        return queued

    def summary(
        self,
        *,
        pending_keys: set[JobKey],
        completed: bool,
        has_candidates: bool,
    ) -> tuple[str | None, bool]:
        job_states = [
            self.server_post_jobs.get(key)
            for key in pending_keys
            if self.server_post_jobs.get(key) is not None
        ]
        if any(state == "processing" for state in job_states):
            return "processing", True
        if any(state == "queued" for state in job_states) or (pending_keys and not job_states):
            return "queued", True
        if any(state == "canceled" for state in job_states):
            return "canceled", bool(pending_keys)
        if not has_candidates and completed:
            return "completed", False
        return None, False

    def clear(self) -> None:
        self.trashed_sessions.clear()
        self.server_post_jobs.clear()
        self.server_post_active_tasks.clear()

    def remove_session(self, session_id: str) -> None:
        self.trashed_sessions.pop(session_id, None)
        for key in list(self.server_post_jobs.keys()):
            if key[1] == session_id:
                self.server_post_jobs.pop(key, None)
                self.server_post_active_tasks.discard(key)
