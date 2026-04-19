"""Reference sync chirp for 時間校正 — extracted from main.py.

The signal is a **dual chirp**: up-sweep 2→8 kHz (100 ms, Hann) followed by
50 ms of silence, then a down-sweep 8→2 kHz (100 ms, Hann). The detector on
each phone locates both sweeps independently and averages their centers to
produce a Doppler-free anchor, and uses the 150 ms center-to-center gap as a
consistency check against stray same-band transients.

If you change any of the timing constants here (durations, gap, frequencies),
update the matching defaults in `AudioChirpDetector.init(...)` on the iOS
side so the detector expects the same inter-chirp gap.
"""

from __future__ import annotations

import functools
import io
import wave

import numpy as np


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
