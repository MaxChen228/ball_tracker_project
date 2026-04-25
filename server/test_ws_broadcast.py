"""WebSocket fan-out / pairing: live WS stream, sync trigger broadcast,
calibration broadcast to siblings."""
from __future__ import annotations

import time

import numpy as np
from fastapi.testclient import TestClient

import main
from main import app

from _test_helpers import (
    _make_scene,
    _project_pixels,
)


def _post_calibration(client: TestClient, camera_id: str, K: np.ndarray, H: np.ndarray):
    return client.post(
        "/calibration",
        json={
            "camera_id": camera_id,
            "intrinsics": {
                "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
            },
            "homography": H.flatten().tolist(),
            "image_width_px": 1920,
            "image_height_px": 1080,
        },
    )


def test_live_websocket_stream_pairs_frames_and_emits_events(monkeypatch):
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    client = TestClient(app)

    cal_a = {
        "camera_id": "A",
        "intrinsics": {
            "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        },
        "homography": H_a.flatten().tolist(),
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    cal_b = {
        "camera_id": "B",
        "intrinsics": {
            "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        },
        "homography": H_b.flatten().tolist(),
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    assert client.post("/calibration", json=cal_a).status_code == 200
    assert client.post("/calibration", json=cal_b).status_code == 200

    events: list[tuple[str, dict]] = []

    def wait_for_event(predicate, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if any(predicate(name, data) for name, data in events):
                return True
            time.sleep(0.01)
        return any(predicate(name, data) for name, data in events)

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    with client.websocket_connect("/ws/device/A") as ws_a, client.websocket_connect("/ws/device/B") as ws_b:
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        ws_a.send_json({
            "type": "hello",
            "cam": "A",
            "time_synced": True,
            "time_sync_id": "sy_deadbeef",
            "sync_anchor_timestamp_s": 0.0,
        })
        ws_b.send_json({
            "type": "hello",
            "cam": "B",
            "time_synced": True,
            "time_sync_id": "sy_deadbeef",
            "sync_anchor_timestamp_s": 0.0,
        })
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        arm = client.post(
            "/sessions/arm",
            json={"paths": ["live"]},
            headers={"Accept": "application/json"},
        )
        assert arm.status_code == 200, arm.text
        session_id = arm.json()["session"]["id"]
        assert arm.json()["session"]["paths"] == ["live"]

        assert ws_a.receive_json()["type"] == "arm"
        assert ws_b.receive_json()["type"] == "arm"

        ua, va = _project_pixels(K, R_a, t_a, P_true)
        ub, vb = _project_pixels(K, R_b, t_b, P_true)
        ws_a.send_json({
            "type": "frame",
            "cam": "A",
            "sid": session_id,
            "i": 0,
            "ts": 0.25,
            "candidates": [{"px": ua, "py": va, "area": 100, "area_score": 1.0}],
        })
        ws_b.send_json({
            "type": "frame",
            "cam": "B",
            "sid": session_id,
            "i": 0,
            "ts": 0.25,
            "candidates": [{"px": ub, "py": vb, "area": 100, "area_score": 1.0}],
        })
        assert wait_for_event(
            lambda name, data: name == "frame_count"
            and data["cam"] == "B"
            and data["count"] == 1
        )
        ws_a.send_json({
            "type": "cycle_end",
            "cam": "A",
            "sid": session_id,
            "reason": "disarmed",
        })
        ws_b.send_json({
            "type": "cycle_end",
            "cam": "B",
            "sid": session_id,
            "reason": "disarmed",
        })

    result = client.get(f"/results/{session_id}").json()
    assert len(result["points"]) == 1
    pt = result["points"][0]
    assert abs(pt["x_m"] - P_true[0]) < 1e-6
    assert abs(pt["y_m"] - P_true[1]) < 1e-6
    assert abs(pt["z_m"] - P_true[2]) < 1e-6
    assert result["paths_completed"] == ["live"]
    assert result["triangulated_by_path"]["live"]

    live_status = client.get("/status").json()["live_session"]
    assert live_status["session_id"] == session_id
    assert live_status["frame_counts"] == {"A": 1, "B": 1}
    assert live_status["point_count"] == 1

    event_names = [name for name, _ in events]
    assert "device_status" in event_names
    assert ("session_armed", {"sid": session_id, "paths": ["live"], "armed_at": arm.json()["session"]["started_at"]}) in events
    assert any(name == "frame_count" and data["cam"] == "A" and data["count"] == 1 for name, data in events)
    assert any(name == "frame_count" and data["cam"] == "B" and data["count"] == 1 for name, data in events)
    assert any(name == "ray" and data["sid"] == session_id and data["cam"] == "A" for name, data in events)
    assert any(name == "ray" and data["sid"] == session_id and data["cam"] == "B" for name, data in events)
    assert any(name == "point" and data["sid"] == session_id and abs(data["x"] - P_true[0]) < 1e-6 for name, data in events)
    assert any(name == "path_completed" and data["sid"] == session_id and data["cam"] == "A" for name, data in events)
    assert any(name == "path_completed" and data["sid"] == session_id and data["point_count"] == 1 for name, data in events)


def test_live_websocket_single_camera_emits_ray_without_sync(monkeypatch):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    u, v = _project_pixels(K, R_a, t_a, P_true)
    client = TestClient(app)
    assert _post_calibration(client, "A", K, H_a).status_code == 200

    events: list[tuple[str, dict]] = []

    def wait_for_event(predicate, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if any(predicate(name, data) for name, data in events):
                return True
            time.sleep(0.01)
        return any(predicate(name, data) for name, data in events)

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        ws_a.send_json({"type": "hello", "cam": "A"})
        assert ws_a.receive_json()["type"] == "settings"

        arm = client.post(
            "/sessions/arm",
            json={"paths": ["live"]},
            headers={"Accept": "application/json"},
        )
        assert arm.status_code == 200, arm.text
        session_id = arm.json()["session"]["id"]
        assert ws_a.receive_json()["type"] == "arm"

        ws_a.send_json({
            "type": "frame",
            "sid": session_id,
            "i": 12,
            "ts": 100.0,
            "candidates": [{"px": u, "py": v, "area": 100, "area_score": 1.0}],
        })

        assert wait_for_event(lambda name, data: name == "ray" and data["sid"] == session_id)
        ray_events = [data for name, data in events if name == "ray"]
        assert ray_events[0]["cam"] == "A"
        assert len(ray_events[0]["origin"]) == 3
        assert len(ray_events[0]["endpoint"]) == 3
        assert not any(name == "point" for name, _data in events)


def test_sync_trigger_broadcasts_websocket_command(monkeypatch):
    client = TestClient(app)

    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    with client.websocket_connect("/ws/device/A") as ws_a, client.websocket_connect("/ws/device/B") as ws_b:
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        ws_a.send_json({"type": "hello", "cam": "A"})
        ws_b.send_json({"type": "hello", "cam": "B"})
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        resp = client.post("/sync/trigger", json={"camera_ids": ["A", "B"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["dispatched_to"] == ["A", "B"]

        msg_a = ws_a.receive_json()
        msg_b = ws_b.receive_json()
        assert msg_a["type"] == "sync_command"
        assert msg_b["type"] == "sync_command"
        assert msg_a["command"] == "start"
        assert msg_b["command"] == "start"
        assert msg_a["sync_command_id"] == msg_b["sync_command_id"]


def test_calibration_post_broadcasts_websocket_update_to_siblings(monkeypatch):
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    client = TestClient(app)

    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            return None

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())

    with client.websocket_connect("/ws/device/A") as ws_a, client.websocket_connect("/ws/device/B") as ws_b:
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        ws_a.send_json({"type": "hello", "cam": "A"})
        ws_b.send_json({"type": "hello", "cam": "B"})
        assert ws_a.receive_json()["type"] == "settings"
        assert ws_b.receive_json()["type"] == "settings"

        cal_a = {
            "camera_id": "A",
            "intrinsics": {
                "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
            },
            "homography": H_a.flatten().tolist(),
            "image_width_px": 1920,
            "image_height_px": 1080,
        }
        assert client.post("/calibration", json=cal_a).status_code == 200

        msg_b = ws_b.receive_json()
        assert msg_b == {"type": "calibration_updated", "cam": "A"}

        cal_b = {
            "camera_id": "B",
            "intrinsics": {
                "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
            },
            "homography": H_b.flatten().tolist(),
            "image_width_px": 1920,
            "image_height_px": 1080,
        }
        assert client.post("/calibration", json=cal_b).status_code == 200

        msg_a = ws_a.receive_json()
        assert msg_a == {"type": "calibration_updated", "cam": "B"}


def test_heartbeat_emits_device_heartbeat_sse(monkeypatch):
    """SSE `device_heartbeat` fires on every WS heartbeat with battery /
    ws_latency / time_sync fields so the dashboard can update the
    Devices card without hitting /status."""
    client = TestClient(app)
    events: list[tuple[str, dict]] = []

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        ws_a.send_json({
            "type": "heartbeat",
            "battery_level": 0.82,
            "battery_state": "unplugged",
        })
        # Settle the message loop.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(n == "device_heartbeat" for n, _ in events):
                break
            time.sleep(0.02)

    hb = [d for n, d in events if n == "device_heartbeat"]
    assert hb, "expected at least one device_heartbeat event"
    assert hb[0]["cam"] == "A"
    assert hb[0]["battery_level"] == 0.82
    assert hb[0]["battery_state"] == "unplugged"
    assert "ws_latency_ms" in hb[0]
    assert "last_seen_at" in hb[0]


def test_calibration_state_exposes_plot_etag():
    """/calibration/state returns a stable plot_etag that differs across
    distinct plot payloads (so the dashboard can short-circuit the
    client-side JSON.stringify digest)."""
    client = TestClient(app)
    r = client.get("/calibration/state")
    assert r.status_code == 200
    body = r.json()
    assert "plot_etag" in body
    etag = body["plot_etag"]
    assert isinstance(etag, str) and len(etag) == 16
    # Re-fetch: same calibrations → same etag (deterministic).
    r2 = client.get("/calibration/state")
    assert r2.json()["plot_etag"] == etag
