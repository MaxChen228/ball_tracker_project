"""Persistent FIFO queue for SAM 3 GT labelling jobs.

Design principles (per mini-plan v4):

  * Operator drives the queue from /gt — adds (session, cam, time_range,
    prompt) items, presses Run, watches a single worker chew through.
  * Persistence: every mutation flushes the full queue.json under a single
    lock so a crash mid-write can never produce a partial file. On boot we
    drag any "running" item back to "pending" — the in-process worker
    means a server kill always co-killed the subprocess.
  * Concurrency invariant: callers must NEVER hold this queue's lock while
    calling into State (or vice-versa). Resolve all reads against State
    BEFORE invoking queue methods. State's lock and this lock are not
    nested; mixing them in either order leads to AB/BA deadlock since
    routes hit both surfaces.
  * No reason-stack on pause: a single bool flag is enough — operator who
    wants to interleave distillation manually presses [Pause] before
    [Run distillation], then [Run] when distill is done. Distill never
    auto-touches this queue (mini-plan v4 cut).

The queue is the SOLE coordinator. The worker thread (gt_queue_worker.py)
is the SOLE consumer. Routes only call mutation methods (add / cancel /
retry / pause / resume) and read methods (get_all / next_pending).

JSON shape on disk (data/gt/queue.json):
    {"items": [<GTQueueItem dict>, ...]}
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

logger = logging.getLogger(__name__)


_QUEUE_ID_RE = re.compile(r"^q_[0-9a-f]{8}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class GTQueueItem:
    """One operator-submitted SAM 3 labelling job.

    `time_range` is video-relative seconds (NOT anchor-relative — see
    mini-plan v4 clock convention; sync_anchor can be 357s before
    video_start on the smoke-tested session, so anchor-relative would
    silently filter out empty sets).

    `subprocess_pid` is informational only; recover_on_boot does NOT
    consult it (avoids macOS PID-recycle false positives during long
    server downtime). The worker thread is in-process so a server kill
    necessarily co-kills the subprocess.
    """
    id: str  # "q_" + 8 hex
    session_id: str
    camera_id: str  # "A" | "B"
    time_range: tuple[float, float]
    prompt: str
    status: Literal["pending", "running", "done", "error", "canceled"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict | None = None  # {current_frame, total_frames, ms_per_frame}
    n_labelled: int | None = None
    n_decoded: int | None = None
    error: str | None = None  # truncated stderr tail
    subprocess_pid: int | None = None  # info-only

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tuple → list for JSON
        d["time_range"] = list(self.time_range)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GTQueueItem:
        rng = d.get("time_range")
        if isinstance(rng, list) and len(rng) == 2:
            d = {**d, "time_range": (float(rng[0]), float(rng[1]))}
        return cls(**d)


class GTQueue:
    """Persistent FIFO queue + status machine for SAM 3 labelling jobs.

    All mutation methods acquire `self._lock` and flush `queue.json`
    under it. Reads do NOT need the lock (we return shallow dict copies
    via `get_all` / `get`). The worker thread polls `next_pending` and
    `is_cancel_requested` from a separate process-level loop.
    """

    def __init__(self, queue_path: Path, preview_dir: Path) -> None:
        self._lock = Lock()
        self._path = queue_path
        self._preview_dir = preview_dir
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, GTQueueItem] = {}
        self._cancel_requested: set[str] = set()
        self._paused = False
        self._load_from_disk()

    # ----- persistence -------------------------------------------------

    def _load_from_disk(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text())
        except Exception as e:
            logger.warning("queue.json corrupt — starting empty (%s)", e)
            return
        for d in raw.get("items", []):
            try:
                item = GTQueueItem.from_dict(d)
                self._items[item.id] = item
            except Exception as e:
                logger.warning("skipping malformed queue item: %s (%s)", d, e)

    def _flush_locked(self) -> None:
        """Atomic write — caller must hold self._lock."""
        payload = {"items": [it.to_dict() for it in self._items.values()]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self._path)

    def _preview_path(self, item_id: str) -> Path:
        return self._preview_dir / f"{item_id}.jpg"

    def _delete_preview_locked(self, item_id: str) -> None:
        """Best-effort cleanup. Caller must hold self._lock."""
        try:
            self._preview_path(item_id).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("preview delete failed for %s: %s", item_id, e)

    # ----- mutators ----------------------------------------------------

    def add(
        self,
        *,
        session_id: str,
        camera_id: str,
        time_range: tuple[float, float],
        prompt: str,
    ) -> str:
        """Append a new pending item; returns minted id."""
        item = GTQueueItem(
            id="q_" + secrets.token_hex(4),
            session_id=session_id,
            camera_id=camera_id,
            time_range=(float(time_range[0]), float(time_range[1])),
            prompt=prompt,
            status="pending",
            created_at=_utc_now_iso(),
        )
        with self._lock:
            self._items[item.id] = item
            self._flush_locked()
        return item.id

    def cancel(self, item_id: str) -> bool:
        """Cancel a pending or running item.

        Pending → status flips to canceled, preview cleaned, item kept
        for audit (Clear errors/canceled UI button removes it).
        Running → set _cancel_requested, worker poll picks it up,
        worker calls mark_canceled which finalises + cleans preview.
        Returns True if the cancel was applied to a non-terminal item.
        """
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if item.status == "pending":
                item.status = "canceled"
                item.finished_at = _utc_now_iso()
                self._delete_preview_locked(item_id)
                self._flush_locked()
                return True
            if item.status == "running":
                self._cancel_requested.add(item_id)
                # Worker will SIGTERM/SIGKILL and call mark_canceled.
                return True
            return False

    def retry(self, item_id: str) -> str | None:
        """Re-queue an error/canceled item with a fresh id.

        Old item stays (audit trail) but its preview JPEG is removed
        because the new id will get its own. Returns the new id, or
        None if the source item isn't retryable.
        """
        with self._lock:
            old = self._items.get(item_id)
            if old is None or old.status not in ("error", "canceled", "done"):
                return None
            new_item = GTQueueItem(
                id="q_" + secrets.token_hex(4),
                session_id=old.session_id,
                camera_id=old.camera_id,
                time_range=old.time_range,
                prompt=old.prompt,
                status="pending",
                created_at=_utc_now_iso(),
            )
            self._items[new_item.id] = new_item
            self._delete_preview_locked(old.id)
            self._flush_locked()
        return new_item.id

    def remove(self, item_id: str) -> bool:
        """Hard-delete a terminal item (done/error/canceled). Used by
        UI Clear buttons. Returns True if removed."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.status in ("pending", "running"):
                return False
            self._delete_preview_locked(item_id)
            del self._items[item_id]
            self._flush_locked()
            return True

    def clear_done(self) -> int:
        with self._lock:
            doomed = [i for i, it in self._items.items() if it.status == "done"]
            for i in doomed:
                self._delete_preview_locked(i)
                del self._items[i]
            if doomed:
                self._flush_locked()
            return len(doomed)

    def clear_errors(self) -> int:
        with self._lock:
            doomed = [
                i for i, it in self._items.items()
                if it.status in ("error", "canceled")
            ]
            for i in doomed:
                self._delete_preview_locked(i)
                del self._items[i]
            if doomed:
                self._flush_locked()
            return len(doomed)

    # ----- worker-side mutators ----------------------------------------

    def mark_running(self, item_id: str, pid: int) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.status = "running"
            item.started_at = _utc_now_iso()
            item.subprocess_pid = pid
            item.error = None
            self._flush_locked()

    def mark_progress(self, item_id: str, progress: dict) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.progress = progress
            self._flush_locked()

    def mark_done(self, item_id: str, n_labelled: int, n_decoded: int) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.status = "done"
            item.finished_at = _utc_now_iso()
            item.n_labelled = n_labelled
            item.n_decoded = n_decoded
            item.subprocess_pid = None
            self._cancel_requested.discard(item_id)
            self._delete_preview_locked(item_id)
            self._flush_locked()

    def mark_error(self, item_id: str, msg: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.status = "error"
            item.finished_at = _utc_now_iso()
            item.error = msg[-2000:]
            item.subprocess_pid = None
            self._cancel_requested.discard(item_id)
            self._delete_preview_locked(item_id)
            self._flush_locked()

    def mark_canceled(self, item_id: str) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.status = "canceled"
            item.finished_at = _utc_now_iso()
            item.subprocess_pid = None
            self._cancel_requested.discard(item_id)
            self._delete_preview_locked(item_id)
            self._flush_locked()

    # ----- queries -----------------------------------------------------

    def get_all(self) -> list[GTQueueItem]:
        with self._lock:
            # Stable order: created_at ascending = FIFO.
            return sorted(self._items.values(), key=lambda i: i.created_at)

    def get(self, item_id: str) -> GTQueueItem | None:
        with self._lock:
            return self._items.get(item_id)

    def next_pending(self) -> GTQueueItem | None:
        """First pending item by FIFO (created_at ascending)."""
        with self._lock:
            pendings = [it for it in self._items.values() if it.status == "pending"]
            if not pendings:
                return None
            pendings.sort(key=lambda i: i.created_at)
            return pendings[0]

    def is_cancel_requested(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._cancel_requested

    def has_running(self) -> bool:
        with self._lock:
            return any(it.status == "running" for it in self._items.values())

    # ----- pause / resume ---------------------------------------------

    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    # ----- boot recovery ----------------------------------------------

    def recover_on_boot(self) -> tuple[int, int]:
        """Drag stuck `running` items back to `pending`; sweep orphan
        preview JPEGs.

        Why no PID check: macOS recycles PIDs aggressively, and the
        worker is an in-process thread anyway — server kill always
        co-kills the subprocess. Mass-requeue is the simple correct move.

        Returns (n_requeued, n_orphans_swept).
        """
        n_requeued = 0
        with self._lock:
            for it in self._items.values():
                if it.status == "running":
                    it.status = "pending"
                    it.started_at = None
                    it.subprocess_pid = None
                    it.progress = None
                    n_requeued += 1
            if n_requeued > 0:
                self._flush_locked()

        # Sweep orphan preview JPEGs (not under lock — file ops only).
        valid_ids = {it.id for it in self.get_all()}
        n_orphans = 0
        try:
            for jpg in self._preview_dir.glob("*.jpg"):
                if not _QUEUE_ID_RE.match(jpg.stem):
                    continue
                if jpg.stem not in valid_ids:
                    try:
                        jpg.unlink(missing_ok=True)
                        n_orphans += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("orphan preview sweep failed: %s", e)
        return n_requeued, n_orphans
