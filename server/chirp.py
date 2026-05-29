"""Reference sync chirp for 時間校正 — extracted from main.py.

Mutual sync uses **two disjoint single bands**, one per phone: each phone
emits a single 100 ms up-sweep Hann chirp in its own band (A = 2→4 kHz,
B = 5→7 kHz, 1 kHz guard between them — see `SYNC_BAND_*` below). The
detector on each phone matched-filters both bands off its own mic stream to
recover self/other arrival times. Quick sync reuses band A alone (one
physical chirp, every listener finds it). Robustness comes from emitting N
bursts per band and taking the median arrival across bursts (see
`detect_band_windowed` + `_median_band_detection` in `sync_audio_detect.py`).

If you change any of the timing constants here (durations, frequencies),
update the matching defaults in `AudioSyncDetector.swift` on the iOS side so
the detector expects the same bands.
"""

from __future__ import annotations

import functools
import io
import wave

import numpy as np


# --- Mutual chirp sync bands ----------------------------------------------
# Each phone emits a single 100 ms up-sweep in its own disjoint band. Bands
# sit entirely within the iPhone speaker's flat-response region (roughly
# 1–7 kHz) and are separated by a 1 kHz guard to keep matched-filter cross-
# correlation leakage well below the detection threshold. If rig validation
# shows band B attenuated by the speaker's 6 kHz rolloff, shift to
# (4500, 6500) — keep 1 kHz guard against band A.
SYNC_BAND_A_F0: float = 2000.0
SYNC_BAND_A_F1: float = 4000.0
SYNC_BAND_B_F0: float = 5000.0
SYNC_BAND_B_F1: float = 7000.0
SYNC_CHIRP_DURATION_S: float = 0.1


def _hann_chirp(sample_rate: int, f0: float, f1: float, duration: float) -> np.ndarray:
    """Single linear chirp, Hann-windowed. `f0 > f1` produces a down-sweep
    (the phase formula handles either sweep direction)."""
    n = int(sample_rate * duration)
    t = np.arange(n) / sample_rate
    phase = 2.0 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2.0 * duration))
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(n) / (n - 1)))
    return np.sin(phase) * window


@functools.lru_cache(maxsize=1)
def chirp_wav_bytes() -> bytes:
    """Build the reference sync chirp WAV once and cache. The signal is
    deterministic (constants only) so any subsequent request reuses the
    exact same bytes."""
    sr = 44100
    f0 = 2000.0
    f1 = 8000.0
    chirp_duration = 0.1
    inter_chirp_silence = 0.05  # → 150 ms center-to-center

    up = _hann_chirp(sr, f0, f1, chirp_duration)
    down = _hann_chirp(sr, f1, f0, chirp_duration)
    mid_silence = np.zeros(int(sr * inter_chirp_silence), dtype=np.float64)
    pad = np.zeros(int(sr * 0.5), dtype=np.float64)

    full = np.concatenate([pad, up, mid_silence, down, pad])
    pcm = np.clip(full * 0.8, -1.0, 1.0)
    pcm_int = (pcm * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_int.tobytes())
    return buf.getvalue()
