"""Hot-path perf regression tests for live frame WS handler.

At 240 fps × 2 cam the live ingest path used to emit:
  - N `ray` SSE events per frame (one per candidate)
  - 1 `point` SSE event per new triangulated point
  - 1 `frame_count` event per frame (~480 events/sec across both cams)

These tests pin the post-perf-fix wire shape:
  - Single `rays` event per frame, payload = list of candidate dicts
  - Single `points` event per frame batch
  - `frame_count` throttled to ~1 Hz per (sid, cam) regardless of fps.
"""
from __future__ import annotations

import time

import numpy as np
from fastapi.testclient import TestClient

import main
from main import app
import routes.device_ws as device_ws_module

from _test_helpers import _make_scene, _project_pixels


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


class _CaptureHub:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    async def subscribe(self):
        if False:
            yield ""


def test_rays_event_is_single_broadcast_per_frame_with_array(monkeypatch):
    """Per-frame fan-out is coalesced — N candidates produce 1 'rays' event,
    not N. Pre-fix, server emitted N broadcasts per frame (~3k JSON
    encodes/sec at 240 fps × 2 cam)."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    client = TestClient(app)
    assert _post_calibration(client, "A", K, H_a).status_code == 200
    assert _post_calibration(client, "B", K, H_b).status_code == 200

    hub = _CaptureHub()
    monkeypatch.setattr(main, "sse_hub", hub)
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    # Reset module-level throttle state so this test sees a fresh emit.
    device_ws_module._last_frame_count_emit_ts.clear()

    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        ws_a.send_json({
            "type": "hello", "cam": "A",
            "time_synced": True,
            "time_sync_id": "sy_perf",
            "sync_anchor_timestamp_s": 0.0,
        })
        assert ws_a.receive_json()["type"] == "settings"

        arm = client.post("/sessions/arm", json={"paths": ["live"]})
        assert arm.status_code == 200
        sid = arm.json()["session"]["id"]
        assert ws_a.receive_json()["type"] == "arm"

        ua, va = _project_pixels(K, R_a, t_a, P_true)
        # 3 candidates in one frame — should produce ONE 'rays' event with
        # an array, not three separate 'ray' events.
        ws_a.send_json({
            "type": "frame",
            "cam": "A",
            "sid": sid,
            "i": 0,
            "ts": 0.25,
            "candidates": [
                {"px": ua, "py": va, "area": 100, "area_score": 1.0, "aspect": 1.0, "fill": 0.68},
                {"px": ua + 5, "py": va + 5, "area": 90, "area_score": 0.9, "aspect": 0.95, "fill": 0.65},
                {"px": ua - 4, "py": va - 4, "area": 80, "area_score": 0.8, "aspect": 0.92, "fill": 0.60},
            ],
        })

        # Wait for handler to process.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(n == "rays" and d.get("sid") == sid for n, d in hub.events):
                break
            time.sleep(0.01)

    rays_events = [d for n, d in hub.events if n == "rays" and d.get("sid") == sid]
    assert len(rays_events) == 1, (
        f"expected exactly one coalesced 'rays' event per frame, got {len(rays_events)}: {hub.events}"
    )
    payload = rays_events[0]
    assert isinstance(payload["rays"], list)
    assert len(payload["rays"]) >= 1
    # Legacy single-ray event name must not be emitted.
    assert not any(n == "ray" for n, _ in hub.events), \
        "legacy 'ray' event must be replaced by coalesced 'rays'"


def test_frame_count_emit_is_throttled_to_1hz(monkeypatch):
    """Many frames in <1s produce at most a couple of frame_count emits.
    Pre-fix: 1 emit per frame (240 Hz); post-fix: ~1 Hz per (sid, cam)."""
    K, *_, (R_a, t_a, _, H_a), (R_b, t_b, _, H_b) = _make_scene()
    P_true = np.array([0.08, 0.34, 0.92])
    client = TestClient(app)
    assert _post_calibration(client, "A", K, H_a).status_code == 200

    hub = _CaptureHub()
    monkeypatch.setattr(main, "sse_hub", hub)
    monkeypatch.setattr(main, "device_ws", main.DeviceSocketManager())
    device_ws_module._last_frame_count_emit_ts.clear()

    with client.websocket_connect("/ws/device/A") as ws_a:
        assert ws_a.receive_json()["type"] == "settings"
        ws_a.send_json({
            "type": "hello", "cam": "A",
            "time_synced": True,
            "time_sync_id": "sy_throttle",
            "sync_anchor_timestamp_s": 0.0,
        })
        assert ws_a.receive_json()["type"] == "settings"

        arm = client.post("/sessions/arm", json={"paths": ["live"]})
        assert arm.status_code == 200
        sid = arm.json()["session"]["id"]
        assert ws_a.receive_json()["type"] == "arm"

        ua, va = _project_pixels(K, R_a, t_a, P_true)
        # Send 240 frames as fast as possible (simulating 1 sec @ 240fps).
        N = 240
        for i in range(N):
            ws_a.send_json({
                "type": "frame",
                "cam": "A",
                "sid": sid,
                "i": i,
                "ts": 0.25 + i * (1.0 / 240.0),
                "candidates": [
                    {"px": ua, "py": va, "area": 100, "area_score": 1.0, "aspect": 1.0, "fill": 0.68},
                ],
            })
        # Drain handler.
        ws_a.send_json({"type": "cycle_end", "cam": "A", "sid": sid, "reason": "disarmed"})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(n == "path_completed" and d.get("sid") == sid for n, d in hub.events):
                break
            time.sleep(0.01)

    fc_events = [d for n, d in hub.events if n == "frame_count" and d.get("sid") == sid]
    # Allow generous upper bound: even on a slow box this loop typically
    # finishes in <1s wall-clock so we should see exactly 1 emit. Cap at 4
    # to tolerate scheduler jitter on CI.
    assert 1 <= len(fc_events) <= 4, (
        f"frame_count throttle broken: got {len(fc_events)} emits for {N} frames "
        f"(expected 1-4 at 1Hz throttle). events={fc_events}"
    )
