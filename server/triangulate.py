"""Stereo triangulation from two iPhone cameras looking at home plate.

World frame (from iPhone calibration):
  X = plate left/right, Y = plate depth (front→back), Z = plate normal (up)
Plate plane: Z = 0.

Camera frame (OpenCV pinhole):
  X = image right, Y = image down, Z = optical axis (forward)
Pixel projection: u = fx * X/Z + cx, v = fy * Y/Z + cy.
"""
from __future__ import annotations

import cv2
import numpy as np


def build_K(fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def recover_extrinsics(K: np.ndarray, H: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Decompose planar homography (world plate → image pixel) into (R_wc, t_wc).

    H maps world (X, Y, 1) → pixel (u, v, 1) up to scale, with h33 = 1.
    H = K [r1 r2 t] (Zhang's planar calibration).
    R_wc transforms a world vector into the camera frame.

    Only handles the overall-sign `H ↔ -H` ambiguity (flip when `t[2] < 0`).
    The "plate normal direction" ambiguity (two Zhang twins) is determined
    entirely by the sign of `r1 × r2`, which Zhang derives directly from
    the decomposition; there is no second branch to pick here. If the
    resulting camera center `C = -R^T t` lands below the plate (Z < 0),
    the root cause is on the caller's side — typically ArUco markers taped
    in a mirrored layout or with IDs swapped — not an ambiguity this
    routine can resolve. A warning is logged so the dashboard operator
    can tell at a glance that the calibration needs to be redone.
    """
    M = np.linalg.inv(K) @ H
    lam = 1.0 / np.linalg.norm(M[:, 0])
    r1 = lam * M[:, 0]
    r2 = lam * M[:, 1]
    t = lam * M[:, 2]
    r3 = np.cross(r1, r2)
    R_approx = np.column_stack([r1, r2, r3])
    U, _, Vt = np.linalg.svd(R_approx)
    D = np.diag([1.0, 1.0, float(np.sign(np.linalg.det(U @ Vt)))])
    R = U @ D @ Vt

    if abs(t[2]) < 1e-6:
        raise ValueError("degenerate homography")
    if t[2] < 0:
        R = -R
        t = -t
        if np.linalg.det(R) < 0:
            R[:, 2] *= -1

    return R, t


def camera_center_world(R_wc: np.ndarray, t_wc: np.ndarray) -> np.ndarray:
    """Camera optical center expressed in world coords. C = -R^T t."""
    return -R_wc.T @ t_wc


def undistorted_ray_cam(
    px: float, py: float, K: np.ndarray, dist_coeffs: np.ndarray
) -> np.ndarray:
    """Unit ray in camera coords from a raw (distorted) pixel.

    Uses cv2.undistortPoints to invert the lens distortion model and obtain
    the normalized camera-coord direction (x_n, y_n, 1). Returns the ray
    normalized to unit length.

    dist_coeffs: OpenCV-format 5-element array [k1, k2, p1, p2, k3].
    """
    pts = np.array([[[float(px), float(py)]]], dtype=np.float64)
    dist = np.asarray(dist_coeffs, dtype=np.float64).reshape(-1)
    undist = cv2.undistortPoints(pts, K.astype(np.float64), dist)
    x_n = float(undist[0, 0, 0])
    y_n = float(undist[0, 0, 1])
    d = np.array([x_n, y_n, 1.0])
    return d / np.linalg.norm(d)


def triangulate_rays(
    C1: np.ndarray, d1: np.ndarray, C2: np.ndarray, d2: np.ndarray
) -> tuple[np.ndarray | None, float]:
    """Midpoint of the shortest segment connecting two 3D rays.

    Ray i : p(s) = C_i + s * d_i
    Returns (midpoint, gap) where gap is the distance between the two closest
    points (ideally 0 for perfect rays).

    When the rays are (near-)parallel the 2×2 system is singular and no
    meaningful midpoint exists — returns (None, inf) so the caller can
    drop that frame pair instead of placing the ball at the arbitrary
    midpoint of the two camera centers.
    """
    v = C1 - C2
    a11 = float(np.dot(d1, d1))
    a22 = float(np.dot(d2, d2))
    a12 = float(np.dot(d1, d2))
    b1 = float(-np.dot(d1, v))
    b2 = float(np.dot(d2, v))
    A = np.array([[a11, -a12], [-a12, a22]])
    rhs = np.array([b1, b2])
    det = np.linalg.det(A)
    if abs(det) < 1e-12:
        # Parallel / near-parallel rays: no intersection geometry to midpoint.
        return None, float("inf")
    s, t = np.linalg.solve(A, rhs)
    P1 = C1 + s * d1
    P2 = C2 + t * d2
    return 0.5 * (P1 + P2), float(np.linalg.norm(P1 - P2))
