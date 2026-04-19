"""Mutual chirp sync solver.

Each phone emits a distinct audio chirp AND simultaneously listens to both
chirps via its own microphone, producing two mic-clock timestamps:

- `t_self_s`       — the phone heard its OWN chirp (self-hear)
- `t_from_other_s` — the phone heard the PEER's chirp (cross-hear)

Let Δ = A_clock − B_clock (positive means A is ahead of B) and D = inter-
phone distance. Ignoring speaker-to-mic distance (same-model iPhones,
<200 μs worst-case contribution):

    t_A_self      ≈ e_A
    t_B_self      ≈ e_B
    t_A_from_B     = e_B + Δ + D/c
    t_B_from_A     = e_A − Δ + D/c

Subtracting / adding eliminates e_A, e_B:

    Δ = [(t_A_from_B − t_B_self) − (t_B_from_A − t_A_self)] / 2
    D = c · [(t_A_from_B − t_B_self) + (t_B_from_A − t_A_self)] / 2

The subtraction `t_A_from_B − t_B_self` mixes A- and B-clock readings as
bare numbers; the algebra works because the clock-offset contribution
falls out symmetrically across the two cross terms. Temperature affects
D but NOT Δ — Δ is purely geometric/temporal and is sound-speed-free.
"""

from __future__ import annotations

from schemas import SyncReport, SyncResult


# Dry air at 20 °C. Good to ~0.5% over 0–40 °C; the D we care about is
# only a sanity check against homography baseline so temperature drift
# is tolerable.
DEFAULT_SOUND_SPEED_M_S: float = 343.0


def compute_mutual_sync(
    report_a: SyncReport,
    report_b: SyncReport,
    *,
    solved_at: float,
    sound_speed_m_s: float = DEFAULT_SOUND_SPEED_M_S,
) -> SyncResult:
    """Solve the two-way chirp exchange for (Δ, D).

    `report_a` must have role="A" and `report_b` must have role="B"; both
    must share the same `sync_id`. Raises ValueError on misuse — this is
    a pure math helper and the handler is responsible for validating the
    run is current and each role reports exactly once.
    """
    if report_a.role != "A" or report_b.role != "B":
        raise ValueError("report_a must be role A and report_b must be role B")
    if report_a.sync_id != report_b.sync_id:
        raise ValueError("reports belong to different sync runs")

    x_a = report_a.t_from_other_s - report_b.t_self_s
    x_b = report_b.t_from_other_s - report_a.t_self_s

    delta_s = (x_a - x_b) / 2.0
    distance_m = sound_speed_m_s * (x_a + x_b) / 2.0

    return SyncResult(
        id=report_a.sync_id,
        delta_s=delta_s,
        distance_m=distance_m,
        solved_at=solved_at,
        t_a_self_s=report_a.t_self_s,
        t_a_from_b_s=report_a.t_from_other_s,
        t_b_self_s=report_b.t_self_s,
        t_b_from_a_s=report_b.t_from_other_s,
    )
