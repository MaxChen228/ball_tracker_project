"""Phase 1 quick-sync: a listener that misses the chirp is DISABLED for the
session, not a fatal error — as long as the emitter self-heard.

This is the death-of-mutual-sync payoff: with N≥3, one deaf cam no longer
kills the whole sync. The missed cam lands in `missing_cam_ids` (explicit,
surfaced to the operator) and is simply absent from `deltas_s`. No silent
fallback — the operator decides to re-sync or proceed without it."""
from __future__ import annotations

import main
from schemas import QuickSyncReport


def _start(emitter: str, cams: list[str]):
    for c in cams:
        main.state.heartbeat(c)
    run, reason = main.state.start_quick_sync(emitter)
    assert reason is None and run is not None
    return run


def test_one_listener_misses_others_still_solve():
    run = _start("A", ["A", "B", "C"])
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=5.0))
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=5.004))
    # C heard nothing → reports aborted with null anchor.
    _, result, _ = main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="C", sync_id=run.id, anchor_pts_s=None,
                        aborted=True, abort_reason="no_chirp_detected"))
    assert result is not None
    assert result.aborted is False  # emitter self-heard → run is valid
    assert set(result.deltas_s.keys()) == {"A", "B"}
    assert result.missing_cam_ids == ["C"]
    assert result.abort_reasons.get("C") == "no_chirp_detected"
    assert "C" not in result.anchors_pts_s


def test_null_anchor_without_aborted_flag_also_disables():
    """A report with anchor_pts_s=None but aborted=False (defensive: a
    malformed listener) is still treated as a miss, not a delta of 0."""
    run = _start("A", ["A", "B"])
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=3.0))
    _, result, _ = main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=None))
    assert result is not None and result.aborted is False
    assert result.missing_cam_ids == ["B"]
    assert "B" not in result.deltas_s


def test_timeout_solves_with_partial_reports(monkeypatch):
    """If a listener never POSTs before the timeout, the run still solves
    with whatever arrived (emitter present → partial solve)."""
    import state_sync
    run = _start("A", ["A", "B", "C"])
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=7.0))
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=7.002))
    # Advance the coordinator's clock past the timeout; C never reported.
    base = main.state.sync._time_fn()
    monkeypatch.setattr(main.state.sync, "_time_fn",
                        lambda: base + state_sync._QUICK_SYNC_TIMEOUT_S + 1.0)
    # current_quick_sync() runs the timeout check and finalizes the run.
    assert main.state.sync.current_quick_sync() is None
    result = main.state.sync.last_quick_sync_result()
    assert result is not None and result.aborted is False
    assert set(result.deltas_s.keys()) == {"A", "B"}
    assert result.missing_cam_ids == ["C"]
