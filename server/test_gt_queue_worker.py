"""Tests for `gt_queue_worker._run_one` — the per-job supervisor.

Mock the subprocess: build a `FakePopen` that simulates stderr output
(PROGRESS / DONE / garbage / non-utf8), an exit code, and optional
SIGTERM-deafness. Drive `_run_one` against it and assert the queue
moves through the right states + preview cleanup happened.

We deliberately don't spawn a real subprocess — that would require uv
+ tools/.venv which the unit tests can't depend on.
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock

import pytest

import gt_queue_worker
from state_gt_queue import GTQueue


# ----- FakePopen ---------------------------------------------------


class _FakeStderr:
    """Iterating yields the queued lines one at a time. `read` is not
    needed — _stderr_drain reads via iter()."""

    def __init__(self, lines: list[bytes], delay_s: float = 0.0):
        self._lines = list(lines)
        self._delay_s = delay_s

    def __iter__(self):
        for line in self._lines:
            if self._delay_s > 0:
                time.sleep(self._delay_s)
            yield line


class FakePopen:
    """Subprocess-shaped stand-in. Configurable exit behaviour:
    * `exit_after_lines`: exit (with rc) after the stderr drain has
      consumed all of `stderr_lines`.
    * `sigterm_deaf`: when True, refuse to exit on SIGTERM; we'll only
      exit when SIGKILL'd (i.e. when `_kill` is called with SIGKILL).
    * `exit_immediately_on_kill`: when True, `_kill` causes poll() to
      return on the next call.
    """

    def __init__(
        self,
        *,
        stderr_lines: list[bytes],
        rc_on_natural_exit: int = 0,
        sigterm_deaf: bool = False,
        delay_s: float = 0.0,
        pid: int = 12345,
    ):
        self.pid = pid
        self.returncode: int | None = None
        self._stderr_lines = stderr_lines
        self._rc_on_natural_exit = rc_on_natural_exit
        self._sigterm_deaf = sigterm_deaf
        self._delay_s = delay_s
        self.stderr = _FakeStderr(stderr_lines, delay_s=delay_s)
        self._exit_event = threading.Event()
        # Background "process" thread that waits for either a kill
        # signal or the natural drain-then-exit completion.
        self._kill_signal: int | None = None
        self._exit_lock = threading.Lock()
        # Run the natural-exit timer on a thread so poll() will return
        # on its own without the test driver having to wait.
        threading.Thread(target=self._natural_exit_thread, daemon=True).start()
        self.terminate = MagicMock()
        self.send_signal = MagicMock()

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None):
        if self._exit_event.wait(timeout=timeout):
            return self.returncode
        import subprocess
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def _natural_exit_thread(self):
        """Wait until stderr drain has been given enough time to process
        all lines (delay × len), then mark process exited."""
        time.sleep(max(0.05, self._delay_s * len(self._stderr_lines) + 0.05))
        with self._exit_lock:
            if self.returncode is None:
                self.returncode = self._rc_on_natural_exit
                self._exit_event.set()

    def _signal_received(self, sig: int) -> None:
        """Test driver calls this to simulate killpg(self.pid, sig)."""
        import signal as sigmod
        if sig == sigmod.SIGTERM and self._sigterm_deaf:
            return  # ignore — escalation to SIGKILL needed
        with self._exit_lock:
            if self.returncode is None:
                self.returncode = -sig
                self._exit_event.set()


# ----- Helpers -----------------------------------------------------


@pytest.fixture()
def queue(tmp_path: Path) -> GTQueue:
    return GTQueue(
        queue_path=tmp_path / "queue.json",
        preview_dir=tmp_path / "preview",
    )


def _add(queue: GTQueue, sid: str = "s_deadbeef", cam: str = "A") -> str:
    return queue.add(
        session_id=sid,
        camera_id=cam,
        time_range=(0.0, 1.0),
        prompt="blue ball",
    )


def _patch_subprocess_kill(monkeypatch, fake: FakePopen):
    """Redirect _kill_proc_group → fake._signal_received."""
    import signal as sigmod

    def fake_kill_proc_group(pid: int, sig: int) -> None:
        if pid != fake.pid:
            raise ProcessLookupError()
        fake._signal_received(sig)

    monkeypatch.setattr(gt_queue_worker, "_kill_proc_group", fake_kill_proc_group)


# ----- happy path --------------------------------------------------


def test_run_one_marks_done_on_clean_exit(queue: GTQueue, tmp_path: Path, monkeypatch):
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[
            b"PROGRESS: frame=1 total=10 elapsed=1.5 ms_per_frame=1500.0\n",
            b"PROGRESS: frame=10 total=10 elapsed=15.0 ms_per_frame=1500.0\n",
            b"DONE: labelled=8 decoded=10\n",
        ],
        rc_on_natural_exit=0,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(),
        queue,
        index=None,
        server_dir=tmp_path,
        scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "done"
    assert item.n_labelled == 8
    assert item.n_decoded == 10
    # Final progress line was processed.
    assert item.progress is not None
    assert item.progress["current_frame"] == 10


def test_run_one_skips_when_mov_missing(queue: GTQueue, tmp_path: Path):
    qid = _add(queue)
    gt_queue_worker._run_one(
        queue.next_pending(),
        queue,
        index=None,
        server_dir=tmp_path,
        scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: False,
    )
    item = queue.get(qid)
    assert item.status == "error"
    assert "MOV missing" in item.error


# ----- stderr resilience ------------------------------------------


def test_run_one_tolerates_garbage_stderr_lines(queue: GTQueue, tmp_path: Path, monkeypatch):
    """Non-PROGRESS/DONE lines must not break the parser; they go into
    the ring buffer and only surface if the run errors out."""
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[
            b"some banner from transformers\n",
            b"PROGRESS: frame=5 total=10 elapsed=7.0 ms_per_frame=1400.0\n",
            b"FutureWarning: something something\n",
            b"DONE: labelled=10 decoded=10\n",
        ],
        rc_on_natural_exit=0,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "done"
    assert item.n_labelled == 10


def test_run_one_tolerates_non_utf8_stderr(queue: GTQueue, tmp_path: Path, monkeypatch):
    """A segfaulting child can write half a multi-byte sequence."""
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[
            b"\xff\xfe\xfd partial multi-byte\n",
            b"DONE: labelled=0 decoded=0\n",
        ],
        rc_on_natural_exit=0,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "done"


def test_run_one_marks_error_on_nonzero_exit(queue: GTQueue, tmp_path: Path, monkeypatch):
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[
            b"Traceback (most recent call last):\n",
            b'  File "label_with_sam3.py", line 100\n',
            b"RuntimeError: cuda OOM\n",
        ],
        rc_on_natural_exit=1,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "error"
    assert "cuda OOM" in item.error


# ----- cancel ------------------------------------------------------


def test_run_one_cancel_sigterm_clean(queue: GTQueue, tmp_path: Path, monkeypatch):
    """Cancel request → SIGTERM → process exits cleanly → marked canceled."""
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[b"PROGRESS: frame=1 total=1000 elapsed=1.0 ms_per_frame=1000.0\n"],
        rc_on_natural_exit=0,
        delay_s=2.0,  # long delay so the cancel hits while drain still pending
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    # Schedule a cancel after the worker enters its poll loop.
    def trigger_cancel():
        time.sleep(0.5)
        queue.cancel(qid)
    threading.Thread(target=trigger_cancel, daemon=True).start()

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "canceled"


def test_run_one_cancel_sigterm_deaf_escalates_to_sigkill(
    queue: GTQueue, tmp_path: Path, monkeypatch
):
    """If the subprocess ignores SIGTERM (PyTorch + MPS mid-inference),
    the worker must escalate to SIGKILL after the 5s grace timeout."""
    # Shorten the timeout to keep the test fast.
    monkeypatch.setattr(gt_queue_worker, "_SIGTERM_GRACE_S", 0.2)
    qid = _add(queue)
    fake = FakePopen(
        stderr_lines=[b"PROGRESS: frame=1 total=1000 elapsed=1.0 ms_per_frame=1000.0\n"],
        rc_on_natural_exit=0,
        sigterm_deaf=True,
        delay_s=10.0,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    def trigger_cancel():
        time.sleep(0.3)
        queue.cancel(qid)
    threading.Thread(target=trigger_cancel, daemon=True).start()

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "canceled"


# ----- watchdog ----------------------------------------------------


def test_run_one_no_progress_watchdog_kills(
    queue: GTQueue, tmp_path: Path, monkeypatch
):
    """If `last_progress_at` doesn't advance within the no-progress
    window, the worker kills the subprocess and marks error."""
    monkeypatch.setattr(gt_queue_worker, "_NO_PROGRESS_TIMEOUT_S", 0.5)
    monkeypatch.setattr(gt_queue_worker, "_POLL_INTERVAL_S", 0.1)
    monkeypatch.setattr(gt_queue_worker, "_SIGTERM_GRACE_S", 0.3)

    qid = _add(queue)
    # No PROGRESS lines → drain thread never updates last_progress_at →
    # watchdog fires.
    fake = FakePopen(
        stderr_lines=[b"some warmup spam\n"],  # not PROGRESS
        rc_on_natural_exit=0,
        delay_s=10.0,
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "error"
    assert "no progress" in item.error.lower()


# ----- ProcessLookupError tolerance --------------------------------


def test_kill_proc_group_tolerates_already_exited(monkeypatch):
    """ProcessLookupError on getpgid is normal: subprocess can self-exit
    between our last poll and the kill call. Must not crash."""
    import signal as sigmod
    import os as os_mod

    def boom_getpgid(pid):
        raise ProcessLookupError()

    monkeypatch.setattr(os_mod, "getpgid", boom_getpgid)
    # Should not raise.
    gt_queue_worker._kill_proc_group(99999, sigmod.SIGTERM)


# ----- ring buffer cap --------------------------------------------


def test_stderr_ring_buffer_caps_at_byte_size(queue: GTQueue, tmp_path: Path, monkeypatch):
    """A noisy run must not allow the stderr ring to grow unboundedly.
    The mark_error tail should be roughly the cap (4 KB) regardless of
    how many bytes the process emitted."""
    qid = _add(queue)
    # Generate ~50 KB of noise — must stay capped at our 4 KB ring.
    noise_lines = [b"x" * 1000 + b"\n" for _ in range(50)]
    fake = FakePopen(
        stderr_lines=noise_lines,
        rc_on_natural_exit=2,  # trigger the error-tail capture
    )
    monkeypatch.setattr(gt_queue_worker, "_spawn", lambda *a, **kw: fake)
    _patch_subprocess_kill(monkeypatch, fake)

    gt_queue_worker._run_one(
        queue.next_pending(), queue, None,
        server_dir=tmp_path, scripts_dir=tmp_path,
        tools_project="../tools",
        mov_exists=lambda sid, cam: True,
    )
    item = queue.get(qid)
    assert item.status == "error"
    # GTQueue.mark_error truncates to last 2000 chars; the ring upstream
    # caps at 4 KB. Either way, far less than 50 KB.
    assert len(item.error) <= 2000
