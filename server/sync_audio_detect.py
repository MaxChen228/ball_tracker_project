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


def _compute_norm_correlation(
    audio: np.ndarray,
    reference: np.ndarray,
) -> tuple[np.ndarray, int] | None:
    """Compute normalized matched-filter over full recording. Returns
    (norm array, ref_len) or None if audio is too short."""
    n = len(audio)
    ref_len = len(reference)
    if n < ref_len:
        return None
    audio64 = audio.astype(np.float64)
    cumsq = np.concatenate(([0.0], np.cumsum(audio64 * audio64)))
    n_lags = n - ref_len + 1
    window_energy = cumsq[ref_len:] - cumsq[:n_lags]
    window_energy = np.maximum(window_energy, 1e-12)
    dot = np.correlate(audio64, reference.astype(np.float64), mode="valid")
    norm = np.minimum(np.abs(dot) / np.sqrt(window_energy), 1.0).astype(np.float32)
    return norm, ref_len


def _refine_peak(norm: np.ndarray, best_idx: int) -> float:
    """Parabolic sub-sample refinement. Returns fractional offset in [-1,1]."""
    if 0 < best_idx < len(norm) - 1:
        left = float(norm[best_idx - 1])
        right = float(norm[best_idx + 1])
        best = float(norm[best_idx])
        denom = left - 2.0 * best + right
        frac = 0.5 * (left - right) / denom if denom != 0.0 else 0.0
        return frac if -1.0 < frac < 1.0 else 0.0
    return 0.0


def _psr_in_window(norm: np.ndarray, best_idx: int, ref_len: int) -> float:
    exclusion = ref_len // 2
    mask = np.ones(len(norm), dtype=bool)
    mask[max(0, best_idx - exclusion):min(len(norm), best_idx + exclusion + 1)] = False
    second = float(norm[mask].max()) if mask.any() else 0.0
    best = float(norm[best_idx])
    return (best / second) if second > 0.0 else 0.0


def detect_band(
    audio: np.ndarray,
    sample_rate: int,
    reference: np.ndarray,
    audio_start_pts_s: float,
) -> BandDetection:
    """Global matched-filter search (single best peak). Used as fallback
    when no expected emission times are available."""
    result = _compute_norm_correlation(audio, reference)
    if result is None:
        return BandDetection(center_pts_s=audio_start_pts_s, peak_norm=0.0, psr=0.0, trace=[])
    norm, ref_len = result

    best_idx = int(np.argmax(norm))
    best_norm = float(norm[best_idx])
    frac = _refine_peak(norm, best_idx)
    psr = _psr_in_window(norm, best_idx, ref_len)
    center_pts_s = audio_start_pts_s + (best_idx + ref_len / 2.0 + frac) / sample_rate

    trace: list[SyncTraceSample] = []
    hop = max(1, int(round(sample_rate * _TRACE_HOP_S)))
    for t_idx in range(0, len(norm), hop):
        trace.append(SyncTraceSample(t=float(t_idx) / float(sample_rate), peak=float(norm[t_idx]), psr=0.0))

    return BandDetection(center_pts_s=center_pts_s, peak_norm=best_norm, psr=float(psr), trace=trace)


def detect_band_windowed(
    audio: np.ndarray,
    sample_rate: int,
    reference: np.ndarray,
    audio_start_pts_s: float,
    emit_at_s: list[float],
    search_window_s: float = 0.3,
) -> list[BandDetection]:
    """Windowed multi-peak search: for each expected emission time, search
    ±search_window_s and return the best peak in that window.

    Returns one BandDetection per expected emission (same length as
    emit_at_s). A window that misses (audio too short, peak too weak)
    still returns a BandDetection with peak_norm=0.

    Caller takes median of the center_pts_s values across the N results
    to get a single robust timestamp for each band."""
    result = _compute_norm_correlation(audio, reference)
    if result is None:
        return [BandDetection(center_pts_s=audio_start_pts_s, peak_norm=0.0, psr=0.0, trace=[])
                for _ in emit_at_s]
    norm, ref_len = result
    n_lags = len(norm)
    out: list[BandDetection] = []

    for expected_t in emit_at_s:
        # expected_t is relative to audio_start (recording start).
        # chirp center sample = expected_t * sr
        # lag = center - ref_len/2
        expected_lag = expected_t * sample_rate - ref_len / 2.0
        win = int(search_window_s * sample_rate)
        lo = max(0, int(expected_lag) - win)
        hi = min(n_lags, int(expected_lag) + win + 1)
        if lo >= hi:
            out.append(BandDetection(center_pts_s=audio_start_pts_s + expected_t,
                                     peak_norm=0.0, psr=0.0, trace=[]))
            continue
        local_best = int(np.argmax(norm[lo:hi]))
        abs_idx = lo + local_best
        best_norm = float(norm[abs_idx])
        frac = _refine_peak(norm, abs_idx)
        # PSR within the search window (not global)
        w_norm = norm[lo:hi]
        excl = ref_len // 2
        mask = np.ones(len(w_norm), dtype=bool)
        mask[max(0, local_best - excl):min(len(w_norm), local_best + excl + 1)] = False
        second = float(w_norm[mask].max()) if mask.any() else 0.0
        psr = (best_norm / second) if second > 0.0 else 0.0
        center_pts_s = audio_start_pts_s + (abs_idx + ref_len / 2.0 + frac) / sample_rate
        out.append(BandDetection(center_pts_s=center_pts_s, peak_norm=best_norm, psr=float(psr), trace=[]))

    return out


def _median_band_detection(detections: list[BandDetection]) -> BandDetection:
    """Combine N per-burst detections → single BandDetection with median
    center PTS and mean peak/PSR. Provides the trace from the last entry
    (empty from windowed detection) for API compat."""
    if not detections:
        raise ValueError("empty detections list")
    centers = [d.center_pts_s for d in detections]
    peaks = [d.peak_norm for d in detections]
    psrs = [d.psr for d in detections]
    return BandDetection(
        center_pts_s=float(np.median(centers)),
        peak_norm=float(np.mean(peaks)),
        psr=float(np.mean(psrs)),
        trace=detections[-1].trace,
    )


def detect_sync_report(
    wav_bytes: bytes,
    sync_id: str,
    camera_id: str,
    role: str,
    audio_start_pts_s: float,
    emit_at_s_self: list[float],
    emit_at_s_other: list[float],
    search_window_s: float = 0.3,
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

    ref_a = _build_reference(sample_rate, SYNC_BAND_A_F0, SYNC_BAND_A_F1)
    ref_b = _build_reference(sample_rate, SYNC_BAND_B_F0, SYNC_BAND_B_F1)

    if not emit_at_s_self or not emit_at_s_other:
        raise ValueError("emit_at_s_self and emit_at_s_other are required (non-empty lists)")
    ref_self = ref_a if role == "A" else ref_b
    ref_other = ref_b if role == "A" else ref_a
    dets_self = detect_band_windowed(audio, sample_rate, ref_self,
                                     audio_start_pts_s, emit_at_s_self, search_window_s)
    dets_other = detect_band_windowed(audio, sample_rate, ref_other,
                                      audio_start_pts_s, emit_at_s_other, search_window_s)
    det_self = _median_band_detection(dets_self)
    det_other = _median_band_detection(dets_other)
    n_burst = len(emit_at_s_self)

    t_self_s = det_self.center_pts_s
    t_from_other_s = det_other.center_pts_s
    trace_self = det_self.trace
    trace_other = det_other.trace
    peak_self = det_self.peak_norm
    peak_other = det_other.peak_norm
    psr_self = det_self.psr
    psr_other = det_other.psr

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
        "n_burst": n_burst,
        "windowed": emit_at_s_self is not None and emit_at_s_other is not None,
    }
    return report, debug


def now_s() -> float:
    """Monotonic seconds used when the caller needs to stamp detection
    latency. Shim kept here so tests can monkey-patch without touching
    `time`."""
    return time.monotonic()
