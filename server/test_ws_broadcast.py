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
            "candidates": [{"px": ua, "py": va, "area": 100, "area_score": 1.0, "aspect": 1.0, "fill": 0.68}],
        })
        ws_b.send_json({
            "type": "frame",
            "cam": "B",
            "sid": session_id,
            "i": 0,
            "ts": 0.25,
            "candidates": [{"px": ub, "py": vb, "area": 100, "area_score": 1.0, "aspect": 1.0, "fill": 0.68}],
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
    assert any(
        name == "rays" and data["sid"] == session_id and data["cam"] == "A"
        and isinstance(data.get("rays"), list) and data["rays"]
        for name, data in events
    )
    assert any(
        name == "rays" and data["sid"] == session_id and data["cam"] == "B"
        and isinstance(data.get("rays"), list) and data["rays"]
        for name, data in events
    )
    assert any(
        name == "points" and data["sid"] == session_id
        and isinstance(data.get("points"), list)
        and any(abs(p["x"] - P_true[0]) < 1e-6 for p in data["points"])
        for name, data in events
    )
    assert any(name == "path_completed" and data["sid"] == session_id and data["cam"] == "A" for name, data in events)
    assert any(name == "path_completed" and data["sid"] == session_id and data["point_count"] == 1 for name, data in events)
    # `fit` SSE is broadcast on every cycle_end (per-cam, since rebuild
    # may produce different segments after the second cam reports).
    # One point cannot form a segment so segments is always empty here;
    # we assert at least one event arrives and all of them carry [].
    fit_events = [data for name, data in events if name == "fit" and data.get("sid") == session_id]
    assert fit_events, f"expected at least one fit event, got {events}"
    for fe in fit_events:
        assert fe["cause"] == "cycle_end"
        assert fe["segments"] == []
        # gap_threshold_m must ship on every fit event so dashboard can
        # refresh its client-side mask without a /results round-trip.
        # Cost is per-algorithm (post cost-absorption refactor) and
        # MUST NOT ship on the broadcast — clients resolve it from
        # algorithm metadata.
        assert "gap_threshold_m" in fe
        assert "cost_threshold" not in fe


def test_stamp_segments_on_result_populates_segments_for_ballistic_input():
    """`stamp_segments_on_result` runs `find_segments` on the chosen
    authoritative path's points and writes `result.segments`. With ≥
    `min_seg_len` (5) points falling on a clean ballistic curve the
    segmenter must produce exactly one segment with the correct speed."""
    import numpy as np
    from schemas import SessionResult, TriangulatedPoint
    from session_results import stamp_segments_on_result

    # Synthetic 50 fps trajectory: 30 m/s release, 5° upward, no spin.
    G = np.array([0.0, 0.0, -9.81])
    p0 = np.array([0.0, 0.0, 1.8])
    v0 = np.array([0.0, 30.0 * np.cos(np.deg2rad(5.0)), 30.0 * np.sin(np.deg2rad(5.0))])
    pts = []
    for i in range(20):
        t = i * 0.02
        pos = p0 + v0 * t + 0.5 * G * t * t
        pts.append(TriangulatedPoint(
            t_rel_s=t, x_m=float(pos[0]), y_m=float(pos[1]), z_m=float(pos[2]),
            residual_m=0.001,
            cost_a=None, cost_b=None,
        ))
    result = SessionResult(
        session_id="s_seg_test",
        camera_a_received=True,
        camera_b_received=True,
        triangulated=pts,
        triangulated_by_algorithm={"v11_hsv_cc": pts},
        algorithms_completed={"v11_hsv_cc"},
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    stamp_segments_on_result(result)
    assert len(result.segments) == 1, [s.model_dump() for s in result.segments]
    seg = result.segments[0]
    assert abs(seg.speed_kph - 30.0 * 3.6) < 0.5
    assert seg.rmse_m < 0.01
    assert seg.t_start == 0.0
    assert abs(seg.t_end - 19 * 0.02) < 1e-9


def test_live_websocket_single_camera_no_sync_anchor_drops_rays(monkeypatch):
    """Regression: when a phone has no `sync_anchor_timestamp_s` (never
    completed mutual sync), `live_rays_for_frame` MUST return [] rather
    than synthesise an anchor from `frame.ts - i/240`. The previous
    silent fallback produced rays whose `t_rel_s` looked plausible but
    was decoupled from any real clock — they would ship to the dashboard
    indistinguishable from genuine sync-aligned rays. See
    state.py::live_rays_for_frame and CLAUDE.md silent-fallback rule."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    u, v = _project_pixels(K, R_a, t_a, P_true)
    client = TestClient(app)
    assert _post_calibration(client, "A", K, H_a).status_code == 200

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
        # Hello WITHOUT sync_anchor_timestamp_s — phone never synced.
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
            "candidates": [{"px": u, "py": v, "area": 100, "area_score": 1.0, "aspect": 1.0, "fill": 0.68}],
        })

        # Drain a beat so the frame handler runs.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if any(name == "frame_count" for name, _ in events):
                break
            time.sleep(0.01)

        # frame_count still fires (it doesn't depend on the anchor) but
        # no rays should escape since the device has no sync anchor.
        assert any(name == "frame_count" for name, _ in events)
        assert not any(name == "rays" for name, _ in events)
        assert not any(name == "points" for name, _ in events)


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


def test_device_ws_normal_disconnect_emits_offline_broadcast(monkeypatch):
    """Route-level test: when a phone closes its WS normally (no
    reconnect race), the `finally` block in `routes.device_ws.ws_device`
    MUST broadcast `device_status online=False` and call
    `state.mark_device_offline`. R1 found the previous patch ordered the
    snapshot BEFORE `disconnect()` — our own still-occupying socket made
    `snap.connected=True` unconditionally, so the guard short-circuited
    every disconnect and the dashboard kept painting the cam online for
    up to 3 s after the phone dropped. This test pins the corrected
    ordering (disconnect → snapshot → check)."""
    events: list[tuple[str, dict]] = []
    mark_offline_calls: list[str] = []

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    real_mark_offline = main.state.mark_device_offline

    def _spy_mark_offline(cam: str):
        mark_offline_calls.append(cam)
        return real_mark_offline(cam)

    monkeypatch.setattr(main.state, "mark_device_offline", _spy_mark_offline)

    client = TestClient(app)
    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        # Online event fires on connect.
        assert any(
            n == "device_status" and d.get("cam") == "A" and d.get("online") is True
            for n, d in events
        )
    # Exiting the `with` closes ws_a → handler's finally runs.
    # Settle event loop so async broadcast lands.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if any(
            n == "device_status" and d.get("cam") == "A" and d.get("online") is False
            for n, d in events
        ):
            break
        time.sleep(0.02)

    assert any(
        n == "device_status" and d.get("cam") == "A" and d.get("online") is False
        for n, d in events
    ), f"normal disconnect must broadcast offline, got {events}"
    assert "A" in mark_offline_calls, (
        "normal disconnect must call mark_device_offline; reconnect-race "
        "guard regressed and is now swallowing real disconnects"
    )


def test_device_ws_reconnect_race_skips_offline_broadcast(monkeypatch):
    """Route-level test: when a NEWER ws task replaces the slot before
    the OLDER task's `finally` fires, the older task MUST NOT broadcast
    `online=False` (the newer connect() already broadcast online=True;
    a stale offline would paint a freshly-online cam offline for one
    tick).

    Mechanism: `DeviceSocketManager.connect` blindly overwrites
    `_sockets[cam]`; the newer connect makes `disconnect(cam, ws_old)`
    a no-op (identity-guarded). The route's `finally` snapshots AFTER
    that no-op disconnect → sees the newer socket still present →
    `snap.connected=True` → bail before painting offline."""
    events: list[tuple[str, dict]] = []
    mark_offline_calls: list[str] = []

    class _CaptureHub:
        async def broadcast(self, event: str, data: dict) -> None:
            events.append((event, data))

        async def subscribe(self):
            if False:
                yield ""

    monkeypatch.setattr(main, "sse_hub", _CaptureHub())
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())

    real_mark_offline = main.state.mark_device_offline

    def _spy_mark_offline(cam: str):
        mark_offline_calls.append(cam)
        return real_mark_offline(cam)

    monkeypatch.setattr(main.state, "mark_device_offline", _spy_mark_offline)

    client = TestClient(app)
    # Open ws_a, then ws_b for the SAME cam id. `connect()` overwrites
    # `_sockets["A"]` with ws_b's socket — that's the reconnect race in
    # miniature. Close ws_a INSIDE the ws_b block so ws_a's finally
    # fires while ws_b still holds the slot.
    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        events_at_a_connect = len(events)
        with client.websocket_connect("/ws/device/A") as ws_b:
            assert ws_b.receive_json()["type"] == "settings"
            # ws_b's connect broadcast online=True a second time —
            # confirm before we close ws_a.
            assert sum(
                1
                for n, d in events
                if n == "device_status"
                and d.get("cam") == "A"
                and d.get("online") is True
            ) >= 2

            # Close ws_a (the OLDER socket) while ws_b still occupies
            # the slot. We do this by exiting ws_a's context manager —
            # but TestClient holds the handle until our `with` block
            # ends. Instead, we send a close frame manually.
            ws_a.close()

            # Drain a beat for ws_a's finally to run.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                # If the bug were back, we'd see a spurious offline.
                if any(
                    n == "device_status"
                    and d.get("cam") == "A"
                    and d.get("online") is False
                    for n, d in events[events_at_a_connect:]
                ):
                    break
                time.sleep(0.02)

            # The reconnect-race guard must have suppressed the offline
            # broadcast — the newer ws_b still owns the cam.
            offline_events_during_race = [
                (n, d)
                for n, d in events[events_at_a_connect:]
                if n == "device_status"
                and d.get("cam") == "A"
                and d.get("online") is False
            ]
            assert not offline_events_during_race, (
                "reconnect race must NOT emit offline while newer ws is "
                f"still connected; got {offline_events_during_race}"
            )
            assert "A" not in mark_offline_calls, (
                "reconnect race must NOT call mark_device_offline while "
                "newer ws holds the slot — would race the freshly-online "
                "cam back to offline"
            )

            # Sanity: the manager still reports cam A as connected
            # (newer ws_b owns it).
            snap = main.device_ws.snapshot().get("A")
            assert snap is not None and snap.connected is True


def test_device_ws_disconnect_identity_guard():
    """`DeviceSocketManager.disconnect(cam, websocket)` must be a no-op
    when the current socket for `cam` is a different object than
    `websocket` (reconnect race: a newer task already replaced the
    socket; the old task's `finally` then calls disconnect — we must
    not pop the newer socket). Together with the finally-side
    snapshot guard in `routes.device_ws`, this prevents the old ws
    task from painting an actively-connected cam offline."""
    mgr = main.DeviceSocketManager()
    # Simulate two sockets: the "old" one and a "new" one that replaced
    # it in the slot. Plain object() is fine — disconnect only does
    # identity comparison.
    sock_old = object()
    sock_new = object()
    mgr._sockets["A"] = sock_old
    mgr.disconnect("A", sock_old)
    assert "A" not in mgr._sockets, "matching disconnect must pop"

    # Re-insert as the "new" socket, then call disconnect with the old
    # one — must be a no-op.
    mgr._sockets["A"] = sock_new
    mgr.disconnect("A", sock_old)
    assert mgr._sockets["A"] is sock_new, (
        "disconnect(cam, old_ws) must NOT pop a newer socket — identity "
        "guard reintroduced silently?"
    )

    # And snapshot reflects the cam as connected throughout — that's
    # the signal the finally block in routes.device_ws uses to decide
    # whether to skip the offline broadcasts.
    snap = mgr.snapshot().get("A")
    assert snap is not None and snap.connected is True


def test_calibration_state_returns_camera_list_for_threejs_dashboard():
    """/calibration/state ships the raw scene + per-camera image dims +
    last-touched timestamps. The Plotly-era `plot` + `plot_etag` fields
    were retired with the Three.js migration — the dashboard reads
    `scene.cameras` directly and short-circuits on the JSON.stringify
    of the camera tuple list, not a server-issued etag."""
    client = TestClient(app)
    r = client.get("/calibration/state")
    assert r.status_code == 200
    body = r.json()
    assert "calibrations" in body
    assert "scene" in body
    assert "cameras" in body["scene"]
    # Plotly-era leakage is gone.
    assert "plot" not in body
    assert "plot_etag" not in body
