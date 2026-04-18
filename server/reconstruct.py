"""Per-cycle 3D scene builder for the viewer.

Single-phone scope: each phone's homography → camera pose; every
ball-detected frame whose ray actually crosses the plate plane (Z=0 at
positive parameter t) becomes a ray (origin = camera center, direction =
normalized ray in world frame, endpoint = ground-plane intersection).
Rays that point upward or parallel to the plate are dropped — they
almost always come from false-positive ball detections (sky, ceiling,
reflections) and drawing them as long poles swamps the viewer.

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cameras": [vars(c) for c in self.cameras],
            "rays": [vars(r) for r in self.rays],
            "triangulated": list(self.triangulated),
        }


def _ray_endpoint(origin: np.ndarray, direction: np.ndarray) -> np.ndarray | None:
    """Intersect a ray with the ground plane (Z=0) at positive parameter t.

    Returns the intersection point, or `None` if the ray points upward /
    parallel to the plate — such rays are caller-dropped so the viewer
    never renders "pole-into-the-sky" artifacts from false positives.
    Visualisation-only; never used for geometry math."""
    dz = float(direction[2])
    if abs(dz) <= 1e-9:
        return None
    t = -float(origin[2]) / dz
    if t <= 0:
        return None
    return origin + t * direction


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
            if endpoint is None:
                continue
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
