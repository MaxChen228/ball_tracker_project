"""Runtime settings POST + preview + ws device visibility tests."""
from __future__ import annotations

import json as _json

import pytest
from fastapi.testclient import TestClient

import main
from main import app


def _fetch_ws_settings(test_client, camera_id: str):
    with test_client.websocket_connect(f"/ws/device/{camera_id}") as ws:
        ws.send_json({"type": "hello"})
        for _ in range(5):
            msg = ws.receive_json()
            if msg.get("type") == "settings":
                return msg
        return {}


def test_chirp_threshold_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    # Default surfaces on /status.
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["chirp_detect_threshold"] == pytest.approx(0.18)

    # JSON push.
    r = client.post("/settings/chirp_threshold", json={"threshold": 0.27})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": pytest.approx(0.27)}

    # Surfaces on /status and WS settings message.
    assert client.get("/status").json()["chirp_detect_threshold"] == pytest.approx(0.27)
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["chirp_detect_threshold"] == pytest.approx(0.27)

    # Form push (HTML caller) redirects 303.
    r = client.post(
        "/settings/chirp_threshold",
        data={"threshold": "0.33"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.chirp_detect_threshold() == pytest.approx(0.33)

    # Persisted to disk.
    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["chirp_detect_threshold"] == pytest.approx(0.33)


def test_chirp_threshold_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in (0.0, -0.1, 1.5, 10.0):
        r = client.post("/settings/chirp_threshold", json={"threshold": bad})
        assert r.status_code == 400, f"expected 400 for {bad}"
    # State unchanged.
    assert main.state.chirp_detect_threshold() == pytest.approx(0.18)
    # Direct setter also raises.
    with pytest.raises(ValueError):
        main.state.set_chirp_detect_threshold(2.0)


def test_detection_hsv_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["hsv_range"] == {
        "h_min": 25,
        "h_max": 55,
        "s_min": 90,
        "s_max": 255,
        "v_min": 90,
        "v_max": 255,
    }

    r = client.post(
        "/detection/hsv",
        json={"h_min": 100, "h_max": 130, "s_min": 140, "s_max": 255, "v_min": 40, "v_max": 255},
    )
    assert r.status_code == 200
    assert r.json()["hsv_range"] == {
        "h_min": 100,
        "h_max": 130,
        "s_min": 140,
        "s_max": 255,
        "v_min": 40,
        "v_max": 255,
    }

    assert client.get("/status").json()["hsv_range"] == r.json()["hsv_range"]
    ws_json = _fetch_ws_settings(client, "A")
    assert ws_json["hsv_range"] == r.json()["hsv_range"]

    r = client.post(
        "/detection/hsv",
        data={"h_min": "25", "h_max": "55", "s_min": "90", "s_max": "255", "v_min": "90", "v_max": "255"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Phase 2 of unified-config redesign: the triple now lives in a
    # single `detection_config.json`. Editing one section drops preset
    # binding (since the resulting config no longer matches any named
    # preset by definition) and stamps `last_applied_at`.
    persisted = _json.loads((tmp_path / "detection_config.json").read_text())
    assert persisted["hsv"] == {
        "h_min": 25,
        "h_max": 55,
        "s_min": 90,
        "s_max": 255,
        "v_min": 90,
        "v_max": 255,
    }
    assert persisted["preset"] is None
    assert isinstance(persisted["last_applied_at"], (int, float))


def test_detection_hsv_rejects_invalid_values(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    r = client.post(
        "/detection/hsv",
        json={"h_min": -1, "h_max": 55, "s_min": 90, "s_max": 255, "v_min": 90, "v_max": 255},
    )
    assert r.status_code == 400
    assert "out of range" in r.json()["detail"]


def test_heartbeat_interval_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["heartbeat_interval_s"] == pytest.approx(1.0)

    r = client.post("/settings/heartbeat_interval", json={"interval_s": 3.5})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": pytest.approx(3.5)}

    assert client.get("/status").json()["heartbeat_interval_s"] == pytest.approx(3.5)
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["heartbeat_interval_s"] == pytest.approx(3.5)

    r = client.post(
        "/settings/heartbeat_interval",
        data={"interval_s": "5"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.heartbeat_interval_s() == pytest.approx(5.0)

    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["heartbeat_interval_s"] == pytest.approx(5.0)


def test_heartbeat_interval_rejects_out_of_range(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in (0.0, 0.5, 61.0, -1.0):
        r = client.post("/settings/heartbeat_interval", json={"interval_s": bad})
        assert r.status_code == 400, f"expected 400 for {bad}"
    assert main.state.heartbeat_interval_s() == pytest.approx(1.0)
    with pytest.raises(ValueError):
        main.state.set_heartbeat_interval_s(0.1)


def test_tracking_exposure_cap_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["tracking_exposure_cap"] == "frame_duration"

    r = client.post("/settings/tracking_exposure_cap", json={"mode": "shutter_1000"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": "shutter_1000"}

    assert client.get("/status").json()["tracking_exposure_cap"] == "shutter_1000"
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["tracking_exposure_cap"] == "shutter_1000"

    r = client.post(
        "/settings/tracking_exposure_cap",
        data={"mode": "shutter_500"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.tracking_exposure_cap().value == "shutter_500"

    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["tracking_exposure_cap"] == "shutter_500"


def test_tracking_exposure_cap_rejects_invalid_value(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in ("", "1/1000", "fast", "240fps"):
        r = client.post("/settings/tracking_exposure_cap", json={"mode": bad})
        assert r.status_code == 400, f"expected 400 for {bad!r}"
    assert main.state.tracking_exposure_cap().value == "frame_duration"


def test_capture_height_post_persists_and_surfaces_on_status(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)

    r = client.get("/status")
    assert r.json()["capture_height_px"] == 1080

    r = client.post("/settings/capture_height", json={"height": 720})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": 720}

    assert client.get("/status").json()["capture_height_px"] == 720
    ws_json = _fetch_ws_settings(client, "A")
    assert ws_json["capture_height_px"] == 720

    r = client.post(
        "/settings/capture_height",
        data={"height": "1080"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main.state.capture_height_px() == 1080

    persisted = _json.loads((tmp_path / "runtime_settings.json").read_text())
    assert persisted["capture_height_px"] == 1080


def test_capture_height_rejects_540_and_other_invalid_values(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    client = TestClient(main.app)
    for bad in (540, 0, 999, 2160):
        r = client.post("/settings/capture_height", json={"height": bad})
        assert r.status_code == 400, f"expected 400 for {bad}"
    assert main.state.capture_height_px() == 1080
    with pytest.raises(ValueError):
        main.state.set_capture_height_px(540)


def test_runtime_settings_restored_from_disk_on_state_init(tmp_path):
    import main
    # Seed a file and confirm a fresh State picks it up.
    (tmp_path / "runtime_settings.json").write_text(
        _json.dumps({
            "chirp_detect_threshold": 0.42,
            "heartbeat_interval_s": 7.5,
            "tracking_exposure_cap": "shutter_1000",
        })
    )
    s = main.State(data_dir=tmp_path)
    assert s.chirp_detect_threshold() == pytest.approx(0.42)
    assert s.heartbeat_interval_s() == pytest.approx(7.5)
    assert s.tracking_exposure_cap().value == "shutter_1000"

    # Out-of-range values on disk are ignored, defaults retained.
    (tmp_path / "runtime_settings.json").write_text(
        _json.dumps({"chirp_detect_threshold": 99.0, "heartbeat_interval_s": 0.001})
    )
    s2 = main.State(data_dir=tmp_path)
    assert s2.chirp_detect_threshold() == pytest.approx(0.18)
    assert s2.heartbeat_interval_s() == pytest.approx(1.0)


# ---------------------------- Phase 4a · live preview -------------------------


def _minimal_jpeg() -> bytes:
    """A tiny valid-enough JPEG for the buffer round-trip tests. We don't
    decode it — the buffer treats the bytes opaquely — so a few bytes with
    a JPEG SOI/EOI is enough to represent a "frame"."""
    # SOI + a single APP0 + EOI — not a displayable image, but `push`
    # accepts it and `latest` round-trips exactly these bytes.
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def test_preview_push_rejected_when_not_requested():
    client = TestClient(app)
    r = client.post("/camera/A/preview_frame", content=_minimal_jpeg(),
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 409
    # Buffer must not have stored anything.
    assert main.state._preview.latest("A") is None


def test_preview_push_and_fetch_round_trip():
    client = TestClient(app)
    # Dashboard enables preview.
    r = client.post("/camera/A/preview_request", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": True}
    # Status surfaces the flag.
    assert client.get("/status").json()["preview_requested"] == {"A": True}
    # Phone pushes a frame (raw image/jpeg body).
    jpeg = _minimal_jpeg()
    r = client.post("/camera/A/preview_frame", content=jpeg,
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # Dashboard fetches the latest JPEG.
    r = client.get("/camera/A/preview")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == jpeg
    # Disable → flag drops AND cached frame is cleared.
    r = client.post("/camera/A/preview_request", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert main.state._preview.latest("A") is None
    r = client.get("/camera/A/preview")
    assert r.status_code == 404


def test_preview_request_flag_persists_without_ttl(tmp_path, monkeypatch):
    # Preview is a server-owned bool — no TTL / keep-alive. Enabled stays
    # enabled until an explicit False or a WS-disconnect clears it.
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state._preview.request("A", enabled=True)
    assert main.state._preview.is_requested("A")
    # Jump an hour into the future — flag must NOT auto-expire.
    clock[0] += 3600.0
    assert main.state._preview.is_requested("A")
    assert main.state._preview.requested_map() == {"A": True}
    # Explicit off — flag drops.
    main.state._preview.request("A", enabled=False)
    assert not main.state._preview.is_requested("A")
    assert main.state._preview.requested_map() == {}


def test_preview_frame_expires_after_age_limit(tmp_path, monkeypatch):
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state._preview.request("A", enabled=True)
    jpeg = _minimal_jpeg()
    assert main.state._preview.push("A", jpeg, ts=clock[0]) is True
    assert main.state._preview.latest("A", max_age_s=main._PREVIEW_FRAME_MAX_AGE_S) == (jpeg, clock[0])
    clock[0] += main._PREVIEW_FRAME_MAX_AGE_S + 0.1
    assert main.state._preview.latest("A", max_age_s=main._PREVIEW_FRAME_MAX_AGE_S) is None


def test_preview_oversize_rejected_413():
    client = TestClient(app)
    client.post("/camera/A/preview_request", json={"enabled": True})
    # 2 MB + 1 byte.
    huge = b"\xff\xd8" + b"\x00" * (2 * 1024 * 1024)
    r = client.post("/camera/A/preview_frame", content=huge,
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 413
    assert main.state._preview.latest("A") is None


def test_status_surfaces_preview_requested_map():
    client = TestClient(app)
    # Initially empty.
    assert client.get("/status").json().get("preview_requested") == {}
    client.post("/camera/A/preview_request", json={"enabled": True})
    client.post("/camera/B/preview_request", json={"enabled": True})
    got = client.get("/status").json()["preview_requested"]
    assert got == {"A": True, "B": True}
    # WS response carries the per-camera scalar for the connected phone.
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["preview_requested"] is True
    # Turn A off; B's flag is independent.
    client.post("/camera/A/preview_request", json={"enabled": False})
    hb_json = _fetch_ws_settings(client, "A")
    assert hb_json["preview_requested"] is False
    hb_json_b = _fetch_ws_settings(client, "B")
    assert hb_json_b["preview_requested"] is True


def test_ws_connected_device_visible_within_stale_window_then_drops(tmp_path, monkeypatch):
    # connect calls state.heartbeat() immediately → device appears for up to
    # _DEVICE_STALE_S seconds even without further messages. After that it
    # disappears from the list (no WS-connected fallback — that was removed to
    # fix the ghost-device bug when a phone switches role A→B).
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    client = TestClient(app)
    with client.websocket_connect("/ws/device/A") as ws:
        # Within stale window — device should appear
        clock[0] += 1.0
        got = client.get("/status").json()["devices"]
        assert [d["camera_id"] for d in got] == ["A"]
        assert got[0]["ws_connected"] is True

        # Past stale window with no heartbeat — device disappears
        clock[0] += 10.0
        got = client.get("/status").json()["devices"]
        assert [d["camera_id"] for d in got] == []


def test_setup_ssr_uses_same_time_sync_rule_as_status(tmp_path, monkeypatch):
    clock = [1000.0]
    def fake_time() -> float:
        return clock[0]
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path, time_fn=fake_time))
    main.state.heartbeat("A", time_synced=True, time_sync_id="sync_1", sync_anchor_timestamp_s=1.23)
    clock[0] += main._TIME_SYNC_MAX_AGE_S + 1.0
    client = TestClient(app)
    body = client.get("/setup").text.lower()
    assert "time sync &middot; synced" not in body
