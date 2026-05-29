"""Phase 1 quick-sync core: N=2/3/4 solve correctness.

Quick sync replaces the A↔B mutual chirp. A single emitter plays band A;
every online cam (emitter included) self-/hears it and reports the
chirp-arrival PTS on its OWN host clock. The solver differences each
listener's anchor against the emitter's → per-cam clock offset `delta_s`.

The huddle-then-place workflow means all phones sit <10cm apart at sync
time, so propagation delay is negligible and intentionally NOT
compensated — the recovered deltas are pure clock offsets.

These tests drive the state machine directly with constructed
`QuickSyncReport`s (the WAV→anchor detection path is covered by
`test_sync_audio_detect`/`test_quick_sync_audio_upload`)."""
from __future__ import annotations

import main
from schemas import QuickSyncReport


def _report(sync_id: str, cam: str, anchor: float) -> QuickSyncReport:
    return QuickSyncReport(camera_id=cam, sync_id=sync_id, anchor_pts_s=anchor)


def _start(emitter: str, cams: list[str]):
    for c in cams:
        main.state.heartbeat(c)
    run, reason = main.state.start_quick_sync(emitter)
    assert reason is None, reason
    assert run is not None
    return run


def test_n2_solve_deltas_relative_to_emitter():
    run = _start("A", ["A", "B"])
    # A is emitter (zero point). B's clock is 12.3 ms ahead.
    main.state.sync.record_quick_sync_report(_report(run.id, "A", 100.000000))
    _, result, reason = main.state.sync.record_quick_sync_report(
        _report(run.id, "B", 100.012300))
    assert reason is None
    assert result is not None
    assert result.aborted is False
    assert result.emitter_cam_id == "A"
    assert result.deltas_s["A"] == 0.0
    assert abs(result.deltas_s["B"] - 0.012300) < 1e-9
    assert result.missing_cam_ids == []


def test_n3_all_aligned():
    run = _start("B", ["A", "B", "C"])
    anchors = {"A": 50.001, "B": 50.000, "C": 49.997}
    result = None
    for cam in run.listener_cam_ids:
        _, result, _ = main.state.sync.record_quick_sync_report(
            _report(run.id, cam, anchors[cam]))
    assert result is not None and result.aborted is False
    # Emitter B is the zero point.
    assert result.deltas_s["B"] == 0.0
    assert abs(result.deltas_s["A"] - 0.001) < 1e-9
    assert abs(result.deltas_s["C"] - (-0.003)) < 1e-9
    assert set(result.anchors_pts_s.keys()) == {"A", "B", "C"}


def test_n4_solve_complete():
    run = _start("A", ["A", "B", "C", "D"])
    anchors = {"A": 10.0, "B": 10.0005, "C": 9.9992, "D": 10.0011}
    result = None
    for cam in run.listener_cam_ids:
        _, result, _ = main.state.sync.record_quick_sync_report(
            _report(run.id, cam, anchors[cam]))
    assert result is not None and result.aborted is False
    assert len(result.deltas_s) == 4
    for cam in ("A", "B", "C", "D"):
        assert abs(result.deltas_s[cam] - (anchors[cam] - anchors["A"])) < 1e-9


def test_incomplete_run_returns_run_not_result():
    run = _start("A", ["A", "B", "C"])
    run_after, result, reason = main.state.sync.record_quick_sync_report(
        _report(run.id, "A", 1.0))
    assert reason is None
    assert result is None
    assert run_after is not None
    assert run_after.id == run.id


def test_stale_sync_id_rejected():
    run = _start("A", ["A", "B"])
    _, result, reason = main.state.sync.record_quick_sync_report(
        _report("sy_deadbeef", "A", 1.0))
    assert reason == "stale_sync_id"
    assert result is None


def test_report_with_no_active_run():
    _, result, reason = main.state.sync.record_quick_sync_report(
        _report("sy_deadbeef", "A", 1.0))
    assert reason == "no_sync"
    assert result is None


def test_emitter_must_be_online():
    main.state.heartbeat("A")
    run, reason = main.state.start_quick_sync("Z")
    assert run is None
    assert reason == "emitter_offline"
