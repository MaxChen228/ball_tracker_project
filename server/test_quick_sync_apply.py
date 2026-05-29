"""Phase 4-3: POST /sync/quick_apply/{sync_id} writes the solved
quick-sync anchors onto the device registry.

A quick sync solves per-cam chirp-arrival PTS (`anchors_pts_s`) but
nothing pushed them into `Device.sync_anchor_timestamp_s` — the field
`LivePairingSession` reads for its anchor-relative window math. Without
apply, a quick sync ran but live pairing still saw stale/no anchors.
This route closes that gap.

Failure modes are loud (no silent fallback): 404 when nothing solved,
409 on an aborted run, 409 on a stale sync_id."""
from __future__ import annotations

import main
from fastapi.testclient import TestClient
from main import app
from schemas import QuickSyncReport


def _solve_quick_sync(emitter: str, anchors: dict[str, float]):
    """Drive the state machine to a solved QuickSyncResult. Heartbeats
    every cam online, starts a run, feeds one report per listener.
    Returns the final QuickSyncResult."""
    for cam in anchors:
        main.state.heartbeat(cam)
    run, reason = main.state.start_quick_sync(emitter)
    assert reason is None and run is not None, reason
    result = None
    for cam in run.listener_cam_ids:
        _, result, _ = main.state.sync.record_quick_sync_report(
            QuickSyncReport(camera_id=cam, sync_id=run.id,
                            anchor_pts_s=anchors[cam]))
    assert result is not None and result.aborted is False
    return result


def test_apply_stamps_all_cam_anchors():
    result = _solve_quick_sync("A", {"A": 100.0, "B": 100.0123, "C": 99.997})
    client = TestClient(app)
    r = client.post(f"/sync/quick_apply/{result.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["applied"] == ["A", "B", "C"]
    assert body["missing"] == []
    for cam in ("A", "B", "C"):
        dev = main.state.device_snapshot(cam)
        assert dev is not None
        assert dev.sync_anchor_timestamp_s == result.anchors_pts_s[cam]
        assert dev.time_synced is True
        assert dev.time_sync_id == result.id


def test_apply_with_no_result_404():
    client = TestClient(app)
    r = client.post("/sync/quick_apply/sy_deadbeef")
    assert r.status_code == 404
    assert r.json()["detail"] == "no_result"


def test_apply_aborted_run_409():
    """Emitter self-hear miss → whole run aborts, no zero point, no
    anchors. Apply must refuse rather than stamp an empty set."""
    for cam in ("A", "B"):
        main.state.heartbeat(cam)
    run, reason = main.state.start_quick_sync("A")
    assert reason is None and run is not None
    main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="A", sync_id=run.id, anchor_pts_s=None,
                        aborted=True, abort_reason="speaker_muted"))
    _, result, _ = main.state.sync.record_quick_sync_report(
        QuickSyncReport(camera_id="B", sync_id=run.id, anchor_pts_s=2.0))
    assert result is not None and result.aborted is True

    client = TestClient(app)
    r = client.post(f"/sync/quick_apply/{result.id}")
    assert r.status_code == 409
    assert r.json()["detail"] == "aborted"
    # Nothing stamped — B's anchor stays unset.
    assert main.state.device_snapshot("B").sync_anchor_timestamp_s is None


def test_apply_stale_sync_id_409_with_expected():
    result = _solve_quick_sync("A", {"A": 10.0, "B": 10.0005})
    client = TestClient(app)
    r = client.post("/sync/quick_apply/sy_wrongone")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "stale_sync_id"
    assert detail["expected"] == result.id
    # No anchors stamped on the mismatch.
    assert main.state.device_snapshot("A").sync_anchor_timestamp_s is None


def test_apply_is_idempotent():
    result = _solve_quick_sync("B", {"A": 50.001, "B": 50.0, "C": 49.997})
    client = TestClient(app)
    r1 = client.post(f"/sync/quick_apply/{result.id}")
    assert r1.status_code == 200
    first = {c: main.state.device_snapshot(c).sync_anchor_timestamp_s
             for c in ("A", "B", "C")}
    r2 = client.post(f"/sync/quick_apply/{result.id}")
    assert r2.status_code == 200
    second = {c: main.state.device_snapshot(c).sync_anchor_timestamp_s
              for c in ("A", "B", "C")}
    assert first == second == result.anchors_pts_s
