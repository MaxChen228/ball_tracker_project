"""Phase 1 quick-sync: emitter self-hear is the run's zero point. If the
emitter misses its own chirp there is no common reference, so the WHOLE
run aborts loudly (no silent fallback to some other cam as the zero).

Contrast with `test_quick_sync_partial_miss`: a *listener* miss is
survivable, an *emitter* miss is fatal."""
from __future__ import annotations

import main
from schemas import QuickSyncReport


def _start(emitter: str, cams: list[str]):
    for c in cams:
        main.state.heartbeat(c)
    run, reason = main.state.start_quick_sync(emitter)
    assert reason is None and run is not None
    return run


def test_emitter_no_self_hear_aborts_whole_run():
    run = _start("A", ["A", "B", "C"])
    # A (emitter) heard nothing; B and C heard fine.
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=None,
                        aborted=True, abort_reason="speaker_muted"))
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=2.0))
    _, result, _ = main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="C", sync_id=run.id, anchor_pts_s=2.001))
    assert result is not None
    assert result.aborted is True
    assert result.abort_reasons["A"] == "speaker_muted"
    assert result.deltas_s == {}
    assert result.anchors_pts_s == {}


def test_emitter_null_anchor_no_flag_also_aborts():
    run = _start("A", ["A", "B"])
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=None))
    _, result, _ = main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=1.0))
    assert result is not None and result.aborted is True
    assert result.abort_reasons["A"] == "emitter_no_self_hear"


def test_emitter_never_reports_aborts_on_timeout(monkeypatch):
    import state_sync
    run = _start("A", ["A", "B"])
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=4.0))
    base = main.state.sync._time_fn()
    monkeypatch.setattr(main.state.sync, "_time_fn",
                        lambda: base + state_sync._QUICK_SYNC_TIMEOUT_S + 1.0)
    assert main.state.sync.current_quick_sync() is None
    result = main.state.sync.last_quick_sync_result()
    assert result is not None and result.aborted is True
    assert result.abort_reasons["A"] == "emitter_no_report"
