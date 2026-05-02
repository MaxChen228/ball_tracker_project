"""Phase 6b — algorithm-id-keyed accessor contract.

Pins the read/write API that `POST /sessions/{sid}/runs/{algorithm_id}`
(Phase 7) will build on. The accessors live in `detection_paths` so
the path-keyed and algorithm-keyed views share resolution logic
(`live → ios_capture_time`, `server_post → stamped or legacy alg id`).
"""
from __future__ import annotations

from detection_paths import (
    algorithm_id_for_path,
    get_algorithm_frames,
    get_path_frames,
    pitch_with_algorithm_frames,
    set_algorithm_frames,
)
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    DetectionPath,
    FramePayload,
    HSVRangePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    ShapeGatePayload,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(px=10.0, py=20.0, area=100, area_score=1.0)],
    )


def _snapshot(alg_id: str) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        hsv=HSVRangePayload(h_min=10, h_max=20, s_min=30, s_max=200, v_min=40, v_max=210),
        shape_gate=ShapeGatePayload(aspect_min=0.7, fill_min=0.55),
        preset_name=None,
    )


def _pitch(**kw) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id="s_deadbeef",
        video_start_pts_s=0.0,
        **kw,
    )


def test_algorithm_id_for_live_path_is_ios_capture_time():
    p = _pitch()
    assert algorithm_id_for_path(p, DetectionPath.live) == IOS_CAPTURE_TIME_ALGORITHM_ID


def test_algorithm_id_for_server_post_reads_snapshot_stamp():
    snap = _snapshot("v11_hsv_cc")
    p = _pitch(server_post_config_used=snap)
    assert algorithm_id_for_path(p, DetectionPath.server_post) == "v11_hsv_cc"


def test_algorithm_id_for_server_post_falls_back_to_legacy_when_no_snapshot():
    p = _pitch()
    # Legacy bucket — pre-Phase-2 pitches lack the snapshot.
    assert algorithm_id_for_path(p, DetectionPath.server_post) == "v11_hsv_cc"


def test_get_algorithm_frames_returns_empty_for_unrun_algorithm():
    """Match `get_path_frames` invariant: callers don't have to guard."""
    p = _pitch()
    assert get_algorithm_frames(p, "v12_xyz") == []


def test_get_algorithm_frames_returns_frames_after_validator_mirror():
    p = _pitch(frames_live=[_frame(1), _frame(2)])
    assert len(get_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID)) == 2


def test_set_algorithm_frames_for_ios_capture_time_syncs_frames_live():
    """Back-compat: writers using the new algorithm-keyed API must
    leave path-keyed readers seeing the same frames. ios_capture_time
    is the canonical mirror of frames_live."""
    p = _pitch()
    set_algorithm_frames(p, IOS_CAPTURE_TIME_ALGORITHM_ID, [_frame(1), _frame(2)])
    assert len(p.frames_live) == 2
    assert get_path_frames(p, DetectionPath.live) == p.frames_live


def test_set_algorithm_frames_for_current_server_post_alg_syncs_frames_server_post():
    snap = _snapshot("v11_hsv_cc")
    p = _pitch(server_post_config_used=snap)
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(1), _frame(2), _frame(3)])
    assert len(p.frames_server_post) == 3


def test_set_algorithm_frames_for_other_alg_leaves_frames_server_post_alone():
    """Writing v12 frames while v11 is the stamped server_post must
    NOT clobber the v11 frames in `frames_server_post`. v12 lives
    only in the dict until a future endpoint promotes it."""
    snap = _snapshot("v11_hsv_cc")
    v11_frames = [_frame(1), _frame(2)]
    p = _pitch(server_post_config_used=snap, frames_server_post=v11_frames)
    set_algorithm_frames(p, "v12_future", [_frame(10), _frame(11), _frame(12)])
    # v11 untouched in legacy bucket:
    assert len(p.frames_server_post) == 2
    # v12 in the dict only:
    assert len(p.frames_by_algorithm["v12_future"]) == 3
    assert "v11_hsv_cc" in p.frames_by_algorithm  # mirrored from frames_server_post


def test_set_algorithm_frames_legacy_pre_snapshot_writes_through_to_server_post():
    """Pre-snapshot pitches: `server_post_config_used is None`. The
    legacy bucket fallback is `v11_hsv_cc`; writing under that id
    should still sync `frames_server_post` so existing readers see
    the new frames."""
    p = _pitch()
    set_algorithm_frames(p, "v11_hsv_cc", [_frame(1)])
    assert len(p.frames_server_post) == 1


def test_stamp_server_post_run_accumulates_two_algorithms(monkeypatch):
    """Phase 7 multi-algorithm contract: running v11 then v12 in the
    same session must leave BOTH algorithms' frames on disk. The
    legacy `frames_server_post` reflects the most recent run; the
    dict accumulates both. Monkeypatches a fake `v12_test` into the
    registry so this test runs today (v11 is the only real entry as
    of Phase 7) and stays valid when v12 lands."""
    import algorithms as algorithms_mod
    from detection_paths import stamp_server_post_run
    from schemas import persist_pitch_json
    import json

    fake = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_test",
        label="test",
        description="test",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
    )
    patched = dict(algorithms_mod._REGISTRY)
    patched["v12_test"] = fake
    monkeypatch.setattr(algorithms_mod, "_REGISTRY", patched)

    p = _pitch()
    snap_v11 = _snapshot("v11_hsv_cc")
    stamp_server_post_run(p, snap_v11, [_frame(1), _frame(2)])
    blob1 = persist_pitch_json(p)
    parsed1 = json.loads(blob1)
    assert len(parsed1["frames_by_algorithm"]["v11_hsv_cc"]) == 2
    assert len(parsed1["frames_server_post"]) == 2
    assert parsed1["server_post_config_used"]["algorithm_id"] == "v11_hsv_cc"

    snap_v12 = _snapshot("v12_test")
    stamp_server_post_run(p, snap_v12, [_frame(10), _frame(11), _frame(12)])
    blob2 = persist_pitch_json(p)
    parsed2 = json.loads(blob2)
    assert len(parsed2["frames_by_algorithm"]["v11_hsv_cc"]) == 2
    assert len(parsed2["frames_by_algorithm"]["v12_test"]) == 3
    assert len(parsed2["frames_server_post"]) == 3
    assert parsed2["server_post_config_used"]["algorithm_id"] == "v12_test"
    p2 = PitchPayload.model_validate(parsed2)
    assert len(p2.frames_by_algorithm["v11_hsv_cc"]) == 2
    assert len(p2.frames_by_algorithm["v12_test"]) == 3


def test_stamp_server_post_run_rejects_ios_capture_time():
    """Storage-correctness guard: `ios_capture_time` has special
    semantics in `set_algorithm_frames` (back-syncs `frames_live`,
    not `frames_server_post`) that would corrupt the server-post
    slot if combined with the snapshot mutation `stamp_server_post_run`
    performs."""
    from detection_paths import stamp_server_post_run

    p = _pitch()
    snap_ios = _snapshot("ios_capture_time")
    try:
        stamp_server_post_run(p, snap_ios, [_frame(1)])
        raise AssertionError("should have rejected ios_capture_time")
    except ValueError as e:
        assert "ios_capture_time" in str(e)


def test_stamp_server_post_run_rerun_same_algorithm_overwrites():
    """Re-running the same algorithm overwrites its bucket — same
    semantics as the legacy single-slot behaviour for v11-only
    workflows. Pin so a future "always-append" change is caught."""
    from detection_paths import stamp_server_post_run

    p = _pitch()
    snap_v11 = _snapshot("v11_hsv_cc")
    stamp_server_post_run(p, snap_v11, [_frame(1), _frame(2), _frame(3)])
    stamp_server_post_run(p, snap_v11, [_frame(10)])
    assert len(p.frames_by_algorithm["v11_hsv_cc"]) == 1
    assert len(p.frames_server_post) == 1


def test_state_record_merge_preserves_existing_dict_buckets(tmp_path, monkeypatch):
    """Phase 7-fix Block 2: state.record's in-memory merge runs
    BEFORE persist (where the after-validator regenerates dict from
    old fields). If existing pitch has v11 in dict but the incoming
    pitch was constructed without v11 (e.g., a fresh re-record
    carrying only v12), the merge must carry v11 forward — otherwise
    multi-algorithm history is silently dropped on disk."""
    import algorithms as algorithms_mod
    from detection_paths import stamp_server_post_run
    import main

    fake = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_test",
        label="test",
        description="test",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v12_test", fake)

    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)

    # First record: stamp v11 frames.
    p1 = _pitch()
    stamp_server_post_run(p1, _snapshot("v11_hsv_cc"), [_frame(1), _frame(2)])
    s.record(p1)

    # Second record: a NEW pitch object (simulating a re-record from
    # an upload that doesn't know about v11 history) carrying only
    # v12. The merge must still preserve v11 in the dict.
    p2 = _pitch()
    stamp_server_post_run(p2, _snapshot("v12_test"), [_frame(10), _frame(11), _frame(12)])
    s.record(p2)

    merged = s.pitches[("A", "s_deadbeef")]
    assert "v11_hsv_cc" in merged.frames_by_algorithm, (
        "v11 history was lost across re-record; "
        "state.record merge must preserve existing dict buckets"
    )
    assert len(merged.frames_by_algorithm["v11_hsv_cc"]) == 2
    assert len(merged.frames_by_algorithm["v12_test"]) == 3
    assert "v11_hsv_cc" in merged.config_used_by_algorithm
    assert "v12_test" in merged.config_used_by_algorithm


def test_state_record_merge_deep_copies_preserved_frames(tmp_path, monkeypatch):
    """Phase 7-fix-2 (2/2): the merge that carries forward existing dict
    buckets must deep-copy frames, not pass list references through.
    FramePayload is not frozen; without deep-copy, any future in-place
    mutation on the merged pitch would bleed back into the dict the
    test still holds a reference to via `existing`."""
    import algorithms as algorithms_mod
    import main

    fake = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_test",
        label="test", description="test",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v12_test", fake)

    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)

    p1 = _pitch()
    stamp_frames = [_frame(1), _frame(2)]
    from detection_paths import stamp_server_post_run
    stamp_server_post_run(p1, _snapshot("v11_hsv_cc"), stamp_frames)
    s.record(p1)
    p1_v11_frame = s.pitches[("A", "s_deadbeef")].frames_by_algorithm["v11_hsv_cc"][0]

    p2 = _pitch()
    stamp_server_post_run(p2, _snapshot("v12_test"), [_frame(10)])
    s.record(p2)

    merged = s.pitches[("A", "s_deadbeef")]
    merged_v11_frame = merged.frames_by_algorithm["v11_hsv_cc"][0]
    # Same content, different object identity — deep-copied.
    assert merged_v11_frame.frame_index == p1_v11_frame.frame_index
    assert merged_v11_frame is not p1_v11_frame


def test_pitch_with_algorithm_frames_projects_into_server_post_slot():
    """Counterpart to `pitch_with_path_frames`. Downstream code
    (reconstruct, rays) reads `frames_server_post` on the clone."""
    snap_v11 = _snapshot("v11_hsv_cc")
    p = _pitch(
        server_post_config_used=snap_v11,
        frames_server_post=[_frame(1)],
        frames_by_algorithm={"v12_future": [_frame(10), _frame(11)]},
    )
    clone = pitch_with_algorithm_frames(p, "v12_future")
    assert len(clone.frames_server_post) == 2
    # Original untouched:
    assert len(p.frames_server_post) == 1
