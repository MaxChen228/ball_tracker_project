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
    fast path never invokes it."""
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, state, a, b, *, source: str = "server"):
        if not a.frames_server_post:
            return []
        marker = a.frames_server_post[0].frame_index
        self.calls.append(marker)
        return [
            TriangulatedPoint(
                t_rel_s=float(i) * 0.1,
                x_m=float(marker), y_m=0.0, z_m=0.0,
                residual_m=0.01, cost_a=0.0, cost_b=0.0,
            )
            for i in range(2)
        ]


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
    """Deep-equal except for wall-clock fields that depend on call order."""
    ad = a.model_dump(mode="json")
    bd = b.model_dump(mode="json")
    for k in ("solved_at", "server_post_ran_at"):
        ad.pop(k, None)
        bd.pop(k, None)
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
    monkeypatch.setattr(session_results, "triangulate_pair", fake)
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
        session_results, "triangulate_pair", _FakeTriangulatePair(),
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
        session_results, "triangulate_pair", _FakeTriangulatePair(),
    )
    s = main.State(data_dir=tmp_path)
    _build_mono_session(s, monkeypatch, "s_a10ce")

    result = s.set_active_server_post_algorithm("s_a10ce", "v11_hsv_cc")

    assert result is not None
    assert result.active_server_post_algorithm_id == "v11_hsv_cc"
    assert "v11_hsv_cc" in result.algorithms_completed


def test_sync_error_falls_back_to_rebuild(monkeypatch, tmp_path):
    """Mismatched `sync_id` between A and B trips `validate_pair_sync`.
    Fast path skips this case (cached bucket points were computed
    pre-sync-break and may not reflect the current pair-rejection
    semantics); rebuild surfaces `result.error`."""
    monkeypatch.setattr(
        session_results, "triangulate_pair", _FakeTriangulatePair(),
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
    monkeypatch.setattr(session_results, "triangulate_pair", fake)
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

def test_prior_result_reference_not_mutated(monkeypatch, tmp_path):
    """Critical concurrency invariant: a reader holding a reference to
    `state.results[sid]` before the switch must see the OLD result
    unchanged after the switch returns. `stamp_segments_on_result`
    mutates dicts in-place, so the fast path's `model_copy(deep=True)`
    guards against in-flight SSE serializers / `/results/{sid}` GETs
    observing a half-stamped object."""
    monkeypatch.setattr(
        session_results, "triangulate_pair", _FakeTriangulatePair(),
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
