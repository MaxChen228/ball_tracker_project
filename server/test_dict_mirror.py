"""Phase 6a — additive `*_by_algorithm` dict mirrors.

Pins the contract that the after-validator on `PitchPayload` /
`SessionResult` mirrors old per-path fields (`frames_live`,
`frames_server_post`, `live_config_used`, `server_post_config_used`,
`triangulated_by_path`, `segments_by_path`, `frame_counts_by_path`,
`paths_completed`) into the new algorithm-id-keyed dicts on every
construction / load. Old fields remain canonical in 6a; these tests
ensure 6b can flip readers safely because the dict is always present.
"""
from __future__ import annotations

import algorithms
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    FramePayload,
    HSVRangePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    SegmentRecord,
    SessionResult,
    ShapeGatePayload,
    TriangulatedPoint,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    )


def _snapshot(alg_id: str, preset: str | None = None) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        hsv=HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=200, v_min=40, v_max=210),
        shape_gate=ShapeGatePayload(aspect_min=0.7, fill_min=0.55),
        preset_name=preset,
    )


def _base_pitch(**kw):
    defaults = dict(
        camera_id="A",
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
    )
    defaults.update(kw)
    return PitchPayload(**defaults)


def test_pitch_mirrors_frames_live_into_ios_capture_time():
    p = _base_pitch(frames_live=[_frame(1), _frame(2)])
    assert IOS_CAPTURE_TIME_ALGORITHM_ID in p.frames_by_algorithm
    assert len(p.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 2


def test_pitch_mirrors_frames_server_post_under_snapshot_algorithm_id():
    snap = _snapshot("v11_hsv_cc")
    p = _base_pitch(
        frames_server_post=[_frame(1), _frame(2), _frame(3)],
        server_post_config_used=snap,
    )
    assert "v11_hsv_cc" in p.frames_by_algorithm
    assert len(p.frames_by_algorithm["v11_hsv_cc"]) == 3


def test_pitch_frames_server_post_without_snapshot_falls_back_to_default():
    """Legacy pitch JSONs predating snapshot persistence — the dict
    mirror still has to file frames somewhere. Use the registry default
    so 6b readers find the frames under a real algorithm id."""
    p = _base_pitch(frames_server_post=[_frame(1)])
    assert algorithms.DEFAULT_ALGORITHM_ID in p.frames_by_algorithm


def test_pitch_mirrors_config_used_into_dict():
    live_snap = _snapshot("ios_capture_time", preset="blue_ball")
    srv_snap = _snapshot("v11_hsv_cc", preset="tennis")
    p = _base_pitch(live_config_used=live_snap, server_post_config_used=srv_snap)
    assert p.config_used_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID].preset_name == "blue_ball"
    assert p.config_used_by_algorithm["v11_hsv_cc"].preset_name == "tennis"


def test_pitch_empty_old_fields_yields_empty_dict():
    """Mirror must NOT fabricate dict keys when old fields are empty —
    otherwise 6b readers would see ghost algorithm rows for sessions
    that never ran that path."""
    p = _base_pitch()
    assert p.frames_by_algorithm == {}
    assert p.config_used_by_algorithm == {}


def test_pitch_mirror_idempotent_on_revalidation():
    p = _base_pitch(frames_live=[_frame(1)])
    dumped = p.model_dump(mode="json")
    p2 = PitchPayload.model_validate(dumped)
    assert list(p.frames_by_algorithm.keys()) == list(p2.frames_by_algorithm.keys())
    assert len(p2.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 1


def _tri_point() -> TriangulatedPoint:
    return TriangulatedPoint(
        t_rel_s=0.1, x_m=1.0, y_m=2.0, z_m=3.0, residual_m=0.01,
        cost_a=0.5, cost_b=0.4,
    )


def _segment() -> SegmentRecord:
    return SegmentRecord(
        indices=[0, 1, 2], original_indices=[0, 1, 2],
        p0=[1.0, 2.0, 3.0], v0=[10.0, 0.0, -1.0],
        t_anchor=0.0, t_start=0.0, t_end=0.2,
        rmse_m=0.01, speed_kph=130.0,
    )


def test_result_mirrors_triangulated_and_segments_by_path():
    srv_snap = _snapshot("v11_hsv_cc")
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_path={"live": [_tri_point()], "server_post": [_tri_point(), _tri_point()]},
        segments_by_path={"server_post": [_segment()]},
        frame_counts_by_path={"live": {"A": 100, "B": 100}, "server_post": {"A": 200, "B": 200}},
        paths_completed={"live", "server_post"},
        live_config_used=_snapshot("ios_capture_time"),
        server_post_config_used=srv_snap,
    )
    assert len(r.triangulated_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 1
    assert len(r.triangulated_by_algorithm["v11_hsv_cc"]) == 2
    assert len(r.segments_by_algorithm["v11_hsv_cc"]) == 1
    assert IOS_CAPTURE_TIME_ALGORITHM_ID not in r.segments_by_algorithm  # nothing to mirror
    assert r.frame_counts_by_algorithm["v11_hsv_cc"] == {"A": 200, "B": 200}
    assert r.algorithms_completed == {IOS_CAPTURE_TIME_ALGORITHM_ID, "v11_hsv_cc"}
    assert set(r.config_used_by_algorithm) == {IOS_CAPTURE_TIME_ALGORITHM_ID, "v11_hsv_cc"}


def test_result_server_post_alg_id_falls_back_when_no_snapshot():
    """Pre-snapshot legacy results — server_post points still have to
    file under a real algorithm id."""
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_path={"server_post": [_tri_point()]},
        paths_completed={"server_post"},
    )
    assert algorithms.DEFAULT_ALGORITHM_ID in r.triangulated_by_algorithm
    assert algorithms.DEFAULT_ALGORITHM_ID in r.algorithms_completed


def test_pitch_legacy_hsv_used_plus_dict_keys_round_trip():
    """Mixed JSON: pre-phase-2 legacy `hsv_range_used` trio + a wire
    payload that already carries `frames_by_algorithm` keys. The
    before-validator migrates legacy → per-path, then the after-
    validator mirrors per-path → dict. Pre-existing dict keys for the
    same algorithm id must be overwritten by the freshly-derived
    mirror (old fields are canonical in 6a)."""
    raw = {
        "camera_id": "A",
        "session_id": "s_deadbeef",
        "video_start_pts_s": 0.0,
        "frames_live": [_frame(1).model_dump()],
        "hsv_range_used": {
            "h_min": 10, "h_max": 20, "s_min": 30, "s_max": 200,
            "v_min": 40, "v_max": 210,
        },
        "shape_gate_used": {"aspect_min": 0.7, "fill_min": 0.55},
        "live_preset_name": "blue_ball",
        # Hand-supplied dict key that mirror should overwrite:
        "frames_by_algorithm": {
            IOS_CAPTURE_TIME_ALGORITHM_ID: [],  # stale ghost: empty list
        },
    }
    p = PitchPayload.model_validate(raw)
    # Legacy fields migrated to per-path:
    assert p.live_config_used is not None
    assert p.live_config_used.preset_name == "blue_ball"
    # Mirror overwrote the stale dict entry from old field's truth:
    assert len(p.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID]) == 1


def test_pitch_dict_ghost_keys_unrelated_to_old_fields_persist():
    """Phase-6a contract: mirror is **union** semantics, not
    projection. Ghost keys not corresponding to any old field are
    preserved. This test pins the documented behavior so a future
    refactor that flips to projection doesn't slip past review."""
    p = _base_pitch(
        frames_by_algorithm={"v12_future": [_frame(99)]},
    )
    assert "v12_future" in p.frames_by_algorithm
    assert len(p.frames_by_algorithm["v12_future"]) == 1


def test_pitch_mirror_under_non_v11_server_post_algorithm_id():
    """Confirm dict key tracks `server_post_config_used.algorithm_id`,
    not a hardcoded `v11_hsv_cc`. Future v12+ detectors must file
    under their own id without requiring schema changes."""
    # Use a non-runnable id since registry currently only has v11; the
    # snapshot validator only checks `validate_id` (not runnable), so
    # `ios_capture_time` is accepted as a placeholder for "some future
    # registered detector" without us having to register a fake.
    snap = _snapshot("ios_capture_time")
    p = _base_pitch(
        frames_server_post=[_frame(1)],
        server_post_config_used=snap,
    )
    assert "ios_capture_time" in p.frames_by_algorithm
    assert "v11_hsv_cc" not in p.frames_by_algorithm


def test_persist_pitch_json_syncs_dict_after_mutation():
    """Writer-side sync hook: model_copy + mutation of old fields does
    NOT re-run the after-validator, so the dict goes stale on the
    in-memory model. `persist_pitch_json` must call the mirror helper
    so the on-disk JSON always reflects the latest old-field state."""
    from schemas import persist_pitch_json
    import json

    p = _base_pitch(frames_live=[_frame(1)])
    # Mutate AFTER construction — validator already ran, dict frozen.
    p.frames_server_post = [_frame(2), _frame(3)]
    snap = _snapshot("v11_hsv_cc")
    p.server_post_config_used = snap
    # Without the writer hook, model_dump_json would emit the stale
    # dict (still missing v11_hsv_cc). persist_pitch_json must fix it.
    blob = persist_pitch_json(p)
    parsed = json.loads(blob)
    assert "v11_hsv_cc" in parsed["frames_by_algorithm"]
    assert len(parsed["frames_by_algorithm"]["v11_hsv_cc"]) == 2
    assert "v11_hsv_cc" in parsed["config_used_by_algorithm"]


def test_persist_result_json_syncs_dict_after_mutation():
    from schemas import persist_result_json
    import json

    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
    )
    r.triangulated_by_path = {"server_post": [_tri_point()]}
    r.paths_completed = {"server_post"}
    blob = persist_result_json(r)
    parsed = json.loads(blob)
    # Legacy fallback algorithm id used because snapshot is None:
    assert algorithms.DEFAULT_ALGORITHM_ID in parsed["triangulated_by_algorithm"]
    assert algorithms.DEFAULT_ALGORITHM_ID in parsed["algorithms_completed"]


def test_validate_runnable_id_rejects_ios_capture_time():
    """`ios_capture_time` is a valid wire/disk id but not runnable.
    `validate_runnable_id` is the strict variant for callsites whose
    contract is 'this id will reach run_detection'."""
    algorithms.validate_id("ios_capture_time")  # OK
    algorithms.validate_runnable_id("v11_hsv_cc")  # OK
    try:
        algorithms.validate_runnable_id("ios_capture_time")
        raise AssertionError("should have rejected non-runnable id")
    except ValueError as e:
        assert "non-runnable" in str(e)


def test_drift_guard_catches_schemas_constant_mismatch(monkeypatch):
    """`algorithms._check_schemas_constant_drift` pins
    `IOS_CAPTURE_TIME` literal equality across the back-import-cycle
    boundary. Simulate a developer editing one but not the other."""
    import algorithms as algorithms_mod
    import schemas as schemas_mod
    monkeypatch.setattr(schemas_mod, "IOS_CAPTURE_TIME_ALGORITHM_ID", "drifted_value")
    try:
        algorithms_mod._check_schemas_constant_drift()
        raise AssertionError("drift guard should have raised")
    except RuntimeError as e:
        assert "drifted" in str(e)


def test_legacy_bucket_drift_guard_catches_unregistered_id(monkeypatch):
    """Drift guard #2: removing v11 from the registry without updating
    `_LEGACY_PRE_SNAPSHOT_ALGORITHM_ID` would silently break legacy
    pre-snapshot pitch reads. Boot must fail loudly."""
    import algorithms as algorithms_mod
    import schemas as schemas_mod
    monkeypatch.setattr(
        schemas_mod, "_LEGACY_PRE_SNAPSHOT_ALGORITHM_ID", "v999_dangling"
    )
    try:
        algorithms_mod._check_legacy_bucket_in_registry()
        raise AssertionError("legacy-bucket drift guard should have raised")
    except RuntimeError as e:
        assert "v999_dangling" in str(e)


def test_set_algorithm_frames_round_trip_through_persist_and_reload():
    """End-to-end Phase 6b contract: caller writes via
    `set_algorithm_frames` (e.g. Phase 7's run-algorithm endpoint),
    persists via `persist_pitch_json`, reloads from JSON. The new
    algorithm's frames + dict key must survive — including for
    algorithm ids OTHER than the current server_post stamp (v12 while
    v11 is canonical), which is the multi-algorithm point."""
    from detection_paths import set_algorithm_frames
    from schemas import persist_pitch_json
    import json

    # v11 is the current server_post; v12 frames will live in dict only.
    snap_v11 = _snapshot("v11_hsv_cc")
    p = _base_pitch(
        frames_server_post=[_frame(1)],
        server_post_config_used=snap_v11,
    )
    set_algorithm_frames(p, "ios_capture_time", [_frame(10)])  # back-syncs frames_live
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(20), _frame(21)])  # back-syncs frames_server_post
    # ios_capture_time is non-runnable but valid in snapshot (validate_id, not _runnable_).
    # Use it as a stand-in for a future second runnable algorithm so this
    # test doesn't break when v12 lands as a real registry entry.
    set_algorithm_frames(p, "ios_capture_time", [_frame(10)])

    blob = persist_pitch_json(p)
    parsed = json.loads(blob)
    assert "ios_capture_time" in parsed["frames_by_algorithm"]
    assert "v11_hsv_cc" in parsed["frames_by_algorithm"]
    assert parsed["frames_live"] == parsed["frames_by_algorithm"]["ios_capture_time"]
    assert parsed["frames_server_post"] == parsed["frames_by_algorithm"]["v11_hsv_cc"]

    p2 = PitchPayload.model_validate(parsed)
    assert len(p2.frames_by_algorithm["v11_hsv_cc"]) == 2
    assert len(p2.frames_by_algorithm["ios_capture_time"]) == 1


def test_raw_model_dump_json_without_persist_helper_emits_stale_dict():
    """Negative contract: bypassing `persist_pitch_json` and dumping
    raw must result in a stale dict on disk. Pins the failure mode so a
    future regression that re-introduces direct `model_dump_json()`
    calls would visibly break this test instead of silently shipping
    inconsistent JSON."""
    import json

    p = _base_pitch(frames_live=[_frame(1)])
    # Direct mutation, no persist hook:
    p.frames_server_post = [_frame(2), _frame(3)]
    p.server_post_config_used = _snapshot("v11_hsv_cc")
    raw = json.loads(p.model_dump_json())
    # frames_server_post is on disk, but the dict mirror was not refreshed:
    assert "v11_hsv_cc" not in raw["frames_by_algorithm"]
    assert raw["frames_server_post"][0]["frame_index"] == 2
