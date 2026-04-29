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
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
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
