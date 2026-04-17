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
    """
    M = np.linalg.inv(K) @ H
    lam = 1.0 / np.linalg.norm(M[:, 0])
    r1 = lam * M[:, 0]
    r2 = lam * M[:, 1]
    t = lam * M[:, 2]
    r3 = np.cross(r1, r2)
    R_approx = np.column_stack([r1, r2, r3])
    # Orthonormalize (closest rotation in Frobenius norm).
    U, _, Vt = np.linalg.svd(R_approx)
    D = np.diag([1.0, 1.0, float(np.sign(np.linalg.det(U @ Vt)))])
    R = U @ D @ Vt

    # Camera must be in front of plate plane → world-point-in-cam Z > 0 for any
    # point on the plate. Test with world origin: z_cam = (R @ 0 + t)[2] = t[2] > 0.
    if t[2] < 0:
        R = -R
        t = -t
        # Re-fix determinant if sign flip broke it.
        if np.linalg.det(R) < 0:
            R[:, 2] *= -1

    return R, t


def camera_center_world(R_wc: np.ndarray, t_wc: np.ndarray) -> np.ndarray:
    """Camera optical center expressed in world coords. C = -R^T t."""
    return -R_wc.T @ t_wc


def angle_ray_cam(theta_x: float, theta_z: float) -> np.ndarray:
    """Unit ray in camera coords for BallDetector angles.

    BallDetector.swift uses:
      theta_x = atan2(u - cx, fx)
      theta_z = atan2(v - cy, fy)  (the 'fz' in Swift is fy in OpenCV pinhole)
    So the direction in camera coords is (tan θx, tan θz, 1) normalized.
    """
    d = np.array([np.tan(theta_x), np.tan(theta_z), 1.0])
    return d / np.linalg.norm(d)


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
) -> tuple[np.ndarray, float]:
    """Midpoint of the shortest segment connecting two 3D rays.

    Ray i : p(s) = C_i + s * d_i
    Returns (midpoint, gap) where gap is the distance between the two closest
    points (ideally 0 for perfect rays).
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
        # Parallel rays: return midpoint of the two origins as a fallback.
        mid = 0.5 * (C1 + C2)
        return mid, float(np.linalg.norm(v))
    s, t = np.linalg.solve(A, rhs)
    P1 = C1 + s * d1
    P2 = C2 + t * d2
    return 0.5 * (P1 + P2), float(np.linalg.norm(P1 - P2))
