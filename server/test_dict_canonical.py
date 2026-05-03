"""Phase 1 dict-canonical contract for PitchPayload + still-flat-canonical
SessionResult.

After the dict-canonical flip (PitchPayload), `frames_by_algorithm` /
`config_used_by_algorithm` / `active_server_post_algorithm_id` are the
single source of truth. The flat surfaces `frames_live`,
`frames_server_post`, `live_config_used`, `server_post_config_used` are
read-only `@computed_field` projections — they round-trip on the wire
but disk persist drops them via `persist_pitch_json`'s exclude set.

SessionResult flip lands in phase 2; for now its mirror direction is
unchanged and tested below as it always was.
"""
from __future__ import annotations

import algorithms
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    FramePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    SegmentRecord,
    SessionResult,
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
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30, "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=preset,
    )


def _base_pitch(**kw):
    """Test helper: passes most kwargs through to PitchPayload, with a
    convenience fold of `frames_by_algorithm` + `config_used_by_algorithm`
    + `active_server_post_algorithm_id` already accepted directly."""
    defaults = dict(
        camera_id="A",
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
    )
    defaults.update(kw)
    return PitchPayload(**defaults)


# --- computed_field projections from canonical dicts ----------------------


def test_frames_live_projects_from_ios_capture_time_bucket():
    p = _base_pitch(
        frames_by_algorithm={IOS_CAPTURE_TIME_ALGORITHM_ID: [_frame(1), _frame(2)]},
    )
    assert len(p.frames_live) == 2
    assert p.frames_live[0].frame_index == 1


def test_frames_server_post_projects_from_active_pointer_bucket():
    p = _base_pitch(
        frames_by_algorithm={"v11_hsv_cc": [_frame(1), _frame(2), _frame(3)]},
        config_used_by_algorithm={"v11_hsv_cc": _snapshot("v11_hsv_cc")},
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    assert len(p.frames_server_post) == 3
    assert p.server_post_config_used is not None
    assert p.server_post_config_used.algorithm_id == "v11_hsv_cc"


def test_frames_server_post_returns_empty_without_pointer():
    """No silent fallback per CLAUDE.md: when
    `active_server_post_algorithm_id` is None the projection returns
    `[]` regardless of what `frames_by_algorithm` happens to hold.
    The collapse shim + migration script stamp the pointer eagerly
    so legitimate legacy pre-snapshot records always have one when
    they arrive at this property."""
    p = _base_pitch(frames_by_algorithm={"v11_hsv_cc": [_frame(1)]})
    assert p.active_server_post_algorithm_id is None
    assert p.frames_server_post == []
    assert p.server_post_config_used is None


def test_live_config_used_projects_from_ios_capture_time_bucket():
    p = _base_pitch(
        config_used_by_algorithm={IOS_CAPTURE_TIME_ALGORITHM_ID: _snapshot("ios_capture_time", preset="blue_ball")},
    )
    assert p.live_config_used is not None
    assert p.live_config_used.preset_name == "blue_ball"


def test_empty_canonical_dicts_yield_empty_projections():
    p = _base_pitch()
    assert p.frames_live == []
    assert p.frames_server_post == []
    assert p.live_config_used is None
    assert p.server_post_config_used is None


def test_legacy_flat_kwargs_now_rejected_post_phase3():
    """After phase 3 deleted `_collapse_legacy_*_flat_input`, the old
    flat-key kwargs hit `extra="forbid"` and raise. Pin the failure
    mode so a future re-introduction of the shim breaks this test."""
    import pytest as _pytest
    with _pytest.raises(Exception) as exc_info:
        _base_pitch(frames_live=[_frame(1)])
    assert "frames_live" in str(exc_info.value)


# --- persist round-trip ----------------------------------------------------


def test_persist_pitch_json_drops_flat_keys_from_disk_payload():
    """Disk shape is dict-canonical: flat surfaces excluded from
    `persist_pitch_json` so the disk record carries a single source of
    truth. Reload reconstructs the projection."""
    from schemas import persist_pitch_json
    import json

    p = _base_pitch(
        frames_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: [_frame(1)],
            "v11_hsv_cc": [_frame(2), _frame(3)],
        },
        config_used_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: _snapshot("ios_capture_time"),
            "v11_hsv_cc": _snapshot("v11_hsv_cc"),
        },
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    blob = persist_pitch_json(p)
    parsed = json.loads(blob)
    # Disk has only canonical shape:
    assert "frames_live" not in parsed
    assert "frames_server_post" not in parsed
    assert "live_config_used" not in parsed
    assert "server_post_config_used" not in parsed
    assert IOS_CAPTURE_TIME_ALGORITHM_ID in parsed["frames_by_algorithm"]
    assert "v11_hsv_cc" in parsed["frames_by_algorithm"]
    assert parsed["active_server_post_algorithm_id"] == "v11_hsv_cc"

    # Reload — flat surfaces project from dict via computed_field.
    p2 = PitchPayload.model_validate_json(blob)
    assert len(p2.frames_live) == 1
    assert len(p2.frames_server_post) == 2
    assert p2.server_post_config_used.algorithm_id == "v11_hsv_cc"


def test_wire_dump_keeps_flat_surfaces_for_clients():
    """HTTP / WS wire keeps the flat surfaces (computed_field default
    serialize) so dashboard / viewer JS clients don't have to learn the
    new dict shape."""
    p = _base_pitch(
        frames_by_algorithm={IOS_CAPTURE_TIME_ALGORITHM_ID: [_frame(1)]},
    )
    import json
    wire = json.loads(p.model_dump_json())
    assert "frames_live" in wire
    assert wire["frames_live"][0]["frame_index"] == 1
    assert "frames_by_algorithm" in wire  # dict still on wire too


def test_writers_set_dict_directly_no_legacy_back_sync():
    """`set_algorithm_frames` writes only the dict bucket — there is
    no flat field to back-sync. Reading the projection picks up the
    new state automatically."""
    from detection_paths import set_algorithm_frames

    p = _base_pitch()
    set_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID, [_frame(10)])
    assert len(p.frames_live) == 1
    assert p.frames_live[0].frame_index == 10


def test_stamp_server_post_run_atomic_writes_pointer_dict_and_snapshot():
    """`stamp_server_post_run` is the canonical entry for server-side
    detection runs. It writes the pointer + snapshot + frames in one
    call so projection invariants stay coherent."""
    from detection_paths import stamp_server_post_run

    p = _base_pitch()
    snap = _snapshot("v11_hsv_cc", preset="tennis")
    stamp_server_post_run(p, snap, [_frame(20), _frame(21)])
    assert p.active_server_post_algorithm_id == "v11_hsv_cc"
    assert p.frames_by_algorithm["v11_hsv_cc"] == [_frame(20), _frame(21)]
    assert p.config_used_by_algorithm["v11_hsv_cc"].preset_name == "tennis"
    # Projections agree:
    assert len(p.frames_server_post) == 2
    assert p.server_post_config_used.preset_name == "tennis"


def test_persist_round_trip_byte_stable_after_no_op_load_dump():
    """Load-and-resave a dict-canonical record must be byte-stable.
    Pins that no spurious mutation (sorting, default backfill) creeps
    into the wire boundary."""
    from schemas import persist_pitch_json
    p = _base_pitch(
        frames_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: [_frame(1)],
            "v11_hsv_cc": [_frame(2)],
        },
        config_used_by_algorithm={
            "v11_hsv_cc": _snapshot("v11_hsv_cc"),
        },
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    blob1 = persist_pitch_json(p)
    p2 = PitchPayload.model_validate_json(blob1)
    blob2 = persist_pitch_json(p2)
    assert blob1 == blob2


# --- SessionResult side: still flat canonical (phase 2 territory) ---------


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


def test_result_projections_from_dict_canonical_construction():
    """SessionResult constructed via canonical dict-keyed kwargs.
    Verifies the path-keyed `@computed_field` projections."""
    srv_snap = _snapshot("v11_hsv_cc")
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: [_tri_point()],
            "v11_hsv_cc": [_tri_point(), _tri_point()],
        },
        segments_by_algorithm={"v11_hsv_cc": [_segment()]},
        frame_counts_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: {"A": 100, "B": 100},
            "v11_hsv_cc": {"A": 200, "B": 200},
        },
        algorithms_completed={IOS_CAPTURE_TIME_ALGORITHM_ID, "v11_hsv_cc"},
        config_used_by_algorithm={
            IOS_CAPTURE_TIME_ALGORITHM_ID: _snapshot("ios_capture_time"),
            "v11_hsv_cc": srv_snap,
        },
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    # Path-keyed projections route to the right buckets.
    assert len(r.triangulated_by_path["live"]) == 1
    assert len(r.triangulated_by_path["server_post"]) == 2
    assert len(r.segments_by_path["server_post"]) == 1
    assert "live" not in r.segments_by_path
    assert r.frame_counts_by_path["server_post"] == {"A": 200, "B": 200}
    assert r.paths_completed == {"live", "server_post"}
    assert r.live_config_used.algorithm_id == "ios_capture_time"
    assert r.server_post_config_used is srv_snap


def test_paths_completed_excludes_non_current_alg_history():
    """Reviewer BLOCK 1 (phase 2): when
    `_triangulate_non_current_algorithms` adds v12 history to
    `algorithms_completed`, `paths_completed` MUST NOT add
    "server_post" unless the current pointer's bucket is also
    completed. Otherwise the path-keyed projection diverges from
    `triangulated_by_path` (which only surfaces the current pointer)."""
    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_algorithm={"v12_test": [_tri_point()]},
        algorithms_completed={"v12_test"},
        active_server_post_algorithm_id="v11_hsv_cc",  # v11 is current, but never ran
    )
    # v12 in algorithms_completed but current pointer (v11) is not →
    # paths_completed has neither "live" nor "server_post".
    assert r.paths_completed == set()
    # triangulated_by_path projection: v12 bucket not surfaced because
    # active pointer points to v11.
    assert "server_post" not in r.triangulated_by_path


def test_persist_result_json_drops_flat_keys_from_disk_payload():
    """Disk shape is dict-canonical. Wire (model_dump_json without
    exclude) keeps the flat surfaces via computed_field default
    serialize."""
    from schemas import persist_result_json
    import json

    r = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True, camera_b_received=True,
        triangulated_by_algorithm={"v11_hsv_cc": [_tri_point()]},
        algorithms_completed={"v11_hsv_cc"},
        active_server_post_algorithm_id="v11_hsv_cc",
    )
    blob = persist_result_json(r)
    parsed = json.loads(blob)
    # Disk: dict-only canonical
    assert "triangulated_by_path" not in parsed
    assert "segments_by_path" not in parsed
    assert "frame_counts_by_path" not in parsed
    assert "paths_completed" not in parsed
    assert "live_config_used" not in parsed
    assert "server_post_config_used" not in parsed
    assert parsed["active_server_post_algorithm_id"] == "v11_hsv_cc"
    assert "v11_hsv_cc" in parsed["triangulated_by_algorithm"]
    assert "v11_hsv_cc" in parsed["algorithms_completed"]
    # Reload — flat surfaces project from dict via computed_field.
    r2 = SessionResult.model_validate_json(blob)
    assert "server_post" in r2.triangulated_by_path
    assert r2.paths_completed == {"server_post"}


# --- Drift guards ----------------------------------------------------------


def test_validate_runnable_id_rejects_ios_capture_time():
    algorithms.validate_id("ios_capture_time")
    algorithms.validate_runnable_id("v11_hsv_cc")
    try:
        algorithms.validate_runnable_id("ios_capture_time")
        raise AssertionError("should have rejected non-runnable id")
    except ValueError as e:
        assert "non-runnable" in str(e)


def test_drift_guard_catches_schemas_constant_mismatch(monkeypatch):
    import algorithms as algorithms_mod
    import schemas as schemas_mod
    monkeypatch.setattr(schemas_mod, "IOS_CAPTURE_TIME_ALGORITHM_ID", "drifted_value")
    try:
        algorithms_mod._check_schemas_constant_drift()
        raise AssertionError("drift guard should have raised")
    except RuntimeError as e:
        assert "drifted" in str(e)


def test_legacy_bucket_drift_guard_catches_unregistered_id(monkeypatch):
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


# --- end-to-end round-trip pinning multi-algorithm history ----------------


# --- DetectionConfigSnapshotPayload generic-params contract --------------
#
# These pin the post-platform-widening invariants for the snapshot wire
# shape: top-level `params` (no flat `hsv`/`shape_gate`), validator
# round-trips against the registered detector's params_schema, and
# IOS_CAPTURE_TIME bypasses the round-trip (no Detector for that
# data source).


def test_snapshot_wire_dump_uses_top_level_params_no_flat_keys():
    """Wire JSON must serialize `{algorithm_id, params, preset_name}`
    verbatim. Catches a regression where some helper re-introduced a
    legacy `hsv` / `shape_gate` flat surface — clients dispatch on
    `params.hsv.*` and would silently misread."""
    import json
    snap = _snapshot("v11_hsv_cc", preset="blue_ball")
    payload = json.loads(snap.model_dump_json())
    assert set(payload.keys()) == {"algorithm_id", "params", "preset_name"}
    assert "hsv" not in payload
    assert "shape_gate" not in payload
    assert payload["params"]["hsv"]["h_min"] == 10


def test_snapshot_validator_rejects_v11_params_missing_hsv():
    """Validator round-trips params through V11Params — missing keys
    raise pydantic.ValidationError before the snapshot reaches disk
    or wire."""
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        DetectionConfigSnapshotPayload(
            algorithm_id="v11_hsv_cc",
            params={"shape_gate": {"aspect_min": 0.7, "fill_min": 0.55}},
            # missing "hsv"
            preset_name=None,
        )


def test_snapshot_validator_rejects_v11_params_garbage_hsv_payload():
    """Validator rejects a wrong-shape `hsv` payload (string instead
    of int fields). Catches a class of disk-edit / agent-typo bugs at
    the load boundary, not at first detection run."""
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        DetectionConfigSnapshotPayload(
            algorithm_id="v11_hsv_cc",
            params={
                "hsv": "garbage_string_not_a_dict",
                "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
            },
            preset_name=None,
        )


def test_snapshot_validator_skips_round_trip_for_ios_capture_time():
    """`ios_capture_time` is a non-runnable data source — no Detector,
    no params_schema. The validator MUST skip schema round-trip
    (otherwise loading any historical iOS-stamped snapshot crashes).
    The iOS upload boundary is the validating step for that source."""
    snap = DetectionConfigSnapshotPayload(
        algorithm_id="ios_capture_time",
        params={"any": "shape", "even": {"nested": True}},  # not v11
        preset_name=None,
    )
    assert snap.algorithm_id == "ios_capture_time"
    assert snap.params == {"any": "shape", "even": {"nested": True}}


def test_set_algorithm_frames_round_trip_through_persist_and_reload():
    """End-to-end: write under multiple algorithm ids, persist, reload.
    All algorithm buckets survive — including ids OTHER than the
    current active_server_post pointer (multi-algorithm point)."""
    from detection_paths import set_algorithm_frames, stamp_server_post_run
    from schemas import persist_pitch_json
    import json

    snap_v11 = _snapshot("v11_hsv_cc")
    p = _base_pitch()
    stamp_server_post_run(p, snap_v11, [_frame(1)])
    set_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID, [_frame(10)])
    # Drop a future-algorithm bucket (not promoted to active):
    set_algorithm_frames(p, "ios_capture_time", [_frame(10)])
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(20), _frame(21)])

    blob = persist_pitch_json(p)
    parsed = json.loads(blob)
    assert "ios_capture_time" in parsed["frames_by_algorithm"]
    assert "v11_hsv_cc" in parsed["frames_by_algorithm"]
    assert parsed["active_server_post_algorithm_id"] == "v11_hsv_cc"

    p2 = PitchPayload.model_validate(parsed)
    assert len(p2.frames_by_algorithm["v11_hsv_cc"]) == 2
    assert len(p2.frames_by_algorithm["ios_capture_time"]) == 1
    # Projections agree post-reload:
    assert len(p2.frames_server_post) == 2
    assert len(p2.frames_live) == 1
