"""Unit tests for mutual chirp sync solver.

The solver is pure math on 4 timestamps; these tests construct synthetic
measurements from a known (Δ, D) and assert the solver recovers them
within floating-point noise, across sign and magnitude corners.
"""

from __future__ import annotations

import math

import pytest

from schemas import SyncReport
from sync_solver import DEFAULT_SOUND_SPEED_M_S, compute_mutual_sync


SYNC_ID = "sy_deadbeef"


def _make_reports(
    delta_s: float,
    distance_m: float,
    *,
    e_a: float = 100.0,
    e_b: float = 100.0,
    c: float = DEFAULT_SOUND_SPEED_M_S,
) -> tuple[SyncReport, SyncReport]:
    """Construct the pair of SyncReports that corresponds to a given
    ground-truth (delta, distance).

    Convention (matches sync_solver docstring):
        t_A_self     = e_A
        t_B_self     = e_B
        t_A_from_B   = e_B + delta + D/c
        t_B_from_A   = e_A − delta + D/c
    """
    tof = distance_m / c
    a = SyncReport(
        camera_id="A",
        sync_id=SYNC_ID,
        role="A",
        t_self_s=e_a,
        t_from_other_s=e_b + delta_s + tof,
        emitted_band="A",
    )
    b = SyncReport(
        camera_id="B",
        sync_id=SYNC_ID,
        role="B",
        t_self_s=e_b,
        t_from_other_s=e_a - delta_s + tof,
        emitted_band="B",
    )
    return a, b


def test_zero_offset_zero_distance() -> None:
    a, b = _make_reports(delta_s=0.0, distance_m=0.0)
    result = compute_mutual_sync(a, b, solved_at=0.0)
    assert result.delta_s == pytest.approx(0.0, abs=1e-12)
    assert result.distance_m == pytest.approx(0.0, abs=1e-9)


def test_known_offset_and_distance_roundtrip() -> None:
    """Δ=10 ms, D=2.5 m — the nominal rig scale."""
    delta = 0.010
    distance = 2.5
    a, b = _make_reports(delta_s=delta, distance_m=distance)
    result = compute_mutual_sync(a, b, solved_at=42.0)
    assert result.delta_s == pytest.approx(delta, abs=1e-9)
    assert result.distance_m == pytest.approx(distance, abs=1e-6)
    # Raw timestamps preserved so the viewer can render the exchange later.
    assert result.t_a_self_s == a.t_self_s
    assert result.t_b_self_s == b.t_self_s
    assert result.t_a_from_b_s == a.t_from_other_s
    assert result.t_b_from_a_s == b.t_from_other_s
    assert result.id == SYNC_ID
    assert result.solved_at == 42.0


def test_delta_sign_flips_when_a_and_b_swap() -> None:
    """Swapping the two input reports must flip the sign of `delta_s`
    and leave `distance_m` unchanged — classic symmetry check that
    catches the most common bug (sign of the subtraction)."""
    delta = 0.005
    distance = 3.0
    a, b = _make_reports(delta_s=delta, distance_m=distance)

    forward = compute_mutual_sync(a, b, solved_at=0.0)

    swapped_a = SyncReport(
        camera_id="A",
        sync_id=SYNC_ID,
        role="A",
        t_self_s=b.t_self_s,
        t_from_other_s=b.t_from_other_s,
        emitted_band="A",
    )
    swapped_b = SyncReport(
        camera_id="B",
        sync_id=SYNC_ID,
        role="B",
        t_self_s=a.t_self_s,
        t_from_other_s=a.t_from_other_s,
        emitted_band="B",
    )
    reversed_ = compute_mutual_sync(swapped_a, swapped_b, solved_at=0.0)

    assert reversed_.delta_s == pytest.approx(-forward.delta_s, abs=1e-12)
    assert reversed_.distance_m == pytest.approx(forward.distance_m, abs=1e-9)


def test_negative_delta() -> None:
    a, b = _make_reports(delta_s=-0.003, distance_m=1.2)
    result = compute_mutual_sync(a, b, solved_at=0.0)
    assert result.delta_s == pytest.approx(-0.003, abs=1e-9)
    assert result.distance_m == pytest.approx(1.2, abs=1e-6)


def test_custom_sound_speed_only_affects_distance() -> None:
    """Δ is sound-speed-free by construction. Running the solver with a
    different `c` on the same measurements must rescale D but leave Δ
    invariant."""
    delta = 0.002
    distance = 2.0
    a, b = _make_reports(delta_s=delta, distance_m=distance, c=343.0)

    hot_air = compute_mutual_sync(a, b, solved_at=0.0, sound_speed_m_s=360.0)
    cold_air = compute_mutual_sync(a, b, solved_at=0.0, sound_speed_m_s=330.0)

    assert hot_air.delta_s == pytest.approx(delta, abs=1e-9)
    assert cold_air.delta_s == pytest.approx(delta, abs=1e-9)
    # D scales linearly with c: the measurements encode D/c, so solving
    # with a different c just multiplies by the ratio.
    assert hot_air.distance_m / cold_air.distance_m == pytest.approx(360.0 / 330.0, rel=1e-9)


def test_rejects_wrong_roles() -> None:
    a, b = _make_reports(delta_s=0.0, distance_m=1.0)
    with pytest.raises(ValueError):
        compute_mutual_sync(b, a, solved_at=0.0)  # roles swapped


def test_rejects_mismatched_sync_ids() -> None:
    a = SyncReport(
        camera_id="A", sync_id="sy_aaaaaaaa", role="A",
        t_self_s=1.0, t_from_other_s=1.5, emitted_band="A",
    )
    b = SyncReport(
        camera_id="B", sync_id="sy_bbbbbbbb", role="B",
        t_self_s=2.0, t_from_other_s=1.2, emitted_band="B",
    )
    with pytest.raises(ValueError):
        compute_mutual_sync(a, b, solved_at=0.0)


def test_large_clock_difference_survives_float_precision() -> None:
    """Mic PTS values sit on `mach_absolute_time` → ~1e5 s after a phone
    has been up an hour. Verify the subtraction-first solver doesn't
    lose precision on large absolute timestamps (common Float64 pitfall
    when you naively multiply before subtracting)."""
    base = 1.0e5
    delta = 0.001  # 1 ms — typical inter-phone drift
    distance = 2.7
    a, b = _make_reports(
        delta_s=delta, distance_m=distance, e_a=base, e_b=base + 0.5
    )
    result = compute_mutual_sync(a, b, solved_at=0.0)
    assert result.delta_s == pytest.approx(delta, abs=1e-9)
    assert result.distance_m == pytest.approx(distance, abs=1e-4)
