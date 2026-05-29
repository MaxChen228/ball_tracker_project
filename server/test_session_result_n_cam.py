"""Phase 4-2: rebuild_result_for_session must work for N≥3 cameras.

Live ingest is already N-aware (Phase 2a). This locks the offline /
server_post rebuild mirror: persisted PitchPayloads from 3 cameras must
produce a SessionResult whose triangulated points cover all C(N,2)
pairs, with frame_counts and cameras_received reflecting every cam.

N=2 invariance: scenes built from 2 cams must produce output identical
to the pre-N path (pair_key already required since Phase 3a)."""
from __future__ import annotations

import numpy as np

import main
import session_results
from conftest import sid
from triangulate import build_K

from _test_helpers import _look_at, _project_pixels


def _pitch_payload(cam_id: str, session_id: str, path: np.ndarray, ts: np.ndarray,
                   C: np.ndarray, K: np.ndarray) -> "main.PitchPayload":
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
        intrinsics=main.IntrinsicsPayload(fx=K[0, 0], fy=K[1, 1],
                                          cx=K[0, 2], cy=K[1, 2]),
        homography=H.flatten().tolist(),
    )


def _scene(centers: dict[str, np.ndarray]) -> tuple[dict[str, "main.PitchPayload"], np.ndarray]:
    K = build_K(1600.0, 1600.0, 960.0, 540.0)
    ts = np.linspace(0.0, 0.2, 8)
    path = np.stack([0.1 * np.sin(ts * 10), 18.0 - 45.0 * ts, 2.0 - 4.9 * ts**2], axis=1)
    session_id = sid(1)
    pitches = {c: _pitch_payload(c, session_id, path, ts, C, K)
               for c, C in centers.items()}
    return pitches, path


def _ingest(pitches: dict[str, "main.PitchPayload"]) -> None:
    """Stamp sync state for every cam + record their pitches. State
    bootstraps the live_pairing only on first heartbeat per session, so
    we heartbeat before recording so expected_camera_ids() includes C."""
    for cam in pitches:
        main.state.heartbeat(cam, time_synced=True, time_sync_id="sy_deadbeef",
                             sync_anchor_timestamp_s=0.0)
    for cam, p in pitches.items():
        main.state.record(p)


def test_n3_rebuild_emits_three_pair_keys():
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    assert result.error is None
    pair_keys = {tuple(p.pair_key) for p in result.triangulated}
    assert pair_keys == {("A", "B"), ("A", "C"), ("B", "C")}


def test_n3_frame_counts_and_cameras_received():
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    counts = result.frame_counts_by_algorithm["v11_hsv_cc"]
    assert set(counts.keys()) == {"A", "B", "C"}
    assert all(v > 0 for v in counts.values())
    assert result.cameras_received["A"] is True
    assert result.cameras_received["B"] is True
    assert result.cameras_received["C"] is True


def test_n2_regression_bit_identical():
    """N=2 path through the N-cam wrapper must emit exactly the same
    points as the bare triangulate_pair_rays primitive (modulo ordering;
    the wrapper sorts by t_rel_s)."""
    import pairing
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
    }
    pitches, _ = _scene(centers)
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    direct = sorted(
        pairing.triangulate_pair_rays(pitches["A"], pitches["B"]),
        key=lambda p: p.t_rel_s,
    )
    rebuilt = sorted(result.triangulated, key=lambda p: p.t_rel_s)
    assert len(rebuilt) == len(direct)
    for r, d in zip(rebuilt, direct):
        assert abs(r.x_m - d.x_m) < 1e-9
        assert abs(r.y_m - d.y_m) < 1e-9
        assert abs(r.z_m - d.z_m) < 1e-9
        assert tuple(r.pair_key) == ("A", "B")


def test_mono_session_no_triangulation():
    """Single-cam pitch → no pair → triangulated empty, no ValueError."""
    centers = {"A": np.array([1.8, -2.5, 1.2])}
    pitches, _ = _scene(centers)
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)
    assert result.triangulated == []
    # mono sessions don't error — frame counts still surface
    counts = result.frame_counts_by_algorithm.get("v11_hsv_cc", {})
    assert "A" in counts


def test_n3_with_one_uncalibrated_camera_still_emits_other_pairs():
    """C has no calibration → A|C and B|C drop out (logged in
    triangulate_all_pairs), A|B keeps emitting. Rebuild must not crash.
    The two dropped pairs are surfaced as explicit abort_reasons."""
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    # Strip C's calibration in place.
    pitches["C"] = pitches["C"].model_copy(update={
        "intrinsics": None, "homography": None,
    })
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)
    pair_keys = {tuple(p.pair_key) for p in result.triangulated}
    assert pair_keys == {("A", "B")}, pair_keys
    # The C-involving pairs are visible, not silently zero-filled.
    assert "missing_calibration:server_post:A-C" in result.abort_reasons
    assert "missing_calibration:server_post:B-C" in result.abort_reasons


# ---------------------------------------------------------------------------
# Phase A: silent-fallback elimination at the N-cam boundary. Each of the
# following used to silently degrade (drop C's pointer / config, or mask
# an all-uncalibrated session as a generic "no detection"). They must now
# surface an explicit signal instead.
# ---------------------------------------------------------------------------

def _snap(alg_id: str, h_min: int):
    """server_post_config_used snapshot whose params vary by `h_min` so
    two cams can be made to diverge."""
    from schemas import DetectionConfigSnapshotPayload
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        params={
            "hsv": {"h_min": h_min, "h_max": 20, "s_min": 30,
                    "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )


def test_n3_server_post_pointer_mismatch_records_abort_reason():
    """A=B=v11 but C points at a different alg → no silent A-wins pick.
    The divergence is recorded explicitly so the operator sees it."""
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    pitches["C"] = pitches["C"].model_copy(update={
        "active_server_post_algorithm_id": "v12_other",
    })
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    assert "server_post_pointer_mismatch" in result.abort_reasons
    msg = result.abort_reasons["server_post_pointer_mismatch"]
    assert "v12_other" in msg and "v11_hsv_cc" in msg


def test_n3_frozen_config_divergence_records_abort_reason():
    """A=B share one frozen server_post_config_used snapshot, C carries a
    different one → aggregate must raise → recorded as abort_reason rather
    than silently picking A's snapshot."""
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    # server_post_config_used is a computed view of
    # config_used_by_algorithm[active pointer], so seed the dict directly.
    pitches["A"] = pitches["A"].model_copy(update={
        "config_used_by_algorithm": {"v11_hsv_cc": _snap("v11_hsv_cc", 10)}})
    pitches["B"] = pitches["B"].model_copy(update={
        "config_used_by_algorithm": {"v11_hsv_cc": _snap("v11_hsv_cc", 10)}})
    pitches["C"] = pitches["C"].model_copy(update={
        "config_used_by_algorithm": {"v11_hsv_cc": _snap("v11_hsv_cc", 15)}})
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    assert "frozen_config_diverged" in result.abort_reasons


def test_n3_divergent_sync_id_surfaces_error():
    """C carries a sync_id distinct from A/B → validate_session_sync (now
    N-cam) sees all three cams and rejects the session, where the old
    pair-only validator would have passed on A==B alone."""
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    pitches["C"] = pitches["C"].model_copy(update={"sync_id": "sy_different"})
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    assert result.error == "sync id mismatch"
    assert result.triangulated == []


def test_n3_all_uncalibrated_records_missing_calibration():
    """Every cam lacks calibration → all pairs skip. The result must carry
    explicit missing_calibration abort_reasons (and aborted=True), not the
    generic 'no detection completed' that masks the real cause."""
    centers = {
        "A": np.array([1.8, -2.5, 1.2]),
        "B": np.array([-1.8, -2.5, 1.2]),
        "C": np.array([0.0, -3.0, 2.4]),
    }
    pitches, _ = _scene(centers)
    for cam in list(pitches):
        pitches[cam] = pitches[cam].model_copy(update={
            "intrinsics": None, "homography": None,
        })
    _ingest(pitches)
    sid_ = next(iter(pitches.values())).session_id
    result = session_results.rebuild_result_for_session(main.state, sid_)

    assert result.triangulated == []
    missing = [k for k in result.abort_reasons if k.startswith("missing_calibration:")]
    assert len(missing) == 3, result.abort_reasons
    assert result.aborted is True
    assert result.error is None
