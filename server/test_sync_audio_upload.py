"""End-to-end tests for POST /sync/audio_upload.

Validates that uploading two WAVs (one per role, each containing both
self- and other-band chirps at known offsets) ends up solving via the
existing mutual-sync state machine — same flow the legacy /sync/report
endpoint drives, only the detection lives server-side now.
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

SAMPLE_RATE = 48000


def _wav_bytes(mono: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth_both_chirps(
    a_start_sample: int,
    b_start_sample: int,
    duration_s: float = 2.0,
    amplitude: float = 0.5,
    seed: int = 7,
) -> np.ndarray:
    """One recording containing both A- and B-band chirps at known
    sample offsets, plus faint gaussian noise to make PSR numbers
    realistic."""
    n = int(SAMPLE_RATE * duration_s)
    rng = np.random.default_rng(seed)
    audio = rng.normal(0.0, 1e-4, size=n).astype(np.float32)
    a_chirp = (_hann_chirp(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1, SYNC_CHIRP_DURATION_S
    ) * amplitude).astype(np.float32)
    b_chirp = (_hann_chirp(
        SAMPLE_RATE, SYNC_BAND_B_F0, SYNC_BAND_B_F1, SYNC_CHIRP_DURATION_S
    ) * amplitude).astype(np.float32)
    audio[a_start_sample : a_start_sample + len(a_chirp)] += a_chirp
    audio[b_start_sample : b_start_sample + len(b_chirp)] += b_chirp
    return audio


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    # Tests share the module-level `main.state`. Clear mutual-sync
    # bookkeeping before each run so one test's pending sync doesn't
    # bleed into the next.
    main.state._current_sync = None
    main.state._last_sync_result = None
    main.state._sync_cooldown_until = 0.0
    yield


def _heartbeat_both() -> None:
    main.state.heartbeat("A")
    main.state.heartbeat("B")


def _upload(
    client: TestClient, *, sync_id: str, camera_id: str, role: str,
    wav: bytes, audio_start_pts_s: float, emission_pts_s: float | None = None,
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

    wav_a = _wav_bytes(_synth_both_chirps(
        a_start_sample=10000, b_start_sample=30000
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
    assert body["detection"]["peak_self"] > 0.8
    assert body["detection"]["peak_other"] > 0.8


def test_audio_upload_both_roles_solves_via_state_machine() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]

    # Both cams hear both chirps, but at different offsets within their
    # own recording (simulating A being slightly closer to B's emission).
    wav_a = _wav_bytes(_synth_both_chirps(
        a_start_sample=10000, b_start_sample=30000
    ))
    wav_b = _wav_bytes(_synth_both_chirps(
        a_start_sample=31000, b_start_sample=11000
    ))

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
    # delta_s should be roughly (B start PTS - A start PTS) = 4900 s
    # since the chirps are laid out symmetrically in sample-space.
    assert body["result"]["aborted"] is False


def test_audio_upload_rejects_missing_fields() -> None:
    client = TestClient(main.app)
    _heartbeat_both()
    start = client.post("/sync/start").json()
    sync_id = start["sync"]["id"]
    wav = _wav_bytes(_synth_both_chirps(
        a_start_sample=10000, b_start_sample=30000
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
    wav = _wav_bytes(_synth_both_chirps(
        a_start_sample=10000, b_start_sample=30000
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
    wav = _wav_bytes(_synth_both_chirps(
        a_start_sample=10000, b_start_sample=30000
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
