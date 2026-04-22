"""End-to-end tests for POST /sync/audio_upload.

Tests now generate 3-burst synthetic audio matching the server's default
SyncParams (emit_a_at_s=[0.3,0.5,0.7], emit_b_at_s=[1.8,2.0,2.2]) so
windowed multi-peak detection finds all N chirps at the right positions.
"""
from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

import main
from chirp import (
    SYNC_BAND_A_F0, SYNC_BAND_A_F1,
    SYNC_BAND_B_F0, SYNC_BAND_B_F1,
    SYNC_CHIRP_DURATION_S,
    _hann_chirp,
)
from state import SyncParams

SAMPLE_RATE = 48000
# Match server default SyncParams so windowed detection finds chirps.
DEFAULT_PARAMS = SyncParams()


def _wav_bytes(mono: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth_burst(
    emit_a_at_s: list[float],
    emit_b_at_s: list[float],
    duration_s: float = 4.5,
    amplitude: float = 0.5,
    seed: int = 7,
) -> np.ndarray:
    """Synthetic recording with N A-band and N B-band chirps at the
    specified time offsets (seconds from recording start).
    Adds faint gaussian noise to make PSR values realistic."""
    n = int(SAMPLE_RATE * duration_s)
    rng = np.random.default_rng(seed)
    audio = rng.normal(0.0, 1e-4, size=n).astype(np.float32)
    a_chirp = (_hann_chirp(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1, SYNC_CHIRP_DURATION_S
    ) * amplitude).astype(np.float32)
    b_chirp = (_hann_chirp(
        SAMPLE_RATE, SYNC_BAND_B_F0, SYNC_BAND_B_F1, SYNC_CHIRP_DURATION_S
    ) * amplitude).astype(np.float32)
    for t in emit_a_at_s:
        s = int(t * SAMPLE_RATE)
        if s + len(a_chirp) <= n:
            audio[s:s + len(a_chirp)] += a_chirp
    for t in emit_b_at_s:
        s = int(t * SAMPLE_RATE)
        if s + len(b_chirp) <= n:
            audio[s:s + len(b_chirp)] += b_chirp
    return audio


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    main.state._current_sync = None
    main.state._last_sync_result = None
    main.state._sync_cooldown_until = 0.0
    # Ensure default SyncParams for predictable windowed detection windows.
    main.state.set_sync_params(SyncParams())
    yield


def _heartbeat_both() -> None:
    main.state.heartbeat("A")
    main.state.heartbeat("B")


def _upload(
    client: TestClient, *, sync_id: str, camera_id: str, role: str,
    wav: bytes, audio_start_pts_s: float, emission_pts_s: list[float] | None = None,
) -> dict:
    payload = {
        "sync_id": sync_id,
        "camera_id": camera_id,
        "role": role,
        "audio_start_pts_s": audio_start_pts_s,
        "sample_rate": SAMPLE_RATE,
    }
    if emission_pts_s is not None:
        payload["emission_pts_s"] = emission_pts_s
    r = client.post(
        "/sync/audio_upload",
        data={"payload": json.dumps(payload)},
        files={"audio": ("clip.wav", wav, "audio/wav")},
    )
    return r


def test_audio_upload_a_only_marks_run_pending() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]

    # Role A: self=A-band at emit_a_at_s, other=B-band at emit_b_at_s
    wav_a = _wav_bytes(_synth_burst(
        emit_a_at_s=DEFAULT_PARAMS.emit_a_at_s,
        emit_b_at_s=DEFAULT_PARAMS.emit_b_at_s,
    ))
    r = _upload(
        client, sync_id=sync_id, camera_id="A", role="A",
        wav=wav_a, audio_start_pts_s=100.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["solved"] is False
    assert body["run"]["reports_received"] == ["A"]
    assert body["detection"]["peak_self"] > 0.6
    assert body["detection"]["peak_other"] > 0.6
    assert body["detection"]["n_burst"] == 3
    assert body["detection"]["windowed"] is True


def test_audio_upload_both_roles_solves_via_state_machine() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]

    p = DEFAULT_PARAMS
    # Cam A: hears own A chirps + peer B chirps (B's chirps arrive at slightly
    # different offsets due to propagation, but within search_window_s).
    wav_a = _wav_bytes(_synth_burst(emit_a_at_s=p.emit_a_at_s, emit_b_at_s=p.emit_b_at_s))
    # Cam B: hears A's chirps at emit_a_at_s + δ, own B chirps at emit_b_at_s
    wav_b = _wav_bytes(_synth_burst(emit_a_at_s=p.emit_a_at_s, emit_b_at_s=p.emit_b_at_s))

    r1 = _upload(
        client, sync_id=sync_id, camera_id="A", role="A",
        wav=wav_a, audio_start_pts_s=100.0,
    )
    assert r1.json()["solved"] is False

    r2 = _upload(
        client, sync_id=sync_id, camera_id="B", role="B",
        wav=wav_b, audio_start_pts_s=5000.0,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["solved"] is True
    assert body["result"]["id"] == sync_id
    assert body["result"]["aborted"] is False


def test_audio_upload_rejects_missing_fields() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]
    wav = _wav_bytes(_synth_burst(
        emit_a_at_s=DEFAULT_PARAMS.emit_a_at_s,
        emit_b_at_s=DEFAULT_PARAMS.emit_b_at_s,
    ))

    r = client.post(
        "/sync/audio_upload",
        data={"payload": json.dumps({"sync_id": sync_id, "camera_id": "A"})},
        files={"audio": ("clip.wav", wav, "audio/wav")},
    )
    assert r.status_code == 422
    assert "role" in r.text or "audio_start_pts_s" in r.text


def test_audio_upload_rejects_no_active_sync() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    wav = _wav_bytes(_synth_burst(
        emit_a_at_s=DEFAULT_PARAMS.emit_a_at_s,
        emit_b_at_s=DEFAULT_PARAMS.emit_b_at_s,
    ))
    r = _upload(
        client, sync_id="sy_deadbeef", camera_id="A", role="A",
        wav=wav, audio_start_pts_s=0.0,
    )
    assert r.status_code == 409
    assert "no_sync" in r.text


def test_audio_upload_persists_wav_to_disk(tmp_path: Path) -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]
    wav = _wav_bytes(_synth_burst(
        emit_a_at_s=DEFAULT_PARAMS.emit_a_at_s,
        emit_b_at_s=DEFAULT_PARAMS.emit_b_at_s,
    ))
    r = _upload(
        client, sync_id=sync_id, camera_id="A", role="A",
        wav=wav, audio_start_pts_s=0.0,
    )
    assert r.status_code == 200
    wav_rel = r.json()["detection"]["wav_path"]
    persisted = main.state.data_dir / wav_rel
    assert persisted.exists()
    assert persisted.read_bytes() == wav


def test_sync_params_endpoint() -> None:
    client = TestClient(main.app)
    # GET current params
    r = client.get("/sync/params")
    assert r.status_code == 200
    body = r.json()
    assert body["emit_a_at_s"] == [0.3, 0.5, 0.7]
    assert body["emit_b_at_s"] == [1.8, 2.0, 2.2]
    assert body["record_duration_s"] == 4.0

    # POST update
    new_params = {"emit_a_at_s": [0.4, 0.6], "emit_b_at_s": [2.0, 2.2], "record_duration_s": 5.0, "search_window_s": 0.25}
    r2 = client.post("/settings/sync_params", json=new_params)
    assert r2.status_code == 200
    assert r2.json()["emit_a_at_s"] == [0.4, 0.6]
    assert r2.json()["record_duration_s"] == 5.0

    # Verify persisted
    r3 = client.get("/sync/params")
    assert r3.json()["emit_a_at_s"] == [0.4, 0.6]
