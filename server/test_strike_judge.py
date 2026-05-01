"""Strike-zone judgment tests + Python↔JS parity.

Operator-defined semantics: the verdict comes from seg0 ONLY, with seg0's
ballistic curve extended both forward and backward (no clipping to
seg0's [t_start, t_end] window). Bounces / multi-segment splits /
detection-loss are intentionally ignored — only seg0's (p0, v0) determine
the call. Two-state result: STRIKE or BALL.

Canonical Python: `strike_zone.judge_pitch_strike`. Client mirror:
`overlays_ui.OVERLAYS_RUNTIME_JS::BallTrackerOverlays.judgePitch`. Parity
pinned by `test_judge_pitch_js_parity` so the two implementations cannot
silently drift.
"""
from __future__ import annotations

from dataclasses import dataclass

import json
import shutil
import subprocess

import pytest

from overlays_ui import OVERLAYS_RUNTIME_JS
from strike_zone import (
    BALL_RADIUS_M,
    DEFAULT_BATTER_HEIGHT_CM,
    StrikeJudgement,
    instant_speed_kph,
    judge_pitch_strike,
    strike_zone_geometry_for_height,
)


@dataclass
class _Seg:
    """Test seg-like — duck-typed for `judge_pitch_strike`. t_start /
    t_end are present so the type matches viewer fixtures, but the
    judgment ignores them by design (curve extends both ways)."""
    p0: tuple[float, float, float]
    v0: tuple[float, float, float]
    t_anchor: float
    t_start: float = 0.0
    t_end: float = 0.0


def _geom(h: int = DEFAULT_BATTER_HEIGHT_CM):
    return strike_zone_geometry_for_height(h)


def _release_p0(*, x: float = 0.0, z: float = 1.7, y: float = -18.0):
    return (x, y, z)


def _v0_to_target(p0, target_xyz, t_flight_s: float):
    g = -9.81
    vx = (target_xyz[0] - p0[0]) / t_flight_s
    vy = (target_xyz[1] - p0[1]) / t_flight_s
    vz = (target_xyz[2] - p0[2] - 0.5 * g * t_flight_s * t_flight_s) / t_flight_s
    return (vx, vy, vz)


def _seg(p0, v0, *, t_anchor=0.0):
    return _Seg(p0=p0, v0=v0, t_anchor=t_anchor)


# --- Core verdicts ---


def test_strike_dead_center():
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    v0 = _v0_to_target(p0, (0.0, geom.y_front_m, z_mid), t_flight_s=0.5)
    res = judge_pitch_strike([_seg(p0, v0)], _geom())
    assert res.verdict == StrikeJudgement.STRIKE
    assert abs(res.crossing_x_m) < 0.05


def test_ball_wide_outside():
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    target_x = geom.x_half_m + 0.30
    v0 = _v0_to_target(p0, (target_x, geom.y_front_m, z_mid), t_flight_s=0.5)
    res = judge_pitch_strike([_seg(p0, v0)], geom)
    assert res.verdict == StrikeJudgement.BALL


def test_ball_above_zone():
    geom = _geom()
    p0 = _release_p0()
    target_z = geom.z_top_m + 0.20
    v0 = _v0_to_target(p0, (0.0, geom.y_front_m, target_z), t_flight_s=0.5)
    res = judge_pitch_strike([_seg(p0, v0)], geom)
    assert res.verdict == StrikeJudgement.BALL


def test_ball_below_zone():
    geom = _geom()
    p0 = _release_p0()
    target_z = geom.z_bottom_m - 0.20
    v0 = _v0_to_target(p0, (0.0, geom.y_front_m, target_z), t_flight_s=0.5)
    res = judge_pitch_strike([_seg(p0, v0)], geom)
    assert res.verdict == StrikeJudgement.BALL


def test_strike_corner_clip_via_ball_radius():
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    target_x = geom.x_half_m + 0.8 * BALL_RADIUS_M
    v0 = _v0_to_target(p0, (target_x, geom.y_front_m, z_mid), t_flight_s=0.5)
    res = judge_pitch_strike([_seg(p0, v0)], geom)
    assert res.verdict == StrikeJudgement.STRIKE


def test_ball_when_vy_zero():
    """Curve parallel to plate — never enters y-band → BALL."""
    geom = _geom()
    res = judge_pitch_strike(
        [_seg((0.0, geom.y_front_m, 1.0), (5.0, 0.0, 0.0))],
        geom,
    )
    assert res.verdict == StrikeJudgement.BALL


def test_no_segments_returns_ball():
    """Empty input is BALL (caller hides the badge separately)."""
    res = judge_pitch_strike([], _geom())
    assert res.verdict == StrikeJudgement.BALL


# --- seg0-only semantics: curve extends both ways, segs[1:] ignored ---


def test_extends_forward_past_seg_t_end():
    """Detection ends mid-flight (seg.t_end short of plate). Curve
    extension forward must still find the strike crossing."""
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    v0 = _v0_to_target(p0, (0.0, geom.y_front_m, z_mid), t_flight_s=0.5)
    seg = _Seg(p0=p0, v0=v0, t_anchor=0.0, t_start=0.0, t_end=0.1)  # cut short
    res = judge_pitch_strike([seg], geom)
    assert res.verdict == StrikeJudgement.STRIKE


def test_segs_after_seg0_are_ignored():
    """seg1 is wide, seg0 is dead-center. Verdict follows seg0 → STRIKE
    even though seg1's curve completely misses."""
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    v0_strike = _v0_to_target(p0, (0.0, geom.y_front_m, z_mid), t_flight_s=0.5)
    seg0 = _Seg(p0=p0, v0=v0_strike, t_anchor=0.0)
    # A wild seg1 that goes far right — must NOT alter the verdict.
    p1 = (0.0, -5.0, 1.5)
    v0_wide = _v0_to_target(p1, (geom.x_half_m + 1.0, 0.0, 0.0), t_flight_s=0.2)
    seg1 = _Seg(p0=p1, v0=v0_wide, t_anchor=0.5)
    res = judge_pitch_strike([seg0, seg1], geom)
    assert res.verdict == StrikeJudgement.STRIKE


def test_seg0_picked_by_earliest_t_anchor_not_input_order():
    """Defensive sort: caller passing segs out of order shouldn't flip
    the verdict."""
    geom = _geom()
    p0 = _release_p0()
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2
    v0_strike = _v0_to_target(p0, (0.0, geom.y_front_m, z_mid), 0.5)
    real_seg0 = _Seg(p0=p0, v0=v0_strike, t_anchor=0.0)
    # A noise seg with later t_anchor but passed first in the list.
    p_noise = (0.0, -5.0, 1.5)
    v_noise = _v0_to_target(p_noise, (geom.x_half_m + 1.0, 0.0, 0.0), 0.2)
    noise = _Seg(p0=p_noise, v0=v_noise, t_anchor=0.8)
    res = judge_pitch_strike([noise, real_seg0], geom)
    assert res.verdict == StrikeJudgement.STRIKE


# --- Instantaneous speed ---


def test_instant_speed_release_equals_release_kph():
    v0 = (10.0, 30.0, 0.0)
    speed = instant_speed_kph(v0, t_anchor=0.0, t=0.0)
    expected = ((10.0**2 + 30.0**2) ** 0.5) * 3.6
    assert speed == pytest.approx(expected, rel=1e-9)


def test_instant_speed_drops_with_gravity():
    v0 = (0.0, 0.0, 9.81)
    s_apex = instant_speed_kph(v0, 0.0, 1.0)
    assert s_apex == pytest.approx(0.0, abs=1e-6)


# --- Python ↔ JS parity ---


def _build_fixtures(geom):
    """Cover STRIKE / BALL plus the seg0-only contract."""
    p0 = (0.0, -18.0, 1.7)
    z_mid = (geom.z_bottom_m + geom.z_top_m) / 2

    v0_strike = list(_v0_to_target(p0, (0.0, 0.0, z_mid), 0.5))
    v0_wide = list(_v0_to_target(p0, (geom.x_half_m + 0.3, 0.0, z_mid), 0.5))

    return [
        {
            "name": "single_strike",
            "expect": "strike",
            "segments": [{
                "p0": list(p0), "v0": v0_strike, "t_anchor": 0.0,
                "t_start": 0.0, "t_end": 0.5,
            }],
        },
        {
            "name": "single_wide",
            "expect": "ball",
            "segments": [{
                "p0": list(p0), "v0": v0_wide, "t_anchor": 0.0,
                "t_start": 0.0, "t_end": 0.5,
            }],
        },
        {
            "name": "extends_past_short_seg_window",
            "expect": "strike",
            "segments": [{
                "p0": list(p0), "v0": v0_strike, "t_anchor": 0.0,
                "t_start": 0.0, "t_end": 0.1,  # detection cut short — curve still extends
            }],
        },
        {
            "name": "wild_seg1_ignored",
            "expect": "strike",
            "segments": [
                {
                    "p0": list(p0), "v0": v0_strike, "t_anchor": 0.0,
                    "t_start": 0.0, "t_end": 0.5,
                },
                {
                    "p0": [0.0, -5.0, 1.5],
                    "v0": list(_v0_to_target((0.0, -5.0, 1.5), (geom.x_half_m + 1.0, 0.0, 0.0), 0.2)),
                    "t_anchor": 0.5, "t_start": 0.5, "t_end": 0.7,
                },
            ],
        },
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_judge_pitch_js_parity():
    """Run identical fixtures through Python and JS; verdicts and
    crossing points must match. This is the only thing keeping the two
    implementations from drifting silently."""
    geom = _geom()
    fixtures = _build_fixtures(geom)
    zone_dict = {
        "x_half_m": geom.x_half_m,
        "y_front_m": geom.y_front_m,
        "y_back_m": geom.y_back_m,
        "z_bottom_m": geom.z_bottom_m,
        "z_top_m": geom.z_top_m,
    }
    js = (
        "globalThis.window = globalThis;\n"
        + OVERLAYS_RUNTIME_JS
        + """
const fixtures = JSON.parse(process.argv[1]);
const zone = JSON.parse(process.argv[2]);
const out = [];
for (const f of fixtures) {
  const r = window.BallTrackerOverlays.judgePitch(f.segments, zone);
  out.push({
    name: f.name,
    verdict: r ? r.verdict : null,
    crossing_x_m: r ? r.crossing_x_m : null,
    crossing_z_m: r ? r.crossing_z_m : null,
    crossing_t: r ? r.crossing_t : null,
  });
}
process.stdout.write(JSON.stringify(out));
"""
    )
    proc = subprocess.run(
        ["node", "-e", js, json.dumps(fixtures), json.dumps(zone_dict)],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    js_results = json.loads(proc.stdout)
    js_by_name = {r["name"]: r for r in js_results}

    for f in fixtures:
        py_segs = [
            _Seg(
                p0=tuple(s["p0"]), v0=tuple(s["v0"]),
                t_anchor=s["t_anchor"],
                t_start=s.get("t_start", 0.0), t_end=s.get("t_end", 0.0),
            )
            for s in f["segments"]
        ]
        py = judge_pitch_strike(py_segs, geom)
        assert py.verdict.value == f["expect"], (
            f"python disagrees with fixture {f['name']}: "
            f"py={py.verdict.value} expected={f['expect']}"
        )
        js_r = js_by_name[f["name"]]
        assert js_r["verdict"] == py.verdict.value, (
            f"verdict mismatch on {f['name']}: "
            f"py={py.verdict.value} js={js_r['verdict']}"
        )
        assert js_r["crossing_x_m"] == pytest.approx(py.crossing_x_m, abs=1e-9)
        assert js_r["crossing_z_m"] == pytest.approx(py.crossing_z_m, abs=1e-9)
        assert js_r["crossing_t"] == pytest.approx(py.crossing_t, abs=1e-9)


def test_overlays_runtime_exposes_helpers():
    """Refactor guard — bottom-left badge silently degrades to '—' on
    every pitch if these get dropped."""
    assert "judgePitch" in OVERLAYS_RUNTIME_JS
    assert "instantSpeedKph" in OVERLAYS_RUNTIME_JS
    assert "activeSegmentIndex" in OVERLAYS_RUNTIME_JS
