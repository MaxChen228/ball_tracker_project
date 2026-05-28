"""GET /devices/pool + POST /devices/assign + POST /devices/unassign.

Phase 0 PR1 — purely additive REST endpoints. These tests pin the wire
shape the dashboard Device Pool panel will consume in PR2 and the
collision / validation rules the operator hits when mis-typing
assignments.

The store is not yet enforced at WS handshake (that lands in PR2), so
these tests only exercise the bookkeeping layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    return main


def test_pool_empty_when_no_devices(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/devices/pool")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "assignments": [],
        "pending": [],
        "observed_unassigned": [],
        "cam_id_in_use": [],
    }


def test_assign_creates_record(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/devices/assign", json={
        "device_uuid": "uuid-1",
        "camera_id": "A",
        "device_model": "iPhone15,3",
    })
    assert r.status_code == 200, r.text
    rec = r.json()["assignment"]
    assert rec["device_uuid"] == "uuid-1"
    assert rec["camera_id"] == "A"
    assert rec["device_model"] == "iPhone15,3"

    pool = client.get("/devices/pool").json()
    assert len(pool["assignments"]) == 1
    assert pool["assignments"][0]["camera_id"] == "A"
    # Offline (no WS connection in the test): online flag must be False.
    assert pool["assignments"][0]["online"] is False


def test_assign_camera_id_collision_returns_409(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "A",
    })
    r = client.post("/devices/assign", json={
        "device_uuid": "uuid-2", "camera_id": "A",
    })
    assert r.status_code == 409, r.text
    assert "already assigned" in r.json()["detail"]


def test_reassign_same_uuid_releases_old_camera(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "A",
    })
    # Re-assigning uuid-1 to B must release A so uuid-2 can take it.
    r = client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "B",
    })
    assert r.status_code == 200
    r = client.post("/devices/assign", json={
        "device_uuid": "uuid-2", "camera_id": "A",
    })
    assert r.status_code == 200

    pool = client.get("/devices/pool").json()
    cam_to_uuid = {a["camera_id"]: a["device_uuid"] for a in pool["assignments"]}
    assert cam_to_uuid == {"A": "uuid-2", "B": "uuid-1"}


def test_assign_rejects_invalid_camera_id_format(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for bad in ["", "way-too-long-camera-id-name", "A B"]:
        r = client.post("/devices/assign", json={
            "device_uuid": "uuid-1", "camera_id": bad,
        })
        assert r.status_code == 422, f"{bad!r}: {r.text}"


def test_assign_requires_device_uuid_and_camera_id(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.post("/devices/assign", json={"camera_id": "A"})
    assert r.status_code == 422
    r = client.post("/devices/assign", json={"device_uuid": "u1"})
    assert r.status_code == 422


def test_unassign_by_camera_id(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "A",
    })
    r = client.post("/devices/unassign", json={"camera_id": "A"})
    assert r.status_code == 200
    assert r.json()["unassigned"] is True
    # Idempotent: second call returns unassigned=False.
    r = client.post("/devices/unassign", json={"camera_id": "A"})
    assert r.json()["unassigned"] is False


def test_unassign_by_device_uuid(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "A",
    })
    r = client.post("/devices/unassign", json={"device_uuid": "uuid-1"})
    assert r.status_code == 200
    assert r.json()["unassigned"] is True


def test_unassign_requires_exactly_one_key(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    # Both keys present → 422
    r = client.post("/devices/unassign", json={
        "camera_id": "A", "device_uuid": "u1",
    })
    assert r.status_code == 422
    # Neither → 422
    r = client.post("/devices/unassign", json={})
    assert r.status_code == 422


def test_pool_persists_across_state_rebuild(tmp_path, monkeypatch):
    """Assignment must survive a State re-instantiation (server restart
    proxy). Otherwise an operator's careful pre-game cam_id mapping
    vanishes on every reboot."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-1", "camera_id": "A", "device_model": "iPhone15,3",
    })

    # Re-instantiate state pointing at the same data dir.
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client2 = TestClient(main.app)
    pool = client2.get("/devices/pool").json()
    assert len(pool["assignments"]) == 1
    assert pool["assignments"][0]["device_uuid"] == "uuid-1"
    assert pool["assignments"][0]["device_model"] == "iPhone15,3"


def test_observed_unassigned_appears_after_heartbeat(tmp_path, monkeypatch):
    """A phone reporting a device_uuid via heartbeat that has no
    assignment yet must surface under `observed_unassigned` so the
    operator can promote it."""
    main = _fresh_main(tmp_path, monkeypatch)
    main.state.heartbeat(
        "A",
        device_id="uuid-fresh",
        device_model="iPhone15,3",
    )
    client = TestClient(main.app)
    pool = client.get("/devices/pool").json()
    assert len(pool["assignments"]) == 0
    assert len(pool["observed_unassigned"]) == 1
    rec = pool["observed_unassigned"][0]
    assert rec["device_uuid"] == "uuid-fresh"
    assert rec["camera_id"] == "A"
    assert rec["device_model"] == "iPhone15,3"


def test_assigned_record_marked_online_when_uuid_matches(tmp_path, monkeypatch):
    """After assign(), if that exact device_uuid is now heartbeating
    under the assigned camera_id, the pool record's `online` flips to
    True. Hot-swap protection: a *different* device_uuid showing up on
    the same cam_id does NOT mark the assignment online (that'd lie to
    the operator about which physical phone is plugged in)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/devices/assign", json={
        "device_uuid": "uuid-real", "camera_id": "A",
    })
    # Same device heartbeats → online True.
    main.state.heartbeat("A", device_id="uuid-real")
    pool = client.get("/devices/pool").json()
    assert pool["assignments"][0]["online"] is True

    # Different device heartbeats on A → online flips back to False
    # (assignment record still names the original UUID).
    main.state.heartbeat("A", device_id="uuid-imposter")
    pool = client.get("/devices/pool").json()
    assert pool["assignments"][0]["online"] is False
    # And the imposter shows up under observed_unassigned (it's a known
    # device_uuid with no matching assignment).
    obs_uuids = {r["device_uuid"] for r in pool["observed_unassigned"]}
    assert "uuid-imposter" in obs_uuids
