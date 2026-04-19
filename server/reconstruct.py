"""Per-cycle 3D scene builder for the viewer.

Single-phone scope: each phone's homography → camera pose; every
ball-detected frame becomes a ray (origin = camera center, direction =
normalized ray in world frame). Upward-pointing rays are kept — a ball
mid-flight above camera height is geometrically valid, and monocular
outlier rejection is deferred to the dual-camera trajectory-fit step.
The ray's visual endpoint is clamped to the plate plane (Z=0) when the
direction crosses it at positive t, otherwise extended along the ray a
scene-scale length so upward rays still render.

Ground trace (`scene.ground_traces[cam]`) is a separate projection of
the ray∩Z=0 intersection, ordered by anchor-relative time — the
"assume-ball-is-on-ground" single-camera proxy. Only frames whose ray
actually hits the plate contribute here.

Two-phone scope: the CycleResult's triangulated points are attached as
a 3D polyline — same `Scene` shape so the viewer renders either mode
without branching.

The output is a plain-dict `Scene` so the JSON endpoint and the Plotly
renderer can both consume it without sharing imports with FastAPI /
Plotly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    undistorted_ray_cam,
)

if TYPE_CHECKING:
    from main import PitchPayload, TriangulatedPoint
    from schemas import CalibrationSnapshot


# Maximum render distance from the camera (for rays / ground trace points)
# or from the world origin (for triangulated points). Anything beyond this
# is dropped from the scene entirely. Near-horizontal rays otherwise hit
# the plate plane tens of metres out, which blows up the Plotly auto-fit
# axis and makes the near-field trajectory unreadable.
_MAX_RENDER_DIST_M = 10.0


@dataclass
class CameraView:
    camera_id: str
    center_world: list[float]
    # Unit vectors pointing along the camera's local axes expressed in the
    # world frame. Used by the viewer to draw an RGB triad at the camera.
    axis_forward_world: list[float]   # cam +Z → world
    axis_right_world: list[float]     # cam +X → world
    axis_up_world: list[float]        # cam -Y (image down flipped) → world


@dataclass
class Ray:
    camera_id: str
    t_rel_s: float
    frame_index: int
    origin: list[float]
    endpoint: list[float]


@dataclass
class Scene:
    session_id: str
    cameras: list[CameraView] = field(default_factory=list)
    rays: list[Ray] = field(default_factory=list)
    triangulated: list[dict[str, float]] = field(default_factory=list)
    # Per-camera ground-plane trace: the (x, y, 0) intersection of every
    # ball-detected ray with the plate plane, ordered by anchor-relative
    # time. This is the single-camera proxy for a trajectory — it's what
    # a monocular view can recover without B pairing (equivalent to
    # "assume the ball is on the ground"). Keyed by camera_id so multi-
    # camera scenes still draw one trace per phone.
    ground_traces: dict[str, list[dict[str, float]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cameras": [vars(c) for c in self.cameras],
            "rays": [vars(r) for r in self.rays],
            "triangulated": list(self.triangulated),
            "ground_traces": {
                cam: list(trace) for cam, trace in self.ground_traces.items()
            },
        }


def _ray_ground_intersection(
    origin: np.ndarray, direction: np.ndarray
) -> np.ndarray | None:
    """Ray ∩ plate plane (Z=0) at positive parameter t. Returns the
    intersection point, or `None` if the ray points upward / parallel to
    the plate (no physical ground-projection exists in that case).
    Visualisation-only; never used for geometry math."""
    dz = float(direction[2])
    if abs(dz) <= 1e-9:
        return None
    t = -float(origin[2]) / dz
    if t <= 0:
        return None
    return origin + t * direction


def _ray_viz_endpoint(
    origin: np.ndarray, direction: np.ndarray, length: float
) -> np.ndarray:
    """Fixed-length projection along ray direction — used as the Ray's
    visual endpoint when no ground intersection exists. Keeps upward rays
    on screen instead of dropping them; direction carries the information
    the viewer needs."""
    return origin + length * direction


def _world_ray(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
    R_wc: np.ndarray,
) -> np.ndarray:
    if px is not None and py is not None:
        coeffs = (
            np.asarray(dist_coeffs, dtype=float)
            if dist_coeffs is not None
            else np.zeros(5, dtype=float)
        )
        d_cam = undistorted_ray_cam(px, py, K, coeffs)
    elif theta_x is not None and theta_z is not None:
        d_cam = angle_ray_cam(theta_x, theta_z)
    else:
        raise ValueError("frame has neither angles nor pixels")
    d_world = R_wc.T @ d_cam
    return d_world / np.linalg.norm(d_world)


def build_scene(
    session_id: str,
    pitches: dict[str, "PitchPayload"],
    triangulated: list["TriangulatedPoint"] | None = None,
) -> Scene:
    """Construct a renderable `Scene` for one session.

    `pitches`: camera_id → PitchPayload (subset of State.pitches filtered
               to this session). Cameras missing intrinsics or homography
               are silently skipped — they show up as no camera + no rays
               in the viewer, which is itself the diagnostic signal.
    `triangulated`: SessionResult.points if both cameras were paired.
    """
    scene = Scene(session_id=session_id)

    for cam_id in sorted(pitches.keys()):
        pitch = pitches[cam_id]
        if pitch.intrinsics is None or pitch.homography is None:
            continue
        intr = pitch.intrinsics
        K = build_K(intr.fx, intr.fz, intr.cx, intr.cy)
        H = np.array(pitch.homography, dtype=float).reshape(3, 3)
        R_wc, t_wc = recover_extrinsics(K, H)
        C = camera_center_world(R_wc, t_wc)

        R_inv = R_wc.T
        forward = R_inv @ np.array([0.0, 0.0, 1.0])
        right = R_inv @ np.array([1.0, 0.0, 0.0])
        up = R_inv @ np.array([0.0, -1.0, 0.0])

        scene.cameras.append(
            CameraView(
                camera_id=cam_id,
                center_world=C.tolist(),
                axis_forward_world=(forward / np.linalg.norm(forward)).tolist(),
                axis_right_world=(right / np.linalg.norm(right)).tolist(),
                axis_up_world=(up / np.linalg.norm(up)).tolist(),
            )
        )

        dist = intr.distortion
        anchor = pitch.sync_anchor_timestamp_s or 0.0
        # Scene-scale fallback length for rays that don't cross the plate —
        # scales with the camera's distance from the world origin so the
        # line stays visually comparable to ground-hit rays drawn by the
        # same camera. Floor at 5 m keeps it readable when the camera is
        # very close to the plate.
        viz_length = max(5.0, 2.0 * float(np.linalg.norm(C)))
        trace: list[dict[str, float]] = []
        for f in pitch.frames:
            if not f.ball_detected:
                continue
            has_angles = f.theta_x_rad is not None and f.theta_z_rad is not None
            has_pixels = f.px is not None and f.py is not None
            if not (has_angles or has_pixels):
                continue
            try:
                d_world = _world_ray(
                    f.theta_x_rad, f.theta_z_rad, f.px, f.py, K, dist, R_wc
                )
            except Exception:
                continue
            ground = _ray_ground_intersection(C, d_world)
            # Clamp endpoint distance from camera to `_MAX_RENDER_DIST_M`.
            # Near-horizontal rays would otherwise hit the plate tens of
            # metres out, blowing up Plotly's auto-fit axis. Clamping
            # (rather than skipping) preserves the ray's direction in the
            # viewer while bounding the scene. Ground-trace points only
            # contribute when the true intersection sits inside the
            # radius — a trace point at the clamp boundary would lie, and
            # mis-reads as "ball landed here".
            ground_within_radius = (
                ground is not None
                and float(np.linalg.norm(ground - C)) <= _MAX_RENDER_DIST_M
            )
            if ground_within_radius:
                endpoint = ground
            else:
                viz_len = (
                    _MAX_RENDER_DIST_M if ground is not None else min(viz_length, _MAX_RENDER_DIST_M)
                )
                endpoint = C + viz_len * d_world
            t_rel = float(f.timestamp_s - anchor)
            scene.rays.append(
                Ray(
                    camera_id=cam_id,
                    t_rel_s=t_rel,
                    frame_index=f.frame_index,
                    origin=C.tolist(),
                    endpoint=endpoint.tolist(),
                )
            )
            if ground_within_radius:
                trace.append(
                    {
                        "t_rel_s": t_rel,
                        "x": float(ground[0]),
                        "y": float(ground[1]),
                        "z": float(ground[2]),
                    }
                )
        if trace:
            trace.sort(key=lambda p: p["t_rel_s"])
            scene.ground_traces[cam_id] = trace

    if triangulated:
        scene.triangulated = [
            {
                "t_rel_s": float(p.t_rel_s),
                "x": float(p.x_m),
                "y": float(p.y_m),
                "z": float(p.z_m),
                "residual_m": float(p.residual_m),
            }
            for p in triangulated
            if (p.x_m ** 2 + p.y_m ** 2 + p.z_m ** 2) ** 0.5 <= _MAX_RENDER_DIST_M
        ]

    return scene


def _camera_view_from_intrinsics_and_homography(
    camera_id: str,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    homography: list[float],
) -> CameraView:
    """Shared pose-recovery path. Same math `build_scene` does for each
    pitch — centralised here so the calibration-preview scene renders the
    camera in the exact same world-frame pose a triangulation would."""
    K = build_K(fx, fy, cx, cy)
    H = np.array(homography, dtype=float).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H)
    C = camera_center_world(R_wc, t_wc)
    R_inv = R_wc.T
    forward = R_inv @ np.array([0.0, 0.0, 1.0])
    right = R_inv @ np.array([1.0, 0.0, 0.0])
    up = R_inv @ np.array([0.0, -1.0, 0.0])
    return CameraView(
        camera_id=camera_id,
        center_world=C.tolist(),
        axis_forward_world=(forward / np.linalg.norm(forward)).tolist(),
        axis_right_world=(right / np.linalg.norm(right)).tolist(),
        axis_up_world=(up / np.linalg.norm(up)).tolist(),
    )


def build_calibration_scene(
    calibrations: dict[str, "CalibrationSnapshot"],
) -> Scene:
    """Build a scene that only carries camera poses — no rays, no triangulated
    trajectory. Used by the dashboard canvas to preview whatever calibrations
    are currently persisted, independent of any session state. An empty
    `calibrations` dict yields an empty scene (canvas shows just the plate).
    """
    scene = Scene(session_id="_calibration")
    for cam_id in sorted(calibrations.keys()):
        cal = calibrations[cam_id]
        try:
            scene.cameras.append(
                _camera_view_from_intrinsics_and_homography(
                    camera_id=cam_id,
                    fx=cal.intrinsics.fx,
                    fy=cal.intrinsics.fz,
                    cx=cal.intrinsics.cx,
                    cy=cal.intrinsics.cy,
                    homography=cal.homography,
                )
            )
        except Exception:
            # Pose recovery can fail on a pathological homography (e.g. a
            # malformed save from the phone). Skip silently — the dashboard
            # shows "uncalibrated" for that slot rather than 500-ing the
            # whole page.
            continue
    return scene
