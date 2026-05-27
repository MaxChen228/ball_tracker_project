"""Regression tests for the camera_id-keyed dict shape on SyncResult
(`times_by_role` / `traces_by_role`), introduced in the N-camera infra
phase 2 refactor.

Lock the invariants that the flat-field → dict transition could
silently break:
  - Partial reports (one role aborts before sending): the missing role's
    entry must be ABSENT from `times_by_role` / `traces_by_role`, not
    present-with-all-None. Downstream readers gate on `role not in dict`.
  - Late abort merge: when a role's report arrives within the grace
    window AFTER the run resolved aborted, its entry must be merged
    into the existing dicts (not overwriting other roles' data).
"""
from __future__ import annotations

import main
from schemas import SyncReport


def test_aborted_result_omits_missing_role_from_dicts(tmp_path):
    """Sync run times out with only role A having reported. The
    aborted SyncResult MUST contain `"A" in times_by_role` but
    `"B" not in times_by_role` — the absent key is the explicit
    "B never reported" signal, distinct from "B reported all-None"."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")
    run, reason = s.start_sync()
    assert reason is None and run is not None

    # Only A reports — B stays silent.
    rep_a = SyncReport(
        camera_id="A", sync_id=run.id, role="A",
        t_self_s=0.123, t_from_other_s=0.456, emitted_band="A",
    )
    s._sync.record_sync_report(rep_a)

    # Advance past the sync timeout. `current_sync()` checks
    # `_check_sync_timeout_locked` and promotes the stalled run into
    # an aborted result.
    clock["now"] += 9.0  # > _SYNC_TIMEOUT_S
    assert s.sync.current_sync() is None  # forces the timeout sweep

    result = s.sync.last_sync_result()
    assert result is not None
    assert result.aborted is True
    assert "A" in result.times_by_role
    assert "B" not in result.times_by_role
    assert "A" in result.traces_by_role or "B" not in result.traces_by_role
    # A's reported timestamps survived the dict transition intact.
    assert result.times_by_role["A"].t_self_s == 0.123
    assert result.times_by_role["A"].t_from_other_s == 0.456
    # abort_reasons records the silent role.
    assert "B" in result.abort_reasons


def test_late_abort_report_merges_into_role_dict(tmp_path):
    """After a run aborts with rep_a present + rep_b missing, if rep_b
    arrives within `_SYNC_LATE_REPORT_GRACE_S` carrying partial data
    (e.g. abort + trace_self only), `_merge_late_abort_report_locked`
    must add "B" to the existing times/traces dicts WITHOUT clobbering
    "A"."""
    clock = {"now": 1000.0}
    s = main.State(data_dir=tmp_path, time_fn=lambda: clock["now"])
    s.heartbeat("A")
    s.heartbeat("B")
    run, _ = s.start_sync()
    assert run is not None
    rep_a = SyncReport(
        camera_id="A", sync_id=run.id, role="A",
        t_self_s=0.111, t_from_other_s=0.222, emitted_band="A",
    )
    s._sync.record_sync_report(rep_a)
    clock["now"] += 9.0
    assert s.sync.current_sync() is None  # force timeout → aborted, A-only dict

    # Now B's late abort report lands within grace window with t_self_s set.
    rep_b_late = SyncReport(
        camera_id="B", sync_id=run.id, role="B",
        t_self_s=0.999, t_from_other_s=None, emitted_band="B",
        aborted=True, abort_reason="dismissed",
    )
    s._sync.record_sync_report(rep_b_late)

    result = s.sync.last_sync_result()
    assert result is not None
    assert result.aborted is True
    # A's data untouched.
    assert "A" in result.times_by_role
    assert result.times_by_role["A"].t_self_s == 0.111
    # B's late entry merged in with the partial timestamp it carried.
    assert "B" in result.times_by_role
    assert result.times_by_role["B"].t_self_s == 0.999
    assert result.times_by_role["B"].t_from_other_s is None
    # abort_reasons reflects the late report's reason.
    assert result.abort_reasons["B"] == "dismissed"
