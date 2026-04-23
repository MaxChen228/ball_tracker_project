"""Sync debug export: auto-analyze last sync result and render a compact
text report designed to be pasted to an AI for diagnosis.

The report covers:
- Last sync result status (success / aborted / none)
- Mutual-sync MATH BREAKDOWN: raw scalars → α, β → δ (clock offset) + d (distance)
- Sanity flags (impossible distance, α+β vs expected 2d, lopsided reports)
- Quick-chirp telemetry peaks (input level, matched-filter peaks, noise floor)
- Trimmed sync log tail
- Automated diagnosis with specific remediation suggestions

Format is plain text designed to be pasted verbatim to an LLM. No
screenshots, no plots — just dense labelled data so the AI can read it
in one pass.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

SPEED_OF_SOUND_MS = 343.0  # m/s at ~20 °C


def _fmt_ts(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


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
    - Never suggest a value >= current threshold.
    - Require SNR >= 3 before suggesting threshold reduction.
    - Suggested = max(best * 0.70, cfar * 2.5) clamped below current_thr.
    """
    snr = best_peak / cfar if cfar > 0 else float("inf")
    if snr < 3.0:
        return ""
    candidate = max(best_peak * 0.70, cfar * 2.5)
    if candidate >= current_thr:
        return ""
    return f"{candidate:.3f}"


def _mutual_math(last_sync: dict[str, Any]) -> dict[str, Any] | None:
    """Extract per-role mic timestamps + derive mutual-sync math.

    Returns None if any required scalar is missing.

    Math:
      α = t_from_other_A − t_self_A       (A-clock, A heard B vs A emitted)
      β = t_from_other_B − t_self_B       (B-clock, B heard A vs B emitted)
      δ = ((t_self_A − t_from_other_B) + (t_from_other_A − t_self_B)) / 2
          clock offset, A − B (positive → A's clock ahead of B's)
      d = ((t_from_other_A − t_self_B) − (t_self_A − t_from_other_B)) / 2
          one-way propagation time between A and B (s)
      distance = SPEED_OF_SOUND_MS × d    (m)

    The report-side check is: |α + β − 2d| should be ~0 (redundant
    derivation of the same d from within-clock deltas). A large value
    means the server picked the wrong bursts for α or β.
    """
    def _f(key: str) -> float | None:
        v = last_sync.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    t_self_a = _f("t_a_self_s")
    t_other_a = _f("t_a_from_b_s")
    t_self_b = _f("t_b_self_s")
    t_other_b = _f("t_b_from_a_s")
    if None in (t_self_a, t_other_a, t_self_b, t_other_b):
        return None

    alpha = t_other_a - t_self_a
    beta = t_other_b - t_self_b
    delta = ((t_self_a - t_other_b) + (t_other_a - t_self_b)) / 2.0
    d_prop = ((t_other_a - t_self_b) - (t_self_a - t_other_b)) / 2.0
    distance = SPEED_OF_SOUND_MS * d_prop
    within_sum = alpha + beta  # should ≈ 2 × d_prop
    within_diff = alpha - beta  # 2 × (emit-B-wall − emit-A-wall); burst separation
    consistency_err_s = within_sum - 2.0 * d_prop
    return {
        "t_self_a": t_self_a,
        "t_other_a": t_other_a,
        "t_self_b": t_self_b,
        "t_other_b": t_other_b,
        "alpha": alpha,
        "beta": beta,
        "delta": delta,
        "d_prop": d_prop,
        "distance": distance,
        "within_sum": within_sum,
        "within_diff": within_diff,
        "consistency_err_s": consistency_err_s,
    }


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

    def _fp(d: dict, key: str) -> float:
        v = d.get(key)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

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
        # Quick chirp path (no SyncResult) or no attempt yet.
        L.append("STATUS: no SyncResult (Quick Chirp path, or no attempt yet)")
        L.append(f"thresholds: mutual={mutual_threshold:.4f}  chirp={chirp_threshold:.4f}")
        L.append("")

        L.append("DEVICE SYNC STATE (authoritative — did phone register anchor?):")
        if not dev_by_cam:
            L.append("  (no devices in registry)")
        else:
            for cam in sorted(dev_by_cam):
                L.append(_dev_sync_line(cam))
        L.append("")

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

        L.append("LOG (last 20 entries):")
        for entry in logs[-20:]:
            ts = entry.get("ts", 0)
            source = str(entry.get("source") or "?").ljust(6)
            event = str(entry.get("event") or "?")
            detail = entry.get("detail") or {}
            detail_str = " ".join(f"{k}={_snip(v)}" for k, v in detail.items())
            L.append(f"  [{_fmt_ts(ts)}] {source} {event} {detail_str}")
        L.append("")

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
            actually_synced = d.get("time_synced", False) if d else False

            if actually_synced:
                if age > 10 or best < chirp_threshold:
                    L.append(f"  [OK]    Cam {cam}: SYNCED (telemetry age={age:.0f}s is from different attempt, ignore peaks)")
                else:
                    L.append(f"  [OK]    Cam {cam}: SYNCED  peak={best:.4f} margin={margin:.2f}x")
                continue

            if not t or age > 15:
                L.append(f"  [ERROR] Cam {cam}: NOT SYNCED, no fresh telemetry (age={age:.0f}s)")
                L.append(f"          → Phone may not have received sync_command, or already back in standby")
                continue

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

    # --- Mutual sync path ---
    run_id = last_sync.get("id", "?")
    L.append(f"run_id: {run_id}")
    L.append("")

    if last_sync.get("aborted"):
        reasons = last_sync.get("abort_reasons") or {}
        r_str = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        L.append(f"STATUS: ABORTED  ({r_str})")
    elif last_sync.get("delta_s") is not None:
        delta_ms = float(last_sync["delta_s"]) * 1000.0
        dist = float(last_sync.get("distance_m") or 0.0)
        L.append(f"STATUS: SOLVED  Δ={delta_ms:+.3f} ms  D={dist:.3f} m")
    else:
        L.append("STATUS: UNKNOWN")
    L.append(f"thresholds: mutual={mutual_threshold:.4f}  chirp={chirp_threshold:.4f}")
    L.append("")

    # --- Mutual sync math breakdown ---
    math_data = _mutual_math(last_sync)
    if math_data is None:
        L.append("MUTUAL-SYNC MATH:")
        L.append("  (missing one or more scalar timestamps — check that both A and B uploaded report_received")
        L.append("   with t_self_s + t_from_other_s populated)")
    else:
        L.append("MUTUAL-SYNC MATH (from per-role scalar timestamps):")
        L.append(f"  A:  t_self={math_data['t_self_a']:.6f}  t_from_other={math_data['t_other_a']:.6f}")
        L.append(f"  B:  t_self={math_data['t_self_b']:.6f}  t_from_other={math_data['t_other_b']:.6f}")
        L.append("")
        L.append(f"  α = t_other_A − t_self_A = {math_data['alpha']:+.6f} s   (within A's clock)")
        L.append(f"  β = t_other_B − t_self_B = {math_data['beta']:+.6f} s   (within B's clock)")
        L.append(f"  α − β                    = {math_data['within_diff']:+.6f} s   ≈ 2 × burst_separation")
        L.append(f"  α + β                    = {math_data['within_sum']:+.6f} s   ≈ 2 × d (propagation)")
        L.append("")
        L.append(f"  δ (clock offset A−B) = ((t_self_A − t_other_B) + (t_other_A − t_self_B)) / 2")
        L.append(f"                       = {math_data['delta']:+.6f} s")
        L.append(f"  d (one-way prop.)    = ((t_other_A − t_self_B) − (t_self_A − t_other_B)) / 2")
        L.append(f"                       = {math_data['d_prop']:+.6f} s")
        L.append(f"  distance             = 343 m/s × d = {math_data['distance']:+.3f} m")
        L.append("")
        L.append(f"  consistency check: (α + β) − 2d = {math_data['consistency_err_s']:+.6f} s")
        L.append(f"                    (0 = same d derived both within-clock and cross-clock; mismatch = wrong bursts matched)")
    L.append("")

    # --- Device sync state ---
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

    # --- Anomaly flags + diagnosis ---
    L.append("DIAGNOSIS:")
    flags: list[str] = []

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
        flags.append(f"[WARN]  ADC clipping on cam(s) {', '.join(clipping_cams)} — reduce speaker volume first")
    elif telemetry:
        flags.append(f"[OK]    No ADC clipping (max inp_peak={max_inp_peak:.3f})")

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
            flags.append(f"[WARN]  High noise floor: cfar_up avg={mean_cfar:.4f} — ambient noise?")
        else:
            flags.append(f"[OK]    Noise floor OK: cfar_up avg={mean_cfar:.4f}")

    # Math-derived anomalies (only if solved, math available)
    if math_data is not None and last_sync.get("delta_s") is not None:
        distance = math_data["distance"]
        consistency = math_data["consistency_err_s"]
        # Physical sanity: two hand-held phones on the same rig are within ~10 m.
        if abs(distance) > 30.0:
            flags.append(
                f"[FAIL]  |distance|={abs(distance):.1f} m is physically impossible for two phones on the same rig"
            )
            flags.append(
                f"        → one of (t_from_other_A, t_from_other_B) matched the WRONG chirp burst"
            )
            flags.append(
                f"        → Likely causes: emit_band label swap in iOS MutualSyncAudio, server band-filter picked wrong peak,"
            )
            flags.append(
                f"        → missed burst and server latched onto an echo/noise peak."
            )
        elif distance < -1.0:
            flags.append(
                f"[FAIL]  distance={distance:.3f} m < 0 — physically impossible"
            )
            flags.append(
                f"        → wrong burst picked on at least one side (see above hints)"
            )
        elif abs(distance) <= 15.0:
            flags.append(f"[OK]    distance={distance:.3f} m is plausible for two co-located phones")

        # Consistency check: |(α+β) - 2d| should be ~0 (machine-precision)
        if abs(consistency) > 0.001:
            flags.append(
                f"[WARN]  math consistency error {consistency*1000:+.3f} ms — scalar timestamps may be from mismatched bursts"
            )

        # Delta sanity: huge |δ| is normal (CLOCK_MONOTONIC boots are arbitrary); flag only if distance is otherwise fine.
        delta_ms_abs = abs(float(last_sync["delta_s"])) * 1000.0
        if delta_ms_abs > 10_000_000:  # > 10000 s
            flags.append(
                f"[INFO]  |δ|={delta_ms_abs/1000:.0f} s — phones' CLOCK_MONOTONIC boots far apart (expected, not a bug)"
            )

    # Aborts
    if last_sync.get("aborted"):
        abort_reasons = last_sync.get("abort_reasons") or {}
        timeout_cams = [k for k, v in abort_reasons.items() if "timeout" in str(v)]
        if timeout_cams:
            flags.append(f"[FAIL]  timeout on cam(s) {', '.join(timeout_cams)} — did not report within listen window")
        missing_cams = [k for k, v in abort_reasons.items() if "not_received" in str(v) or "missing" in str(v)]
        if missing_cams:
            flags.append(f"[FAIL]  no report_received from cam(s) {', '.join(missing_cams)}")

    for f in flags:
        L.append(f"  {f}")

    # Root-cause verdict
    L.append("")
    if last_sync.get("delta_s") is not None and math_data is not None:
        distance = math_data["distance"]
        if abs(distance) > 30.0 or distance < -1.0:
            L.append("  ROOT CAUSE: mutual-sync SOLVED mathematically but distance is impossible")
            L.append("              — the scalar inputs are self-consistent (see α, β, δ, d above)")
            L.append("              — but at least one side matched the wrong chirp")
            L.append("  NEXT STEPS:")
            L.append("    1. Check the LOG for emitted_band values on both report_received lines —")
            L.append("       if A reports emitted_band=A but the peak it reports is actually in B's band, that's the bug.")
            L.append("    2. Compare burst-emit times to sync_params.emit_a_at_s / emit_b_at_s — is the server")
            L.append("       searching the right window for each band?")
            L.append("    3. Download the per-run WAVs via /sync/audio/<run_id>_[AB].wav and inspect offline.")
        else:
            L.append("  ROOT CAUSE: solved plausibly; no action needed unless |Δ| drifts between runs.")
    elif last_sync.get("aborted"):
        L.append("  ROOT CAUSE: attempt aborted before both reports arrived.")
        L.append("  NEXT STEPS:")
        L.append("    1. Check LOG tail for the last report_received — which cam never reported?")
        L.append("    2. If telemetry for that cam shows low peaks → chirp not heard (volume / distance).")
        L.append("    3. If telemetry looks fine → iOS PSR gate rejected; lower mutual_sync_threshold or try again.")
    else:
        L.append("  ROOT CAUSE: unknown — inspect LOG entries above for abort_reason / missing fields.")

    return "\n".join(L)
