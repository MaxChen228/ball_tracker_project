"""Pin: dashboard event dict carries `n_server_post_algorithms` derived
from `pitch.frames_by_algorithm.keys()` — same source viewer's history
dropdown reads. Without this, the dashboard `+N` badge could drift below
viewer's count (e.g., if `SessionResult.frame_counts_by_algorithm`
happens to drop a bucket whose A/B cams both produced zero frames)."""
from __future__ import annotations

from schemas import PitchPayload
from state import State
from state_events import build_events


def _sid(n: int) -> str:
    return f"s_{n:08x}"


def _pitch(cam: str, sid: str, *, alg_keys: list[str]) -> PitchPayload:
    return PitchPayload(
        camera_id=cam,
        session_id=sid,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_by_algorithm={alg: [] for alg in alg_keys},
    )


def _event_for(events: list[dict], sid: str) -> dict:
    return next(e for e in events if e["session_id"] == sid)


def test_single_server_algorithm_yields_count_1(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(1)
    s.record(_pitch("A", sid, alg_keys=["ios_capture_time", "v11_hsv_cc"]))
    s.record(_pitch("B", sid, alg_keys=["ios_capture_time", "v11_hsv_cc"]))
    assert _event_for(build_events(s), sid)["n_server_post_algorithms"] == 1


def test_live_only_session_yields_count_0(tmp_path):
    s = State(data_dir=tmp_path)
    sid = _sid(3)
    s.record(_pitch("A", sid, alg_keys=["ios_capture_time"]))
    s.record(_pitch("B", sid, alg_keys=["ios_capture_time"]))
    assert _event_for(build_events(s), sid)["n_server_post_algorithms"] == 0
