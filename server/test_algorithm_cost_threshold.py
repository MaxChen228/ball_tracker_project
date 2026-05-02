"""Cost-absorption Phase 1 — algorithm-owned cost_threshold.

Covers the strict lookup helper and the AlgorithmEntry field. The
helper is the single source of truth that every triangulation /
segment-filter caller must route through; tests pin its contract
(known runnable id → entry value, ios_capture_time → constant,
unknown id → ValueError, NO silent fallback)."""
from __future__ import annotations

import pytest

import algorithms
from algorithms import (
    IOS_CAPTURE_TIME,
    IOS_CAPTURE_TIME_COST_THRESHOLD,
    V11_HSV_CC,
    cost_threshold_for_algorithm,
)


def test_v11_entry_has_cost_threshold_field():
    entry = algorithms.get(V11_HSV_CC)
    assert isinstance(entry.cost_threshold, float)
    # 0.5 is the v11 baseline (was the global PairingTuning default
    # pre-refactor; absorbed into the registry without behaviour change).
    assert entry.cost_threshold == 0.5


def test_lookup_v11_runnable_algorithm():
    assert cost_threshold_for_algorithm(V11_HSV_CC) == 0.5


def test_lookup_ios_capture_time_non_runnable():
    # Live data source uses the same cost_fn as v11 (both read aspect/fill
    # from HSV+CC mask candidates), so the threshold also matches.
    assert cost_threshold_for_algorithm(IOS_CAPTURE_TIME) == IOS_CAPTURE_TIME_COST_THRESHOLD
    assert cost_threshold_for_algorithm(IOS_CAPTURE_TIME) == 0.5


def test_lookup_unknown_id_raises():
    """No silent fallback (CLAUDE.md). Unknown ids must raise so a typo
    can never quietly route through a default cost. Error mentions
    known ids so callers get an actionable hint."""
    with pytest.raises(ValueError) as excinfo:
        cost_threshold_for_algorithm("v99_nonexistent")
    msg = str(excinfo.value)
    assert "v99_nonexistent" in msg
    assert "ios_capture_time" in msg
    assert "v11_hsv_cc" in msg


def test_lookup_invalid_format_raises():
    """Even garbage that doesn't match the slug regex must raise."""
    with pytest.raises(ValueError):
        cost_threshold_for_algorithm("Has-Hyphens-And-CAPS")
