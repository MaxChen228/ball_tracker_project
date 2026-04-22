"""Server-side mutual-sync chirp detection.

Phase A of the sync-path refactor: iOS now records the full 3-second
listening window as raw PCM and uploads it. This module runs the
matched-filter detection that used to live in
`ball_tracker/AudioSyncDetector.swift` / `CFARNoiseFloor.swift`, and
produces the per-role timestamps + diagnostic traces the existing
server-side state machine expects (via `SyncReport`).

Benefits of moving detection here:
  - Iterating on detection algorithm = edit + `uv run uvicorn reload`,
    no 2-phone rebuild cycle.
  - Failed attempts persist their WAVs on disk → can replay the exact
    bytes against new detection variants offline.
  - numpy / full-vector math instead of vDSP streaming constraints →
    free to try STFT / spectrogram / multi-hypothesis detection later.

The algorithm mirrors the old iOS time-domain matched filter so the
Phase A cutover is a drop-in replacement (same PSR numbers on the
same audio); the detection-algorithm iteration happens in Phase B
once we have accumulated real failure recordings.
"""
from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass, field

import numpy as np

from chirp import (
    SYNC_BAND_A_F0, SYNC_BAND_A_F1,
    SYNC_BAND_B_F0, SYNC_BAND_B_F1,
    SYNC_CHIRP_DURATION_S,
    _hann_chirp,
)
from schemas import SyncReport, SyncTraceSample


# Cadence at which we emit trace samples to the /sync debug plot.
# 30 Hz matches what AudioSyncDetector.swift used to publish so the
# existing Plotly traces look identical to the operator.
_TRACE_HOP_S: float = 1.0 / 30.0


@dataclass(frozen=True)
class BandDetection:
    """Result of matched-filter detection of one band's chirp in one
    recording. `center_pts_s` is the session-clock PTS of the chirp
    center (start + refLen/2 + sub-sample refinement)."""
    center_pts_s: float
    peak_norm: float
    psr: float
    trace: list[SyncTraceSample] = field(default_factory=list)


def load_wav_mono_float(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes to float32 mono in [-1, 1]. Handles 16-bit and
    32-bit PCM; multi-channel collapses to mean. Raises ValueError on
    unsupported formats so the endpoint can 422 with a clear reason."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n = w.getnframes()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(n)
    if sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(
            f"unsupported WAV sampwidth {sampwidth} (expect 16 or 32 bit PCM)"
        )
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return arr, rate


def _build_reference(rate: int, f0: float, f1: float) -> np.ndarray:
    """Unit-energy-normalized Hann-windowed chirp matching iOS / server
    emission. Keeping a single source of truth via `chirp._hann_chirp`
    guarantees the detector's reference is spectrally identical to what
    iOS actually plays."""
    chirp = _hann_chirp(rate, f0, f1, SYNC_CHIRP_DURATION_S)
    energy = float(np.sum(chirp ** 2))
    if energy > 0:
        chirp = chirp / np.sqrt(energy)
    return chirp.astype(np.float32)


def detect_band(
    audio: np.ndarray,
    sample_rate: int,
    reference: np.ndarray,
    audio_start_pts_s: float,
) -> BandDetection:
    """Matched-filter correlation of `audio` against `reference` over all
    valid lags. Returns the best normalized peak, its PSR, and a hopped
    trace for the debug plot.

    Normalization: `|dot(window, ref)| / sqrt(window_energy)` where
    `ref` has unit energy. Cauchy-Schwarz bounds the result in [0, 1].

    Parabolic sub-sample refinement on the normalized peaks (not raw
    dot products) prevents a local energy ramp from skewing the
    reconstructed center PTS — same refinement the iOS detector did."""
    n = len(audio)
    ref_len = len(reference)
    if n < ref_len:
        return BandDetection(
            center_pts_s=audio_start_pts_s, peak_norm=0.0, psr=0.0, trace=[]
        )

    # Exact per-window energy via cumulative sum of squares — one alloc,
    # O(1) lookup per lag, no rolling-update drift.
    audio64 = audio.astype(np.float64)
    cumsq = np.concatenate(([0.0], np.cumsum(audio64 * audio64)))
    n_lags = n - ref_len + 1
    window_energy = cumsq[ref_len:] - cumsq[:n_lags]
    window_energy = np.maximum(window_energy, 1e-12)

    # `np.correlate` with mode='valid' computes exactly the sliding dot
    # product we want: output[k] = sum_i audio[k+i] * reference[i].
    dot = np.correlate(audio.astype(np.float64), reference.astype(np.float64), mode="valid")
    norm = np.abs(dot) / np.sqrt(window_energy)
    # Clamp to [0, 1] as a belt-and-braces guard against numerical
    # overshoot (shouldn't happen given unit-energy reference but cheap
    # to enforce).
    norm = np.minimum(norm, 1.0).astype(np.float32)

    best_idx = int(np.argmax(norm))
    best_norm = float(norm[best_idx])

    # Parabolic sub-sample refinement on normalized peaks.
    if 0 < best_idx < len(norm) - 1:
        left = float(norm[best_idx - 1])
        right = float(norm[best_idx + 1])
        denom = left - 2.0 * best_norm + right
        frac = 0.5 * (left - right) / denom if denom != 0.0 else 0.0
        if not (-1.0 < frac < 1.0):
            frac = 0.0
    else:
        frac = 0.0

    # PSR: best / max outside ±ref_len/2 exclusion. Guard against the
    # exclusion zone swallowing the whole correlation (tiny recordings).
    exclusion = ref_len // 2
    mask = np.ones(len(norm), dtype=bool)
    lo = max(0, best_idx - exclusion)
    hi = min(len(norm), best_idx + exclusion + 1)
    mask[lo:hi] = False
    second_norm = float(norm[mask].max()) if mask.any() else 0.0
    psr = (best_norm / second_norm) if second_norm > 0.0 else 0.0

    # Chirp center (in fractional samples): lag + refLen/2 + parabolic
    # refinement. This lands on the middle of the 100 ms sweep.
    center_frac_samples = best_idx + (ref_len / 2.0) + frac
    center_pts_s = audio_start_pts_s + (center_frac_samples / sample_rate)

    # Hopped trace: emit (t, peak_at_hop, local_psr) at ~30 Hz so the
    # existing /sync Plotly plot can render the noise floor + spike
    # without changing its frontend.
    trace: list[SyncTraceSample] = []
    hop = max(1, int(round(sample_rate * _TRACE_HOP_S)))
    for t_idx in range(0, len(norm), hop):
        trace.append(SyncTraceSample(
            t=float(t_idx) / float(sample_rate),
            peak=float(norm[t_idx]),
            psr=0.0,
        ))

    return BandDetection(
        center_pts_s=center_pts_s,
        peak_norm=best_norm,
        psr=float(psr),
        trace=trace,
    )


def detect_sync_report(
    wav_bytes: bytes,
    sync_id: str,
    camera_id: str,
    role: str,
    audio_start_pts_s: float,
    expected_sample_rate: int | None = None,
) -> tuple[SyncReport, dict[str, float]]:
    """Turn one cam's uploaded WAV + metadata into a `SyncReport` ready
    to feed `State.record_sync_report`.

    The role determines which band is "self" (you emitted it) vs "other"
    (peer emitted it). For role A: self=band A, other=band B; vice versa
    for role B.

    Returns `(report, debug)`. `debug` carries the raw per-band peak +
    PSR values even when the report itself reflects abort logic, so
    failure-mode post-mortem can see the real numbers.

    Currently `aborted` is False in all success paths — we always
    produce both timestamps since server-side detection runs over the
    whole recording. If the peak fails a caller-supplied threshold
    (future hook), we flip aborted=True and null the relevant PTS.
    """
    if role not in ("A", "B"):
        raise ValueError(f"role must be 'A' or 'B', got {role!r}")

    audio, sample_rate = load_wav_mono_float(wav_bytes)
    if expected_sample_rate is not None and sample_rate != expected_sample_rate:
        # Not fatal — iOS may deliver 44.1k on older hardware — but the
        # caller's metadata should agree with the WAV header.
        pass

    ref_a = _build_reference(sample_rate, SYNC_BAND_A_F0, SYNC_BAND_A_F1)
    ref_b = _build_reference(sample_rate, SYNC_BAND_B_F0, SYNC_BAND_B_F1)

    det_a = detect_band(audio, sample_rate, ref_a, audio_start_pts_s)
    det_b = detect_band(audio, sample_rate, ref_b, audio_start_pts_s)

    if role == "A":
        t_self_s = det_a.center_pts_s
        t_from_other_s = det_b.center_pts_s
        trace_self = det_a.trace
        trace_other = det_b.trace
        peak_self = det_a.peak_norm
        peak_other = det_b.peak_norm
        psr_self = det_a.psr
        psr_other = det_b.psr
    else:
        t_self_s = det_b.center_pts_s
        t_from_other_s = det_a.center_pts_s
        trace_self = det_b.trace
        trace_other = det_a.trace
        peak_self = det_b.peak_norm
        peak_other = det_a.peak_norm
        psr_self = det_b.psr
        psr_other = det_a.psr

    report = SyncReport(
        camera_id=camera_id,
        sync_id=sync_id,
        role=role,  # type: ignore[arg-type]
        t_self_s=float(t_self_s),
        t_from_other_s=float(t_from_other_s),
        emitted_band=role,  # type: ignore[arg-type]
        trace_self=trace_self,
        trace_other=trace_other,
        aborted=False,
        abort_reason=None,
    )
    debug = {
        "sample_rate": float(sample_rate),
        "duration_s": float(len(audio)) / float(sample_rate),
        "peak_self": float(peak_self),
        "peak_other": float(peak_other),
        "psr_self": float(psr_self),
        "psr_other": float(psr_other),
    }
    return report, debug


def now_s() -> float:
    """Monotonic seconds used when the caller needs to stamp detection
    latency. Shim kept here so tests can monkey-patch without touching
    `time`."""
    return time.monotonic()
