"""Sync debug export: auto-analyze last sync result and render a compact
text report designed to be pasted to an AI for diagnosis.

The report covers:
- Last sync result status (success / aborted / none)
- Per-stream trace peak metrics vs threshold
- Quick-chirp telemetry peaks (input level, matched-filter peaks, noise floor)
- Trimmed sync log tail (last 30 entries)
- Automated diagnosis with specific remediation suggestions

Format is plain text, ~50 lines, machine-readable by an LLM without
needing screenshots or Plotly visualizations.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

_STREAMS = [
    ("A.self", "trace_a_self"),
    ("A.other", "trace_a_other"),
    ("B.self", "trace_b_self"),
    ("B.other", "trace_b_other"),
]


def _fmt_ts(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _trace_stats(
    trace: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not trace:
        return None
    peaks = [float(s.get("peak", 0)) for s in trace]
    times = [float(s.get("t", 0)) for s in trace]
    psrs = [float(s.get("psr", 0)) for s in trace]
    best_idx = max(range(len(peaks)), key=lambda i: peaks[i])
    sorted_peaks = sorted(peaks, reverse=True)
    p90 = sorted_peaks[max(0, len(sorted_peaks) // 10)]
    noise_median = sorted(peaks)[len(peaks) // 2]
    return {
        "best": peaks[best_idx],
        "t_best": times[best_idx],
        "best_psr": psrs[best_idx],
        "p90": p90,
        "noise_median": noise_median,
        "n": len(trace),
    }


def _snip(v: Any) -> str:
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return f"{v:.6g}"
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def _suggest_threshold(best_peak: float, current_thr: float, cfar: float) -> str:
    """Return a threshold suggestion string, or empty string if not appropriate.

    Rules:
    - Never suggest a value >= current threshold (that contradicts "lower it").
    - Require SNR >= 3 before suggesting threshold reduction (below that, the
      problem is signal level, not threshold, and lowering risks false triggers).
    - Suggested value = max(best * 0.70, cfar * 2.5) clamped below current_thr.
    """
    snr = best_peak / cfar if cfar > 0 else float("inf")
    if snr < 3.0:
        return ""  # signal too weak — recommend volume, not threshold
    candidate = max(best_peak * 0.70, cfar * 2.5)
    if candidate >= current_thr:
        return ""  # candidate is not actually lower — pointless suggestion
    return f"{candidate:.3f}"


def build_debug_report(
    last_sync: dict[str, Any] | None,
    telemetry: dict[str, dict[str, Any]],
    logs: list[dict[str, Any]],
    mutual_threshold: float,
    chirp_threshold: float,
    devices: list[dict[str, Any]] | None = None,
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    L: list[str] = [f"SYNC DEBUG EXPORT  {now_str}"]

    # Helper: consistent float extraction
    def _fp(d: dict, key: str) -> float:
        v = d.get(key)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Build per-cam device sync state lookup
    dev_by_cam: dict[str, dict[str, Any]] = {
        d["camera_id"]: d for d in (devices or [])
    }

    def _dev_sync_line(cam: str) -> str:
        d = dev_by_cam.get(cam)
        if d is None:
            return f"  Cam {cam}: OFFLINE (not in device registry)"
        synced = d.get("time_synced", False)
        sync_id = d.get("time_sync_id") or "—"
        age = d.get("time_sync_age_s")
        age_str = f"{age:.0f}s ago" if age is not None else "unknown"
        anchor = d.get("sync_anchor_timestamp_s")
        anchor_str = f"  anchor={anchor:.3f}s" if anchor is not None else ""
        verdict = "SYNCED" if synced else "NOT SYNCED"
        return f"  Cam {cam}: {verdict}  id={sync_id}  age={age_str}{anchor_str}"

    if last_sync is None:
        # Quick chirp path: no SyncResult is ever created.
        # Mutual sync timeout would produce a SyncResult; None means quick chirp
        # (or no attempt yet).
        L.append("STATUS: no SyncResult (Quick Chirp path, or no attempt yet)")
        L.append(f"thresholds: mutual={mutual_threshold:.4f}  chirp={chirp_threshold:.4f}")
        L.append("")

        # --- Device sync state (most important: did phone actually fire?) ---
        L.append("DEVICE SYNC STATE (authoritative — did phone register anchor?):")
        if not dev_by_cam:
            L.append("  (no devices in registry)")
        else:
            for cam in sorted(dev_by_cam):
                L.append(_dev_sync_line(cam))
        L.append("")

        # --- Quick-chirp telemetry ---
        L.append(f"QUICK CHIRP TELEMETRY (vs chirp threshold={chirp_threshold:.4f}):")
        if not telemetry:
            L.append("  (empty — phones likely did not receive sync_command over WS)")
        else:
            for cam in sorted(telemetry):
                t = telemetry[cam]
                inp_peak = max(_fp(t, "peak_input_peak"), _fp(t, "input_peak"))
                up = max(_fp(t, "peak_up_peak"), _fp(t, "up_peak"))
                dn = max(_fp(t, "peak_down_peak"), _fp(t, "down_peak"))
                cfar_up = _fp(t, "cfar_up_floor")
                age = _fp(t, "age_s")
                clipping = "YES" if inp_peak >= 0.98 else "no"
                up_margin = up / chirp_threshold if chirp_threshold > 0 else float("inf")
                dn_margin = dn / chirp_threshold if chirp_threshold > 0 else float("inf")
                up_v = "PASS" if up >= chirp_threshold else f"FAIL({up_margin:.2f}x)"
                dn_v = "PASS" if dn >= chirp_threshold else f"FAIL({dn_margin:.2f}x)"
                stale_flag = "  [STALE]" if age > 20 else ""
                L.append(
                    f"  Cam {cam}: inp_peak={inp_peak:.3f}  up={up:.4f} {up_v}"
                    f"  dn={dn:.4f} {dn_v}"
                    f"  cfar_up={cfar_up:.4f}  clip={clipping}  age={age:.0f}s{stale_flag}"
                )
        L.append("")

        # --- Log tail ---
        L.append("LOG (last 20 entries):")
        for entry in logs[-20:]:
            ts = entry.get("ts", 0)
            source = str(entry.get("source") or "?").ljust(6)
            event = str(entry.get("event") or "?")
            detail = entry.get("detail") or {}
            detail_str = " ".join(f"{k}={_snip(v)}" for k, v in detail.items())
            L.append(f"  [{_fmt_ts(ts)}] {source} {event} {detail_str}")
        L.append("")

        # --- Quick-chirp diagnosis ---
        # AUTHORITATIVE: device sync state determines success/failure.
        # Telemetry peaks are secondary — they explain HOW it happened or WHY it failed,
        # but they can be from a different attempt than the one that produced the sync state.
        L.append("DIAGNOSIS (Quick Chirp):")

        all_cams = sorted(set(list(dev_by_cam.keys()) + list(telemetry.keys())))
        if not all_cams:
            L.append("  [ERROR] No devices and no telemetry — phones did not receive sync_command")
            L.append("          Check: both phones online? WS connection healthy?")
            return "\n".join(L)

        synced_ids = [
            d.get("time_sync_id") for d in dev_by_cam.values()
            if d.get("time_synced") and d.get("time_sync_id")
        ]
        both_synced = len(synced_ids) == 2 and len(set(synced_ids)) == 1
        if both_synced:
            L.append(f"  [OK]    Both cameras SYNCED with matching id={synced_ids[0]} ← paired ✓")
            L.append(f"          (Telemetry peaks below show the CURRENT/next attempt, not the successful one)")

        for cam in all_cams:
            d = dev_by_cam.get(cam)
            t = telemetry.get(cam, {})
            age = _fp(t, "age_s")
            up = max(_fp(t, "peak_up_peak"), _fp(t, "up_peak"))
            dn = max(_fp(t, "peak_down_peak"), _fp(t, "down_peak"))
            best = max(up, dn)
            cfar = _fp(t, "cfar_up_floor")
            snr = best / cfar if cfar > 0 else float("inf")
            margin = best / chirp_threshold if chirp_threshold > 0 else float("inf")

            # Device state is authoritative
            actually_synced = d.get("time_synced", False) if d else False

            if actually_synced:
                if age > 10 or best < chirp_threshold:
                    # Telemetry is from a different/stale attempt — don't confuse with failure
                    L.append(f"  [OK]    Cam {cam}: SYNCED (telemetry age={age:.0f}s is from different attempt, ignore peaks)")
                else:
                    L.append(f"  [OK]    Cam {cam}: SYNCED  peak={best:.4f} margin={margin:.2f}x")
                continue

            # Not synced — diagnose why using telemetry
            if not t or age > 15:
                L.append(f"  [ERROR] Cam {cam}: NOT SYNCED, no fresh telemetry (age={age:.0f}s)")
                L.append(f"          → Phone may not have received sync_command, or already back in standby")
                continue

            # cfar-normalized SNR is more reliable than absolute threshold alone
            cfar_snr_ok = snr > 1.5 if cfar > 0 else True
            abs_ok = best >= chirp_threshold

            if abs_ok and cfar_snr_ok:
                L.append(f"  [WARN]  Cam {cam}: NOT SYNCED despite peak={best:.4f} margin={margin:.2f}x SNR={snr:.1f}x")
                L.append(f"          → Peak crossed threshold but iOS did not register anchor")
                L.append(f"          → Possible: PSR gate failed (peak too broad/noisy), or race condition")
                L.append(f"          → Try again; if persistent, lower chirp_detect_threshold slightly")
            elif abs_ok and not cfar_snr_ok:
                L.append(f"  [FAIL]  Cam {cam}: NOT SYNCED  peak={best:.4f} margin={margin:.2f}x BUT cfar_snr={snr:.2f}x (≈noise)")
                L.append(f"          → Absolute threshold passed but signal is buried in noise (cfar_up={cfar:.4f})")
                L.append(f"          → Move speaker closer OR reduce ambient noise")
            else:
                L.append(f"  [FAIL]  Cam {cam}: NOT SYNCED  peak={best:.4f} margin={margin:.2f}x SNR={snr:.1f}x")
                sug = _suggest_threshold(best, chirp_threshold, cfar)
                if sug:
                    L.append(f"          → Option 1: lower chirp_detect_threshold {chirp_threshold:.4f} → {sug}")
                    L.append(f"          → Option 2: play chirp louder / move speaker closer to Cam {cam}")
                else:
                    L.append(f"          → SNR too low ({snr:.1f}x) — lowering threshold risks false triggers")
                    L.append(f"          → Play chirp louder / move speaker closer to Cam {cam}")

        return "\n".join(L)

    run_id = last_sync.get("id", "?")
    L.append(f"run_id: {run_id}")
    L.append("")

    # --- Status ---
    if last_sync.get("aborted"):
        reasons = last_sync.get("abort_reasons") or {}
        r_str = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        L.append(f"STATUS: ABORTED  ({r_str})")
    elif last_sync.get("delta_s") is not None:
        delta_ms = float(last_sync["delta_s"]) * 1000.0
        dist = float(last_sync.get("distance_m") or 0.0)
        L.append(f"STATUS: SUCCESS  Δ={delta_ms:+.3f} ms  D={dist:.3f} m")
    else:
        L.append("STATUS: UNKNOWN")

    L.append(
        f"thresholds: mutual={mutual_threshold:.4f}  chirp={chirp_threshold:.4f}"
    )
    L.append("")

    # --- Trace peaks ---
    L.append(f"TRACE PEAKS (mutual threshold={mutual_threshold:.4f}):")
    stream_results: list[tuple[str, dict | None]] = []
    for label, key in _STREAMS:
        raw = last_sync.get(key)
        stats = _trace_stats(raw)
        stream_results.append((label, stats))
        if stats is None:
            L.append(f"  {label:<8}  NO TRACE DATA")
            continue
        best = stats["best"]
        margin = best / mutual_threshold if mutual_threshold > 0 else float("inf")
        verdict = "PASS" if best >= mutual_threshold else ("FAIL[close]" if margin >= 0.8 else "FAIL")
        L.append(
            f"  {label:<8}  best={best:.4f} @t={stats['t_best']:.3f}s"
            f"  margin={margin:.2f}x  psr={stats['best_psr']:.2f}"
            f"  noise_med={stats['noise_median']:.4f}  n={stats['n']}  {verdict}"
        )
    L.append("")

    # --- Device sync state (mutual sync result IS the sync state, but show for completeness) ---
    if dev_by_cam:
        L.append("DEVICE SYNC STATE:")
        for cam in sorted(dev_by_cam):
            L.append(_dev_sync_line(cam))
        L.append("")

    # --- Telemetry ---
    L.append("TELEMETRY (peak during attempt):")
    if not telemetry:
        L.append("  (no telemetry — phone did not send heartbeats during listen)")
    else:
        for cam in sorted(telemetry):
            t = telemetry[cam]
            inp_peak = max(_fp(t, "peak_input_peak"), _fp(t, "input_peak"))
            inp_rms = max(_fp(t, "peak_input_rms"), _fp(t, "input_rms"))
            up = max(_fp(t, "peak_up_peak"), _fp(t, "up_peak"))
            dn = max(_fp(t, "peak_down_peak"), _fp(t, "down_peak"))
            cfar_up = _fp(t, "cfar_up_floor")
            cfar_dn = _fp(t, "cfar_down_floor")
            clipping = "YES" if inp_peak >= 0.98 else "no"
            age = _fp(t, "age_s")
            age_str = f"  age={age:.0f}s" if age > 2 else ""
            L.append(
                f"  Cam {cam}: inp_rms={inp_rms:.3f}  inp_peak={inp_peak:.3f}"
                f"  up={up:.4f}  dn={dn:.4f}"
                f"  clipping={clipping}"
                f"  cfar_up={cfar_up:.4f}  cfar_dn={cfar_dn:.4f}{age_str}"
            )
    L.append("")

    # --- Log tail ---
    L.append("LOG (last 30 entries):")
    for entry in logs[-30:]:
        ts = entry.get("ts", 0)
        source = str(entry.get("source") or "?").ljust(6)
        event = str(entry.get("event") or "?")
        detail = entry.get("detail") or {}
        detail_str = " ".join(f"{k}={_snip(v)}" for k, v in detail.items())
        L.append(f"  [{_fmt_ts(ts)}] {source} {event} {detail_str}")
    L.append("")

    # --- Diagnosis ---
    L.append("DIAGNOSIS:")
    issues: list[str] = []
    fail_streams: list[tuple[str, dict]] = []
    pass_streams: list[tuple[str, dict]] = []
    no_data_streams: list[str] = []

    for label, stats in stream_results:
        if stats is None:
            no_data_streams.append(label)
        elif stats["best"] < mutual_threshold:
            fail_streams.append((label, stats))
        else:
            pass_streams.append((label, stats))

    if no_data_streams:
        issues.append(
            f"[ERROR] No trace data for: {', '.join(no_data_streams)}"
            " — WS sync_run not received by phone?"
        )
    if fail_streams:
        if len(fail_streams) == 4:
            best_of_fail = max(fail_streams, key=lambda x: x[1]["best"])
            issues.append(
                f"[ERROR] All 4 bands below threshold={mutual_threshold:.4f}"
                f" (best was {best_of_fail[0]}={best_of_fail[1]['best']:.4f},"
                f" margin={best_of_fail[1]['best']/mutual_threshold:.2f}x)"
            )
        else:
            for label, stats in fail_streams:
                margin = stats["best"] / mutual_threshold
                issues.append(
                    f"[ERROR] {label}: peak={stats['best']:.4f}"
                    f" margin={margin:.2f}x < 1.0 (threshold={mutual_threshold:.4f})"
                )
    if pass_streams:
        for label, stats in pass_streams:
            margin = stats["best"] / mutual_threshold
            issues.append(
                f"[OK]    {label}: peak={stats['best']:.4f} margin={margin:.2f}x  PASS"
            )

    # Clipping
    clipping_cams = []
    max_inp_peak = 0.0
    for cam, t in telemetry.items():
        try:
            peak = max(float(t.get("peak_input_peak") or 0), float(t.get("input_peak") or 0))
        except (TypeError, ValueError):
            peak = 0.0
        max_inp_peak = max(max_inp_peak, peak)
        if peak >= 0.98:
            clipping_cams.append(cam)

    if clipping_cams:
        issues.append(f"[WARN]  ADC clipping on cam(s) {', '.join(clipping_cams)} — reduce speaker volume")
    elif telemetry:
        issues.append(f"[OK]    No ADC clipping (max inp_peak={max_inp_peak:.3f})")

    # Noise floor
    cfar_vals = []
    for t in telemetry.values():
        try:
            v = float(t.get("cfar_up_floor") or 0)
            if v > 0:
                cfar_vals.append(v)
        except (TypeError, ValueError):
            pass
    if cfar_vals:
        mean_cfar = sum(cfar_vals) / len(cfar_vals)
        if mean_cfar > 0.05:
            issues.append(f"[WARN]  High noise floor: cfar_up avg={mean_cfar:.4f} — ambient noise?")
        else:
            issues.append(f"[OK]    Noise floor OK: cfar_up avg={mean_cfar:.4f}")

    # Delta sanity
    if last_sync.get("delta_s") is not None:
        delta_ms = abs(float(last_sync["delta_s"])) * 1000.0
        dist = float(last_sync.get("distance_m") or 0.0)
        if delta_ms > 10:
            issues.append(f"[WARN]  |Δ|={delta_ms:.2f}ms is large (>10ms) — re-sync recommended")
        else:
            issues.append(f"[OK]    |Δ|={delta_ms:.2f}ms  D={dist:.3f}m — plausible")

    for issue in issues:
        L.append(f"  {issue}")

    # Root cause + recommendations
    L.append("")
    if last_sync.get("delta_s") is not None:
        L.append("  Root cause: N/A — sync succeeded.")
    elif no_data_streams and len(no_data_streams) == 4:
        L.append("  Root cause: Phones did not start listening (WS message lost or iOS build too old).")
        L.append("  Recommendations:")
        L.append("    1. Check both phones are online (green in Devices card)")
        L.append("    2. Check WS connection — restart server if stale")
        L.append("    3. Ensure iOS build supports mutual sync (trace_self/trace_other fields)")
    elif fail_streams:
        all_stats = [s for _, s in fail_streams]
        best_peak = max(s["best"] for s in all_stats)
        # Use cfar noise floor from telemetry (real ambient noise) for SNR
        mean_cfar_telem = sum(cfar_vals) / len(cfar_vals) if cfar_vals else None
        noise_ref = (sum(cfar_vals) / len(cfar_vals)) if cfar_vals else None
        if noise_ref and noise_ref > 0:
            snr = best_peak / noise_ref
        else:
            noise_med = max(s["noise_median"] for s in all_stats)
            snr = best_peak / noise_med if noise_med > 0 else float("inf")
            noise_ref = noise_med or 0.001
        L.append(
            f"  Root cause: Audio level too low or threshold too high"
            f" (best peak={best_peak:.4f}, SNR~{snr:.1f}x cfar noise floor)."
        )
        L.append("  Recommendations:")
        sug = _suggest_threshold(best_peak, mutual_threshold, noise_ref)
        if sug:
            L.append(
                f"    1. Lower mutual_sync_threshold: {mutual_threshold:.4f} → {sug}"
                f"       (Runtime Tuning card on /sync page)"
            )
            L.append(f"    2. OR move phones' speaker closer / increase volume")
        else:
            L.append(f"    1. SNR too low ({snr:.1f}x) — lowering threshold risks false triggers")
            L.append(f"    1. Move speaker closer to both phones / increase volume")
        if clipping_cams:
            L.append(f"    ⚠ ADC clipping on {', '.join(clipping_cams)} — reduce speaker volume first")
        if snr < 5:
            L.append(f"    note: Very low SNR — quiet environment helps")
    else:
        abort_reasons = last_sync.get("abort_reasons") or {}
        timeout_cams = [k for k, v in abort_reasons.items() if "timeout" in str(v)]
        if timeout_cams:
            L.append(f"  Root cause: Phones {', '.join(timeout_cams)} timed out (both bands must fire within 8s).")
            L.append("  Recommendations:")
            L.append("    1. Check trace peaks above — likely one band barely failed threshold")
            L.append("    2. Lower mutual_sync_threshold if peaks are close to current value")
            L.append("    3. Ensure speaker is audible to both phones simultaneously")
        else:
            L.append("  Root cause: Unknown — check log entries above for abort_reason detail.")

    return "\n".join(L)
