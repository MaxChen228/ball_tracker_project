"""Reference sync chirp for 時間校正 — extracted from main.py."""

from __future__ import annotations

import functools
import io
import wave

import numpy as np


@functools.lru_cache(maxsize=1)
def chirp_wav_bytes() -> bytes:
    """Build the reference sync chirp WAV once and cache. The signal is
    deterministic (constants only) so any subsequent request can reuse the
    exact same bytes without re-running the FFT-style synthesis."""
    sr = 44100
    f0 = 2000.0
    f1 = 8000.0
    duration = 0.1
    n = int(sr * duration)
    t = np.arange(n) / sr
    phase = 2.0 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2.0 * duration))
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(n) / (n - 1)))
    chirp = np.sin(phase) * window

    silence = np.zeros(int(sr * 0.5), dtype=np.float64)
    full = np.concatenate([silence, chirp, silence])
    pcm = np.clip(full * 0.8, -1.0, 1.0)
    pcm_int = (pcm * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_int.tobytes())
    return buf.getvalue()
