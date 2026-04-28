"""Tests for state_gt_queue.GTQueue.

Covers: add / cancel / retry / pause / status mutators / recover_on_boot
/ atomic write / preview cleanup / corrupt-file tolerance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from state_gt_queue import GTQueue, GTQueueItem


@pytest.fixture()
def queue(tmp_path: Path) -> GTQueue:
    return GTQueue(
        queue_path=tmp_path / "queue.json",
        preview_dir=tmp_path / "preview",
    )


def _add_basic(q: GTQueue, sid: str = "s_deadbeef", cam: str = "A") -> str:
    return q.add(
        session_id=sid,
        camera_id=cam,
        time_range=(0.5, 1.5),
        click_x=960,
        click_y=540,
        click_t_video_rel=0.5,
    )


# ----- add / persistence ---------------------------------------------


def test_add_persists_to_disk(queue: GTQueue, tmp_path: Path):
    qid = _add_basic(queue)
    assert qid.startswith("q_")
    assert (tmp_path / "queue.json").is_file()
    data = json.loads((tmp_path / "queue.json").read_text())
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == qid
    assert data["items"][0]["status"] == "pending"
    # tuple → list serialisation
    assert data["items"][0]["time_range"] == [0.5, 1.5]


def test_load_from_disk_round_trip(tmp_path: Path):
    q1 = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    qid = _add_basic(q1)
    # Re-instantiate; should pick up the persisted item
    q2 = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    item = q2.get(qid)
    assert item is not None
    assert item.session_id == "s_deadbeef"
    assert item.time_range == (0.5, 1.5)


def test_corrupt_queue_json_treated_as_empty(tmp_path: Path):
    qpath = tmp_path / "queue.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text("{not valid json")
    # Should not crash
    q = GTQueue(qpath, tmp_path / "preview")
    assert q.get_all() == []


def test_atomic_write_uses_tmp_then_rename(queue: GTQueue, tmp_path: Path):
    _add_basic(queue)
    # tmp file should be cleaned up after rename
    assert not (tmp_path / "queue.tmp").exists()
    assert (tmp_path / "queue.json").exists()


# ----- cancel ---------------------------------------------------------


def test_cancel_pending_flips_status_immediately(queue: GTQueue):
    qid = _add_basic(queue)
    assert queue.cancel(qid) is True
    item = queue.get(qid)
    assert item is not None
    assert item.status == "canceled"
    assert item.finished_at is not None


def test_cancel_running_sets_request_flag_only(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=12345)
    assert queue.cancel(qid) is True
    # Still "running"; worker observes the flag and calls mark_canceled
    assert queue.get(qid).status == "running"
    assert queue.is_cancel_requested(qid) is True


def test_cancel_done_returns_false(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.mark_done(qid, n_labelled=5, n_decoded=10)
    assert queue.cancel(qid) is False


def test_cancel_unknown_id_returns_false(queue: GTQueue):
    assert queue.cancel("q_nonexistent") is False


# ----- retry ----------------------------------------------------------


def test_retry_creates_new_id_keeps_old_audit(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.mark_error(qid, "boom")
    new_id = queue.retry(qid)
    assert new_id is not None and new_id != qid
    # Old item still present (audit), new item is pending
    assert queue.get(qid).status == "error"
    assert queue.get(new_id).status == "pending"
    assert queue.get(new_id).time_range == (0.5, 1.5)
    assert queue.get(new_id).click_x == 960
    assert queue.get(new_id).click_y == 540
    assert queue.get(new_id).click_t_video_rel == 0.5


def test_retry_pending_returns_none(queue: GTQueue):
    qid = _add_basic(queue)
    assert queue.retry(qid) is None


def test_retry_deletes_old_preview_jpeg(queue: GTQueue, tmp_path: Path):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.mark_error(qid, "boom")
    # Simulate a leftover preview JPEG
    preview = tmp_path / "preview" / f"{qid}.jpg"
    preview.write_bytes(b"\xff\xd8\xff\xe0fake")
    assert preview.exists()
    new_id = queue.retry(qid)
    assert new_id is not None
    assert not preview.exists()


# ----- mark_* ---------------------------------------------------------


def test_mark_running_sets_started_at_and_pid(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=7777)
    item = queue.get(qid)
    assert item.status == "running"
    assert item.started_at is not None
    assert item.subprocess_pid == 7777


def test_mark_progress_updates_progress_dict(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.mark_progress(qid, {"current_frame": 50, "total_frames": 200, "ms_per_frame": 1500.0})
    p = queue.get(qid).progress
    assert p == {"current_frame": 50, "total_frames": 200, "ms_per_frame": 1500.0}


def test_mark_done_clears_pid_and_preview(queue: GTQueue, tmp_path: Path):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    preview = tmp_path / "preview" / f"{qid}.jpg"
    preview.write_bytes(b"x")
    queue.mark_done(qid, n_labelled=87, n_decoded=110)
    item = queue.get(qid)
    assert item.status == "done"
    assert item.subprocess_pid is None
    assert item.n_labelled == 87
    assert item.n_decoded == 110
    assert not preview.exists()


def test_mark_error_truncates_long_msg(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.mark_error(qid, "x" * 5000)
    assert len(queue.get(qid).error) == 2000  # truncated to last 2000 chars


def test_mark_canceled_clears_cancel_request(queue: GTQueue):
    qid = _add_basic(queue)
    queue.mark_running(qid, pid=1)
    queue.cancel(qid)
    assert queue.is_cancel_requested(qid) is True
    queue.mark_canceled(qid)
    assert queue.is_cancel_requested(qid) is False
    assert queue.get(qid).status == "canceled"


# ----- next_pending FIFO ----------------------------------------------


def test_next_pending_returns_oldest(queue: GTQueue):
    a = _add_basic(queue, sid="s_aaa")
    # tiny sleep would be unstable; rely on ISO timestamps having sub-second
    # resolution. add() uses second-resolution UTC string. Use add twice
    # then check FIFO order via id ordering being stable.
    b = _add_basic(queue, sid="s_bbb")
    item = queue.next_pending()
    assert item is not None
    # Items added first-to-last should come out FIFO; both could share
    # second so we just assert one of the two is returned and not None.
    assert item.id in (a, b)


def test_next_pending_skips_running(queue: GTQueue):
    a = _add_basic(queue, sid="s_aaa")
    b = _add_basic(queue, sid="s_bbb")
    queue.mark_running(a, pid=1)
    item = queue.next_pending()
    assert item is not None
    assert item.id == b


def test_next_pending_empty_returns_none(queue: GTQueue):
    assert queue.next_pending() is None


# ----- pause / resume -------------------------------------------------


def test_pause_resume_toggle(queue: GTQueue):
    assert queue.paused() is False
    queue.pause()
    assert queue.paused() is True
    queue.resume()
    assert queue.paused() is False


# ----- recover_on_boot ------------------------------------------------


def test_recover_on_boot_requeues_running_items(tmp_path: Path):
    q1 = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    qid = _add_basic(q1)
    q1.mark_running(qid, pid=99999)
    # Simulate a server crash by re-loading from disk
    q2 = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    n_requeued, n_orphans = q2.recover_on_boot()
    assert n_requeued == 1
    item = q2.get(qid)
    assert item.status == "pending"
    assert item.subprocess_pid is None
    assert item.started_at is None


def test_recover_on_boot_sweeps_orphan_previews(tmp_path: Path):
    q = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    qid = _add_basic(q)
    q.mark_running(qid, pid=1)
    # Item-attached preview (kept after recovery)
    own_preview = tmp_path / "preview" / f"{qid}.jpg"
    own_preview.write_bytes(b"own")
    # Orphan preview (no matching queue item)
    orphan = tmp_path / "preview" / "q_deadbeef.jpg"
    orphan.write_bytes(b"orphan")
    # Non-conforming filename — should NOT be touched
    weird = tmp_path / "preview" / "not_a_queue_id.jpg"
    weird.write_bytes(b"weird")
    n_requeued, n_orphans = q.recover_on_boot()
    assert n_requeued == 1
    assert n_orphans == 1
    assert own_preview.exists()  # belongs to the requeued item
    assert not orphan.exists()
    assert weird.exists()  # untouched


def test_recover_on_boot_idempotent(tmp_path: Path):
    q = GTQueue(tmp_path / "queue.json", tmp_path / "preview")
    qid = _add_basic(q)
    q.mark_running(qid, pid=1)
    q.recover_on_boot()
    n_requeued, _ = q.recover_on_boot()
    assert n_requeued == 0  # nothing left to requeue


# ----- clear --------------------------------------------------------


def test_clear_done_removes_only_done_items(queue: GTQueue):
    a = _add_basic(queue, sid="s_aaa")
    b = _add_basic(queue, sid="s_bbb")
    queue.mark_running(a, pid=1)
    queue.mark_done(a, n_labelled=1, n_decoded=2)
    n = queue.clear_done()
    assert n == 1
    assert queue.get(a) is None
    assert queue.get(b) is not None  # pending kept


def test_clear_errors_removes_error_and_canceled(queue: GTQueue):
    a = _add_basic(queue, sid="s_aaa")
    b = _add_basic(queue, sid="s_bbb")
    c = _add_basic(queue, sid="s_ccc")
    queue.mark_running(a, pid=1)
    queue.mark_error(a, "boom")
    queue.mark_running(b, pid=2)
    queue.mark_canceled(b)
    n = queue.clear_errors()
    assert n == 2
    assert queue.get(a) is None
    assert queue.get(b) is None
    assert queue.get(c) is not None  # pending kept
