"""Phase 1 of multi-frame calibration accumulation.

Tests cover the MarkerAccumulatorStore + State wrapper methods. The
solve-and-persist policy lives in routes/calibration.py and gets its
own integration tests in phase 2.
"""
from __future__ import annotations

import numpy as np

from state import State
from state_calibration import (
    BUFFER_STALE_S,
    MAX_FAILURES,
    MIN_MARKERS_FOR_SOLVE,
)


def _marker(id_offset: int) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic ArUco-shaped (4,2) image corners + (4,3) world pts."""
    img = np.array(
        [[100 + id_offset, 100], [110 + id_offset, 100],
         [110 + id_offset, 110], [100 + id_offset, 110]],
        dtype=np.float32,
    )
    world = np.array(
        [[id_offset, 0, 0], [id_offset + 1, 0, 0],
         [id_offset + 1, 1, 0], [id_offset, 1, 0]],
        dtype=np.float32,
    )
    return img, world


class _SettableClock:
    """Settable clock — tests bump `now` to skip ahead, every State call
    in between sees the same value (unlike a step-list closure that gets
    drained by incidental time_fn calls)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_accumulate_unions_unique_ids(tmp_path):
    s = State(data_dir=tmp_path)
    new1, all1 = s.accumulate_calibration_markers("A", {0: _marker(0), 1: _marker(1)})
    new2, all2 = s.accumulate_calibration_markers("A", {1: _marker(1), 2: _marker(2)})

    assert new1 == [0, 1]
    assert all1 == [0, 1]
    # Re-seeing id=1 doesn't re-add; id=2 is new.
    assert new2 == [2]
    assert all2 == [0, 1, 2]


def test_buffer_summary_reports_progress(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {0: _marker(0), 5: _marker(5)})
    summary = s.calibration_buffer_summary("A")

    assert summary["marker_ids"] == [0, 5]
    assert summary["count"] == 2
    assert summary["ready"] is False
    assert summary["failure_count"] == 0
    assert summary["last_reproj_px"] is None


def test_ready_to_solve_threshold(tmp_path):
    s = State(data_dir=tmp_path)
    markers = {i: _marker(i) for i in range(MIN_MARKERS_FOR_SOLVE)}
    s.accumulate_calibration_markers("A", markers)
    summary = s.calibration_buffer_summary("A")

    assert summary["count"] == MIN_MARKERS_FOR_SOLVE
    assert summary["ready"] is True


def test_solve_ok_clears_buffer(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {i: _marker(i) for i in range(5)})
    s.record_calibration_solve_result("A", reproj_px=1.5, ok=True, status="ok")

    summary = s.calibration_buffer_summary("A")
    assert summary["count"] == 0  # buffer cleared on success
    assert summary["marker_ids"] == []


def test_solve_failure_keeps_buffer_increments_count(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {i: _marker(i) for i in range(5)})
    s.record_calibration_solve_result(
        "A", reproj_px=42.0, ok=False, status="reproj_too_high",
    )

    summary = s.calibration_buffer_summary("A")
    assert summary["count"] == 5  # markers retained for retry
    assert summary["failure_count"] == 1
    assert summary["last_reproj_px"] == 42.0
    assert summary["last_solve_status"] == "reproj_too_high"


def test_max_failures_auto_clears_buffer(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {i: _marker(i) for i in range(5)})
    for _ in range(MAX_FAILURES):
        s.record_calibration_solve_result(
            "A", reproj_px=99.0, ok=False, status="reproj_too_high",
        )

    summary = s.calibration_buffer_summary("A")
    # MAX_FAILURES hit — buffer GC'd; subsequent read returns fresh state.
    assert summary["count"] == 0
    assert summary["failure_count"] == 0


def test_stale_buffer_auto_clears(tmp_path):
    clock = _SettableClock(start=1000.0)
    s = State(data_dir=tmp_path, time_fn=clock)
    s.accumulate_calibration_markers("A", {0: _marker(0)})
    assert s.calibration_buffer_summary("A")["count"] == 1

    # Jump past BUFFER_STALE_S (5 min) — next read GCs the buffer.
    clock.now = 1000.0 + BUFFER_STALE_S + 1
    assert s.calibration_buffer_summary("A")["count"] == 0


def test_clear_calibration_buffer_explicit(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {0: _marker(0)})
    assert s.clear_calibration_buffer("A") is True
    assert s.calibration_buffer_summary("A")["count"] == 0
    # Idempotent on empty.
    assert s.clear_calibration_buffer("A") is False


def test_buffers_isolated_per_camera(tmp_path):
    s = State(data_dir=tmp_path)
    s.accumulate_calibration_markers("A", {0: _marker(0), 1: _marker(1)})
    s.accumulate_calibration_markers("B", {7: _marker(7)})

    assert s.calibration_buffer_summary("A")["marker_ids"] == [0, 1]
    assert s.calibration_buffer_summary("B")["marker_ids"] == [7]


def test_reset_rig_wipes_everything(tmp_path):
    from schemas import CalibrationSnapshot, IntrinsicsPayload
    from marker_registry import MarkerRecord

    s = State(data_dir=tmp_path)
    # Plant calibration + extended marker + accumulator state.
    snap = CalibrationSnapshot(
        camera_id="A",
        intrinsics=IntrinsicsPayload(fx=1000, fy=1000, cx=960, cy=540, distortion=None),
        homography=[1, 0, 0, 0, 1, 0, 0, 0, 1],
        image_width_px=1920,
        image_height_px=1080,
    )
    s.set_calibration(snap)
    s._marker_registry.upsert(MarkerRecord(
        marker_id=42, x_m=1.0, y_m=2.0, z_m=0.0,
        on_plate_plane=True, source_camera_ids=["A"], label="test",
    ))
    s.accumulate_calibration_markers("A", {0: _marker(0)})

    counts = s.reset_rig()
    assert counts["calibrations_removed"] == 1
    assert counts["extended_markers_removed"] == 1
    assert counts["buffers_cleared"] == 1
    assert s.calibrations() == {}
    assert s._marker_registry.all_records() == []
    assert s.calibration_buffer_summary("A")["count"] == 0


def test_all_summaries_returns_only_active_buffers(tmp_path):
    s = State(data_dir=tmp_path)
    assert s.all_calibration_buffer_summaries() == {}

    s.accumulate_calibration_markers("A", {0: _marker(0)})
    summaries = s.all_calibration_buffer_summaries()
    assert list(summaries.keys()) == ["A"]
    assert summaries["A"]["count"] == 1
