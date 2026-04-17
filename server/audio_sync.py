"""Audio-based time sync via cross-correlation.

Each phone records a WAV file covering one pitch cycle and uploads it beside
the JSON payload. Because iOS `AVCaptureSession` issues video and audio
sample timestamps from one master clock, the first audio sample's PTS
(`audio_start_ts_s` in the payload) shares a time base with every
`frame.timestamp_s`.

Clock-offset recovery:

    event_clockA = T_A_start + s_A / sample_rate
    event_clockB = T_B_start + s_B / sample_rate

where (s_A, s_B) are the sample indices of the same physical sound in the two
WAVs, and (T_A_start, T_B_start) are `audio_start_ts_s` for A and B.

Define δ = clockA − clockB (phone-to-phone constant offset). Then:

    δ = (T_A_start − T_B_start) + (s_A − s_B) / sample_rate

Cross-correlation peak lag gives (s_A − s_B). To align B's frame timestamps
to A's clock, add δ:  b_frame_ts_in_A_clock = b_frame_ts + δ.

Uses only numpy; scipy is not required.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Return (samples_float64, sample_rate). Int16/int32 PCM only."""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32)
    else:
        raise ValueError(f"unsupported sample width: {sw} bytes")
    if nch > 1:
        data = data.reshape(-1, nch).mean(axis=1)
    return data.astype(np.float64), sr


def _emphasize_transients(x: np.ndarray) -> np.ndarray:
    """First-order high-pass via differentiation. Suppresses DC, LF drift and
    rumble while amplifying the sharp edges typical of a clap / chirp."""
    return np.diff(x)


def _fft_cross_correlate(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Linear cross-correlation of (a, b) via FFT, returned in scipy.signal
    mode='full' ordering (index 0 = lag -(len(b)-1), last = lag len(a)-1)."""
    n = len(a) + len(b) - 1
    n_fft = 1 << (n - 1).bit_length()  # next pow-2 for speed
    A = np.fft.rfft(a, n_fft)
    B = np.fft.rfft(b, n_fft)
    c_full = np.fft.irfft(A * np.conj(B), n_fft)
    # c_full is circular; slice to linear ordering.
    linear = np.concatenate([c_full[-(len(b) - 1):], c_full[: len(a)]])
    return linear


def _parabolic_peak(y_left: float, y_center: float, y_right: float) -> float:
    """Sub-sample refinement of a discrete peak. Returns the fractional offset
    in (-0.5, 0.5) to add to the integer lag."""
    denom = y_left - 2 * y_center + y_right
    if denom == 0:
        return 0.0
    return 0.5 * (y_left - y_right) / denom


def compute_audio_offset(
    wav_a_path: Path,
    t_a_start_s: float,
    wav_b_path: Path,
    t_b_start_s: float,
) -> tuple[float, float]:
    """Compute the A↔B clock offset using audio cross-correlation.

    Returns `(delta_s, normalized_peak)` where `delta_s = clockA − clockB`.
    To align B frame timestamps to A's clock:  `ts_A_equiv = ts_B + delta_s`.

    `normalized_peak` in [0, 1] — rough confidence; a clean clap typically
    gives > 0.1, whereas uncorrelated noise stays near 0.

    Raises `ValueError` on unreadable WAVs or mismatched sample rates.
    """
    a_raw, sr_a = _read_wav_mono(wav_a_path)
    b_raw, sr_b = _read_wav_mono(wav_b_path)
    if sr_a != sr_b:
        raise ValueError(f"sample rate mismatch: A={sr_a} B={sr_b}")
    if len(a_raw) < 2 or len(b_raw) < 2:
        raise ValueError("WAV too short for correlation")
    sr = sr_a

    a = _emphasize_transients(a_raw)
    b = _emphasize_transients(b_raw)

    corr = _fft_cross_correlate(a, b)
    peak_idx = int(np.argmax(np.abs(corr)))
    lag_int = peak_idx - (len(b) - 1)  # s_A - s_B in samples

    # Parabolic sub-sample refinement for <1-sample precision.
    if 0 < peak_idx < len(corr) - 1:
        frac = _parabolic_peak(
            float(corr[peak_idx - 1]),
            float(corr[peak_idx]),
            float(corr[peak_idx + 1]),
        )
    else:
        frac = 0.0
    lag = lag_int + frac

    delta_s = (t_a_start_s - t_b_start_s) + lag / sr

    # Normalized peak — cosine similarity style (independent of loudness).
    a_energy = float(np.linalg.norm(a))
    b_energy = float(np.linalg.norm(b))
    peak_norm = (
        float(corr[peak_idx]) / (a_energy * b_energy)
        if a_energy > 0 and b_energy > 0 else 0.0
    )
    return float(delta_s), abs(peak_norm)
