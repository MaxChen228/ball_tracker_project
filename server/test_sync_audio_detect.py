"""Unit tests for `sync_audio_detect`.

Strategy: build synthetic WAVs containing known chirp signatures at known
sample offsets, run the detector, and verify:
  1. The detected center PTS round-trips correctly (accuracy ≤ 1 sample).
  2. Normalized peak is near 1.0 for clean signals and near 0 for pure
     noise (pipeline is working, not a random spike).
  3. Role -> self/other mapping is correct (A-role sees A as self, B as
     other, and vice versa).
  4. End-to-end: `detect_sync_report` produces a valid SyncReport with
     t_self / t_from_other populated and the session-clock math intact.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent))

from chirp import (
    SYNC_BAND_A_F0, SYNC_BAND_A_F1,
    SYNC_BAND_B_F0, SYNC_BAND_B_F1,
    SYNC_CHIRP_DURATION_S,
    _hann_chirp,
)
import sync_audio_detect


SAMPLE_RATE = 48000


def _make_wav_bytes(mono_float: np.ndarray, sample_rate: int) -> bytes:
    """Build a 16-bit PCM WAV from a mono float32 array in [-1, 1]."""
    clipped = np.clip(mono_float, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth_recording(
    sample_rate: int,
    duration_s: float,
    chirp_f0: float,
    chirp_f1: float,
    chirp_start_sample: int,
    chirp_amplitude: float = 0.5,
    noise_amplitude: float = 1e-4,
    seed: int = 42,
) -> np.ndarray:
    """Synthesize a recording: silence + faint noise + one chirp at a
    known sample offset. `chirp_amplitude` 0.5 mimics a reasonably loud
    real-world chirp (`inp_peak ~ 0.5`)."""
    n = int(sample_rate * duration_s)
    rng = np.random.default_rng(seed)
    audio = rng.normal(0.0, noise_amplitude, size=n).astype(np.float32)
    chirp = _hann_chirp(sample_rate, chirp_f0, chirp_f1, SYNC_CHIRP_DURATION_S)
    chirp = (chirp * chirp_amplitude).astype(np.float32)
    end = min(n, chirp_start_sample + len(chirp))
    if chirp_start_sample < n and end > chirp_start_sample:
        audio[chirp_start_sample:end] += chirp[: end - chirp_start_sample]
    return audio


def test_load_wav_roundtrip() -> None:
    sig = np.sin(2 * np.pi * 1000.0 * np.arange(48000) / 48000.0).astype(np.float32) * 0.3
    wav = _make_wav_bytes(sig, 48000)
    audio, rate = sync_audio_detect.load_wav_mono_float(wav)
    assert rate == 48000
    assert len(audio) == 48000
    # 16-bit roundtrip loses ~3e-5 amplitude; compare with generous tol.
    assert np.max(np.abs(audio - sig)) < 1e-3


def test_detect_band_finds_chirp_at_known_offset() -> None:
    """Chirp injected at sample 10000 → detector must locate it with
    sub-sample precision. Audio start PTS is 100.0 s; expected center
    PTS = 100.0 + (10000 + refLen/2) / 48000."""
    audio = _synth_recording(
        sample_rate=SAMPLE_RATE,
        duration_s=1.0,
        chirp_f0=SYNC_BAND_A_F0,
        chirp_f1=SYNC_BAND_A_F1,
        chirp_start_sample=10000,
    )
    ref = sync_audio_detect._build_reference(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1
    )
    result = sync_audio_detect.detect_band(
        audio, SAMPLE_RATE, ref, audio_start_pts_s=100.0
    )
    ref_len = int(SAMPLE_RATE * SYNC_CHIRP_DURATION_S)
    expected_center_s = 100.0 + (10000 + ref_len / 2.0) / SAMPLE_RATE
    # Sub-sample accuracy: ≤ 1 sample error (~21 μs @ 48 kHz).
    assert abs(result.center_pts_s - expected_center_s) < 1.0 / SAMPLE_RATE
    assert result.peak_norm > 0.8, (
        f"clean-signal peak should be near 1.0, got {result.peak_norm}"
    )
    assert result.psr > 3.0


def test_detect_band_returns_low_peak_on_pure_noise() -> None:
    rng = np.random.default_rng(123)
    audio = rng.normal(0.0, 0.01, size=SAMPLE_RATE).astype(np.float32)
    ref = sync_audio_detect._build_reference(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1
    )
    result = sync_audio_detect.detect_band(
        audio, SAMPLE_RATE, ref, audio_start_pts_s=0.0
    )
    # Random noise normalized correlation against a structured reference
    # sits < 0.2 almost always — the chirp pattern is too specific.
    assert result.peak_norm < 0.25, (
        f"noise-only peak should be small, got {result.peak_norm}"
    )


def test_detect_band_cross_band_isolation() -> None:
    """B-band chirp in the audio, correlated against A reference → peak
    should be very small (bands are disjoint + guard-banded)."""
    audio = _synth_recording(
        sample_rate=SAMPLE_RATE,
        duration_s=1.0,
        chirp_f0=SYNC_BAND_B_F0,
        chirp_f1=SYNC_BAND_B_F1,
        chirp_start_sample=20000,
    )
    ref_a = sync_audio_detect._build_reference(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1
    )
    result = sync_audio_detect.detect_band(
        audio, SAMPLE_RATE, ref_a, audio_start_pts_s=0.0
    )
    assert result.peak_norm < 0.25


def test_detect_sync_report_role_a_maps_bands_correctly() -> None:
    """Role A recording contains: A-chirp at sample 5000 (self) and
    B-chirp at sample 15000 (other). Returned report should have
    t_self mapped to the A detection and t_from_other to the B one."""
    audio = _synth_recording(
        sample_rate=SAMPLE_RATE,
        duration_s=1.0,
        chirp_f0=SYNC_BAND_A_F0,
        chirp_f1=SYNC_BAND_A_F1,
        chirp_start_sample=5000,
    )
    # Overlay a B-band chirp at a different offset.
    b_chirp = _hann_chirp(
        SAMPLE_RATE, SYNC_BAND_B_F0, SYNC_BAND_B_F1, SYNC_CHIRP_DURATION_S
    ).astype(np.float32) * 0.5
    audio[15000 : 15000 + len(b_chirp)] += b_chirp

    wav = _make_wav_bytes(audio, SAMPLE_RATE)
    ref_len = int(SAMPLE_RATE * SYNC_CHIRP_DURATION_S)
    emit_self = [(5000 + ref_len / 2.0) / SAMPLE_RATE]
    emit_other = [(15000 + ref_len / 2.0) / SAMPLE_RATE]
    report, debug = sync_audio_detect.detect_sync_report(
        wav_bytes=wav,
        sync_id="sy_abcd0001",
        camera_id="A",
        role="A",
        audio_start_pts_s=1000.0,
        emit_at_s_self=emit_self,
        emit_at_s_other=emit_other,
    )

    expected_t_self = 1000.0 + (5000 + ref_len / 2.0) / SAMPLE_RATE
    expected_t_other = 1000.0 + (15000 + ref_len / 2.0) / SAMPLE_RATE

    assert report.role == "A"
    assert report.emitted_band == "A"
    assert report.t_self_s is not None
    assert report.t_from_other_s is not None
    assert abs(report.t_self_s - expected_t_self) < 1.0 / SAMPLE_RATE
    assert abs(report.t_from_other_s - expected_t_other) < 1.0 / SAMPLE_RATE
    assert not report.aborted
    assert debug["peak_self"] > 0.8
    assert debug["peak_other"] > 0.8


def test_detect_sync_report_role_b_is_mirrored() -> None:
    """Role B: self=B band, other=A band. Same audio shape should yield
    SWAPPED t_self vs t_from_other compared to role A."""
    audio = _synth_recording(
        sample_rate=SAMPLE_RATE,
        duration_s=1.0,
        chirp_f0=SYNC_BAND_A_F0,
        chirp_f1=SYNC_BAND_A_F1,
        chirp_start_sample=5000,
    )
    b_chirp = _hann_chirp(
        SAMPLE_RATE, SYNC_BAND_B_F0, SYNC_BAND_B_F1, SYNC_CHIRP_DURATION_S
    ).astype(np.float32) * 0.5
    audio[15000 : 15000 + len(b_chirp)] += b_chirp

    wav = _make_wav_bytes(audio, SAMPLE_RATE)
    ref_len = int(SAMPLE_RATE * SYNC_CHIRP_DURATION_S)
    # Role B: self = B band (at sample 15000), other = A band (at 5000)
    emit_self = [(15000 + ref_len / 2.0) / SAMPLE_RATE]
    emit_other = [(5000 + ref_len / 2.0) / SAMPLE_RATE]
    report, debug = sync_audio_detect.detect_sync_report(
        wav_bytes=wav, sync_id="sy_abcd0002", camera_id="B", role="B",
        audio_start_pts_s=0.0,
        emit_at_s_self=emit_self,
        emit_at_s_other=emit_other,
    )
    expected_t_self = (15000 + ref_len / 2.0) / SAMPLE_RATE
    expected_t_other = (5000 + ref_len / 2.0) / SAMPLE_RATE
    assert abs(report.t_self_s - expected_t_self) < 1.0 / SAMPLE_RATE
    assert abs(report.t_from_other_s - expected_t_other) < 1.0 / SAMPLE_RATE


def test_detect_sync_report_rejects_invalid_role() -> None:
    wav = _make_wav_bytes(np.zeros(48000, dtype=np.float32), SAMPLE_RATE)
    with pytest.raises(ValueError, match="role must be"):
        sync_audio_detect.detect_sync_report(
            wav_bytes=wav, sync_id="sy_ffff", camera_id="A", role="C",
            audio_start_pts_s=0.0,
            emit_at_s_self=[0.1], emit_at_s_other=[0.2],
        )


def test_trace_emitted_at_30hz() -> None:
    """Trace should carry samples at ~30 Hz — the cadence the /sync
    debug plot was designed for."""
    audio = _synth_recording(
        sample_rate=SAMPLE_RATE,
        duration_s=2.0,
        chirp_f0=SYNC_BAND_A_F0,
        chirp_f1=SYNC_BAND_A_F1,
        chirp_start_sample=48000,
    )
    ref = sync_audio_detect._build_reference(
        SAMPLE_RATE, SYNC_BAND_A_F0, SYNC_BAND_A_F1
    )
    result = sync_audio_detect.detect_band(
        audio, SAMPLE_RATE, ref, audio_start_pts_s=0.0
    )
    # 2s at 30 Hz → ~60 samples. Allow 55-65 window for hop rounding.
    assert 55 <= len(result.trace) <= 65
    # Trace times should be strictly increasing.
    times = [s.t for s in result.trace]
    assert all(t2 > t1 for t1, t2 in zip(times, times[1:]))
