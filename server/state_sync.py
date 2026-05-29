from __future__ import annotations

import logging
import secrets
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from schemas import (
    QuickSyncReport,
    QuickSyncResult,
    QuickSyncRun,
    RoleSyncTimes,
    RoleSyncTraces,
    SyncLogEntry,
    SyncReport,
    SyncResult,
    SyncRun,
)
from state_runtime import RuntimeSettingsStore
from sync_solver import compute_mutual_sync

logger = logging.getLogger("ball_tracker")


# Maximum wall time a mutual-sync run may stay active waiting for both
# phones to post their matched-filter reports. If one side fails to hear
# the peer (weak speaker, noise floor), the run is dropped and the
# dashboard surfaces "Sync timed out".
_SYNC_TIMEOUT_S = 8.0

# Window after a sync ends (solved OR aborted) during which late aborted
# reports can still merge traces into the run's SyncResult. The side that
# never heard both bands typically POSTs its abort report right around
# the server-side timeout, and without this grace window the trace data
# (our main post-mortem signal) gets silently dropped as "no_sync".
_SYNC_LATE_REPORT_GRACE_S = 5.0

# After a mutual sync solves (or times out), block subsequent /sync/start
# for this long. Prevents rapid-fire retries thrashing the phones through
# the state transition and gives the operator time to read the result.
_SYNC_COOLDOWN_S = 10.0

# Time-sync (single-listener chirp) command TTL. When the dashboard's
# CALIBRATE TIME button fires, each target camera gets a pending
# `sync_command: "start"` flag. A camera consumes it on its next
# heartbeat (one-shot), or the flag self-expires after this many
# seconds so a stale command doesn't fire if the operator gave up.
_SYNC_COMMAND_TTL_S = 10.0

# Legacy third-device chirp sync ids stay shareable for one listening
# window so two phones that begin 時間校正 a few seconds apart can still
# claim the same run id.
_TIME_SYNC_INTENT_WINDOW_S = 20.0

# Maximum server-observed age of a legacy chirp sync before it no longer
# counts as "ready" for a fresh arm.
_TIME_SYNC_MAX_AGE_S = 30.0

# Quick sync (single-emitter, N-listener) wall-time budget for all
# listeners to upload their WAV + post their detection. Longer than mutual
# sync's 8s because N≥3 phones each upload sequentially over LAN.
_QUICK_SYNC_TIMEOUT_S = 12.0

# Post-quick-sync cooldown blocking a fresh /sync/quick_start, mirroring the
# mutual-sync cooldown rationale (let the operator read the result, stop
# rapid-fire retries thrashing the phones).
_QUICK_SYNC_COOLDOWN_S = 10.0


def _new_sync_id() -> str:
    # Distinct `sy_` prefix so log lines immediately differentiate a
    # mutual-sync run id from a pitch session id at a glance.
    return "sy_" + secrets.token_hex(4)


@dataclass
class TimeSyncIntent:
    id: str
    started_at: float
    expires_at: float


class SyncCoordinator:
    """Sync / chirp subsystem state. Owns the mutual-chirp run lifecycle,
    the single-listener time-sync command dispatch, the diagnostic log
    ring, and per-cam live quick-chirp telemetry.

    The coordinator does NOT own its own lock — it expects the caller
    (State) to hold the shared lock whenever a `*_locked` helper or a
    public method that mutates internal state is invoked. Public methods
    without the `_locked` suffix take the lock themselves (matching the
    original State semantics). This mirrors how `DeviceRegistry` /
    `SessionProcessingState` are embedded.
    """

    def __init__(
        self,
        lock: Lock,
        time_fn: Callable[[], float],
        runtime_settings: RuntimeSettingsStore,
    ) -> None:
        self._lock = lock
        self._time_fn = time_fn
        self._runtime_settings = runtime_settings
        # Mutual chirp sync: at most one run active at a time.
        self._current_sync: SyncRun | None = None
        self._last_sync_result: SyncResult | None = None
        self._sync_cooldown_until: float = 0.0
        # Quick sync (single-emitter, N-listener): at most one run active.
        self._current_quick_sync: QuickSyncRun | None = None
        self._last_quick_sync_result: QuickSyncResult | None = None
        self._quick_sync_cooldown_until: float = 0.0
        # Ring buffer of diagnostic events from the mutual-sync flow.
        self._sync_log: deque[SyncLogEntry] = deque(maxlen=500)
        # Legacy third-device chirp sync intent + per-cam pending command
        # dispatch (one-shot flag consumed on next WS heartbeat).
        self._current_time_sync_intent: TimeSyncIntent | None = None
        self._sync_command_pending: dict[str, TimeSyncIntent] = {}
        # Per-cam "the id we EXPECT this cam to report back with after the
        # current attempt succeeds".
        self._expected_sync_id_per_cam: dict[str, str] = {}
        # Per-cam live quick-chirp telemetry (input RMS, peak, matched-
        # filter peaks, CFAR floors).
        self._sync_telemetry: dict[str, dict[str, Any]] = {}

    # ---- mutual sync accessors ----------------------------------------

    def current_sync(self) -> SyncRun | None:
        now = self._time_fn()
        with self._lock:
            self._check_sync_timeout_locked(now)
            return self._current_sync

    def check_sync_timeout_locked(self, now: float) -> None:
        """Public accessor for `_check_sync_timeout_locked` so external
        modules (ws_messages.py) can prune an expired run without poking
        the private method directly. Caller must already hold `self._lock`."""
        self._check_sync_timeout_locked(now)

    def current_sync_locked(self) -> SyncRun | None:
        """Public accessor for `_current_sync` so external modules
        (ws_messages.py) can read the active run without poking the
        private attribute. Caller must already hold `self._lock`."""
        return self._current_sync

    def last_sync_result(self) -> SyncResult | None:
        with self._lock:
            return self._last_sync_result

    def sync_cooldown_remaining_s(self) -> float:
        now = self._time_fn()
        with self._lock:
            return max(0.0, self._sync_cooldown_until - now)

    def clear_last_sync_result(self) -> None:
        with self._lock:
            self._last_sync_result = None

    # ---- sync log ------------------------------------------------------

    def log_sync_event(
        self, source: str, event: str, detail: dict[str, Any] | None = None
    ) -> None:
        entry = SyncLogEntry(
            ts=self._time_fn(),
            source=source,
            event=event,
            detail=detail or {},
        )
        with self._lock:
            self._sync_log.append(entry)
        logger.info(
            "sync_log source=%s event=%s detail=%s",
            source, event, entry.detail,
        )

    def sync_logs(self, limit: int = 200) -> list[SyncLogEntry]:
        with self._lock:
            return list(self._sync_log)[-limit:]

    # ---- telemetry -----------------------------------------------------

    def record_sync_telemetry(self, camera_id: str, telem: dict[str, Any]) -> None:
        now = self._time_fn()
        with self._lock:
            prior = self._sync_telemetry.get(camera_id, {})

            def roll_max(key: str) -> float | None:
                new_raw = telem.get(key)
                try:
                    new_v = None if new_raw is None else float(new_raw)
                except (TypeError, ValueError):
                    new_v = None
                old_raw = prior.get(f"peak_{key}")
                try:
                    old_v = None if old_raw is None else float(old_raw)
                except (TypeError, ValueError):
                    old_v = None
                if new_v is None:
                    return old_v
                if old_v is None:
                    return new_v
                return max(old_v, new_v)

            rolled = {
                f"peak_{k}": roll_max(k)
                for k in ("input_rms", "input_peak", "up_peak", "down_peak")
            }
            self._sync_telemetry[camera_id] = {
                "ts": now,
                **{k: telem.get(k) for k in (
                    "mode", "armed", "input_rms", "input_peak",
                    "up_peak", "down_peak", "cfar_up_floor",
                    "cfar_down_floor", "threshold", "pending_up",
                )},
                **rolled,
            }

    def reset_sync_telemetry_peaks(self, camera_ids: list[str] | None = None) -> None:
        with self._lock:
            targets = camera_ids if camera_ids is not None else list(self._sync_telemetry.keys())
            for cam in targets:
                rec = self._sync_telemetry.get(cam)
                if rec is None:
                    continue
                for k in ("input_rms", "input_peak", "up_peak", "down_peak"):
                    rec.pop(f"peak_{k}", None)

    def sync_telemetry_snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._time_fn()
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for cam, rec in self._sync_telemetry.items():
                r = dict(rec)
                r["age_s"] = max(0.0, now - float(rec.get("ts", now)))
                out[cam] = r
        return out

    # ---- legacy chirp intent (single-listener) -------------------------

    def _live_time_sync_intent_locked(self, now: float) -> TimeSyncIntent | None:
        intent = self._current_time_sync_intent
        if intent is None:
            return None
        if intent.expires_at <= now:
            self._current_time_sync_intent = None
            return None
        return intent

    def _claim_time_sync_intent_locked(
        self, now: float, *, force_new: bool = False,
    ) -> TimeSyncIntent:
        if not force_new:
            intent = self._live_time_sync_intent_locked(now)
            if intent is not None:
                return intent
        intent = TimeSyncIntent(
            id=_new_sync_id(),
            started_at=now,
            expires_at=now + _TIME_SYNC_INTENT_WINDOW_S,
        )
        self._current_time_sync_intent = intent
        return intent

    def claim_time_sync_intent(self) -> TimeSyncIntent:
        now = self._time_fn()
        with self._lock:
            return self._claim_time_sync_intent_locked(now)

    def clear_time_sync_intent_locked(self) -> None:
        self._current_time_sync_intent = None

    # ---- single-listener command dispatch ------------------------------

    def dispatch_sync_commands_locked(
        self, now: float, targets: list[str],
    ) -> list[str]:
        """Write pending flags for `targets`, mint a fresh intent id, prune
        expired entries. Returns the sorted+deduped dispatched list.
        `targets == []` still refreshes the intent reference to None."""
        intent = (
            self._claim_time_sync_intent_locked(now, force_new=True)
            if targets else None
        )
        dispatched: list[str] = []
        for cam in sorted(set(targets)):
            assert intent is not None
            self._sync_command_pending[cam] = TimeSyncIntent(
                id=intent.id,
                started_at=intent.started_at,
                expires_at=now + _SYNC_COMMAND_TTL_S,
            )
            dispatched.append(cam)
        stale = [
            c for c, pending in self._sync_command_pending.items()
            if pending.expires_at <= now
        ]
        for c in stale:
            del self._sync_command_pending[c]
        return dispatched

    def consume_sync_command(self, camera_id: str) -> tuple[str | None, str | None]:
        now = self._time_fn()
        with self._lock:
            pending = self._sync_command_pending.pop(camera_id, None)
        if pending is None:
            return None, None
        if pending.expires_at <= now:
            return None, None
        return "start", pending.id

    def pending_sync_commands(self) -> dict[str, str]:
        now = self._time_fn()
        with self._lock:
            return {
                cam: "start"
                for cam, pending in self._sync_command_pending.items()
                if pending.expires_at > now
            }

    def pending_sync_command_ids(self) -> dict[str, str]:
        now = self._time_fn()
        with self._lock:
            return {
                cam: pending.id
                for cam, pending in self._sync_command_pending.items()
                if pending.expires_at > now
            }

    def set_expected_sync_id(self, camera_ids: list[str], sync_id: str) -> None:
        with self._lock:
            for cam in camera_ids:
                self._expected_sync_id_per_cam[cam] = sync_id

    def expected_sync_id_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._expected_sync_id_per_cam)

    # ---- mutual-sync run machinery -------------------------------------

    def _check_sync_timeout_locked(self, now: float) -> None:
        s = self._current_sync
        if s is None:
            return
        if now - s.started_at > _SYNC_TIMEOUT_S:
            received = sorted(s.reports.keys())
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="timeout",
                detail={"id": s.id, "reports_received": received},
            ))
            logger.warning(
                "sync timeout id=%s received=%s", s.id, received
            )
            self._last_sync_result = self._build_aborted_result_locked(s, now)
            self._current_sync = None
            self._sync_cooldown_until = now + _SYNC_COOLDOWN_S

    def _merge_late_abort_report_locked(
        self, report: SyncReport, now: float,
    ) -> None:
        result = self._last_sync_result
        if result is None:
            return
        updates: dict[str, Any] = {}
        reasons = dict(result.abort_reasons)
        if report.abort_reason:
            reasons[report.role] = report.abort_reason
        else:
            reasons.setdefault(report.role, "aborted_late")
        updates["abort_reasons"] = reasons
        updates["aborted"] = True
        # Merge this role's late telemetry into the existing dicts.
        # Existing model_copy `update=` replaces top-level keys outright,
        # so we rebuild the two dicts here rather than relying on a
        # deep merge that pydantic doesn't perform.
        times = dict(result.times_by_role)
        traces = dict(result.traces_by_role)
        # Explicit None-branch instead of `dict.get(...) or default()`
        # so a stored RoleSyncTimes with all-None fields (legitimate
        # partial-report state) doesn't silently get replaced by a
        # fresh empty instance — CLAUDE.md 禁 `a or b` fallback.
        existing_times = times.get(report.role)
        if existing_times is None:
            existing_times = RoleSyncTimes()
        existing_traces = traces.get(report.role)
        if existing_traces is None:
            existing_traces = RoleSyncTraces()
        time_updates: dict[str, Any] = {}
        if report.t_self_s is not None:
            time_updates["t_self_s"] = report.t_self_s
        if report.t_from_other_s is not None:
            time_updates["t_from_other_s"] = report.t_from_other_s
        if time_updates:
            times[report.role] = existing_times.model_copy(update=time_updates)
        trace_updates: dict[str, Any] = {}
        if report.trace_self is not None:
            trace_updates["self_trace"] = report.trace_self
        if report.trace_other is not None:
            trace_updates["other_trace"] = report.trace_other
        if trace_updates:
            traces[report.role] = existing_traces.model_copy(update=trace_updates)
        updates["times_by_role"] = times
        updates["traces_by_role"] = traces
        self._last_sync_result = result.model_copy(update=updates)
        self._sync_log.append(SyncLogEntry(
            ts=now, source="server", event="report_late_merged",
            detail={
                "id": report.sync_id,
                "role": report.role,
                "reason": report.abort_reason,
                "had_traces": {
                    "self": report.trace_self is not None,
                    "other": report.trace_other is not None,
                },
            },
        ))
        logger.info(
            "sync report_late_merged id=%s role=%s reason=%s",
            report.sync_id, report.role, report.abort_reason,
        )
        thr = self._runtime_settings.mutual_sync_threshold
        if report.role == "A":
            self._log_trace_post_mortem_locked(
                report.sync_id, "A.self", report.trace_self, thr)
            self._log_trace_post_mortem_locked(
                report.sync_id, "A.other", report.trace_other, thr)
        else:
            self._log_trace_post_mortem_locked(
                report.sync_id, "B.self", report.trace_self, thr)
            self._log_trace_post_mortem_locked(
                report.sync_id, "B.other", report.trace_other, thr)

    def _log_trace_post_mortem_locked(
        self, run_id: str, label: str,
        trace: list | None, threshold: float,
    ) -> None:
        if not trace:
            self._sync_log.append(SyncLogEntry(
                ts=self._time_fn(), source="server", event="post_mortem",
                detail={"id": run_id, "stream": label, "status": "no_trace"},
            ))
            logger.info("sync post_mortem id=%s stream=%s status=no_trace", run_id, label)
            return
        peaks = sorted(float(s.peak) for s in trace)
        n = len(peaks)
        best = peaks[-1]
        median = peaks[n // 2]
        p90 = peaks[min(n - 1, int(n * 0.9))]
        t_best = None
        for s in trace:
            if float(s.peak) == best:
                t_best = float(s.t)
                break
        margin = best / threshold if threshold > 0 else 0.0
        detail = {
            "id": run_id, "stream": label, "status": "ok",
            "n": n, "best": round(best, 4), "t_best": round(t_best or 0.0, 3),
            "noise_median": round(median, 4), "noise_p90": round(p90, 4),
            "threshold": round(threshold, 4),
            "margin_x_threshold": round(margin, 3),
        }
        self._sync_log.append(SyncLogEntry(
            ts=self._time_fn(), source="server", event="post_mortem",
            detail=detail,
        ))
        logger.info(
            "sync post_mortem id=%s stream=%s best=%.3f@%.2fs noise_med=%.3f p90=%.3f thr=%.3f margin=%.2fx n=%d",
            run_id, label, best, t_best or 0.0, median, p90, threshold, margin, n,
        )

    def _build_aborted_result_locked(
        self, run: SyncRun, solved_at: float,
    ) -> SyncResult:
        rep_a = run.reports.get("A")
        rep_b = run.reports.get("B")
        reasons: dict[str, str] = {}
        if rep_a is not None and rep_a.aborted and rep_a.abort_reason:
            reasons["A"] = rep_a.abort_reason
        if rep_b is not None and rep_b.aborted and rep_b.abort_reason:
            reasons["B"] = rep_b.abort_reason
        if rep_a is None:
            reasons.setdefault("A", "no_report")
        if rep_b is None:
            reasons.setdefault("B", "no_report")
        thr = self._runtime_settings.chirp_detect_threshold
        self._log_trace_post_mortem_locked(
            run.id, "A.self",  rep_a.trace_self if rep_a else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "A.other", rep_a.trace_other if rep_a else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "B.self",  rep_b.trace_self if rep_b else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "B.other", rep_b.trace_other if rep_b else None, thr)
        times_by_role: dict[str, RoleSyncTimes] = {}
        traces_by_role: dict[str, RoleSyncTraces] = {}
        for role, rep in (("A", rep_a), ("B", rep_b)):
            if rep is None:
                continue
            times_by_role[role] = RoleSyncTimes(
                t_self_s=rep.t_self_s,
                t_from_other_s=rep.t_from_other_s,
            )
            traces_by_role[role] = RoleSyncTraces(
                self_trace=rep.trace_self,
                other_trace=rep.trace_other,
            )
        return SyncResult(
            id=run.id,
            delta_s=None,
            distance_m=None,
            solved_at=solved_at,
            times_by_role=times_by_role,
            aborted=True,
            abort_reasons=reasons,
            traces_by_role=traces_by_role,
        )

    def start_sync_locked(
        self, now: float, online_ids: list[str], *,
        session_armed: bool,
    ) -> tuple[SyncRun | None, str | None]:
        """Attempt to begin a mutual-sync run. Precondition priority
        (matches the endpoint's response mapping):
          1. An armed session → `"session_armed"`
          2. Sync already in progress → `"sync_in_progress"`
          3. Cooldown window still active → `"cooldown"`
          4. Fewer than 2 cameras online → `"devices_missing"`
        Caller pre-evaluates `session_armed` (session lookup takes its own
        lock) and passes `online_ids`. Caller must hold the shared lock."""
        self._check_sync_timeout_locked(now)
        reject_reason: str | None = None
        if session_armed:
            reject_reason = "session_armed"
        elif self._current_sync is not None:
            reject_reason = "sync_in_progress"
        elif now < self._sync_cooldown_until:
            reject_reason = "cooldown"
        elif len(online_ids) < 2:
            reject_reason = "devices_missing"
        if reject_reason is not None:
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="start_rejected",
                detail={"reason": reject_reason, "online": online_ids},
            ))
            logger.info(
                "sync start rejected reason=%s online=%s",
                reject_reason, online_ids,
            )
            return None, reject_reason
        run = SyncRun(id=_new_sync_id(), started_at=now)
        self._current_sync = run
        # Fresh listen window → drop prior run's result so the "Last"
        # chip doesn't show stale ABORTED / timing from a previous
        # attempt.
        self._last_sync_result = None
        self._sync_log.append(SyncLogEntry(
            ts=now, source="server", event="start",
            detail={"id": run.id, "online": online_ids},
        ))
        logger.info("sync start id=%s online=%s", run.id, online_ids)
        return run, None

    def record_sync_report(
        self, report: SyncReport,
    ) -> tuple[SyncRun | None, SyncResult | None, str | None]:
        now = self._time_fn()
        with self._lock:
            self._check_sync_timeout_locked(now)
            run = self._current_sync
            if run is None:
                if (
                    report.aborted
                    and self._last_sync_result is not None
                    and self._last_sync_result.id == report.sync_id
                    and now - self._last_sync_result.solved_at <= _SYNC_LATE_REPORT_GRACE_S
                ):
                    self._merge_late_abort_report_locked(report, now)
                    return None, None, None
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="report_no_sync",
                    detail={"role": report.role, "sync_id": report.sync_id},
                ))
                logger.info(
                    "sync report no active sync role=%s sync_id=%s",
                    report.role, report.sync_id,
                )
                return None, None, "no_sync"
            if run.id != report.sync_id:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="report_stale",
                    detail={
                        "role": report.role,
                        "posted_sync_id": report.sync_id,
                        "current_sync_id": run.id,
                    },
                ))
                logger.info(
                    "sync report stale role=%s posted=%s current=%s",
                    report.role, report.sync_id, run.id,
                )
                return run, None, "stale_sync_id"
            run.reports[report.role] = report
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="report_received",
                detail={
                    "role": report.role,
                    "t_self_s": report.t_self_s,
                    "t_from_other_s": report.t_from_other_s,
                    "emitted_band": report.emitted_band,
                    "received_so_far": sorted(run.reports.keys()),
                },
            ))
            fmt_ts = lambda v: "None" if v is None else f"{float(v):.6f}"
            logger.info(
                "sync report received id=%s role=%s t_self=%s t_from_other=%s aborted=%s",
                run.id, report.role,
                fmt_ts(report.t_self_s), fmt_ts(report.t_from_other_s),
                bool(report.aborted),
            )
            if not run.complete:
                return run, None, None
            rep_a = run.reports["A"]
            rep_b = run.reports["B"]
            any_aborted = (
                rep_a.aborted or rep_b.aborted
                or rep_a.t_self_s is None or rep_a.t_from_other_s is None
                or rep_b.t_self_s is None or rep_b.t_from_other_s is None
            )
            if any_aborted:
                result = self._build_aborted_result_locked(run, now)
                self._last_sync_result = result
                self._current_sync = None
                self._sync_cooldown_until = now + _SYNC_COOLDOWN_S
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="aborted",
                    detail={
                        "id": result.id,
                        "reasons": result.abort_reasons,
                        "had_traces": {
                            "a_self": rep_a.trace_self is not None,
                            "a_other": rep_a.trace_other is not None,
                            "b_self": rep_b.trace_self is not None,
                            "b_other": rep_b.trace_other is not None,
                        },
                    },
                ))
                logger.warning(
                    "sync aborted id=%s reasons=%s",
                    result.id, result.abort_reasons,
                )
                return None, result, None
            result = compute_mutual_sync(rep_a, rep_b, solved_at=now)
            result = result.model_copy(update={
                "traces_by_role": {
                    "A": RoleSyncTraces(
                        self_trace=rep_a.trace_self,
                        other_trace=rep_a.trace_other,
                    ),
                    "B": RoleSyncTraces(
                        self_trace=rep_b.trace_self,
                        other_trace=rep_b.trace_other,
                    ),
                },
            })
            self._last_sync_result = result
            self._current_sync = None
            self._sync_cooldown_until = now + _SYNC_COOLDOWN_S
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="solved",
                detail={
                    "id": result.id,
                    "delta_s": result.delta_s,
                    "distance_m": result.distance_m,
                },
            ))
            logger.info(
                "sync solved id=%s delta_s=%.6f distance_m=%.3f",
                result.id, result.delta_s, result.distance_m,
            )
            return None, result, None

    # ---- quick-sync run machinery (single-emitter, N-listener) ----------

    def current_quick_sync(self) -> QuickSyncRun | None:
        now = self._time_fn()
        with self._lock:
            self._check_quick_sync_timeout_locked(now)
            return self._current_quick_sync

    def last_quick_sync_result(self) -> QuickSyncResult | None:
        with self._lock:
            return self._last_quick_sync_result

    def quick_sync_cooldown_remaining_s(self) -> float:
        now = self._time_fn()
        with self._lock:
            return max(0.0, self._quick_sync_cooldown_until - now)

    def start_quick_sync_locked(
        self, now: float, *, emitter_cam_id: str, online_ids: list[str],
        session_armed: bool,
    ) -> tuple[QuickSyncRun | None, str | None]:
        """Begin a quick-sync run. Precondition priority mirrors mutual:
          1. armed session → "session_armed"
          2. quick sync already running → "sync_in_progress"
          3. cooldown active → "cooldown"
          4. emitter not online → "emitter_offline"
        The emitter is itself a listener (self-hear anchor = zero point), so
        listeners = all online cams (emitter included). N=1 is allowed: a
        lone emitter still self-hears and trivially solves with delta 0 —
        useful for a single-cam smoke test. Caller holds the shared lock."""
        self._check_quick_sync_timeout_locked(now)
        reject_reason: str | None = None
        if session_armed:
            reject_reason = "session_armed"
        elif self._current_quick_sync is not None:
            reject_reason = "sync_in_progress"
        elif now < self._quick_sync_cooldown_until:
            reject_reason = "cooldown"
        elif emitter_cam_id not in online_ids:
            reject_reason = "emitter_offline"
        if reject_reason is not None:
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="quick_start_rejected",
                detail={
                    "reason": reject_reason,
                    "emitter": emitter_cam_id,
                    "online": online_ids,
                },
            ))
            logger.info(
                "quick_sync start rejected reason=%s emitter=%s online=%s",
                reject_reason, emitter_cam_id, online_ids,
            )
            return None, reject_reason
        listeners = sorted(online_ids)
        run = QuickSyncRun(
            id=_new_sync_id(),
            emitter_cam_id=emitter_cam_id,
            listener_cam_ids=listeners,
            started_at=now,
        )
        self._current_quick_sync = run
        self._last_quick_sync_result = None
        self._sync_log.append(SyncLogEntry(
            ts=now, source="server", event="quick_start",
            detail={"id": run.id, "emitter": emitter_cam_id, "listeners": listeners},
        ))
        logger.info(
            "quick_sync start id=%s emitter=%s listeners=%s",
            run.id, emitter_cam_id, listeners,
        )
        return run, None

    def _check_quick_sync_timeout_locked(self, now: float) -> None:
        s = self._current_quick_sync
        if s is None:
            return
        if now - s.started_at > _QUICK_SYNC_TIMEOUT_S:
            received = sorted(s.reports.keys())
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="quick_timeout",
                detail={"id": s.id, "reports_received": received},
            ))
            logger.warning(
                "quick_sync timeout id=%s received=%s", s.id, received
            )
            # Solve with whatever arrived: emitter self-hear present →
            # partial solve (missing listeners disabled); emitter absent →
            # abort (no zero point).
            self._last_quick_sync_result = self._solve_quick_sync_locked(s, now)
            self._current_quick_sync = None
            self._quick_sync_cooldown_until = now + _QUICK_SYNC_COOLDOWN_S

    def _solve_quick_sync_locked(
        self, run: QuickSyncRun, solved_at: float,
    ) -> QuickSyncResult:
        """Difference every listener's anchor against the emitter's. The
        emitter's self-hear anchor is the run's zero point; without it the
        run aborts (no common reference). A listener with no anchor (missed
        chirp or never reported before timeout) is disabled — recorded in
        `missing_cam_ids`, absent from `deltas_s`. No silent fallback: a
        missing emitter aborts loudly rather than picking some other cam as
        an implicit zero."""
        emitter = run.emitter_cam_id
        emitter_rep = run.reports.get(emitter)
        emitter_anchor = (
            emitter_rep.anchor_pts_s
            if emitter_rep is not None and not emitter_rep.aborted
            else None
        )
        abort_reasons: dict[str, str] = {}
        if emitter_anchor is None:
            if emitter_rep is None:
                abort_reasons[emitter] = "emitter_no_report"
            elif emitter_rep.aborted:
                abort_reasons[emitter] = emitter_rep.abort_reason or "emitter_aborted"
            else:
                abort_reasons[emitter] = "emitter_no_self_hear"
            self._sync_log.append(SyncLogEntry(
                ts=solved_at, source="server", event="quick_aborted",
                detail={"id": run.id, "reasons": abort_reasons},
            ))
            logger.warning(
                "quick_sync aborted id=%s emitter=%s reason=%s",
                run.id, emitter, abort_reasons[emitter],
            )
            return QuickSyncResult(
                id=run.id,
                emitter_cam_id=emitter,
                solved_at=solved_at,
                listener_cam_ids=run.listener_cam_ids,
                aborted=True,
                abort_reasons=abort_reasons,
                missing_cam_ids=[],
            )
        anchors: dict[str, float] = {}
        deltas: dict[str, float] = {}
        missing: list[str] = []
        for cam in run.listener_cam_ids:
            rep = run.reports.get(cam)
            anchor = (
                rep.anchor_pts_s
                if rep is not None and not rep.aborted
                else None
            )
            if anchor is None:
                missing.append(cam)
                abort_reasons[cam] = (
                    (rep.abort_reason or "no_anchor") if rep is not None
                    else "no_report"
                )
                continue
            anchors[cam] = float(anchor)
            deltas[cam] = float(anchor) - float(emitter_anchor)
        self._sync_log.append(SyncLogEntry(
            ts=solved_at, source="server", event="quick_solved",
            detail={
                "id": run.id,
                "emitter": emitter,
                "solved_cams": sorted(deltas.keys()),
                "missing_cams": sorted(missing),
                "deltas_s": {c: round(d, 6) for c, d in deltas.items()},
            },
        ))
        logger.info(
            "quick_sync solved id=%s emitter=%s solved=%s missing=%s",
            run.id, emitter, sorted(deltas.keys()), sorted(missing),
        )
        return QuickSyncResult(
            id=run.id,
            emitter_cam_id=emitter,
            solved_at=solved_at,
            listener_cam_ids=run.listener_cam_ids,
            anchors_pts_s=anchors,
            deltas_s=deltas,
            aborted=False,
            abort_reasons=abort_reasons,
            missing_cam_ids=sorted(missing),
        )

    def record_quick_sync_report(
        self, report: QuickSyncReport,
    ) -> tuple[QuickSyncRun | None, QuickSyncResult | None, str | None]:
        """Ingest one listener's quick-sync detection. Returns
        `(run_after, result, reason)`:
          - reason == "no_sync": no active quick-sync run
          - reason == "stale_sync_id": report's sync_id != current run
          - result is not None: all listeners reported → run solved/aborted
          - run_after is not None (result None): more listeners still pending
        """
        now = self._time_fn()
        with self._lock:
            self._check_quick_sync_timeout_locked(now)
            run = self._current_quick_sync
            if run is None:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="quick_report_no_sync",
                    detail={"camera_id": report.camera_id, "sync_id": report.sync_id},
                ))
                logger.info(
                    "quick_sync report no active sync cam=%s sync_id=%s",
                    report.camera_id, report.sync_id,
                )
                return None, None, "no_sync"
            if run.id != report.sync_id:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="quick_report_stale",
                    detail={
                        "camera_id": report.camera_id,
                        "posted_sync_id": report.sync_id,
                        "current_sync_id": run.id,
                    },
                ))
                logger.info(
                    "quick_sync report stale cam=%s posted=%s current=%s",
                    report.camera_id, report.sync_id, run.id,
                )
                return run, None, "stale_sync_id"
            run.reports[report.camera_id] = report
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="quick_report_received",
                detail={
                    "camera_id": report.camera_id,
                    "anchor_pts_s": report.anchor_pts_s,
                    "aborted": report.aborted,
                    "received_so_far": sorted(run.reports.keys()),
                },
            ))
            logger.info(
                "quick_sync report received id=%s cam=%s anchor=%s aborted=%s",
                run.id, report.camera_id,
                "None" if report.anchor_pts_s is None else f"{report.anchor_pts_s:.6f}",
                bool(report.aborted),
            )
            if not run.complete:
                return run, None, None
            result = self._solve_quick_sync_locked(run, now)
            self._last_quick_sync_result = result
            self._current_quick_sync = None
            self._quick_sync_cooldown_until = now + _QUICK_SYNC_COOLDOWN_S
            return None, result, None
