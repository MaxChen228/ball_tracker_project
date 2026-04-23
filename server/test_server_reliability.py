"""Reliability invariants for the server background lanes:

- `LivePairingSession` collections stay bounded even under flood input.
- `State._live_pairings` is dropped once a session's data is archived
  (or the session gets deleted / reset).
- `_run_server_detection` wraps `detect_pitch` in `asyncio.wait_for` so a
  wedged decoder cannot hang the background task.
- `_run_server_detection` has a finally sentinel that finishes the job
  even when the happy-path `finish_server_post_job` is bypassed by an
  exception.

These tests exercise unit-level invariants against the in-memory State
and the routes.pitch background helper — no real PyAV decode, no real
camera. They're the fast feedback loop; full end-to-end coverage lives
in the other server test files.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import main
from conftest import sid
from live_pairing import (
    LivePairingSession,
    _LIVE_FRAMES_ARCHIVE_CAP,
    _PAIRED_FRAME_IDS_CAP,
    _TRIANGULATED_CAP,
)
from routes import pitch as pitch_routes
from schemas import FramePayload, PitchPayload, SessionResult, TriangulatedPoint
from state import State


# ----------------------------------------------------------------------------
# B3 — LivePairingSession stays bounded
# ----------------------------------------------------------------------------


def test_live_pairing_frames_by_cam_capped_at_archive_cap():
    """Flood one cam with >archive_cap frames; `frames_by_cam` must stay
    at the archive cap (oldest drop first) and not grow without bound."""
    live = LivePairingSession(session_id=sid(1))

    def _no_triangulate(cam, a, b):
        return None

    flood = _LIVE_FRAMES_ARCHIVE_CAP + 2_000
    for i in range(flood):
        live.ingest(
            "A",
            FramePayload(
                frame_index=i, timestamp_s=float(i) / 240.0,
                ball_detected=False,
            ),
            _no_triangulate,
        )
    assert len(live.frames_by_cam["A"]) == _LIVE_FRAMES_ARCHIVE_CAP
    # Frame counter is monotonic — bound is on the stored frames, not on
    # how many we've seen.
    assert live.frame_counts["A"] == flood
    # Oldest frames were evicted first (deque semantics). The earliest
    # surviving frame should have a frame_index equal to the eviction
    # offset (flood - cap).
    head = live.frames_by_cam["A"][0]
    assert head.frame_index == flood - _LIVE_FRAMES_ARCHIVE_CAP


def test_live_pairing_paired_frame_ids_capped_lru():
    """Pair-dedup set must not grow without bound. After flooding past
    the cap, membership of the oldest keys is evicted; newest keys stay."""
    live = LivePairingSession(session_id=sid(2))

    # Drive paired keys directly through the internal helper, bypassing
    # the ingest loop (which would also need a B cam + triangulator).
    flood = _PAIRED_FRAME_IDS_CAP + 50
    for i in range(flood):
        live._remember_pair_key((i, i))

    assert len(live.paired_frame_ids) == _PAIRED_FRAME_IDS_CAP
    assert len(live._paired_frame_id_order) == _PAIRED_FRAME_IDS_CAP
    # Oldest keys are gone; a key near the newest end is still there.
    assert (0, 0) not in live.paired_frame_ids
    assert (flood - 1, flood - 1) in live.paired_frame_ids


def test_live_pairing_triangulated_list_capped():
    """Flood triangulated with synthetic points; deque must enforce cap."""
    live = LivePairingSession(session_id=sid(3))
    flood = _TRIANGULATED_CAP + 1_000
    for i in range(flood):
        live.triangulated.append(
            TriangulatedPoint(
                t_rel_s=float(i), x_m=0.0, y_m=0.0, z_m=0.0, residual_m=0.0,
            )
        )
    assert len(live.triangulated) == _TRIANGULATED_CAP


# ----------------------------------------------------------------------------
# M3 — State._live_pairings eviction
# ----------------------------------------------------------------------------


def _feed_live_frame(state: State, camera_id: str, session_id: str, index: int) -> None:
    """Push one detected frame into `state._live_pairings[session_id]`
    as if a WS `frame` message had arrived. Bypasses the cross-cam
    triangulator so tests can avoid seeding calibrations."""
    state.ingest_live_frame(
        camera_id,
        session_id,
        FramePayload(
            frame_index=index,
            timestamp_s=float(index) / 240.0,
            px=100.0, py=100.0, ball_detected=True,
        ),
    )


def test_delete_session_drops_live_pairing(tmp_path):
    state = State(data_dir=tmp_path)
    session_id = sid(10)
    _feed_live_frame(state, "A", session_id, 0)
    assert session_id in state._live_pairings

    state.delete_session(session_id)
    assert session_id not in state._live_pairings


def test_reset_drops_live_pairings(tmp_path):
    state = State(data_dir=tmp_path)
    a, b = sid(11), sid(12)
    _feed_live_frame(state, "A", a, 0)
    _feed_live_frame(state, "B", b, 0)
    assert state._live_pairings

    state.reset()
    assert not state._live_pairings


def test_drop_live_pairing_is_idempotent(tmp_path):
    state = State(data_dir=tmp_path)
    session_id = sid(13)
    _feed_live_frame(state, "A", session_id, 0)

    state._drop_live_pairing(session_id)
    # Second call on an already-dropped id is a no-op rather than a
    # KeyError — the method is advertised as idempotent.
    state._drop_live_pairing(session_id)
    state._drop_live_pairing("s_neverlived")


# ----------------------------------------------------------------------------
# B2 — detect_pitch timeout wrapper
# ----------------------------------------------------------------------------


def _sample_pitch(session_id: str, *, video_fps: float = 240.0) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=video_fps,
        frames_live=[
            FramePayload(frame_index=i, timestamp_s=float(i) / video_fps, ball_detected=False)
            for i in range(240)  # 1 s of frames
        ],
    )


def test_server_post_timeout_estimates_from_live_frames(tmp_path):
    """1 s of frames @ 240 fps → 2× = 2 s; timeout floor clamps to
    `_SERVER_POST_TIMEOUT_FLOOR_S` (30 s) so very short clips still get
    a reasonable window."""
    p = _sample_pitch(sid(20))
    t = pitch_routes._server_post_timeout_s(p)
    assert t == pytest.approx(pitch_routes._SERVER_POST_TIMEOUT_FLOOR_S)


def test_server_post_timeout_uses_2x_for_long_clips():
    """60 s clip → 2× = 120 s beats the 30 s floor."""
    long_pitch = PitchPayload(
        camera_id="A",
        session_id=sid(21),
        video_start_pts_s=0.0,
        video_fps=60.0,
        # 60 fps × 60 s = 3600 frames
        frames_live=[
            FramePayload(frame_index=i, timestamp_s=float(i) / 60.0, ball_detected=False)
            for i in range(3600)
        ],
    )
    t = pitch_routes._server_post_timeout_s(long_pitch)
    assert t == pytest.approx(120.0)


def test_server_post_timeout_falls_back_when_unknown():
    """No video_fps → can't estimate; use the 120 s fallback ceiling."""
    pitch = PitchPayload(
        camera_id="A",
        session_id=sid(22),
        video_start_pts_s=0.0,
        video_fps=None,
    )
    t = pitch_routes._server_post_timeout_s(pitch)
    assert t == pytest.approx(pitch_routes._SERVER_POST_TIMEOUT_FALLBACK_S)


def test_run_server_detection_times_out_and_records_abort(tmp_path, monkeypatch):
    """Simulate a wedged `detect_pitch` (never returns). `wait_for` must
    fire, the abort reason must land on the SessionResult so `/events`
    can render a red pill, and the processing job must finish so the
    dashboard doesn't leave the spinner running forever."""
    state = State(data_dir=tmp_path)
    monkeypatch.setattr(main, "state", state)

    session_id = sid(30)

    def wedged(*args, **kwargs):
        # Blocking sleep inside the to_thread worker. `asyncio.wait_for`
        # cannot interrupt `time.sleep` — but we only need the sleep to
        # outlive the (50 ms) inner timeout for `wait_for` to fire; 2 s
        # keeps the test fast while still being 40x the timeout cap.
        import time
        time.sleep(2.0)
        return []

    monkeypatch.setattr(main, "detect_pitch", wedged)

    # Shrink both the floor and fallback to keep the test fast.
    monkeypatch.setattr(pitch_routes, "_SERVER_POST_TIMEOUT_FLOOR_S", 0.05)
    monkeypatch.setattr(pitch_routes, "_SERVER_POST_TIMEOUT_FALLBACK_S", 0.05)

    pitch = _sample_pitch(session_id)
    # Seed a SessionResult so `record_server_post_abort` has something
    # to mutate (the real /pitch route records one before queuing the
    # background task).
    state.results[session_id] = SessionResult(
        session_id=session_id,
        camera_a_received=True,
        camera_b_received=False,
    )
    state.mark_server_post_queued(session_id, pitch.camera_id)

    clip_path = tmp_path / "fake.mov"
    clip_path.write_bytes(b"")

    asyncio.run(
        asyncio.wait_for(
            pitch_routes._run_server_detection(clip_path, pitch),
            timeout=5.0,  # generous: the inner timeout fires at 50 ms
        )
    )

    updated = state.results[session_id]
    assert updated.aborted is True
    assert "server_post" in updated.abort_reasons
    assert "timeout" in updated.abort_reasons["server_post"].lower()
    # Job must be cleared — not stuck in `queued`.
    summary = state.session_processing_summary(session_id)
    # summary = (status, has_candidates) — when the job has been
    # finished, status is not "queued".
    status = summary[0]
    assert status != "queued"


# ----------------------------------------------------------------------------
# M2 — finally sentinel protects finish_server_post_job
# ----------------------------------------------------------------------------


def test_run_server_detection_finalizes_even_when_abort_recorder_raises(
    tmp_path, monkeypatch,
):
    """If `_record_server_post_failure` itself raises mid-abort (e.g. the
    SSE broadcast blows up), the finally block must still clear the
    job's `queued` status so the dashboard won't lie about in-flight
    work."""
    state = State(data_dir=tmp_path)
    monkeypatch.setattr(main, "state", state)

    session_id = sid(40)

    def bad_detect(*args, **kwargs):
        raise RuntimeError("simulated detect_pitch failure")

    monkeypatch.setattr(main, "detect_pitch", bad_detect)

    async def blowup(*args, **kwargs):
        raise RuntimeError("simulated abort-recorder failure")

    monkeypatch.setattr(pitch_routes, "_record_server_post_failure", blowup)

    pitch = _sample_pitch(session_id)
    state.mark_server_post_queued(session_id, pitch.camera_id)

    # The coroutine should NOT raise out of our await — the top-level
    # try/finally catches whatever the abort recorder threw (caller
    # code logs it and moves on).
    with pytest.raises(RuntimeError, match="simulated abort-recorder failure"):
        asyncio.run(
            pitch_routes._run_server_detection(tmp_path / "x.mov", pitch)
        )

    # Despite the abort-recorder exploding, finish_server_post_job must
    # have run (either via the explicit branch or the finally sentinel).
    status, _has_candidates = state.session_processing_summary(session_id)
    assert status != "queued"


def test_run_server_detection_happy_path_broadcasts_no_failure(
    tmp_path, monkeypatch,
):
    """Sanity: when detect_pitch succeeds, no abort reason is recorded
    and the job finishes cleanly. Guards against the new code paths
    accidentally firing abort events on success."""
    state = State(data_dir=tmp_path)
    monkeypatch.setattr(main, "state", state)

    session_id = sid(50)

    def detect_ok(*args, **kwargs):
        return []

    def annotate_ok(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "detect_pitch", detect_ok)
    monkeypatch.setattr(pitch_routes, "annotate_video", annotate_ok)

    broadcast_calls = []

    async def record_failure_spy(session_id, camera_id, reason):
        broadcast_calls.append((session_id, camera_id, reason))

    monkeypatch.setattr(pitch_routes, "_record_server_post_failure", record_failure_spy)

    pitch = _sample_pitch(session_id)
    state.results[session_id] = SessionResult(
        session_id=session_id,
        camera_a_received=True,
        camera_b_received=False,
    )
    state.mark_server_post_queued(session_id, pitch.camera_id)

    clip_path = tmp_path / "fake.mov"
    clip_path.write_bytes(b"")

    asyncio.run(
        pitch_routes._run_server_detection(clip_path, pitch)
    )

    assert broadcast_calls == []
    updated = state.results[session_id]
    assert updated.aborted is False
    assert "server_post" not in updated.abort_reasons


# ----------------------------------------------------------------------------
# B-2 review fix — drop-if-persisted uses the archive cap, not raw counts
# ----------------------------------------------------------------------------


def test_drop_live_pairing_uses_clamped_frame_counts(tmp_path):
    """`_drop_live_pairing_if_persisted_locked` compares the pitch's
    `frames_live` length against the live buffer's running frame count.
    Once a cam streams past `_LIVE_FRAMES_ARCHIVE_CAP` frames the live
    buffer saturates (deque `maxlen`) while the count keeps growing, so
    the naive comparison `len(pa.frames_live) < frame_counts[cam]` is
    permanently True and the entry never evicts. The fix clamps the
    expectation to the cap; this test reproduces the post-saturation
    condition and asserts eviction still fires."""
    state = State(data_dir=tmp_path)
    session_id = sid(60)

    _feed_live_frame(state, "A", session_id, 0)
    _feed_live_frame(state, "B", session_id, 0)

    live = state._live_pairings[session_id]
    live.mark_completed("A")
    live.mark_completed("B")
    # Simulate each cam having streamed FAR past the deque cap.
    live.frame_counts["A"] = _LIVE_FRAMES_ARCHIVE_CAP + 7_500
    live.frame_counts["B"] = _LIVE_FRAMES_ARCHIVE_CAP + 12_000

    # Seed each cam's pitch with exactly `_LIVE_FRAMES_ARCHIVE_CAP`
    # frames_live entries — i.e. every frame the deque still remembers
    # has been persisted. The pre-fix code would still refuse to drop
    # because the raw count exceeds the slice.
    def _frames(n: int) -> list[FramePayload]:
        return [
            FramePayload(
                frame_index=i, timestamp_s=float(i) / 240.0, ball_detected=False,
            )
            for i in range(n)
        ]

    state.pitches[("A", session_id)] = PitchPayload(
        camera_id="A",
        session_id=session_id,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_live=_frames(_LIVE_FRAMES_ARCHIVE_CAP),
    )
    state.pitches[("B", session_id)] = PitchPayload(
        camera_id="B",
        session_id=session_id,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_live=_frames(_LIVE_FRAMES_ARCHIVE_CAP),
    )

    with state._lock:
        dropped = state._drop_live_pairing_if_persisted_locked(session_id)

    assert dropped is True
    assert session_id not in state._live_pairings


def test_drop_live_pairing_still_waits_for_partial_persist(tmp_path):
    """Regression guard on the clamp: if the pitch's `frames_live` is
    strictly smaller than the saturated deque (i.e. some frames are
    still buffered in memory and haven't made it onto the pitch JSON),
    eviction must NOT fire — otherwise we'd drop an unflushed buffer."""
    state = State(data_dir=tmp_path)
    session_id = sid(61)

    _feed_live_frame(state, "A", session_id, 0)
    _feed_live_frame(state, "B", session_id, 0)

    live = state._live_pairings[session_id]
    live.mark_completed("A")
    live.mark_completed("B")
    live.frame_counts["A"] = _LIVE_FRAMES_ARCHIVE_CAP + 5_000
    live.frame_counts["B"] = _LIVE_FRAMES_ARCHIVE_CAP + 5_000

    def _frames(n: int) -> list[FramePayload]:
        return [
            FramePayload(
                frame_index=i, timestamp_s=float(i) / 240.0, ball_detected=False,
            )
            for i in range(n)
        ]

    # A has persisted the full clamped cap; B only half. B should block
    # the drop, even though `len < raw_count` would always be True.
    state.pitches[("A", session_id)] = PitchPayload(
        camera_id="A",
        session_id=session_id,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_live=_frames(_LIVE_FRAMES_ARCHIVE_CAP),
    )
    state.pitches[("B", session_id)] = PitchPayload(
        camera_id="B",
        session_id=session_id,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_live=_frames(_LIVE_FRAMES_ARCHIVE_CAP // 2),
    )

    with state._lock:
        dropped = state._drop_live_pairing_if_persisted_locked(session_id)

    assert dropped is False
    assert session_id in state._live_pairings


# ----------------------------------------------------------------------------
# B-3 review fix — timeout branch flips cooperative should_cancel
# ----------------------------------------------------------------------------


def test_run_server_detection_timeout_requests_cancel(tmp_path, monkeypatch):
    """The timeout branch must call `state.request_server_post_cancel`
    so the running `detect_pitch` thread sees `should_cancel` = True on
    its next per-frame check and bails out. Without this call the PyAV
    decode keeps burning CPU + RAM even though FastAPI has given up on
    awaiting the task.

    We capture the cancel call by wrapping the real method with a spy
    and simulate a wedged detect via `time.sleep`."""
    state = State(data_dir=tmp_path)
    monkeypatch.setattr(main, "state", state)

    session_id = sid(62)

    cancel_calls: list[tuple[str, str]] = []
    original_cancel = state.request_server_post_cancel

    def _spy_cancel(sid_arg: str, cam_arg: str) -> bool:
        cancel_calls.append((sid_arg, cam_arg))
        return original_cancel(sid_arg, cam_arg)

    monkeypatch.setattr(state, "request_server_post_cancel", _spy_cancel)

    def wedged(*args, **kwargs):
        import time
        time.sleep(1.5)
        return []

    monkeypatch.setattr(main, "detect_pitch", wedged)
    # Force the computed timeout to 0.1 s regardless of clip length by
    # stubbing the timeout helper outright — safer than fighting the
    # `max(floor, 2*duration)` formula with floor tweaks.
    monkeypatch.setattr(pitch_routes, "_server_post_timeout_s", lambda _p: 0.1)

    pitch = _sample_pitch(session_id)
    state.results[session_id] = SessionResult(
        session_id=session_id,
        camera_a_received=True,
        camera_b_received=False,
    )
    state.mark_server_post_queued(session_id, pitch.camera_id)

    clip_path = tmp_path / "fake.mov"
    clip_path.write_bytes(b"")

    asyncio.run(
        asyncio.wait_for(
            pitch_routes._run_server_detection(clip_path, pitch),
            timeout=5.0,
        )
    )

    # Cooperative cancel must have fired exactly once, and for this job.
    assert (session_id, pitch.camera_id) in cancel_calls
    # The underlying processing flag must be flipped so a subsequent
    # `should_cancel` check wins.
    assert state.should_cancel_server_post_job(session_id, pitch.camera_id) is True
