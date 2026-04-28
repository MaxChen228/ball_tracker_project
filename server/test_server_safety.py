"""save_clip, validation, lock discipline."""
from __future__ import annotations

import json as _json
import threading
import time

import numpy as np
from fastapi.testclient import TestClient

import main
from conftest import sid
from main import app

from _test_helpers import (
    _base_payload,
    _encode_single_ball_mov,
    _make_scene,
    _post_pitch,
    _project_pixels,
)


# --------------------------- save_clip ---------------------------------------


def test_save_clip_writes_atomically_and_overwrites(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_id = sid(900)
    first = s.save_clip("A", session_id, b"alpha", "mov")
    assert first == tmp_path / "videos" / f"session_{session_id}_A.mov"
    assert first.read_bytes() == b"alpha"

    second = s.save_clip("A", session_id, b"beta beta", "mov")
    assert second == first
    assert second.read_bytes() == b"beta beta"

    assert not any(p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir())


def test_save_clip_rejects_path_traversal_extensions(tmp_path):
    s = main.State(data_dir=tmp_path)
    path_bad = s.save_clip("B", sid(7), b"x", "../etc/passwd")
    assert path_bad.parent == tmp_path / "videos"
    assert path_bad.suffix == ".mov"

    path_empty = s.save_clip("B", sid(8), b"y", "")
    assert path_empty.suffix == ".mov"


# --------------------------- Payload validation ------------------------------


def test_malformed_payload_returns_422(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    client = TestClient(app)
    with open(mov, "rb") as f:
        files = {"video": (mov.name, f.read(), "video/quicktime")}
    r = client.post(
        "/pitch",
        data={"payload": '{"bogus": true}'},
        files=files,
    )
    assert r.status_code == 422


def test_path_traversing_camera_id_is_rejected(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    body = _base_payload("A", sid(600), K, H_a)
    body["camera_id"] = "../etc"
    client = TestClient(app)
    r = _post_pitch(client, body, mov)
    assert r.status_code == 422


def test_malformed_session_id_is_rejected(tmp_path):
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    mov = _encode_single_ball_mov(
        tmp_path, K, R_a, t_a, np.array([0.1, 0.3, 1.0]), filename="ok.mov"
    )
    body = _base_payload("A", "../etc", K, H_a)
    client = TestClient(app)
    r = _post_pitch(client, body, mov)
    assert r.status_code == 422


# --------------------------- Concurrency + DoS guards ------------------------


def test_save_clip_is_lock_protected(tmp_path):
    s = main.State(data_dir=tmp_path)
    session_id = sid(910)

    payloads = [bytes([i]) * (640 * 1024) for i in (0x11, 0x22, 0x33, 0x44)]
    barrier = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def worker(data: bytes):
        try:
            barrier.wait(timeout=5.0)
            s.save_clip("A", session_id, data, "mov")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "worker hung"
    assert not errors, errors

    final_path = tmp_path / "videos" / f"session_{session_id}_A.mov"
    assert final_path.exists()
    final_bytes = final_path.read_bytes()
    assert final_bytes in payloads, (
        f"clip is a torn mix of writes (len={len(final_bytes)})"
    )
    assert not any(
        p.suffix == ".tmp" for p in (tmp_path / "videos").iterdir()
    )


def test_record_does_not_hold_lock_during_io(tmp_path, monkeypatch):
    """`State.record` must release `self._lock` before its disk writes."""
    K, *_, (R_a, t_a, _, H_a), _ = _make_scene()
    P_true = np.array([0.1, 0.3, 1.0])
    session_id = sid(920)

    s = main.State(data_dir=tmp_path)

    release = threading.Event()
    entered_io = threading.Event()
    original_atomic_write = s._atomic_write

    def blocking_atomic_write(path, payload):
        entered_io.set()
        assert release.wait(timeout=5.0), "release event never fired"
        return original_atomic_write(path, payload)

    monkeypatch.setattr(s, "_atomic_write", blocking_atomic_write)

    u, v = _project_pixels(K, R_a, t_a, P_true)
    pitch = main.PitchPayload(
        camera_id="A",
        session_id=session_id,
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_server_post=[
            main.FramePayload(
                frame_index=0, timestamp_s=0.0,
                px=u, py=v, ball_detected=True,
            )
        ],
        intrinsics=main.IntrinsicsPayload(
            fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        ),
        homography=H_a.flatten().tolist(),
    )

    recorder = threading.Thread(target=s.record, args=(pitch,))
    recorder.start()
    assert entered_io.wait(timeout=5.0), "record never reached _atomic_write"

    t0 = time.perf_counter()
    s.heartbeat("A")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 50, (
        f"heartbeat took {elapsed_ms:.1f} ms — record is still holding the lock"
    )

    release.set()
    recorder.join(timeout=5.0)
    assert not recorder.is_alive()
