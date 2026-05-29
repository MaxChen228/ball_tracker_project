"""Phase 7-fix-2 — non-current-algorithm triangulation helper.

Covers:
- multi-alg accumulation into result.triangulated_by_algorithm
- abort_reasons[f"alg:{id}"] populated when triangulate_pair raises
  (no silent fallback per CLAUDE.md)
- tuning kwarg threads through to triangulate_pair
- recompute_result_for_session preserves non-current alg history
  (Block 1 — the regression that prompted this phase)
- cross-cam algorithm mismatch logs warning but does not crash
"""
from __future__ import annotations

import logging

import session_results
from detection_paths import stamp_server_post_run
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    FramePayload,
    PitchPayload,
    SessionResult,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0,
                                  aspect=1.0, fill=0.68)],
    )


def _snapshot(alg_id: str) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30, "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )


def _pitch(camera_id: str = "A", **kw) -> PitchPayload:
    """Helper that translates a couple of legacy convenience kwargs
    into the post-phase-3 dict-canonical shape so call sites stay
    readable."""
    server_post_config = kw.pop("server_post_config_used", None)
    live_config = kw.pop("live_config_used", None)
    config_dict = dict(kw.pop("config_used_by_algorithm", {}))
    if live_config is not None:
        config_dict.setdefault("ios_capture_time", live_config)
    active = kw.pop("active_server_post_algorithm_id", None)
    if server_post_config is not None:
        config_dict.setdefault(server_post_config.algorithm_id, server_post_config)
        if active is None:
            active = server_post_config.algorithm_id
    if config_dict:
        kw["config_used_by_algorithm"] = config_dict
    if active is not None:
        kw["active_server_post_algorithm_id"] = active
    return PitchPayload(
        camera_id=camera_id,
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
        **kw,
    )


def _empty_result() -> SessionResult:
    return SessionResult(
        session_id="s_deadbeef",
        cameras_received={"A": True, "B": True},
    )


class _FakeTriangulatePair:
    """Records every call so tests can assert alg routing. As of Phase 4-2
    the monkeypatch target is `triangulate_all_pairs_for_session` (N-cam
    entry point) instead of the old single-pair `triangulate_pair`.
    Signature: (state, pitches_by_cam: dict[str, PitchPayload], *, source)."""

    def __init__(self, *, raise_for: set[str] | None = None):
        self.calls: list[dict] = []
        self.raise_for = raise_for or set()

    def __call__(self, state, pitches_by_cam, *, source="server"):
        # Detect which alg this clone is carrying — pitch_with_algorithm_frames
        # projects the alg's frames into frames_server_post, so frame[0].frame_index
        # encodes which bucket was selected by the helper. Read from any
        # representative cam (they all carry the same alg's frames at this point).
        any_pitch = next(iter(pitches_by_cam.values()))
        alg_marker = (
            any_pitch.frames_server_post[0].frame_index
            if any_pitch.frames_server_post else -1
        )
        self.calls.append({
            "alg_marker": alg_marker,
            "source": source,
            "cams": sorted(pitches_by_cam.keys()),
        })
        if alg_marker in self.raise_for:
            raise RuntimeError(f"forced failure for marker={alg_marker}")
        # Deterministic single-point result keyed off the marker. Second
        # tuple element is the skipped-pairs list (empty — fake never
        # simulates uncalibrated cams).
        from schemas import TriangulatedPoint
        return [
            TriangulatedPoint(
                t_rel_s=0.0, x_m=float(alg_marker), y_m=0.0, z_m=0.0,
                residual_m=0.01, cost_a=0.0, cost_b=0.0,
                pair_key=("A","B"),
            ),
        ], []


def test_helper_accumulates_non_current_algorithms(monkeypatch):
    fake = _FakeTriangulatePair()
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)

    snap_v11 = _snapshot("v11_hsv_cc")
    a = _pitch(camera_id="A", server_post_config_used=snap_v11)
    b = _pitch(camera_id="B", server_post_config_used=snap_v11)
    # Both cams currently on v11; v12 is "non-current" history.
    a.frames_by_algorithm = {
        "v11_hsv_cc": [_frame(11)],
        "v12_history": [_frame(12)],
    }
    b.frames_by_algorithm = {
        "v11_hsv_cc": [_frame(11)],
        "v12_history": [_frame(12)],
    }
    result = _empty_result()

    session_results._triangulate_non_current_algorithms(
        state=None, pitches_by_cam={"A": a, "B": b}, sync_error=None, result=result,
    )

    # v11 (current) skipped; v12 triangulated:
    assert "v12_history" in result.triangulated_by_algorithm
    assert "v11_hsv_cc" not in result.triangulated_by_algorithm
    assert result.algorithms_completed == {"v12_history"}
    assert result.frame_counts_by_algorithm["v12_history"] == {"A": 1, "B": 1}
    assert [c["alg_marker"] for c in fake.calls] == [12]


def test_helper_writes_abort_reason_on_exception(monkeypatch):
    """No silent fallback: if triangulate_pair raises, the failure
    must surface via result.abort_reasons[f'alg:{id}']."""
    fake = _FakeTriangulatePair(raise_for={12})
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)

    snap_v11 = _snapshot("v11_hsv_cc")
    a = _pitch(camera_id="A", server_post_config_used=snap_v11)
    b = _pitch(camera_id="B", server_post_config_used=snap_v11)
    a.frames_by_algorithm = {"v11_hsv_cc": [_frame(11)], "v12_bad": [_frame(12)]}
    b.frames_by_algorithm = {"v11_hsv_cc": [_frame(11)], "v12_bad": [_frame(12)]}
    result = _empty_result()

    session_results._triangulate_non_current_algorithms(
        state=None, pitches_by_cam={"A": a, "B": b}, sync_error=None, result=result,
    )

    assert "alg:v12_bad" in result.abort_reasons
    assert "RuntimeError" in result.abort_reasons["alg:v12_bad"]
    assert "v12_bad" not in result.triangulated_by_algorithm
    assert "v12_bad" not in result.algorithms_completed


def test_helper_calls_triangulate_pair_for_each_non_current_alg(monkeypatch):
    """Helper must call triangulate_pair once per non-current algorithm.
    Cost-absorption refactor removed the per-session `tuning` kwarg —
    cost is per-algorithm (resolved at filter time), gap is on the
    SessionResult — so the helper signature is now plain
    `(state, a, b, sync_error, result)`."""
    fake = _FakeTriangulatePair()
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)

    snap_v11 = _snapshot("v11_hsv_cc")
    a = _pitch(camera_id="A", server_post_config_used=snap_v11)
    b = _pitch(camera_id="B", server_post_config_used=snap_v11)
    a.frames_by_algorithm = {"v11_hsv_cc": [_frame(11)], "v12_x": [_frame(12)]}
    b.frames_by_algorithm = {"v11_hsv_cc": [_frame(11)], "v12_x": [_frame(12)]}

    session_results._triangulate_non_current_algorithms(
        state=None, pitches_by_cam={"A": a, "B": b}, sync_error=None,
        result=_empty_result(),
    )

    # Only v12_x is non-current; v11 (current per snapshot) is skipped.
    assert len(fake.calls) == 1
    assert fake.calls[0]["alg_marker"] == 12


def test_helper_logs_cross_cam_algorithm_mismatch(monkeypatch, caplog):
    import algorithms as algorithms_mod
    fake_entry = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_other", label="other", description="other",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
        cost_threshold=0.5,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v12_other", fake_entry)

    fake = _FakeTriangulatePair()
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)

    a = _pitch(camera_id="A", server_post_config_used=_snapshot("v11_hsv_cc"))
    b = _pitch(camera_id="B", server_post_config_used=_snapshot("v12_other"))
    # v11 in A's dict, v12 in B's dict. Both are "current" (per their
    # own snapshots) — the helper must skip both.
    a.frames_by_algorithm = {"v11_hsv_cc": [_frame(11)]}
    b.frames_by_algorithm = {"v12_other": [_frame(12)]}
    result = _empty_result()

    with caplog.at_level(logging.WARNING, logger="session_results"):
        session_results._triangulate_non_current_algorithms(
            state=None, pitches_by_cam={"A": a, "B": b}, sync_error=None, result=result,
        )

    assert any("algorithm mismatch" in rec.message for rec in caplog.records)
    # Neither v11 nor v12 should be in non-current bucket.
    assert result.triangulated_by_algorithm == {}


def test_recompute_preserves_non_current_alg_history(tmp_path, monkeypatch):
    """Block 1 regression test: hitting Recompute on a v11→v12 session
    must keep v11 in result.triangulated_by_algorithm. Pre-fix, only
    rebuild called the helper, so Recompute silently dropped history."""
    import algorithms as algorithms_mod
    import main

    fake = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_test",
        label="test", description="test",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
        cost_threshold=0.5,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v12_test", fake)

    # Stub triangulate_all_pairs_for_session so the test doesn't need
    # calibration / MOVs (Phase 4-2: that's the N-cam entry point now).
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair())

    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)

    # Both cams: v11 then v12 — dict carries both.
    for cam in ("A", "B"):
        p = _pitch(camera_id=cam, sync_id="sy_deadbeef",
                   sync_anchor_timestamp_s=0.0)
        stamp_server_post_run(p, _snapshot("v11_hsv_cc"), [_frame(11)])
        s.record(p)
        p2 = _pitch(camera_id=cam, sync_id="sy_deadbeef",
                    sync_anchor_timestamp_s=0.0)
        stamp_server_post_run(p2, _snapshot("v12_test"), [_frame(12)])
        s.record(p2)

    result = session_results.recompute_result_for_session(
        s, "s_deadbeef", gap_threshold_m=0.2,
    )

    assert "v11_hsv_cc" in result.triangulated_by_algorithm, (
        "Recompute lost v11 history; helper not wired into recompute path"
    )
    assert "v11_hsv_cc" in result.algorithms_completed
