"""Self-calibrate K + distortion from stored session homographies.

Each stored pitch JSON carries an H fitted by on-device ArUco detection
against the known 6-marker pentagon world layout. Over many sessions the
camera pose varies (operator re-aims between runs), so each session's H
provides an independent Zhang constraint on the camera's true intrinsics.

This script treats stored H values as authoritative world→pixel mappings,
reprojects the 6 marker world coords through each H to synthesize (world,
pixel) correspondences, then calls cv2.calibrateCamera to jointly solve
for K + 5-coefficient distortion per camera.

Output: recovered K vs stored K, per-session Zhang residual before/after,
and triangulation re-run on s_50e743fc with recovered K to quantify the
Z-offset and ray-gap reduction.

Run:  uv run python3 recalibrate_from_sessions.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np

PITCHES_DIR = Path(__file__).parent / "data" / "pitches"

# Plate-corner world coordinates (copied from iOS CalibrationShared.swift
# so this script is self-contained — if the iOS layout changes, update here).
PLATE_W = 0.432
PLATE_SHOULDER_Y = 0.216
PLATE_TIP_Y = 0.432
MARKER_WORLD = {
    0: (-PLATE_W / 2, 0.0),              # FL
    1: (PLATE_W / 2, 0.0),               # FR
    2: (PLATE_W / 2, PLATE_SHOULDER_Y),  # RS
    3: (-PLATE_W / 2, PLATE_SHOULDER_Y), # LS
    4: (0.0, PLATE_TIP_Y),               # BT
    5: (0.0, 0.0),                       # MF
}


def build_K(fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)


def zhang_residual(H: np.ndarray, K: np.ndarray) -> tuple[float, float, float]:
    """Return (|r1|, |r2|, angle_deg) from the Zhang decomposition of H
    under intrinsics K. A rigid H gives (1, 1, 90°)."""
    M = np.linalg.inv(K) @ H
    lam = 1.0 / np.linalg.norm(M[:, 0])
    r1 = lam * M[:, 0]
    r2 = lam * M[:, 1]
    n1 = float(np.linalg.norm(r1))
    n2 = float(np.linalg.norm(r2))
    ang = float(np.degrees(np.arccos(np.clip(np.dot(r1, r2) / (n1 * n2), -1, 1))))
    return n1, n2, ang


def synthesize_correspondences(
    homographies: list[np.ndarray],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """For each H, reproject the 6 marker world coords to pixel space via
    H, producing (3D world point with Z=0, 2D pixel) per marker. Returns
    two lists suitable for cv2.calibrateCamera."""
    object_points = []
    image_points = []
    ids_sorted = sorted(MARKER_WORLD.keys())
    world_xy = np.array([MARKER_WORLD[i] for i in ids_sorted], dtype=np.float64)
    world_xyz = np.hstack([world_xy, np.zeros((len(ids_sorted), 1))]).astype(np.float32)
    for H in homographies:
        # Reproject world_xy → pixel via H (homogeneous)
        homog = np.hstack([world_xy, np.ones((len(ids_sorted), 1))])
        px = (H @ homog.T).T
        px = px[:, :2] / px[:, 2:3]
        object_points.append(world_xyz.copy())
        image_points.append(px.astype(np.float32))
    return object_points, image_points


def load_sessions_by_camera() -> dict[str, list[dict]]:
    by_cam: dict[str, list[dict]] = {"A": [], "B": []}
    for fn in sorted(os.listdir(PITCHES_DIR)):
        p = json.load(open(PITCHES_DIR / fn))
        cid = p.get("camera_id")
        if cid in by_cam and p.get("homography") and p.get("intrinsics"):
            by_cam[cid].append(p)
    return by_cam


def calibrate_camera(cam_id: str, pitches: list[dict]) -> dict:
    """Run cv2.calibrateCamera on synthesized correspondences from all
    stored H values for this camera. Returns a result bundle."""
    homographies = [np.array(p["homography"], dtype=np.float64).reshape(3, 3) for p in pitches]
    # Stored intrinsics from the most recent session (reasonable initial guess).
    intr = pitches[-1]["intrinsics"]
    K_init = build_K(intr["fx"], intr["fz"], intr["cx"], intr["cy"])
    W = pitches[-1]["image_width_px"]
    H_img = pitches[-1]["image_height_px"]

    obj_pts, img_pts = synthesize_correspondences(homographies)

    # Calibrate. Fix principal point to image center initially? No —
    # calibrateCamera solves for it. Use RATIONAL_MODEL off (we want the
    # 5-coef Brown model that matches server's undistortPoints).
    flags = cv2.CALIB_USE_INTRINSIC_GUESS
    ret, K_rec, dist_rec, rvecs, tvecs = cv2.calibrateCamera(
        obj_pts, img_pts, (W, H_img), K_init, None, flags=flags
    )

    return {
        "cam_id": cam_id,
        "n_views": len(homographies),
        "image_size": (W, H_img),
        "K_stored": K_init,
        "dist_stored": np.array(intr.get("distortion") or [0] * 5, dtype=np.float64),
        "K_recovered": K_rec,
        "dist_recovered": dist_rec.ravel(),
        "rms_reprojection_px": ret,
        "rvecs": rvecs,
        "tvecs": tvecs,
        "homographies": homographies,
        "pitches": pitches,
    }


def print_comparison(r: dict) -> None:
    K_s, K_r = r["K_stored"], r["K_recovered"]
    d_s, d_r = r["dist_stored"], r["dist_recovered"]
    print(f"\n{'='*72}")
    print(f"Cam {r['cam_id']}: {r['n_views']} views, image {r['image_size']}")
    print(f"  calibrateCamera RMS reprojection error: {r['rms_reprojection_px']:.4f} px")
    print(f"  intrinsics stored → recovered:")
    print(f"    fx:  {K_s[0,0]:>8.2f} → {K_r[0,0]:>8.2f}   Δ={K_r[0,0]-K_s[0,0]:+.2f} ({100*(K_r[0,0]-K_s[0,0])/K_s[0,0]:+.2f}%)")
    print(f"    fy:  {K_s[1,1]:>8.2f} → {K_r[1,1]:>8.2f}   Δ={K_r[1,1]-K_s[1,1]:+.2f} ({100*(K_r[1,1]-K_s[1,1])/K_s[1,1]:+.2f}%)")
    print(f"    cx:  {K_s[0,2]:>8.2f} → {K_r[0,2]:>8.2f}   Δ={K_r[0,2]-K_s[0,2]:+.2f}")
    print(f"    cy:  {K_s[1,2]:>8.2f} → {K_r[1,2]:>8.2f}   Δ={K_r[1,2]-K_s[1,2]:+.2f}")
    print(f"  distortion [k1,k2,p1,p2,k3]:")
    print(f"    stored:    [{', '.join(f'{x:+.5f}' for x in d_s[:5])}]")
    print(f"    recovered: [{', '.join(f'{x:+.5f}' for x in d_r[:5])}]")

    print(f"  per-session Zhang residual (skew from 90° and aniso %):")
    for p, H in zip(r["pitches"], r["homographies"]):
        sid = p["session_id"]
        _, _, ang_s = zhang_residual(H, K_s)
        _, _, ang_r = zhang_residual(H, K_r)
        n1_s, n2_s, _ = zhang_residual(H, K_s)
        n1_r, n2_r, _ = zhang_residual(H, K_r)
        aniso_s = abs(1 - n2_s / n1_s) * 100
        aniso_r = abs(1 - n2_r / n1_r) * 100
        print(
            f"    {sid}: skew {abs(90-ang_s):>5.2f}° → {abs(90-ang_r):>5.2f}°   "
            f"aniso {aniso_s:>4.2f}% → {aniso_r:>4.2f}%"
        )


def recover_pose_from_H(K: np.ndarray, H: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Standard Zhang decomposition (matches server/triangulate.py)."""
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
    if t[2] < 0:
        R = -R
        t = -t
        if np.linalg.det(R) < 0:
            R[:, 2] *= -1
    C = -R.T @ t
    return R, t, C


def triangulate_rays(C1, d1, C2, d2):
    v = C1 - C2
    A = np.array([[d1 @ d1, -(d1 @ d2)], [-(d1 @ d2), d2 @ d2]])
    rhs = np.array([-d1 @ v, d2 @ v])
    s, t = np.linalg.solve(A, rhs)
    return 0.5 * ((C1 + s * d1) + (C2 + t * d2)), float(np.linalg.norm((C1 + s * d1) - (C2 + t * d2)))


def evaluate_on_session(sid: str, res_a: dict, res_b: dict) -> None:
    pa_path = PITCHES_DIR / f"session_{sid}_A.json"
    pb_path = PITCHES_DIR / f"session_{sid}_B.json"
    if not (pa_path.exists() and pb_path.exists()):
        print(f"\nsession {sid} missing A or B JSON — skipping eval")
        return
    pa = json.load(open(pa_path))
    pb = json.load(open(pb_path))
    Ha = np.array(pa["homography"]).reshape(3, 3)
    Hb = np.array(pb["homography"]).reshape(3, 3)

    def run(K_a, dist_a, K_b, dist_b, label):
        Ra, _, Ca = recover_pose_from_H(K_a, Ha)
        Rb, _, Cb = recover_pose_from_H(K_b, Hb)
        fa = [f for f in pa["frames"] if f.get("ball_detected") and f.get("px") is not None]
        fb = [f for f in pb["frames"] if f.get("ball_detected") and f.get("px") is not None]
        anchor_a = pa["sync_anchor_timestamp_s"]
        anchor_b = pb["sync_anchor_timestamp_s"]
        b_times = np.array([f["timestamp_s"] - anchor_b for f in fb])
        zs, gaps = [], []
        for fA in fa:
            tr = fA["timestamp_s"] - anchor_a
            idx = int(np.argmin(np.abs(b_times - tr)))
            if abs(b_times[idx] - tr) > 1 / 120.0:
                continue
            fB = fb[idx]
            pa_px = np.array([[[fA["px"], fA["py"]]]], dtype=np.float64)
            pb_px = np.array([[[fB["px"], fB["py"]]]], dtype=np.float64)
            ua = cv2.undistortPoints(pa_px, K_a, dist_a).reshape(2)
            ub = cv2.undistortPoints(pb_px, K_b, dist_b).reshape(2)
            da = np.array([ua[0], ua[1], 1.0])
            da /= np.linalg.norm(da)
            db = np.array([ub[0], ub[1], 1.0])
            db /= np.linalg.norm(db)
            da_w = Ra.T @ da
            db_w = Rb.T @ db
            P, gap = triangulate_rays(Ca, da_w, Cb, db_w)
            zs.append(P[2])
            gaps.append(gap)
        zs = np.array(zs)
        gaps = np.array(gaps)
        print(
            f"  [{label}]  Ca={Ca.round(3)}  Cb={Cb.round(3)}\n"
            f"    Z: median={np.median(zs):.3f}  p10={np.percentile(zs,10):.3f}  p90={np.percentile(zs,90):.3f}\n"
            f"    gap: median={np.median(gaps):.3f}  p90={np.percentile(gaps,90):.3f}"
        )

    print(f"\n{'='*72}\nTriangulation eval on session {sid} (ball rolled on floor, expect Z ~ 0.03m)")
    run(res_a["K_stored"], res_a["dist_stored"], res_b["K_stored"], res_b["dist_stored"], "stored K")
    run(res_a["K_recovered"], res_a["dist_recovered"], res_b["K_recovered"], res_b["dist_recovered"], "recovered K")


def main() -> None:
    by_cam = load_sessions_by_camera()
    results = {}
    for cam_id in ["A", "B"]:
        pitches = by_cam[cam_id]
        if len(pitches) < 3:
            print(f"Cam {cam_id}: only {len(pitches)} sessions — skipping (need ≥3 for Zhang)")
            continue
        results[cam_id] = calibrate_camera(cam_id, pitches)
        print_comparison(results[cam_id])

    if "A" in results and "B" in results:
        evaluate_on_session("s_50e743fc", results["A"], results["B"])


if __name__ == "__main__":
    main()
