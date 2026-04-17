"""Tests for audio-based clock-offset recovery and the audio-sync
triangulation path."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from audio_sync import compute_audio_offset
from main import FramePayload, IntrinsicsPayload, PitchPayload, triangulate_cycle, app
from triangulate import build_K


# ───── WAV / signal helpers ──────────────────────────────────────────────────

def _make_clap(sr: int, duration_s: float = 0.02, freq_hz: float = 3000.0) -> np.ndarray:
    """Synthetic clap: fast-decaying 3 kHz burst, ~20 ms long."""
    t = np.linspace(0.0, duration_s, int(duration_s * sr), endpoint=False)
    envelope = np.exp(-t * 200.0)
    return envelope * np.sin(2.0 * np.pi * freq_hz * t)


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.astype("<i2").tobytes())


def _make_track_with_clap(
    sr: int,
    length_samples: int,
    clap_start_sample: int,
    noise_amp_int16: int = 80,
    seed: int = 0,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    clap = _make_clap(sr) * 10000.0
    noise = rng.standard_normal(length_samples) * noise_amp_int16
    samples = noise.copy()
    end = clap_start_sample + len(clap)
    samples[clap_start_sample:end] += clap[: end - clap_start_sample]
    return np.clip(samples, -32767, 32767).astype(np.int16)


# ───── compute_audio_offset ──────────────────────────────────────────────────

def test_compute_offset_recovers_integer_sample_lag(tmp_path):
    sr = 44100
    length = 2 * sr  # 2 s
    a = _make_track_with_clap(sr, length, clap_start_sample=30_000, seed=1)
    b = _make_track_with_clap(sr, length, clap_start_sample=25_000, seed=2)

    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    _write_wav(wav_a, a, sr)
    _write_wav(wav_b, b, sr)

    # Same start time — delta should be entirely due to the in-WAV sample gap.
    delta, peak = compute_audio_offset(wav_a, 10.0, wav_b, 10.0)
    expected = (30_000 - 25_000) / sr  # seconds
    assert abs(delta - expected) < 5e-4  # under 0.5 ms
    assert peak > 0.01


def test_compute_offset_combines_start_time_and_sample_lag(tmp_path):
    sr = 44100
    length = 2 * sr
    a = _make_track_with_clap(sr, length, clap_start_sample=20_000, seed=3)
    b = _make_track_with_clap(sr, length, clap_start_sample=20_000, seed=4)

    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    _write_wav(wav_a, a, sr)
    _write_wav(wav_b, b, sr)

    # Claps at same sample index, but WAVs started 0.1 s apart.
    # δ = (T_A_start − T_B_start) + 0 = -0.1 s
    delta, _ = compute_audio_offset(wav_a, 100.000, wav_b, 100.100)
    assert abs(delta - (-0.100)) < 5e-4


def test_compute_offset_raises_on_sample_rate_mismatch(tmp_path):
    a = np.zeros(1000, dtype=np.int16)
    b = np.zeros(1000, dtype=np.int16)
    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    _write_wav(wav_a, a, 44100)
    _write_wav(wav_b, b, 48000)
    with pytest.raises(ValueError, match="sample rate"):
        compute_audio_offset(wav_a, 0.0, wav_b, 0.0)


# ───── triangulate_cycle audio path ──────────────────────────────────────────

def _look_at(pos, target, up=np.array([0.0, 0.0, 1.0])):
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
    y_cam = -up - np.dot(-up, z_cam) * z_cam
    y_cam /= np.linalg.norm(y_cam)
    x_cam = np.cross(y_cam, z_cam)
    R_cw = np.column_stack([x_cam, y_cam, z_cam])
    return R_cw.T, -R_cw.T @ pos


def _scene():
    K = build_K(1600.0, 1600.0, 960.0, 540.0)
    C_a = np.array([1.8, -2.5, 1.2])
    C_b = np.array([-1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R_a, t_a = _look_at(C_a, target)
    R_b, t_b = _look_at(C_b, target)
    H_a = K @ np.column_stack([R_a[:, 0], R_a[:, 1], t_a])
    H_b = K @ np.column_stack([R_b[:, 0], R_b[:, 1], t_b])
    H_a /= H_a[2, 2]
    H_b /= H_b[2, 2]
    return K, R_a, t_a, H_a, R_b, t_b, H_b


def _project(K, R, t, P):
    Pc = R @ P + t
    return float(np.arctan2(Pc[0], Pc[2])), float(np.arctan2(Pc[1], Pc[2]))


def test_triangulate_cycle_uses_audio_offset(tmp_path):
    """Two cameras whose phone clocks differ by audio_offset_s should still
    triangulate correctly once that offset is applied."""
    K, R_a, t_a, H_a, R_b, t_b, H_b = _scene()

    # True server-clock timeline
    ts_true = np.linspace(0.0, 0.4, 20)
    # Phone-clock skew larger than path duration so B's frames never pair
    # with A's within 8 ms tolerance unless offset is applied.
    delta_s = 1.5  # clockA − clockB
    ts_phone_a = ts_true.copy()
    ts_phone_b = ts_true - delta_s

    path = np.stack([
        0.1 * np.sin(ts_true * 10),
        18.0 - 45.0 * ts_true,
        2.0 - 4.9 * ts_true**2,
    ], axis=1)

    def build_payload(cam_id, R, t, H, ts_phone):
        frames = []
        for i, (Pi, ti) in enumerate(zip(path, ts_phone)):
            tx, tz = _project(K, R, t, Pi)
            frames.append(FramePayload(
                frame_index=i, timestamp_s=float(ti),
                theta_x_rad=tx, theta_z_rad=tz, ball_detected=True,
            ))
        return PitchPayload(
            camera_id=cam_id, flash_frame_index=0, flash_timestamp_s=0.0,
            cycle_number=1, frames=frames,
            intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
            homography=H.flatten().tolist(),
            audio_start_ts_s=ts_phone[0],
        )

    a = build_payload("A", R_a, t_a, H_a, ts_phone_a)
    b = build_payload("B", R_b, t_b, H_b, ts_phone_b)

    # Without offset: flash fallback; A/B timestamps differ by 0.080 s (> 8 ms
    # pair window), so no points should pair.
    points_no_offset, sync_no = triangulate_cycle(a, b)
    assert sync_no == "flash"
    assert len(points_no_offset) == 0

    # With correct offset: audio path recovers all points.
    points, sync = triangulate_cycle(a, b, audio_offset_s=delta_s)
    assert sync == "audio"
    assert len(points) == len(path)
    recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in points])
    np.testing.assert_allclose(recovered, path, atol=1e-5)


def test_triangulate_cycle_mac_wins_over_audio():
    """If both mac_clock_offset_s and audio_offset_s are present, mac wins."""
    K, R_a, t_a, H_a, R_b, t_b, H_b = _scene()
    ts = np.linspace(0.0, 0.1, 5)
    path = np.stack([np.zeros(5), 10.0 - 20.0 * ts, 1.5 - ts], axis=1)

    def payload(cam_id, R, t, H, mac_off):
        frames = []
        for i, (Pi, ti) in enumerate(zip(path, ts)):
            tx, tz = _project(K, R, t, Pi)
            frames.append(FramePayload(
                frame_index=i, timestamp_s=float(ti),
                theta_x_rad=tx, theta_z_rad=tz, ball_detected=True,
            ))
        return PitchPayload(
            camera_id=cam_id, flash_frame_index=0, flash_timestamp_s=0.0,
            cycle_number=1, frames=frames,
            intrinsics=IntrinsicsPayload(fx=K[0, 0], fz=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
            homography=H.flatten().tolist(),
            mac_clock_offset_s=mac_off,
        )

    a = payload("A", R_a, t_a, H_a, 0.0)
    b = payload("B", R_b, t_b, H_b, 0.0)
    _, sync = triangulate_cycle(a, b, audio_offset_s=0.5)
    assert sync == "mac"


# ───── End-to-end: POST /pitch with audio sidecar ───────────────────────────

@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    yield tmp_path


def test_pitch_endpoint_accepts_multipart_without_audio(_isolated_state):
    """Back-compat: /pitch (multipart) still works with just the payload
    field — no audio file attached."""
    import json as _json
    K, R_a, t_a, H_a, R_b, t_b, H_b = _scene()
    P = np.array([0.1, 0.3, 1.0])
    tx, tz = _project(K, R_a, t_a, P)

    body = {
        "camera_id": "A", "flash_frame_index": 0, "flash_timestamp_s": 0.0,
        "cycle_number": 1,
        "frames": [{"frame_index": 0, "timestamp_s": 0.0,
                    "theta_x_rad": tx, "theta_z_rad": tz, "ball_detected": True}],
        "intrinsics": {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]},
        "homography": H_a.flatten().tolist(),
    }
    client = TestClient(app)
    r = client.post("/pitch", data={"payload": _json.dumps(body)})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_pitch_endpoint_stores_audio_sidecar(_isolated_state):
    """POST /pitch with an audio file part stores the WAV beside the JSON."""
    import json as _json
    K, R_a, t_a, H_a, _, _, _ = _scene()
    tx, tz = _project(K, R_a, t_a, np.array([0.0, 0.5, 1.0]))

    body = {
        "camera_id": "A", "flash_frame_index": 0, "flash_timestamp_s": 0.0,
        "cycle_number": 99,
        "frames": [{"frame_index": 0, "timestamp_s": 0.0,
                    "theta_x_rad": tx, "theta_z_rad": tz, "ball_detected": True}],
        "intrinsics": {"fx": K[0, 0], "fz": K[1, 1], "cx": K[0, 2], "cy": K[1, 2]},
        "homography": H_a.flatten().tolist(),
        "audio_start_ts_s": 42.0,
    }
    # Minimal valid WAV — 1 second of silence
    sr = 44100
    silence = np.zeros(sr, dtype=np.int16)
    wav_path = _isolated_state / "clap.wav"
    _write_wav(wav_path, silence, sr)
    wav_bytes = wav_path.read_bytes()

    client = TestClient(app)
    r = client.post(
        "/pitch",
        data={"payload": _json.dumps(body)},
        files={"audio": ("clap.wav", wav_bytes, "audio/wav")},
    )
    assert r.status_code == 200

    stored = _isolated_state / "pitches" / "cycle_000099_A.wav"
    assert stored.exists()
    assert stored.read_bytes() == wav_bytes
