"""Unit coverage for `state_processing.SessionProcessingState` post-split.

These tests hit the coordinator through a real `State` (so the `attach()`
wiring exercises the lock + pitches-dict access paths) rather than
poking the coordinator bare-handed. That mirrors how routes/* use it.
"""
from __future__ import annotations

from state import State
from schemas import FramePayload, PitchPayload


def _sid(n: int) -> str:
    return f"s_{n:08x}"


def _pitch(cam: str, session_id: str) -> PitchPayload:
    return PitchPayload(
        camera_id=cam,
        session_id=session_id,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=[],
    )


def test_attach_is_required(tmp_path):
    from state_processing import SessionProcessingState

    proc = SessionProcessingState()
    try:
        proc.session_candidates(_sid(1))
    except RuntimeError as e:
        assert "attach" in str(e)
    else:  # pragma: no cover - guard against silent regression
        raise AssertionError("expected RuntimeError when owner not attached")


def test_candidates_exclude_trashed_session(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(1)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")

    assert len(s._processing.session_candidates(sid)) == 1
    s.trash_session(sid)
    assert s._processing.session_candidates(sid) == []


def test_candidates_include_completed_pitches_for_rerun(tmp_path):
    """A pitch that already has frames_server_post is still eligible —
    the viewer's Rerun-server button re-queues it. Eligibility is gated
    on MOV-on-disk only."""
    s = State(data_dir=tmp_path)
    sid = _sid(2)
    p = _pitch("A", sid)
    p.frames_server_post = [
        FramePayload(frame_index=0, timestamp_s=0.0, ball_detected=False)
    ]
    s.record(p)
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")

    cands = s._processing.session_candidates(sid)
    assert len(cands) == 1
    assert cands[0][0] == "A"


def test_cancel_then_resume_transitions_queue_state(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(3)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")

    s._processing.mark_server_post_queued(sid, "A")
    status, resumable = s._processing.session_summary(sid)
    assert (status, resumable) == ("queued", True)

    assert s._processing.cancel_processing(sid) is True
    status, resumable = s._processing.session_summary(sid)
    assert status == "canceled"

    queued = s._processing.resume_processing(sid)
    assert len(queued) == 1
    status, _ = s._processing.session_summary(sid)
    assert status == "queued"


def test_record_and_clear_error_round_trip(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(4)

    s._processing.record_error(sid, "A", "boom")
    s._processing.record_error(sid, "B", "kaboom")
    assert s._processing.errors_for(sid) == {"A": "boom", "B": "kaboom"}

    s._processing.clear_error(sid, "A")
    assert s._processing.errors_for(sid) == {"B": "kaboom"}

    # Clearing the last cam collapses to empty dict.
    s._processing.clear_error(sid, "B")
    assert s._processing.errors_for(sid) == {}


def test_start_job_refuses_when_trashed(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(5)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")
    s.trash_session(sid)

    assert s._processing.start_server_post_job(sid, "A") is False


def test_find_video_skips_tmp(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(6)
    (s.video_dir / f"session_{sid}_A.mov.tmp").write_bytes(b"")
    real = s.video_dir / f"session_{sid}_A.mov"
    real.write_bytes(b"")

    assert s._processing.find_video_for(sid, "A") == real
