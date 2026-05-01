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
