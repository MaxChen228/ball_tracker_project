"""Phase 1 quick-sync end-to-end: WAV upload → band-A detection → solve.

Builds synthetic recordings with band-A chirps at the default emit
schedule (0.3/0.5/0.7 s) and drives the full route path:
  /sync/quick_start → /sync/quick_audio_upload (×N) → QuickSyncResult.
"""
from __future__ import annotations

import io
import json
import wave

import numpy as np
from fastapi.testclient import TestClient

import main
from chirp import SYNC_BAND_A_F0, SYNC_BAND_A_F1, SYNC_CHIRP_DURATION_S, _hann_chirp
from main import app

SAMPLE_RATE = 48000
EMIT_AT_S = [0.3, 0.5, 0.7]
DURATION_S = 4.0


def _make_wav(chirp_offset_s: float) -> bytes:
    """Recording with band-A chirps at EMIT_AT_S + a global clock skew.
    `chirp_offset_s` shifts all chirps to mimic that phone's clock being
    ahead/behind — the detector reports anchor on the phone's own clock."""
    n = int(SAMPLE_RATE * DURATION_S)
    rng = np.random.default_rng(7)
    audio = rng.normal(0.0, 1e-4, size=n).astype(np.float32)
    chirp = (_hann_chirp(SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1,
                         SYNC_CHIRP_DURATION_S) * 0.5).astype(np.float32)
    for t in EMIT_AT_S:
        start = int((t + chirp_offset_s) * SAMPLE_RATE)
        end = min(n, start + len(chirp))
        if 0 <= start < n and end > start:
            audio[start:end] += chirp[: end - start]
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _upload(client: TestClient, sync_id: str, cam: str, audio_start_pts_s: float,
            chirp_offset_s: float):
    meta = {"sync_id": sync_id, "camera_id": cam,
            "audio_start_pts_s": audio_start_pts_s}
    return client.post(
        "/sync/quick_audio_upload",
        data={"payload": json.dumps(meta)},
        files={"audio": ("rec.wav", _make_wav(chirp_offset_s), "audio/wav")},
    )


def test_quick_start_rejects_offline_emitter():
    client = TestClient(app)
    r = client.post("/sync/quick_start", json={"emitter_cam_id": "A"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "emitter_offline"


def test_quick_start_requires_emitter_cam_id():
    client = TestClient(app)
    r = client.post("/sync/quick_start", json={})
    assert r.status_code == 422


def test_full_n2_round_trip():
    client = TestClient(app)
    main.state.heartbeat("A")
    main.state.heartbeat("B")
    r = client.post("/sync/quick_start", json={"emitter_cam_id": "A"})
    assert r.status_code == 200, r.text
    sync_id = r.json()["quick_sync"]["id"]

    # A (emitter) records on its own clock starting at PTS 100.0, chirps
    # land at the emit schedule exactly. B's clock is 5 ms ahead: same
    # physical chirp, but B stamps it 5 ms later in its own PTS.
    ra = _upload(client, sync_id, "A", audio_start_pts_s=100.0, chirp_offset_s=0.0)
    assert ra.status_code == 200, ra.text
    assert ra.json()["solved"] is False

    rb = _upload(client, sync_id, "B", audio_start_pts_s=100.0, chirp_offset_s=0.005)
    assert rb.status_code == 200, rb.text
    body = rb.json()
    assert body["solved"] is True
    result = body["result"]
    assert result["aborted"] is False
    assert result["emitter_cam_id"] == "A"
    assert result["deltas_s"]["A"] == 0.0
    # B chirp shifted +5ms → B anchor is 5ms larger → delta ≈ +0.005.
    assert abs(result["deltas_s"]["B"] - 0.005) < 1e-3
    assert result["missing_cam_ids"] == []


def test_upload_without_active_run_409():
    client = TestClient(app)
    r = _upload(client, "sy_deadbeef", "A", 0.0, 0.0)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_sync"
