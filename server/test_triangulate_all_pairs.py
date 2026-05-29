"""Phase 2a offline N-camera triangulation: pair-as-atom.

`triangulate_all_pairs` runs C(N,2) independent stereo pairs. We build a
3-camera scene viewing a known trajectory and assert:
  - all 3 pairs (A|B, A|C, B|C) emit, each recovering the same path
  - N=2 collapses to the single A|B entry (regression with the old
    single-pair triangulate_pair_rays)
  - a pair whose camera lacks calibration is skipped, not zero-filled
"""
from __future__ import annotations

import numpy as np

import main
import pairing
from triangulate import build_K
from conftest import sid

from _test_helpers import _look_at, _project_pixels


def _cam_payload(cam_id, session_id, path, ts, C, K):
    from schemas import BlobCandidate
    target = np.array([0.0, 0.15, 0.0])
    R, t = _look_at(C, target)
    H = K @ np.column_stack([R[:, 0], R[:, 1], t])
    H /= H[2, 2]
    frames = []
    for i, (Pi, ti) in enumerate(zip(path, ts)):
        u, v = _project_pixels(K, R, t, Pi)
        frames.append(main.FramePayload(
            frame_index=i, timestamp_s=float(ti), px=u, py=v, ball_detected=True,
            candidates=[BlobCandidate(px=u, py=v, area=100, area_score=1.0,
                                      aspect=1.0, fill=0.68)],
        ))
    return main.PitchPayload(
        camera_id=cam_id, session_id=session_id, sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0, video_start_pts_s=0.0, video_fps=240.0,
        frames_by_algorithm={"v11_hsv_cc": frames},
        active_server_post_algorithm_id="v11_hsv_cc",
        intrinsics=main.IntrinsicsPayload(fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]),
        homography=H.flatten().tolist(),
    )


def _scene_n3():
    K = build_K(1600.0, 1600.0, 960.0, 540.0)
    ts = np.linspace(0.0, 0.4, 15)
    path = np.stack([0.1 * np.sin(ts * 10), 18.0 - 45.0 * ts, 2.0 - 4.9 * ts**2], axis=1)
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches = {c: _cam_payload(c, sid(1), path, ts, C, K) for c, C in centers.items()}
    return pitches, path


def test_n3_emits_all_three_pairs():
    pitches, path = _scene_n3()
    result = pairing.triangulate_all_pairs(pitches)
    assert set(result.keys()) == {("A", "B"), ("A", "C"), ("B", "C")}
    for key, pts in result.items():
        assert len(pts) == len(path), key
        recovered = np.array([[p.x_m, p.y_m, p.z_m] for p in pts])
        np.testing.assert_allclose(recovered, path, atol=1e-6,
                                   err_msg=f"pair {key} mismatch")


def test_n2_collapses_to_single_pair():
    pitches, path = _scene_n3()
    two = {"A": pitches["A"], "B": pitches["B"]}
    result = pairing.triangulate_all_pairs(two)
    assert set(result.keys()) == {("A", "B")}
    # Identical to the direct single-pair primitive.
    direct = pairing.triangulate_pair_rays(pitches["A"], pitches["B"])
    assert len(result[("A", "B")]) == len(direct)


def test_uncalibrated_camera_pair_skipped():
    pitches, _ = _scene_n3()
    # Strip C's calibration → pairs involving C must be skipped, A|B survives.
    pitches["C"] = pitches["C"].model_copy(update={"intrinsics": None, "homography": None})
    result = pairing.triangulate_all_pairs(pitches)
    assert set(result.keys()) == {("A", "B")}


def test_pair_key_canonical_and_serialisation():
    assert pairing.pair_key("B", "A") == ("A", "B")
    assert pairing.pair_key("A", "B") == ("A", "B")
    assert pairing.pair_key_str(("A", "B")) == "A|B"
