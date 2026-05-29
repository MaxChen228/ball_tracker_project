"""Fast-path tests for `State.set_active_server_post_algorithm`.

Dual-cam, time-synced sessions with cached results take the
re-stamp-only path (`stamp_active_pointer_projection`). Mono /
sync_error / cache-miss fall back to `rebuild_result_for_session`.
The fast path's output must be deep-equal to a full rebuild's so
the viewer history dropdown behaves identically — the only thing
that changes is whether `triangulate_pair` runs.

This file covers the regression risk introduced by the fast path:
silent drift between dispatch branches, in-place mutation leaking
to concurrent readers, and the eligibility checks letting unsafe
inputs through.
"""
from __future__ import annotations

import algorithms as algorithms_mod
import main
import session_results
from detection_paths import stamp_server_post_run
from schemas import (
    BlobCandidate,
    DetectionConfigSnapshotPayload,
    FramePayload,
    PitchPayload,
    SessionResult,
    TriangulatedPoint,
)


def _frame(idx: int) -> FramePayload:
    return FramePayload(
        frame_index=idx,
        timestamp_s=0.1 * idx,
        ball_detected=True,
        candidates=[BlobCandidate(
            px=10.0, py=20.0, area=100, area_score=1.0,
            aspect=1.0, fill=0.68,
        )],
    )


def _snapshot(alg_id: str) -> DetectionConfigSnapshotPayload:
    return DetectionConfigSnapshotPayload(
        algorithm_id=alg_id,
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30,
                    "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )


class _FakeTriangulatePair:
    """Deterministic stub: emits two points keyed off the alg's
    first frame_index. Records every call so tests can assert the
    fast path never invokes it. As of Phase 4-2 the rebuild path's
    triangulation entry point is `triangulate_all_pairs_for_session`
    (N-cam); signature: (state, pitches_by_cam: dict, *, source)."""
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, state, pitches_by_cam, *, source: str = "server"):
        any_pitch = next(iter(pitches_by_cam.values()))
        if not any_pitch.frames_server_post:
            return [], []
        marker = any_pitch.frames_server_post[0].frame_index
        self.calls.append(marker)
        pts = [
            TriangulatedPoint(
                t_rel_s=float(i) * 0.1,
                x_m=float(marker), y_m=0.0, z_m=0.0,
                residual_m=0.01, cost_a=0.0, cost_b=0.0,
                pair_key=("A","B"),
            )
            for i in range(2)
        ]
        return pts, []


def _register_fake_v12(monkeypatch) -> None:
    if "v12_test" in algorithms_mod._REGISTRY:
        return
    fake = algorithms_mod.AlgorithmEntry(
        algorithm_id="v12_test", label="t", description="t",
        detector=algorithms_mod._REGISTRY["v11_hsv_cc"].detector,
        cost_threshold=0.5,
    )
    monkeypatch.setitem(algorithms_mod._REGISTRY, "v12_test", fake)


def _record_pitch_with_alg(
    s: main.State, *, cam: str, sid: str, sync_id: str, alg_id: str,
    frame_idx: int,
) -> None:
    p = PitchPayload(
        camera_id=cam, session_id=sid,
        sync_id=sync_id, sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
    )
    stamp_server_post_run(p, _snapshot(alg_id), [_frame(frame_idx)])
    s.record(p)


def _build_dual_cam_session(s: main.State, monkeypatch, sid: str) -> None:
    """Plant a 2-cam, time-synced session with v11 + v12 frames on
    both pitches. Final `record()` triggers an initial rebuild that
    populates `state.results[sid]` with both alg buckets."""
    _register_fake_v12(monkeypatch)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    for cam in ("A", "B"):
        _record_pitch_with_alg(
            s, cam=cam, sid=sid, sync_id="sy_deadbeef",
            alg_id="v11_hsv_cc", frame_idx=11,
        )
        _record_pitch_with_alg(
            s, cam=cam, sid=sid, sync_id="sy_deadbeef",
            alg_id="v12_test", frame_idx=12,
        )


def _build_mono_session(s: main.State, monkeypatch, sid: str) -> None:
    """One-cam session: rebuild's mono branch adds the active alg to
    `algorithms_completed` without calling triangulate_pair."""
    _register_fake_v12(monkeypatch)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    _record_pitch_with_alg(
        s, cam="A", sid=sid, sync_id="sy_deadbeef",
        alg_id="v11_hsv_cc", frame_idx=11,
    )
    _record_pitch_with_alg(
        s, cam="A", sid=sid, sync_id="sy_deadbeef",
        alg_id="v12_test", frame_idx=12,
    )


def _eq_modulo_timestamps(
    a: SessionResult, b: SessionResult,
) -> tuple[bool, str]:
    """Deep-equal except for wall-clock fields that depend on call order.

    Set-valued fields (`algorithms_completed: set[str]`) get sorted before
    compare — pydantic v2 serialises a set to JSON list whose order is
    governed by Python hash randomisation, so the same content can flip
    iteration order between two model_dump calls in the same process.
    The schema-level invariant is "same membership", not "same order".
    """
    ad = a.model_dump(mode="json")
    bd = b.model_dump(mode="json")
    for k in ("solved_at", "server_post_ran_at"):
        ad.pop(k, None)
        bd.pop(k, None)
    # Schema-declared set fields: their JSON projection is an unordered
    # list. Normalise so a true content match isn't masked by hash order.
    for k in ("algorithms_completed",):
        if isinstance(ad.get(k), list): ad[k] = sorted(ad[k])
        if isinstance(bd.get(k), list): bd[k] = sorted(bd[k])
    if ad == bd:
        return True, ""
    diffs: list[str] = []
    for k in sorted(set(ad) | set(bd)):
        if ad.get(k) != bd.get(k):
            diffs.append(f"  {k}: {ad.get(k)!r} != {bd.get(k)!r}")
    return False, "\n".join(diffs)


# ---------------------------------------------------------------------------
# Fast-path eligibility + skip-triangulate
# ---------------------------------------------------------------------------

def test_fast_path_dual_cam_skips_triangulate(monkeypatch, tmp_path):
    """Switch on a dual-cam, time-synced session must NOT call
    triangulate_pair — the cached buckets are invariant under pure
    pointer flips. This is the core perf claim."""
    fake = _FakeTriangulatePair()
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")
    fake.calls.clear()  # Drop initial rebuild's calls.

    result = s.set_active_server_post_algorithm("s_fa57", "v11_hsv_cc")

    assert result is not None
    assert result.active_server_post_algorithm_id == "v11_hsv_cc"
    assert fake.calls == [], (
        f"fast path must skip triangulate_pair; got markers {fake.calls}"
    )
    # Both alg buckets must survive the switch — cached values reused.
    assert "v11_hsv_cc" in result.triangulated_by_algorithm
    assert "v12_test" in result.triangulated_by_algorithm


def test_fast_path_output_equals_rebuild_dual_cam(monkeypatch, tmp_path):
    """The fast path's SessionResult must be deep-equal (modulo
    timestamps) to a fresh rebuild. Drift between branches would
    surface as a viewer behaviour change depending on whether the
    cache hit was eligible — exactly the silent divergence
    CLAUDE.md's no-silent-fallback rule guards against."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")

    fast_result = s.set_active_server_post_algorithm(
        "s_fa57", "v11_hsv_cc",
    )
    rebuild_result = session_results.rebuild_result_for_session(
        s, "s_fa57",
    )
    assert fast_result is not None and rebuild_result is not None
    ok, diff = _eq_modulo_timestamps(fast_result, rebuild_result)
    assert ok, f"fast path diverged from rebuild:\n{diff}"


# ---------------------------------------------------------------------------
# Fall-back paths (mono / sync_error / missing bucket)
# ---------------------------------------------------------------------------

def test_mono_session_falls_back_to_rebuild(monkeypatch, tmp_path):
    """Single-cam session — `b is None` knocks the fast path out of
    eligibility; rebuild's mono branch adds the new active alg to
    `algorithms_completed` without triangulating."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_mono_session(s, monkeypatch, "s_a10ce")

    result = s.set_active_server_post_algorithm("s_a10ce", "v11_hsv_cc")

    assert result is not None
    assert result.active_server_post_algorithm_id == "v11_hsv_cc"
    assert "v11_hsv_cc" in result.algorithms_completed


def test_sync_error_falls_back_to_rebuild(monkeypatch, tmp_path):
    """Mismatched `sync_id` between A and B trips `validate_session_sync`.
    Fast path skips this case (cached bucket points were computed
    pre-sync-break and may not reflect the current pair-rejection
    semantics); rebuild surfaces `result.error`."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _register_fake_v12(monkeypatch)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_aaaa",
                sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_bbbb",
                sync_anchor_timestamp_s=0.0)
    for cam, sync_id in (("A", "sy_aaaa"), ("B", "sy_bbbb")):
        _record_pitch_with_alg(
            s, cam=cam, sid="s_53cee", sync_id=sync_id,
            alg_id="v11_hsv_cc", frame_idx=11,
        )
        _record_pitch_with_alg(
            s, cam=cam, sid="s_53cee", sync_id=sync_id,
            alg_id="v12_test", frame_idx=12,
        )

    result = s.set_active_server_post_algorithm("s_53cee", "v11_hsv_cc")
    assert result is not None
    assert result.error is not None and "sync" in result.error.lower(), (
        f"expected sync error surfaced via rebuild, got {result.error!r}"
    )


def test_missing_bucket_falls_back_to_rebuild(monkeypatch, tmp_path):
    """Hand-corrupt the cache so the target alg has frames but no
    `triangulated_by_algorithm` bucket (mimics a prior run where
    triangulate_pair raised, leaving only an `abort_reasons` entry).
    Fast path must defer to rebuild — the canonical path is where
    re-triangulation (and re-raising) belongs."""
    fake = _FakeTriangulatePair()
    monkeypatch.setattr(session_results, "triangulate_all_pairs_for_session", fake)
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")
    # Drop the cached v11 bucket so eligibility check fails.
    s.results["s_fa57"].triangulated_by_algorithm.pop("v11_hsv_cc", None)
    fake.calls.clear()

    result = s.set_active_server_post_algorithm("s_fa57", "v11_hsv_cc")

    assert result is not None
    assert "v11_hsv_cc" in result.triangulated_by_algorithm, (
        "rebuild should have rematerialised the missing bucket"
    )
    assert fake.calls, (
        "rebuild path expected to call triangulate_pair; got no calls"
    )


# ---------------------------------------------------------------------------
# Mutation isolation (copy-mutate-swap, not in-place)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PR #122 R1-NIT-1 / R1-NIT-4: re-republish merge + orphan-unlink
# (concurrent record / delete races during set_active_server_post_algorithm)
# ---------------------------------------------------------------------------

def test_set_active_does_not_clobber_concurrent_record(monkeypatch, tmp_path):
    """Regression for PR #122 R1-NIT-1: between the first disk write and
    the in-memory republish, a concurrent `record()` can publish fresher
    `frames_by_algorithm` / `config_used_by_algorithm` data. The pre-#122
    code clobbered with the stale deep-copy — silently dropping the fresh
    write from both memory and the post-republish disk write.

    Now: the pointer flip must apply ONLY to the pointer field on the
    LATEST in-memory pitch; everything else from the racing record() must
    survive."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")

    # Inject a race: when set_active flushes the first-round disk write
    # for cam A, simulate a concurrent record() that publishes a fresh
    # v12_test frame on cam A (e.g. a re-run that completed mid-flight).
    original_atomic_write = s._atomic_write
    raced: dict[str, bool] = {"fired": False}

    def racing_atomic_write(path, payload):
        original_atomic_write(path, payload)
        if not raced["fired"] and "session_s_fa57_A" in path.name:
            raced["fired"] = True
            # Concurrent record() publishes a fresh frame under v12_test.
            # The newer frame_idx (99) acts as a marker: if the
            # set_active deep-copy clobbered, the merged pitch in memory
            # would carry frame_idx=12 from the snapshot taken at the
            # top of set_active; if the re-republish merge is correct,
            # it carries frame_idx=99 from this race.
            _record_pitch_with_alg(
                s, cam="A", sid="s_fa57", sync_id="sy_deadbeef",
                alg_id="v12_test", frame_idx=99,
            )

    monkeypatch.setattr(s, "_atomic_write", racing_atomic_write)

    result = s.set_active_server_post_algorithm("s_fa57", "v11_hsv_cc")
    assert result is not None
    # The fresh-record's frame_idx=99 frame must survive on cam A under
    # v12_test. Pre-#122 silent clobber would have left frame_idx=12.
    cam_a_pitch = s.pitches[("A", "s_fa57")]
    v12_frames = cam_a_pitch.frames_by_algorithm.get("v12_test", [])
    frame_indices = [f.frame_index for f in v12_frames]
    assert 99 in frame_indices, (
        f"concurrent record() was silently clobbered; v12 frame indices "
        f"on cam A = {frame_indices}, expected 99 to survive"
    )
    # Pointer flip still landed.
    assert cam_a_pitch.active_server_post_algorithm_id == "v11_hsv_cc"


def test_set_active_unlinks_orphan_disk_file_when_cam_pitch_deleted(
    monkeypatch, tmp_path,
):
    """Regression for PR #122 R1-NIT-4: if cam A's pitch is deleted (via
    delete_session/remove_pitch) between the first disk write and the
    re-republish, that disk file is an orphan — the in-memory absence is
    the source of truth. Leaving it would resurrect the tombstoned pitch
    on next boot.

    Forces the race by setting `pitches[(A, sid)] = None` mid-write,
    then verifies the cam-A pitch file is unlinked when set_active
    finishes."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")

    cam_a_path = s._pitch_path("A", "s_fa57")
    assert cam_a_path.exists(), "fixture setup: cam A pitch on disk"

    # Inject the delete race: when set_active flushes cam A's first
    # disk write, simulate a delete_session that drops cam A's pitch
    # from the in-memory map.
    original_atomic_write = s._atomic_write
    raced: dict[str, bool] = {"fired": False}

    def racing_atomic_write(path, payload):
        original_atomic_write(path, payload)
        if not raced["fired"] and "session_s_fa57_A" in path.name:
            raced["fired"] = True
            # Simulate a cam-level pitch eviction during the race window.
            with s._lock:
                s.pitches.pop(("A", "s_fa57"), None)

    monkeypatch.setattr(s, "_atomic_write", racing_atomic_write)

    result = s.set_active_server_post_algorithm("s_fa57", "v11_hsv_cc")
    assert result is not None  # cam B still present → operation continues
    # Cam A pitch file must have been unlinked (orphan from first-round
    # write). Pre-fix code left it on disk, resurrecting the pitch on
    # next State() init.
    assert not cam_a_path.exists(), (
        "set_active must unlink the orphan first-round disk write when "
        "the cam-level pitch was deleted mid-flight"
    )


def test_set_active_repersists_merged_pitch(monkeypatch, tmp_path):
    """Regression for PR #122 R1-NIT-1 disk side: after the in-memory
    republish merges fresh-record fields with the pointer flip, the
    merged pitch must be re-written to disk. Without the re-persist,
    memory holds the merged state but disk holds either the stale
    first-round write OR the racing record()'s write — a restart would
    silently lose one side.

    Verifies the second-round write actually occurred by counting writes
    for the cam-A pitch path."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")

    original_atomic_write = s._atomic_write
    write_counts: dict[str, int] = {}
    raced: dict[str, bool] = {"fired": False}

    def counting_atomic_write(path, payload):
        write_counts[path.name] = write_counts.get(path.name, 0) + 1
        original_atomic_write(path, payload)
        # Trigger the concurrent record() exactly once during cam A's
        # first-round write. The R1-NIT-1 fix re-persists the merged
        # pitch in a second-round write — without it, only one write
        # per cam pitch would occur during set_active.
        if not raced["fired"] and "session_s_fa57_A" in path.name:
            raced["fired"] = True
            _record_pitch_with_alg(
                s, cam="A", sid="s_fa57", sync_id="sy_deadbeef",
                alg_id="v12_test", frame_idx=77,
            )

    write_counts.clear()
    monkeypatch.setattr(s, "_atomic_write", counting_atomic_write)

    result = s.set_active_server_post_algorithm("s_fa57", "v11_hsv_cc")
    assert result is not None

    cam_a_pitch_writes = write_counts.get("session_s_fa57_A.json", 0)
    # Expected writes for cam A during set_active:
    #   1) first-round write (line ~1585) of the pre-merge deep-copy
    #   2) re-persist after the merge (the R1-NIT-1 fix)
    # The racing record() also writes once, so total ≥ 3. Either way,
    # the set_active path itself must contribute ≥ 2 writes for cam A.
    assert cam_a_pitch_writes >= 3, (
        f"expected ≥3 writes on cam A pitch (first-round + re-persist + "
        f"racing record), got {cam_a_pitch_writes}; without R1-NIT-1 the "
        f"merged pitch is not re-persisted to disk"
    )


def test_prior_result_reference_not_mutated(monkeypatch, tmp_path):
    """Critical concurrency invariant: a reader holding a reference to
    `state.results[sid]` before the switch must see the OLD result
    unchanged after the switch returns. `stamp_segments_on_result`
    mutates dicts in-place, so the fast path's `model_copy(deep=True)`
    guards against in-flight SSE serializers / `/results/{sid}` GETs
    observing a half-stamped object."""
    monkeypatch.setattr(
        session_results, "triangulate_all_pairs_for_session", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_dual_cam_session(s, monkeypatch, "s_fa57")

    prior = s.results["s_fa57"]
    prior_active = prior.active_server_post_algorithm_id
    prior_segments_snapshot = list(prior.segments)
    prior_triangulated_keys = set(prior.triangulated_by_algorithm.keys())

    new_result = s.set_active_server_post_algorithm(
        "s_fa57", "v11_hsv_cc",
    )

    assert new_result is not None
    assert new_result is not prior, (
        "fast path returned the same object — copy-mutate-swap broken"
    )
    assert new_result.active_server_post_algorithm_id == "v11_hsv_cc"
    # Old reference must be untouched.
    assert prior.active_server_post_algorithm_id == prior_active
    assert prior.segments == prior_segments_snapshot
    assert set(prior.triangulated_by_algorithm.keys()) == prior_triangulated_keys
