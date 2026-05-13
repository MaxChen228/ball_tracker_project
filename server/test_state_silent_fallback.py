"""Regression guard: PR#93 silent-fallback fix.

Background — PR#93 removed an `or frame` silent fallback in
`State.ingest_live_frame`. The old code did:

    resolved = live.latest_frame_for(camera_id) or frame
    return created, counts, resolved

If a race / bug emptied the live buffer between `live.ingest(...)` and
`latest_frame_for(...)`, the call silently substituted the raw inbound
`frame` (pre-candidate-resolved) and downstream consumers worked off
the wrong pixel basis without ever noticing. The fix raises
`RuntimeError` instead.

This test pins that contract: if `latest_frame_for` ever returns None
after a successful ingest, `ingest_live_frame` MUST raise. If a future
refactor reintroduces an `or frame` (or similar fallback) silently
swallowing the empty buffer, this test fails.
"""
from __future__ import annotations

import main
from schemas import BlobCandidate


def _make_frame(idx: int = 1) -> main.FramePayload:
    return main.FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0,
                                  aspect=1.0, fill=0.68)],
    )


def test_ingest_live_frame_raises_on_empty_buffer(tmp_path, monkeypatch):
    """Simulate the pre-PR#93 race: live buffer goes empty between
    `live.ingest` and `latest_frame_for`. State must raise RuntimeError
    rather than silently substituting the raw inbound frame.
    """
    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    session = s.arm_session(paths={main.DetectionPath.live})

    # Force the post-ingest lookup to return None — exactly the empty-buffer
    # race that PR#93's `or frame` silent fallback used to hide.
    live = s._live_pairings[session.id]
    monkeypatch.setattr(live, "latest_frame_for", lambda cam: None)

    try:
        s.ingest_live_frame("A", session.id, _make_frame(1))
    except RuntimeError as exc:
        msg = str(exc)
        assert "live buffer empty" in msg, (
            f"RuntimeError message changed; expected 'live buffer empty' "
            f"phrase to remain stable for grep-ability, got: {msg!r}"
        )
        assert "cam=A" in msg and f"sid={session.id}" in msg, (
            f"error message must include cam + sid for triage, got: {msg!r}"
        )
        return

    raise AssertionError(
        "ingest_live_frame must raise RuntimeError when latest_frame_for "
        "returns None — silent fallback (`or frame`) reintroduced?"
    )


# ---------------------------------------------------------------------
# W3 BLOCK A: record() session resurrection guard
# ---------------------------------------------------------------------

def _pitch(cam: str, sid: str):
    from schemas import PitchPayload

    return PitchPayload(
        camera_id=cam,
        session_id=sid,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
    )


def test_record_refuses_resurrected_session(tmp_path):
    """A pitch arriving after `delete_session` must not resurrect the
    session on disk + in memory. `delete_session` drops a tombstone
    so a late `record()` (stale iOS retry, in-flight server_post job
    that hadn't finished writing) returns a synthetic SessionResult
    with `error='session_deleted_during_record'` and leaves state
    untouched.

    NOT covered: record() of an UNKNOWN-but-never-deleted sid — that's
    the normal first-record path used by every record-driven test
    fixture in the codebase and must succeed.
    """
    from state import State

    s = State(data_dir=tmp_path)
    sid = "s_deadbeef"
    cam = "A"

    # Land the first pitch through the normal path so disk has a
    # pitch JSON we can verify is gone afterwards.
    s.record(_pitch(cam, sid))
    pitch_path = s._pitch_path(cam, sid)
    assert pitch_path.exists()
    assert (cam, sid) in s.pitches

    # Delete the session — drops the tombstone, wipes disk + memory.
    deleted = s.delete_session(sid)
    assert deleted is True
    assert (cam, sid) not in s.pitches
    assert not pitch_path.exists()

    # Stale upload arrives AFTER delete (the race the audit identified).
    # record() must refuse without writing anything to disk and without
    # repopulating `self.pitches`.
    result = s.record(_pitch(cam, sid))
    assert result.error == "session_deleted_during_record"
    assert (cam, sid) not in s.pitches
    assert sid not in s.results
    assert not pitch_path.exists(), (
        f"record() must not resurrect pitch JSON after delete_session; "
        f"found {pitch_path}"
    )


# ---------------------------------------------------------------------
# W3 BLOCK B: start_job refuses deleted session
# ---------------------------------------------------------------------

def test_start_job_refuses_deleted_session(tmp_path):
    """`SessionProcessingState.start_job` must return False when
    `owner.pitches` no longer holds the (cam, sid) key — the session
    was deleted between routes/pitch.py grabbing a pitch_copy and
    start_job() being called. Without this guard the job flips to
    'processing' and runs detection on a vanished session.
    """
    from state import State

    s = State(data_dir=tmp_path)
    sid = "s_phantom1"
    # Session was never recorded — owner.pitches has no key for (A, sid).
    assert s._processing.start_server_post_job(sid, "A") is False
    # Job entry must NOT have been written to "processing".
    assert ("A", sid) not in s._processing.server_post_jobs


# ---------------------------------------------------------------------
# W3 BLOCKs C-G: write-then-mutate atomicity
# ---------------------------------------------------------------------

def test_set_hsv_range_atomic_on_disk_fail(tmp_path, monkeypatch):
    """If `_atomic_write` fails (disk full / perm), `set_hsv_range`
    must NOT mutate `_detection_config` in memory. Disk + memory
    stay in sync.
    """
    from state import State
    from detection_config import HSVRange

    s = State(data_dir=tmp_path)
    before = s.detection_config()
    new_hsv = HSVRange(h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60)

    # Sabotage _atomic_write to simulate disk failure.
    def _boom(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(s, "_atomic_write", _boom)

    try:
        s.set_hsv_range(new_hsv)
    except OSError:
        pass
    else:
        raise AssertionError("set_hsv_range must propagate disk failure")

    after = s.detection_config()
    assert after == before, (
        f"In-memory detection_config must not mutate when disk write "
        f"fails; before={before!r} after={after!r}"
    )


def test_set_pairing_tuning_atomic_on_disk_fail(tmp_path, monkeypatch):
    from state import State
    from pairing_tuning import PairingTuning

    s = State(data_dir=tmp_path)
    before = s.pairing_tuning()

    def _boom(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(s, "_atomic_write", _boom)

    try:
        s.set_pairing_tuning(PairingTuning(gap_threshold_m=99.0))
    except OSError:
        pass
    else:
        raise AssertionError("set_pairing_tuning must propagate disk failure")

    after = s.pairing_tuning()
    assert after == before, (
        f"In-memory pairing_tuning must not mutate when disk write "
        f"fails; before={before!r} after={after!r}"
    )


# ---------------------------------------------------------------------
# W3 BLOCK I: _atomic_write tmp cleanup
# ---------------------------------------------------------------------

def test_atomic_write_unlinks_tmp_on_failure(tmp_path, monkeypatch):
    """`_atomic_write` must unlink its `.tmp` sibling on any failure
    so abandoned tmps don't accumulate as inode leaks.
    """
    from pathlib import Path
    from state import State

    s = State(data_dir=tmp_path)
    target = tmp_path / "target.json"

    # Force `tmp.replace(path)` to raise, leaving the tmp on disk.
    real_path = Path
    original_replace = real_path.replace

    def _broken_replace(self, _dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", _broken_replace)

    try:
        s._atomic_write(target, "payload")
    except OSError:
        pass
    else:
        raise AssertionError("_atomic_write must propagate failure")

    # Restore so the rest of teardown works.
    monkeypatch.setattr(Path, "replace", original_replace)

    # No `.*.tmp` siblings left behind in the data dir.
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], (
        f"_atomic_write must unlink tmp on failure; leaked: {leftover!r}"
    )
