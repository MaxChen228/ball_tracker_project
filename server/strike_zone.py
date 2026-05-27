from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Protocol, Sequence

import numpy as np


# Free-flight gravity (world frame; +Z = up). Mirrors `segmenter.G` so the
# strike judgment integrates the same physics as segment fitting.
_GRAVITY_Z_MPS2 = -9.81

# Hardball radius the user actually pitches (deep-blue 棒球, ~73mm diameter).
# Treated as an "any-part-of-the-ball-clips-the-zone" margin: zone bounds get
# expanded by this in X / Z when judging seg crossings, matching how MLB rules
# count strikes (any part of the ball over any part of the plate).
BALL_RADIUS_M = 0.0366


DEFAULT_BATTER_HEIGHT_CM = 175
MIN_BATTER_HEIGHT_CM = 120
MAX_BATTER_HEIGHT_CM = 220

ABS_BOTTOM_RATIO = 0.270
ABS_TOP_RATIO = 0.535
BASELINE_Z_BOTTOM_M = (DEFAULT_BATTER_HEIGHT_CM / 100.0) * ABS_BOTTOM_RATIO
BASELINE_Z_TOP_M = (DEFAULT_BATTER_HEIGHT_CM / 100.0) * ABS_TOP_RATIO

PLATE_WIDTH_M = 0.4318
PLATE_SHOULDER_Y_M = 0.216
PLATE_TIP_Y_M = 0.432

STRIKE_ZONE_X_HALF_M = PLATE_WIDTH_M / 2
STRIKE_ZONE_Y_FRONT_M = 0.0
STRIKE_ZONE_Y_BACK_M = PLATE_TIP_Y_M


@dataclass(frozen=True)
class StrikeZoneGeometry:
    batter_height_cm: int
    x_half_m: float
    y_front_m: float
    y_back_m: float
    z_bottom_m: float
    z_top_m: float
    z_height_m: float
    front_face: list[list[float]]
    back_face: list[list[float]]
    connectors: list[list[list[float]]]
    front_grid: list[list[list[float]]]

    def to_dict(self) -> dict:
        return {
            "batter_height_cm": self.batter_height_cm,
            "x_half_m": self.x_half_m,
            "y_front_m": self.y_front_m,
            "y_back_m": self.y_back_m,
            "z_bottom_m": self.z_bottom_m,
            "z_top_m": self.z_top_m,
            "z_height_m": self.z_height_m,
            "front_face": self.front_face,
            "back_face": self.back_face,
            "connectors": self.connectors,
            "front_grid": self.front_grid,
        }


def validate_batter_height_cm(value: int) -> int:
    if not isinstance(value, int):
        raise ValueError("batter_height_cm must be an int")
    if not (MIN_BATTER_HEIGHT_CM <= value <= MAX_BATTER_HEIGHT_CM):
        raise ValueError(
            f"batter_height_cm {value} out of range "
            f"[{MIN_BATTER_HEIGHT_CM}, {MAX_BATTER_HEIGHT_CM}]"
        )
    return value


def strike_zone_geometry_for_height(height_cm: int) -> StrikeZoneGeometry:
    height_cm = validate_batter_height_cm(height_cm)
    height_m = float(height_cm) / 100.0
    z_bottom_m = ABS_BOTTOM_RATIO * height_m
    z_top_m = ABS_TOP_RATIO * height_m
    z_height_m = z_top_m - z_bottom_m

    x_left = -STRIKE_ZONE_X_HALF_M
    x_right = STRIKE_ZONE_X_HALF_M
    y_front = STRIKE_ZONE_Y_FRONT_M
    y_back = STRIKE_ZONE_Y_BACK_M

    front_face = [
        [x_left, y_front, z_bottom_m],
        [x_right, y_front, z_bottom_m],
        [x_right, y_front, z_top_m],
        [x_left, y_front, z_top_m],
    ]
    back_face = [
        [x_left, y_back, z_bottom_m],
        [x_right, y_back, z_bottom_m],
        [x_right, y_back, z_top_m],
        [x_left, y_back, z_top_m],
    ]
    connectors = [
        [front_face[0], back_face[0]],
        [front_face[1], back_face[1]],
        [front_face[2], back_face[2]],
        [front_face[3], back_face[3]],
    ]

    x_thirds = [
        x_left + (2.0 * STRIKE_ZONE_X_HALF_M) / 3.0,
        x_left + 2.0 * (2.0 * STRIKE_ZONE_X_HALF_M) / 3.0,
    ]
    z_thirds = [
        z_bottom_m + z_height_m / 3.0,
        z_bottom_m + 2.0 * z_height_m / 3.0,
    ]
    front_grid = [
        [[x, y_front, z_bottom_m], [x, y_front, z_top_m]]
        for x in x_thirds
    ] + [
        [[x_left, y_front, z], [x_right, y_front, z]]
        for z in z_thirds
    ]

    return StrikeZoneGeometry(
        batter_height_cm=height_cm,
        x_half_m=STRIKE_ZONE_X_HALF_M,
        y_front_m=y_front,
        y_back_m=y_back,
        z_bottom_m=z_bottom_m,
        z_top_m=z_top_m,
        z_height_m=z_height_m,
        front_face=front_face,
        back_face=back_face,
        connectors=connectors,
        front_grid=front_grid,
    )


class StrikeJudgement(StrEnum):
    STRIKE = "strike"
    BALL = "ball"


@dataclass(frozen=True)
class StrikeJudgementResult:
    verdict: StrikeJudgement
    # Representative crossing point in world frame. For a STRIKE this is
    # the first sampled point inside the expanded zone; for a BALL this is
    # the front-face crossing (so callers can render "missed by Δx cm /
    # Δz cm" later). None when verdict is NO_PLATE_CROSS.
    crossing_x_m: float | None
    crossing_z_m: float | None
    crossing_t: float | None  # absolute time on the segment's clock


class _SegLike(Protocol):
    p0: Sequence[float]
    v0: Sequence[float]
    t_anchor: float


def judge_pitch_strike(
    segments: Iterable[_SegLike],
    geometry: StrikeZoneGeometry,
    *,
    ball_radius_m: float = BALL_RADIUS_M,
    sample_count: int = 64,
) -> StrikeJudgementResult:
    """Strike / ball verdict from seg0's ballistic curve, extended both
    forward and backward without clipping to the segment's observed
    `[t_start, t_end]` window.

    By design — operator semantics: the pitch's identity is seg0
    (release physics). Whether the curve, projected as an unbounded
    ballistic line in both directions, clips the strike-zone volume
    determines strike vs ball. Bounces / detection-loss / multi-segment
    splits are all irrelevant — only seg0's (p0, v0) define the verdict.
    Segs[1:] are intentionally ignored.

    Trajectory: p(τ) = p0 + v0·τ + ½·g·τ², gravity on +Z only.
    World frame (docs/reference/protocols.md): +Y points pitcher → catcher; plate
    front face at y=0, tip at y≈0.432.

    y(τ) is linear in τ, so the τ-interval where y∈[y_front, y_back] is
    given analytically (allowing negative τ — the curve extended back
    toward the pitcher counts equally). We sample x(τ), z(τ) over that
    interval and test against the zone expanded by ball_radius_m
    (any-part-of-the-ball clips the zone, MLB-style).

    Returns STRIKE on first inside sample; BALL otherwise. No third
    state — when v0[1] ≈ 0 (curve never crosses the plate), the
    operator's request is to call it BALL ("沒通過就是壞球").
    """
    p0_arr: np.ndarray
    v0_arr: np.ndarray
    seg_list = list(segments)
    if not seg_list:
        return StrikeJudgementResult(StrikeJudgement.BALL, None, None, None)
    seg0 = sorted(seg_list, key=lambda s: float(s.t_anchor))[0]

    p0_arr = np.asarray(seg0.p0, dtype=float)
    v0_arr = np.asarray(seg0.v0, dtype=float)
    if p0_arr.shape != (3,) or v0_arr.shape != (3,):
        raise ValueError("seg0.p0 and seg0.v0 must be 3-vectors")
    if sample_count < 2:
        raise ValueError("sample_count must be >= 2")
    t_anchor = float(seg0.t_anchor)

    vy = float(v0_arr[1])
    if abs(vy) < 1e-6:
        # Curve parallel to the plate — never enters the y-band, so it
        # cannot clip the zone. Operator says: not through → ball.
        return StrikeJudgementResult(StrikeJudgement.BALL, None, None, None)

    tau_front = (geometry.y_front_m - float(p0_arr[1])) / vy
    tau_back = (geometry.y_back_m - float(p0_arr[1])) / vy
    tau_lo = min(tau_front, tau_back)
    tau_hi = max(tau_front, tau_back)

    taus = np.linspace(tau_lo, tau_hi, sample_count)
    x = p0_arr[0] + v0_arr[0] * taus
    z = p0_arr[2] + v0_arr[2] * taus + 0.5 * _GRAVITY_Z_MPS2 * taus * taus

    x_half_r = geometry.x_half_m + ball_radius_m
    z_min = geometry.z_bottom_m - ball_radius_m
    z_max = geometry.z_top_m + ball_radius_m
    in_x = np.abs(x) <= x_half_r
    in_z = (z >= z_min) & (z <= z_max)
    inside = in_x & in_z

    if bool(inside.any()):
        idx = int(np.argmax(inside))
        return StrikeJudgementResult(
            StrikeJudgement.STRIKE,
            float(x[idx]),
            float(z[idx]),
            float(t_anchor + taus[idx]),
        )

    # BALL: report the front-face crossing (analytic, exact at y=y_front)
    # for telemetry / future "missed by Δ" rendering.
    x_front = float(p0_arr[0] + v0_arr[0] * tau_front)
    z_front = float(
        p0_arr[2]
        + v0_arr[2] * tau_front
        + 0.5 * _GRAVITY_Z_MPS2 * tau_front * tau_front
    )
    return StrikeJudgementResult(
        StrikeJudgement.BALL,
        x_front,
        z_front,
        float(t_anchor + tau_front),
    )


def instant_speed_kph(
    v0: Sequence[float],
    t_anchor: float,
    t: float,
) -> float:
    """|v(t)| in km/h for a ballistic segment, with gravity on +Z only.

    Mirrors the JS helper in `overlays_ui.OVERLAYS_RUNTIME_JS`; parity
    enforced by `test_strike_judge.py::test_judge_strike_js_parity`.
    """
    v0_arr = np.asarray(v0, dtype=float)
    if v0_arr.shape != (3,):
        raise ValueError("v0 must be a 3-vector")
    tau = float(t) - float(t_anchor)
    vx = float(v0_arr[0])
    vy = float(v0_arr[1])
    vz = float(v0_arr[2]) + _GRAVITY_Z_MPS2 * tau
    speed_mps = float(np.sqrt(vx * vx + vy * vy + vz * vz))
    return speed_mps * 3.6
