from __future__ import annotations

from sync_analysis import build_debug_report


def test_debug_report_marks_missing_log_timestamp_unknown() -> None:
    report = build_debug_report(
        last_sync=None,
        telemetry={},
        logs=[{"source": "ios", "event": "report_received", "detail": {"cam": "A"}}],
        mutual_threshold=0.5,
        chirp_threshold=0.1,
        devices=[],
    )

    assert "[unknown] ios    report_received cam=A" in report
    assert "[00:00:00.000]" not in report


def test_debug_report_does_not_treat_missing_telemetry_age_as_fresh() -> None:
    report = build_debug_report(
        last_sync=None,
        telemetry={"A": {"peak_up_peak": 0.2, "peak_down_peak": 0.1}},
        logs=[],
        mutual_threshold=0.5,
        chirp_threshold=0.1,
        devices=[{"camera_id": "A", "time_synced": False}],
    )

    assert "age=unknown  [STALE]" in report
    assert "NOT SYNCED, no fresh telemetry (age=unknown)" in report
    assert "age=0s" not in report
