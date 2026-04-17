"""Per-cycle 3D scene builder for the viewer.

Single-phone scope: each phone's homography → camera pose; every
ball-detected frame becomes a ray (origin = camera center, direction =
normalized ray in world frame, endpoint = ground-plane intersection or
capped at `_RAY_MAX_LEN_M`).

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
    cycle_number: int
    cameras: list[CameraView] = field(default_factory=list)
    rays: list[Ray] = field(default_factory=list)
    triangulated: list[dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_number": self.cycle_number,
            "cameras": [vars(c) for c in self.cameras],
            "rays": [vars(r) for r in self.rays],
            "triangulated": list(self.triangulated),
        }


# Arbitrary "big enough" length for rays that never cross Z=0 with positive
# parameter t (e.g. pointing upward or parallel to the plate plane).
_RAY_MAX_LEN_M = 10.0


def _ray_endpoint(origin: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """Extend a ray to either the ground plane (Z=0) along positive t, or a
    fixed max length. Visualisation-only; never used for geometry math."""
    dz = float(direction[2])
    if abs(dz) > 1e-9:
        t = -float(origin[2]) / dz
        if t > 0:
            return origin + t * direction
    return origin + _RAY_MAX_LEN_M * direction


def _world_ray(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
    R_wc: np.ndarray,
) -> np.ndarray:
    if dist_coeffs is not None and px is not None and py is not None:
        d_cam = undistorted_ray_cam(px, py, K, np.asarray(dist_coeffs, dtype=float))
    elif theta_x is not None and theta_z is not None:
        d_cam = angle_ray_cam(theta_x, theta_z)
    else:
        raise ValueError("frame has neither angles nor pixels")
    d_world = R_wc.T @ d_cam
    return d_world / np.linalg.norm(d_world)


def build_scene(
    cycle_number: int,
    pitches: dict[str, "PitchPayload"],
    triangulated: list["TriangulatedPoint"] | None = None,
) -> Scene:
    """Construct a renderable `Scene` for one cycle.

    `pitches`: camera_id → PitchPayload (subset of State.pitches filtered
               to this cycle). Cameras missing intrinsics or homography
               are silently skipped — they show up as no camera + no rays
               in the viewer, which is itself the diagnostic signal.
    `triangulated`: CycleResult.points if both cameras were paired.
    """
    scene = Scene(cycle_number=cycle_number)

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
        anchor = pitch.sync_anchor_timestamp_s
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
            endpoint = _ray_endpoint(C, d_world)
            scene.rays.append(
                Ray(
                    camera_id=cam_id,
                    t_rel_s=float(f.timestamp_s - anchor),
                    frame_index=f.frame_index,
                    origin=C.tolist(),
                    endpoint=endpoint.tolist(),
                )
            )

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
        ]

    return scene
