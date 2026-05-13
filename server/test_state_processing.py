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
    # Pin frames under the legacy pre-snapshot bucket so the
    # `frames_server_post` computed_field projects them out and the
    # session_processing eligibility check sees a "completed" pitch.
    p.frames_by_algorithm["v11_hsv_cc"] = [
        FramePayload(frame_index=0, timestamp_s=0.0, ball_detected=False)
    ]
    p.active_server_post_algorithm_id = "v11_hsv_cc"
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


def test_completed_session_summary_survives_rerun_eligibility(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(31)
    p = _pitch("A", sid)
    # Pin frames under the legacy pre-snapshot bucket so the
    # `frames_server_post` computed_field projects them out and the
    # session_processing eligibility check sees a "completed" pitch.
    p.frames_by_algorithm["v11_hsv_cc"] = [
        FramePayload(frame_index=0, timestamp_s=0.0, ball_detected=False)
    ]
    p.active_server_post_algorithm_id = "v11_hsv_cc"
    s.record(p)
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")

    status, resumable = s._processing.session_summary(sid)
    assert (status, resumable) == ("completed", False)


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


def test_session_progress_empty_when_no_run(tmp_path):
    """Untouched sessions report an empty progress dict — the viewer
    SSR seed falls through to "waiting for first frame…" placeholder."""
    s = State(data_dir=tmp_path)
    sid = _sid(7)
    assert s._processing.session_progress(sid) == {}


def test_set_progress_round_trips(tmp_path):
    """`set_server_post_progress` writes one snapshot per (cam, sid).
    `session_progress(sid)` projects every cam's latest snapshot back
    to the viewer SSR layer as a `{cam: {done, total, pct}}` dict."""
    s = State(data_dir=tmp_path)
    sid = _sid(8)
    s._processing.set_server_post_progress(sid, "A", done=120, total=974, pct=12)
    s._processing.set_server_post_progress(sid, "B", done=80, total=907, pct=8)
    snap = s._processing.session_progress(sid)
    assert snap == {
        "A": {"done": 120, "total": 974, "pct": 12},
        "B": {"done": 80, "total": 907, "pct": 8},
    }


def test_set_progress_handles_unknown_total(tmp_path):
    """`probe_frame_count` returning None propagates as `total=None` and
    `pct=None`. The viewer JS renders "?/" and the bar fill stays at 0%
    — explicit "we don't know" beats a fake number."""
    s = State(data_dir=tmp_path)
    sid = _sid(9)
    s._processing.set_server_post_progress(sid, "A", done=15, total=None, pct=None)
    snap = s._processing.session_progress(sid)
    assert snap == {"A": {"done": 15, "total": None, "pct": None}}


def test_start_job_clears_stale_progress(tmp_path):
    """A fresh run on the same (cam, sid) must not surface yesterday's
    counters — `start_server_post_job` drops any prior progress snapshot
    so the SSR seed shows the empty state until the next emit lands."""
    s = State(data_dir=tmp_path)
    sid = _sid(10)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")
    s._processing.set_server_post_progress(sid, "A", done=999, total=1000, pct=99)

    assert s._processing.start_server_post_job(sid, "A") is True
    assert s._processing.session_progress(sid) == {}


def test_finish_job_clears_progress(tmp_path):
    """Done events fire on ok/canceled/error; the SSR seed must not
    show a frozen "999/1200" after detection has actually completed."""
    s = State(data_dir=tmp_path)
    sid = _sid(11)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")
    s._processing.start_server_post_job(sid, "A")
    s._processing.set_server_post_progress(sid, "A", done=500, total=974, pct=51)

    s._processing.finish_server_post_job(sid, "A", canceled=False)
    assert s._processing.session_progress(sid) == {}


def test_priming_write_after_start_job_survives(tmp_path):
    """Load-bearing ordering invariant: `routes/pitch.py::_run_server_detection`
    calls `start_server_post_job` first (which clears any stale
    progress), then issues the priming `set_server_post_progress`.
    A reversed order would let start_job's clear blow away the
    priming write and the SSR seed would never observe done=0 until
    the next throttled tick (one throttle-window where the operator
    sees "waiting for first frame…" needlessly)."""
    s = State(data_dir=tmp_path)
    sid = _sid(12)
    s.record(_pitch("A", sid))
    (s.video_dir / f"session_{sid}_A.mov").write_bytes(b"x")
    # Stale entry from a prior run.
    s._processing.set_server_post_progress(sid, "A", done=999, total=1000, pct=99)

    # Mirror pitch.py's real sequence: start_job first, then priming.
    assert s._processing.start_server_post_job(sid, "A") is True
    s._processing.set_server_post_progress(sid, "A", done=0, total=974, pct=0)

    snap = s._processing.session_progress(sid)
    assert snap == {"A": {"done": 0, "total": 974, "pct": 0}}
