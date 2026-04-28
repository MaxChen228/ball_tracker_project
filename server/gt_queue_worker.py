"""Background worker that drives the SAM 3 labelling queue.

Single-threaded: one SAM 3 model fits in MPS at a time on M4, so
parallelism would only thrash. The worker thread pulls one
`GTQueueItem` at a time, spawns `label_with_sam3.py` as a subprocess,
parses progress from its stderr, and finalises the item.

Hard-won subprocess details (per mini-plan v4 review findings):

  * `start_new_session=True` puts the SAM 3 process in its own process
    group. Cancel uses `os.killpg(pgid, SIGTERM)` so child threads /
    tokenizer workers all die together. PyTorch + MPS kernels can ignore
    SIGTERM mid-inference, so we escalate to `SIGKILL` after a 5-second
    grace period.
  * `stdout=DEVNULL` because PyTorch / transformers occasionally write
    progress bars / banners to stdout and a full pipe buffer (~64 KB on
    macOS) would deadlock if we ever ignored stdout but kept the pipe.
  * stderr is read on a **dedicated drain thread** — the main worker
    poll thread can't both wait on `proc.poll()` AND `proc.stderr.readline()`
    without risking the pipe filling under burst output.
  * stderr decode uses `errors="replace"` because a segfaulting child
    can write half a multi-byte sequence on its way out.
  * Non-PROGRESS / non-DONE stderr lines accumulate to a `bytearray`
    ring buffer with a **byte cap** of 4 KB. We tried `collections.deque`
    earlier; that bounds entry count, not bytes, so a chatty PyTorch
    warning could blow it up to ~400 KB.
  * Cancel watchdog and 60-second no-progress watchdog share the same
    1-second poll loop — adding a third thread for the no-progress
    watchdog would just race the cancel path. `last_progress_at` is a
    single-element list (mutable ref) shared between drain thread and
    poll loop.
  * `ProcessLookupError` on `os.getpgid` / `os.killpg` is normal: the
    child can self-exit between `proc.poll()` returning None and our
    kill call. Catch and ignore.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state_gt_queue import GTQueue, GTQueueItem
    from state_gt_index import GTIndex

logger = logging.getLogger(__name__)

# ----- stderr contract regexes -----------------------------------------

_PROGRESS_RE = re.compile(
    r"^PROGRESS:\s+frame=(\d+)\s+total=(\d+)\s+elapsed=([\d.]+)\s+ms_per_frame=([\d.]+)\s*$"
)
_DONE_RE = re.compile(r"^DONE:\s+labelled=(\d+)\s+decoded=(\d+)\s*$")

_STDERR_BYTE_CAP = 4096
_NO_PROGRESS_TIMEOUT_S = 60.0
_SIGTERM_GRACE_S = 5.0
_POLL_INTERVAL_S = 1.0


def _spawn(
    item: "GTQueueItem",
    *,
    server_dir: Path,
    scripts_dir: Path,
    tools_project: str,
) -> subprocess.Popen:
    """Spawn the SAM 3 labeller subprocess. cwd is pinned to server_dir
    (avoid inheriting an arbitrary cwd from the FastAPI launcher), and
    `--project ../tools` is resolved relative to that fixed cwd."""
    cmd = [
        "uv", "run", "--project", tools_project,
        "python", str(scripts_dir / "label_with_sam3.py"),
        "--session", item.session_id,
        "--cam", item.camera_id,
        "--time-range", f"{item.time_range[0]:.3f}", f"{item.time_range[1]:.3f}",
        "--prompt", item.prompt,
        "--queue-id", item.id,
        "--overwrite",
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(server_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _stderr_drain(
    proc: subprocess.Popen,
    queue: "GTQueue",
    item_id: str,
    ring: bytearray,
    last_progress_at: list,
    parsed_done: list,
) -> None:
    """Read stderr line-by-line until EOF.

    PROGRESS → mark_progress + bump last_progress_at (watchdog reset).
    DONE → store (labelled, decoded) for the worker thread to consume.
    Other → append raw bytes to `ring`, capped at _STDERR_BYTE_CAP bytes.
    """
    assert proc.stderr is not None
    for line_bytes in proc.stderr:
        try:
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:
            ring.extend(line_bytes)
            _trim_ring(ring)
            continue
        m = _PROGRESS_RE.match(line)
        if m:
            current_frame = int(m.group(1))
            total_frames = int(m.group(2))
            elapsed = float(m.group(3))
            ms_per_frame = float(m.group(4))
            queue.mark_progress(item_id, {
                "current_frame": current_frame,
                "total_frames": total_frames,
                "elapsed": elapsed,
                "ms_per_frame": ms_per_frame,
            })
            last_progress_at[0] = time.monotonic()
            continue
        m2 = _DONE_RE.match(line)
        if m2:
            parsed_done[0] = (int(m2.group(1)), int(m2.group(2)))
            continue
        ring.extend(line_bytes)
        _trim_ring(ring)


def _trim_ring(ring: bytearray) -> None:
    """Keep only the last _STDERR_BYTE_CAP bytes."""
    if len(ring) > _STDERR_BYTE_CAP:
        del ring[: len(ring) - _STDERR_BYTE_CAP]


def _kill_proc_group(pid: int, sig: int) -> None:
    """Send `sig` to the process group. Tolerates the natural race where
    the subprocess self-exited between our last poll and this call."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        logger.warning("killpg(%d, %d) refused: %s", pid, sig, e)


def _run_one(
    item: "GTQueueItem",
    queue: "GTQueue",
    index: "GTIndex | None",
    *,
    server_dir: Path,
    scripts_dir: Path,
    tools_project: str,
    mov_exists: callable,
) -> None:
    """Spawn + supervise one labelling job. Updates queue state + index
    invalidation on completion."""
    if not mov_exists(item.session_id, item.camera_id):
        queue.mark_error(item.id, f"MOV missing for {item.session_id}/{item.camera_id}")
        if index is not None:
            index.invalidate(item.session_id)
        return

    proc = _spawn(
        item,
        server_dir=server_dir,
        scripts_dir=scripts_dir,
        tools_project=tools_project,
    )
    queue.mark_running(item.id, proc.pid)

    ring = bytearray()
    last_progress_at = [time.monotonic()]
    parsed_done: list = [None]

    drain_thread = threading.Thread(
        target=_stderr_drain,
        args=(proc, queue, item.id, ring, last_progress_at, parsed_done),
        daemon=True,
        name=f"sam3-stderr-{item.id}",
    )
    drain_thread.start()

    cancel_or_watchdog_killed = False
    cancel_reason: str | None = None  # "cancel" | "no_progress" | None
    while True:
        if proc.poll() is not None:
            break
        if queue.is_cancel_requested(item.id):
            cancel_reason = "cancel"
        elif (time.monotonic() - last_progress_at[0]) > _NO_PROGRESS_TIMEOUT_S:
            cancel_reason = "no_progress"
        if cancel_reason is not None:
            cancel_or_watchdog_killed = True
            _kill_proc_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=_SIGTERM_GRACE_S)
            except subprocess.TimeoutExpired:
                _kill_proc_group(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=_SIGTERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "subprocess pid=%s refused SIGKILL — abandoning",
                        proc.pid,
                    )
            break
        time.sleep(_POLL_INTERVAL_S)

    # Drain thread will exit when stderr closes (proc exit). Give it a
    # brief join so any remaining DONE / PROGRESS line is parsed before
    # we read parsed_done.
    drain_thread.join(timeout=2.0)

    rc = proc.returncode
    if cancel_or_watchdog_killed:
        if cancel_reason == "cancel":
            queue.mark_canceled(item.id)
        else:
            tail = bytes(ring).decode("utf-8", errors="replace")[-2000:]
            queue.mark_error(
                item.id,
                f"no progress for {_NO_PROGRESS_TIMEOUT_S:.0f}s — watchdog killed\n{tail}",
            )
    elif rc == 0:
        n_lab, n_dec = parsed_done[0] if parsed_done[0] else (0, 0)
        queue.mark_done(item.id, n_lab, n_dec)
    elif rc < 0 or rc == 130:
        # Negative rc = killed by signal; 130 = SIGINT. Treat as canceled.
        queue.mark_canceled(item.id)
    else:
        tail = bytes(ring).decode("utf-8", errors="replace")[-2000:]
        queue.mark_error(item.id, tail or f"exit code {rc}")

    if index is not None:
        index.invalidate(item.session_id)


# ----- worker thread ---------------------------------------------------


class GTQueueWorker:
    """Single-thread loop over `queue.next_pending()`.

    The worker is started in the FastAPI lifespan; teardown sets
    `_stop_flag` and joins with a 5-second timeout (any in-flight
    subprocess is left to die when its parent goes away — start_new_session
    detached it from our terminal but it inherits our parent process
    death detection: when uvicorn exits, the kernel reaps).
    """

    def __init__(
        self,
        queue: "GTQueue",
        index: "GTIndex | None",
        *,
        server_dir: Path,
        scripts_dir: Path,
        tools_project: str,
        mov_exists: callable,
    ) -> None:
        self._queue = queue
        self._index = index
        self._server_dir = server_dir
        self._scripts_dir = scripts_dir
        self._tools_project = tools_project
        self._mov_exists = mov_exists
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gt-queue-worker"
        )
        self._thread.start()
        logger.info("GTQueueWorker started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("GTQueueWorker did not stop in %.1fs", timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            if self._queue.paused():
                time.sleep(_POLL_INTERVAL_S)
                continue
            item = self._queue.next_pending()
            if item is None:
                time.sleep(_POLL_INTERVAL_S)
                continue
            try:
                _run_one(
                    item,
                    self._queue,
                    self._index,
                    server_dir=self._server_dir,
                    scripts_dir=self._scripts_dir,
                    tools_project=self._tools_project,
                    mov_exists=self._mov_exists,
                )
            except Exception as e:
                logger.exception("worker loop crashed on %s: %s", item.id, e)
                try:
                    self._queue.mark_error(item.id, f"worker exception: {e}")
                except Exception:
                    pass
                # Continue running; one bad item shouldn't kill the worker.
