"""Unit tests for geometry primitives in `triangulate.py`.

Covers the two bugs flagged in the audit:

* `triangulate_rays` near-parallel fallback — must return (None, inf)
  so the caller can drop the frame instead of placing the ball at the
  arbitrary midpoint of the two camera centers.
* `recover_extrinsics` sign-flip robustness — near-degenerate
  homographies (camera near plate plane) must raise ValueError rather
  than silently flipping via an unreliable `sign(t[2])` check.
"""
from __future__ import annotations

import numpy as np
import pytest

from pairing import scale_pitch_to_video_dims
from schemas import IntrinsicsPayload, PitchPayload
from triangulate import (
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
)


# --------------------------- helpers -----------------------------------------


def _look_at(pos: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 0.0, 1.0])):
    """Build (R_wc, t_wc) for a camera at `pos` looking at `target`.

    Camera frame (OpenCV): X right, Y down, Z forward.
    Duplicated from test_server._look_at to keep this module standalone.
    """
    z_cam = target - pos
    z_cam /= np.linalg.norm(z_cam)
    y_cam = -up - np.dot(-up, z_cam) * z_cam
    y_cam /= np.linalg.norm(y_cam)
    x_cam = np.cross(y_cam, z_cam)
    R_cw = np.column_stack([x_cam, y_cam, z_cam])  # cam → world
    R_wc = R_cw.T
    t_wc = -R_wc @ pos
    return R_wc, t_wc


def _homography_from_pose(K: np.ndarray, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Compose world-plate → pixel homography from a calibrated pose.

    H = K [r1 r2 t], then normalise h33 = 1 to match the iPhone-side
    convention `PitchPayload.homography`.
    """
    H = K @ np.column_stack([R[:, 0], R[:, 1], t])
    return H / H[2, 2]


# --------------------------- triangulate_rays --------------------------------


def test_triangulate_rays_converging_returns_exact_point():
    """Two rays that provably cross at P_true → midpoint == P_true, gap ≈ 0."""
    P_true = np.array([0.5, 1.2, 1.8])
    C1 = np.array([2.0, -3.0, 1.0])
    C2 = np.array([-2.0, -3.0, 1.0])
    d1 = (P_true - C1) / np.linalg.norm(P_true - C1)
    d2 = (P_true - C2) / np.linalg.norm(P_true - C2)

    P_rec, gap = triangulate_rays(C1, d1, C2, d2)

    assert P_rec is not None
    np.testing.assert_allclose(P_rec, P_true, atol=1e-9)
    assert gap < 1e-9


def test_triangulate_rays_parallel_returns_none_inf():
    """Identical direction from distinct origins → no intersection geometry,
    must refuse to fabricate a midpoint."""
    C1 = np.array([1.5, 0.0, 1.2])
    C2 = np.array([-1.5, 0.0, 1.2])
    d = np.array([0.0, 1.0, 0.0])  # both rays shoot straight forward (Y)

    P_rec, gap = triangulate_rays(C1, d, C2, d)

    assert P_rec is None
    assert gap == float("inf")


def test_triangulate_rays_anti_parallel_returns_none_inf():
    """Opposing directions also drive det(A) ≈ 0; same contract holds."""
    C1 = np.array([0.0, -1.0, 1.0])
    C2 = np.array([0.0, 1.0, 1.0])
    d1 = np.array([0.0, 1.0, 0.0])
    d2 = np.array([0.0, -1.0, 0.0])

    P_rec, gap = triangulate_rays(C1, d1, C2, d2)

    assert P_rec is None
    assert gap == float("inf")


# --------------------------- recover_extrinsics ------------------------------


def test_recover_extrinsics_happy_path_round_trip():
    """Sanity: a well-posed homography round-trips through the decomposition."""
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)
    C = np.array([1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R, t = _look_at(C, target)
    H = _homography_from_pose(K, R, t)

    R_rec, t_rec = recover_extrinsics(K, H)

    np.testing.assert_allclose(R_rec, R, atol=1e-8)
    np.testing.assert_allclose(t_rec, t, atol=1e-8)
    # Recovered camera center matches the rig.
    np.testing.assert_allclose(camera_center_world(R_rec, t_rec), C, atol=1e-8)


def test_recover_extrinsics_sign_flip_restores_positive_tz():
    """An H whose raw Zhang decomposition yields t[2] < 0 must trigger the
    sign-flip branch and emerge with t[2] > 0 (camera in front of plate).

    Negating the entire homography leaves the induced point correspondence
    unchanged (H and -H map world → image identically up to a scale of -1),
    but flips the sign of the recovered (R, t). That lets us test the flip
    branch without manufacturing an impossible pose.
    """
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)
    C = np.array([1.8, -2.5, 1.2])
    target = np.array([0.0, 0.15, 0.0])
    R, t = _look_at(C, target)
    H = _homography_from_pose(K, R, t)
    # Sanity: the un-flipped version already decomposes to t[2] > 0, so we
    # need to negate H to force the raw decomposition through the < 0 branch.
    H_negated = -H

    R_rec, t_rec = recover_extrinsics(K, H_negated)

    assert t_rec[2] > 0, "sign-flip branch should restore camera in front of plate"
    # Recovered pose equals the original (sign-flip undid the negation).
    np.testing.assert_allclose(R_rec, R, atol=1e-8)
    np.testing.assert_allclose(t_rec, t, atol=1e-8)


def test_recover_extrinsics_degenerate_small_tz_raises():
    """A homography whose decomposition yields |t[2]| < 1e-6 is geometrically
    a camera (nearly) on the plate plane — the sign of t[2] is numerically
    unreliable, so the function must raise ValueError instead of flipping."""
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)
    # Camera with optical axis (z_cam = +Y) orthogonal to its own position
    # vector (0, 1, 0) · (1, 1e-7, 2) ≈ 1e-7 → t[2] ≈ -1e-7, well under 1e-6.
    C = np.array([1.0, 1e-7, 2.0])
    target = np.array([1.0, 1.0 + 1e-7, 2.0])
    R, t = _look_at(C, target)
    assert abs(t[2]) < 1e-6, "precondition: pose must decompose to |t[2]| < 1e-6"
    H = _homography_from_pose(K, R, t)

    with pytest.raises(ValueError, match="degenerate homography"):
        recover_extrinsics(K, H)


def test_recover_extrinsics_threshold_boundary_passes():
    """Just above the 1e-6 threshold must still decompose successfully —
    guard against the check being too aggressive for legitimate poses."""
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    K = build_K(fx, fy, cx, cy)
    # |t[2]| ≈ 1e-4, comfortably above the threshold.
    C = np.array([1.0, 1e-4, 2.0])
    target = np.array([1.0, 1.0 + 1e-4, 2.0])
    R, t = _look_at(C, target)
    assert abs(t[2]) > 1e-6
    H = _homography_from_pose(K, R, t)

    # Should not raise.
    R_rec, t_rec = recover_extrinsics(K, H)
    assert t_rec[2] > 0


# --------------------------- scale_pitch_to_video_dims -----------------------


def _pitch_at(
    width: int,
    height: int,
    intrinsics: IntrinsicsPayload | None,
    homography: list[float] | None,
) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id="s_cafebabe",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        intrinsics=intrinsics,
        homography=homography,
        image_width_px=width,
        image_height_px=height,
    )


def test_scale_pitch_noop_when_dims_match():
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=540.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1920, 1080, intr, H)

    out = scale_pitch_to_video_dims(pitch, (1920, 1080))

    assert out is pitch  # identity returned without copy


def test_scale_pitch_noop_when_calibration_missing():
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=540.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, intr, H)

    out = scale_pitch_to_video_dims(pitch, None)

    assert out is pitch


def test_scale_pitch_noop_when_intrinsics_missing():
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, None, H)

    out = scale_pitch_to_video_dims(pitch, (1920, 1080))

    assert out is pitch


def test_scale_pitch_1080_to_720_scales_intrinsics_proportionally():
    intr = IntrinsicsPayload(
        fx=1600.0, fy=1600.0, cx=960.0, cy=540.0,
        distortion=[0.1, -0.05, 0.001, -0.002, 0.02],
    )
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, intr, H)

    out = scale_pitch_to_video_dims(pitch, (1920, 1080))

    sx = 1280 / 1920  # 0.6667
    sy = 720 / 1080  # 0.6667
    assert out.intrinsics is not None
    assert out.intrinsics.fx == pytest.approx(1600.0 * sx)
    assert out.intrinsics.fy == pytest.approx(1600.0 * sy)
    assert out.intrinsics.cx == pytest.approx(960.0 * sx)
    assert out.intrinsics.cy == pytest.approx(540.0 * sy)
    # Distortion is dimensionless; must be unchanged.
    assert out.intrinsics.distortion == [0.1, -0.05, 0.001, -0.002, 0.02]


def test_scale_pitch_scales_homography_first_two_rows_and_renormalises():
    """Scaling by S = diag(sx, sy, 1) must leave the last row (normalised to 1)
    intact after the h33 renormalisation."""
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=540.0)
    H_in = [2.0, 0.1, 30.0, 0.2, 3.0, 40.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, intr, H_in)

    out = scale_pitch_to_video_dims(pitch, (1920, 1080))

    assert out.homography is not None
    H_out = np.array(out.homography).reshape(3, 3)
    # h33 convention preserved (inputs normalised, scale preserves it).
    assert H_out[2, 2] == pytest.approx(1.0)
    # First row × sx, second × sy.
    sx = sy = 1280 / 1920
    np.testing.assert_allclose(H_out[0, :], np.array(H_in[0:3]) * sx, atol=1e-12)
    np.testing.assert_allclose(H_out[1, :], np.array(H_in[3:6]) * sy, atol=1e-12)
    np.testing.assert_allclose(H_out[2, :], np.array(H_in[6:9]), atol=1e-12)


def test_scale_pitch_roundtrip_preserves_projected_pixel():
    """End-to-end invariant: if we project a world point through the original
    (K, H) to get a 1080p pixel, and scale (K, H) to 720p, projecting the same
    world point must yield a pixel at the same fractional position (scaled
    down by the same ratio)."""
    fx = fy = 1600.0
    cx, cy = 960.0, 540.0
    intr = IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy)
    K = build_K(fx, fy, cx, cy)
    C = np.array([1.8, -2.5, 1.2])
    R, t = _look_at(C, np.array([0.0, 0.15, 0.0]))
    H_true = _homography_from_pose(K, R, t)
    pitch = _pitch_at(1280, 720, intr, H_true.flatten().tolist())

    # Arbitrary plate-plane point.
    world = np.array([0.2, 0.3, 1.0])
    pix_1080 = H_true @ world
    pix_1080 = pix_1080[:2] / pix_1080[2]

    out = scale_pitch_to_video_dims(pitch, (1920, 1080))
    assert out.homography is not None
    H_720 = np.array(out.homography).reshape(3, 3)
    pix_720 = H_720 @ world
    pix_720 = pix_720[:2] / pix_720[2]

    sx = 1280 / 1920
    sy = 720 / 1080
    np.testing.assert_allclose(pix_720, pix_1080 * np.array([sx, sy]), atol=1e-9)


# --------------------------- intrinsics sanity check ------------------------


def test_sanity_check_quiet_on_centered_intrinsics(caplog):
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=540.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1920, 1080, intr, H)
    import logging
    with caplog.at_level(logging.WARNING, logger="pairing"):
        scale_pitch_to_video_dims(pitch, (1920, 1080))
    # No WARNING for a well-centered cx/cy.
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_sanity_check_warns_when_principal_point_off_center(caplog):
    # cy=800 out of 1080 ⇒ cy/h=0.741 — exactly the footprint of a 4:3→16:9
    # crop basis mismatch.
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=800.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1920, 1080, intr, H)
    import logging
    with caplog.at_level(logging.WARNING, logger="pairing"):
        scale_pitch_to_video_dims(pitch, (1920, 1080))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "principal-point OFF" in warnings[0].getMessage()


def test_sanity_check_runs_after_scaling(caplog):
    # Intrinsics baked at 1920×1080 but video actually at 1280×720. After
    # scale_pitch rescales cx/cy proportionally, they should still be near
    # the centre of the new grid — so no warning fires.
    intr = IntrinsicsPayload(fx=1600.0, fy=1600.0, cx=960.0, cy=540.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, intr, H)
    import logging
    with caplog.at_level(logging.WARNING, logger="pairing"):
        scale_pitch_to_video_dims(pitch, (1920, 1080))
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_sanity_check_warns_on_wrong_basis_after_scaling(caplog):
    # Intrinsics baked at 4032×3024 (iPhone photo), metadata lies and
    # claims 1920×1080. Our rescale from 1920→1280 then leaves cx/cy way
    # past the frame — loud warning is exactly the outcome we want.
    intr = IntrinsicsPayload(fx=3000.0, fy=3000.0, cx=2016.0, cy=1512.0)
    H = [1.0, 0.0, 10.0, 0.0, 1.0, 20.0, 0.0, 0.0, 1.0]
    pitch = _pitch_at(1280, 720, intr, H)
    import logging
    with caplog.at_level(logging.WARNING, logger="pairing"):
        scale_pitch_to_video_dims(pitch, (1920, 1080))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "principal-point OFF" in warnings[0].getMessage()
