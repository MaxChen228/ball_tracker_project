"""Regression tests for server-side silent-fallback removals.

Two paths historically masked invariant violations with `or` shims:

1. `session_results.py`: legacy `result.points` used to fall back from
   server_post to live (`result.triangulated_by_path.get(...) or []`).
   Research-mode invariant: a missing path's points must read empty,
   not silently substitute another path's points. Cross-path
   substitution corrupts live-vs-server_post comparisons.

2. `pipeline.py`: server-side per-frame detection wrote
   `candidates=blobs if winner else (blobs or None)`. Empty list
   ("detector ran, found 0 candidates") collapsed to None ("no
   detection attempted"). These two states must remain distinguishable.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. session_results.py — explicit per-path selection, no cross-path fallback
# ---------------------------------------------------------------------------


def _make_triangulated_point():
    """Minimal TriangulatedPoint stub with required fields."""
    from schemas import TriangulatedPoint

    return TriangulatedPoint(
        t_rel_s=0.0,
        x_m=1.0,
        y_m=2.0,
        z_m=3.0,
        residual_m=0.01,
        cost_a=None,
        cost_b=None,
    )


def test_legacy_points_no_silent_fallback_to_live_when_server_post_requested():
    """If the caller selected server_post for the legacy surface but
    server_post produced nothing, `result.points` MUST stay empty even
    after segment stamping sees live points.
    """
    from schemas import DetectionPath, SessionResult
    from session_results import stamp_segments_on_result

    from schemas import IOS_CAPTURE_TIME_ALGORITHM_ID
    live_pt = _make_triangulated_point()
    result = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True,
        camera_b_received=True,
        triangulated_by_algorithm={IOS_CAPTURE_TIME_ALGORITHM_ID: [live_pt]},
        algorithms_completed={IOS_CAPTURE_TIME_ALGORITHM_ID},
        # server_post entry absent → simulates "server_post never ran /
        # produced no triangulation"
    )

    stamp_segments_on_result(
        result,
        legacy_points_path=DetectionPath.server_post,
    )

    assert result.triangulated == [live_pt]
    assert result.points == [], (
        "server_post requested but missing must yield empty points; "
        "silently borrowing live points would contaminate "
        "live-vs-server_post comparisons."
    )
    assert result.segments == []


def test_legacy_points_uses_live_only_when_server_post_not_requested():
    from schemas import DetectionPath, SessionResult
    from session_results import stamp_segments_on_result

    from schemas import IOS_CAPTURE_TIME_ALGORITHM_ID
    live_pt = _make_triangulated_point()
    result = SessionResult(
        session_id="s_deadbeef",
        camera_a_received=True,
        camera_b_received=True,
        triangulated_by_algorithm={IOS_CAPTURE_TIME_ALGORITHM_ID: [live_pt]},
        algorithms_completed={IOS_CAPTURE_TIME_ALGORITHM_ID},
    )

    stamp_segments_on_result(result, legacy_points_path=DetectionPath.live)

    assert result.points == [live_pt]


def test_session_results_module_uses_explicit_branches():
    """Source-level guard: confirm the silent `or []` pattern is gone
    from the legacy-points selection so future edits don't reintroduce
    it without flipping this test."""
    from pathlib import Path

    src = Path(__file__).parent / "session_results.py"
    text = src.read_text()
    # The exact removed shim:
    assert "result.triangulated_by_path.get(DetectionPath.live.value)\n            or []" not in text, (
        "silent fallback `... .get(live) or []` reintroduced — see "
        "CLAUDE.md 'Experimental phase — 禁止 silent fallback'."
    )
    assert "or result.triangulated_by_path.get(DetectionPath.live.value" not in text, (
        "silent server_post→live fallback reintroduced in the recompute "
        "or segment-stamping path."
    )


# ---------------------------------------------------------------------------
# 2. pipeline.py — empty list ≠ None
# ---------------------------------------------------------------------------


def test_pipeline_pass_through_empty_blobs_list_not_none():
    """Source-level invariant: pipeline.py must NOT collapse an empty
    blobs list to None. Reader code distinguishes 'detector ran with
    0 candidates' (empty list) from 'detector did not run' (None);
    `blobs or None` confused these and obscured detection failures."""
    from pathlib import Path

    src = Path(__file__).parent / "pipeline.py"
    text = src.read_text()
    assert "blobs or None" not in text, (
        "Reintroduced `blobs or None` silent fallback in pipeline.py. "
        "Empty blobs list must propagate as []; use `candidates=blobs`."
    )


def test_framepayload_accepts_empty_candidates_list():
    """Schema must permit candidates=[] (the post-fix default for
    detector-ran-but-nothing-found). Regression guard against schema
    drift that would force pipeline.py back into a fallback shim."""
    from schemas import FramePayload

    fp = FramePayload(
        frame_index=0,
        timestamp_s=0.0,
        px=None,
        py=None,
        ball_detected=False,
        candidates=[],
    )
    assert fp.candidates == []
    assert fp.candidates is not None


# ---------------------------------------------------------------------------
# 3. state.py — corrupt pairing_tuning.json must raise, not silent-default
# ---------------------------------------------------------------------------


def test_corrupt_pairing_tuning_raises_not_silent_default(tmp_path):
    """A present-but-corrupt `pairing_tuning.json` historically reverted
    to `PairingTuning.default()` via a bare `except Exception`. That
    silently hid hand-edit typos and contaminated comparisons across
    pairing parameter sweeps. The loader must raise so the operator
    sees the parse error immediately."""
    import pytest
    from state import State

    # Pre-create a corrupt pairing_tuning.json before constructing State.
    pairing_path = tmp_path / "pairing_tuning.json"
    pairing_path.write_text("{this is not json")

    with pytest.raises(ValueError, match=r"pairing_tuning\.json"):
        State(data_dir=tmp_path)


def test_corrupt_pairing_tuning_missing_field_raises(tmp_path):
    """Even if the JSON parses, missing the required `gap_threshold_m`
    field must raise — silent fall-through to default would mask a
    schema-drift bug after a manual hand-edit."""
    import pytest
    from state import State

    pairing_path = tmp_path / "pairing_tuning.json"
    pairing_path.write_text('{"some_other_field": 0.5}')

    with pytest.raises(ValueError, match="gap_threshold_m"):
        State(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# 4. state.py — corrupt session_meta.json must raise, not silently drop trash
# ---------------------------------------------------------------------------


def test_corrupt_session_meta_raises_not_silent_drop(tmp_path):
    """A present-but-corrupt `session_meta.json` historically returned
    early on parse failure, silently un-trashing every session the
    operator had hidden. Silent revert of operator-controlled state is
    research-contaminating. The loader must raise so the failure is
    visible at boot."""
    import pytest
    from state import State

    meta_path = tmp_path / "session_meta.json"
    meta_path.write_text("{broken json")

    with pytest.raises(ValueError, match=r"session_meta\.json"):
        State(data_dir=tmp_path)


def test_corrupt_session_meta_wrong_type_raises(tmp_path):
    """`session_meta.json` whose root is an array (not an object), or
    whose `trashed_sessions` value is malformed, must raise — not
    silently skip the malformed slot."""
    import pytest
    from state import State

    meta_path = tmp_path / "session_meta.json"
    meta_path.write_text('["wrong", "type"]')

    with pytest.raises(ValueError, match=r"session_meta\.json.*JSON object"):
        State(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# 5. state_runtime.py — corrupt runtime_settings.json must raise
# ---------------------------------------------------------------------------


def test_corrupt_runtime_settings_raises_not_silent_revert(tmp_path):
    """A present-but-corrupt `runtime_settings.json` historically
    swallowed the parse error and silently reverted to ctor seed
    defaults (chirp=0.18, heartbeat=1.0, capture=1080, etc). Operator
    hand-edits a value to a typo'd string → thinks they're testing
    chirp=0.18 but actually at the default. The loader must raise so
    boot dies visibly."""
    import pytest
    from state_runtime import RuntimeSettingsStore

    settings_path = tmp_path / "runtime_settings.json"
    settings_path.write_text("not valid json {")

    def _atomic_write(path, payload):
        path.write_text(payload)

    with pytest.raises(ValueError, match=r"runtime_settings\.json"):
        RuntimeSettingsStore(settings_path, atomic_write=_atomic_write)


def test_runtime_settings_missing_required_field_raises(tmp_path):
    """A parseable but incomplete `runtime_settings.json` (e.g. missing
    `chirp_detect_threshold`) must raise — silent fallback to the
    seed default for that one field would create a half-applied state
    that's hard to debug."""
    import json
    import pytest
    from state_runtime import RuntimeSettingsStore

    settings_path = tmp_path / "runtime_settings.json"
    # Valid JSON but missing required fields.
    settings_path.write_text(json.dumps({"capture_height_px": 1080}))

    def _atomic_write(path, payload):
        path.write_text(payload)

    with pytest.raises(ValueError, match="chirp_detect_threshold"):
        RuntimeSettingsStore(settings_path, atomic_write=_atomic_write)


# ---------------------------------------------------------------------------
# 6. pairing.py — frame.candidates is the sole source for fan-out
# ---------------------------------------------------------------------------


def test_pairing_no_synthetic_candidate_from_px_py():
    """`_frame_candidates` must NOT synthesize a stand-in BlobCandidate
    from `frame.px / frame.py` when `frame.candidates` is empty. The
    previous synthesis carried cost=None which bypassed the cost
    ceiling gate (None > x → False), letting frames that never went
    through the selector contaminate triangulation. Per CLAUDE.md
    silent-fallback rule, frames without explicit candidates produce
    zero triangulated points."""
    from schemas import FramePayload
    from pairing import _frame_candidates, _valid_frame

    # Frame with px/py but no candidates list — used to synth a stand-in.
    f = FramePayload(
        frame_index=0, timestamp_s=0.0,
        px=100.0, py=200.0,
        ball_detected=True,
    )
    assert _frame_candidates(f) == [], (
        "frame with px/py but no candidates must produce empty list — "
        "synthetic stand-in reintroduced?"
    )
    assert _valid_frame(f) is False


# ---------------------------------------------------------------------------
# 7. detection_paths.algorithm_id_for_path — no legacy fallback
# ---------------------------------------------------------------------------


def test_algorithm_id_for_path_raises_when_server_post_pointer_missing():
    """`algorithm_id_for_path(pitch, server_post)` MUST raise when
    `pitch.active_server_post_algorithm_id is None`. The legacy
    fallback (`return _LEGACY_PRE_SNAPSHOT_ALGORITHM_ID`) was a silent
    drop into the v11_hsv_cc bucket — that contradicted the
    `frames_server_post` computed_field docstring which already states
    'no silent fallback to a legacy bucket'."""
    import pytest
    from detection_paths import algorithm_id_for_path
    from schemas import BlobCandidate, DetectionPath, FramePayload, PitchPayload

    p = PitchPayload(
        camera_id="A",
        session_id="s_deadbeef",
        sync_id="sy_deadbeef",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        video_fps=240.0,
        frames_by_algorithm={"ios_capture_time": [
            FramePayload(
                frame_index=0, timestamp_s=0.0, ball_detected=True,
                candidates=[BlobCandidate(
                    px=10.0, py=20.0, area=100, area_score=1.0,
                    aspect=1.0, fill=0.68,
                )],
            )
        ]},
        # No active_server_post_algorithm_id set.
    )
    # live path resolves cleanly.
    assert algorithm_id_for_path(p, DetectionPath.live) == "ios_capture_time"
    # server_post raises.
    with pytest.raises(ValueError, match="active_server_post_algorithm_id"):
        algorithm_id_for_path(p, DetectionPath.server_post)


# ---------------------------------------------------------------------------
# 8. BlobCandidate.aspect / fill — required
# ---------------------------------------------------------------------------


def test_blob_candidate_aspect_required():
    """`BlobCandidate.aspect` is required. Treating None as zero
    penalty in the selector let unscored candidates win on tie-break."""
    import pytest
    from pydantic import ValidationError
    from schemas import BlobCandidate

    with pytest.raises(ValidationError):
        BlobCandidate(px=1.0, py=2.0, area=100, area_score=1.0, fill=0.68)


def test_blob_candidate_fill_required():
    import pytest
    from pydantic import ValidationError
    from schemas import BlobCandidate

    with pytest.raises(ValidationError):
        BlobCandidate(px=1.0, py=2.0, area=100, area_score=1.0, aspect=1.0)


# ---------------------------------------------------------------------------
# 9. HSVRangePayload / ShapeGatePayload — Field bounds enforced
# ---------------------------------------------------------------------------


def test_hsv_range_payload_rejects_hue_above_179():
    """OpenCV's `cv2.inRange` interprets hue in 0-179 (each step = 2°).
    A 0-360 input from a UI picker MUST be rejected at the boundary —
    silently wrapping 210 to 30 (the green band) used to be a real
    silent-drift failure mode."""
    import pytest
    from pydantic import ValidationError
    from schemas import HSVRangePayload

    with pytest.raises(ValidationError):
        HSVRangePayload(h_min=0, h_max=210, s_min=0, s_max=255,
                        v_min=0, v_max=255)


def test_hsv_range_payload_rejects_negative_hue():
    import pytest
    from pydantic import ValidationError
    from schemas import HSVRangePayload

    with pytest.raises(ValidationError):
        HSVRangePayload(h_min=-1, h_max=100, s_min=0, s_max=255,
                        v_min=0, v_max=255)


def test_hsv_range_payload_rejects_saturation_above_255():
    import pytest
    from pydantic import ValidationError
    from schemas import HSVRangePayload

    with pytest.raises(ValidationError):
        HSVRangePayload(h_min=0, h_max=100, s_min=0, s_max=300,
                        v_min=0, v_max=255)


def test_shape_gate_payload_rejects_aspect_above_one():
    import pytest
    from pydantic import ValidationError
    from schemas import ShapeGatePayload

    with pytest.raises(ValidationError):
        ShapeGatePayload(aspect_min=2.0, fill_min=0.5)


def test_shape_gate_payload_rejects_negative_fill():
    import pytest
    from pydantic import ValidationError
    from schemas import ShapeGatePayload

    with pytest.raises(ValidationError):
        ShapeGatePayload(aspect_min=0.7, fill_min=-0.1)


# ---------------------------------------------------------------------------
# 10. state.stamp_server_post_config — returns None on store_result race
# ---------------------------------------------------------------------------


def test_stamp_server_post_config_returns_none_when_session_deleted(tmp_path):
    """`stamp_server_post_config` previously returned an empty
    SessionResult shell when the session was deleted between record()
    and the stamp call. The caller in routes/pitch.py used that shell
    to broadcast a `fit` SSE — segments from a result that never
    landed in `state.results`. Now it must return None and the caller
    must skip the broadcast."""
    from state import State
    s = State(data_dir=tmp_path)
    # No record() ever called for this sid → results dict empty.
    from schemas import DetectionConfigSnapshotPayload
    snap = DetectionConfigSnapshotPayload(
        algorithm_id="v11_hsv_cc",
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30, "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )
    result = s.stamp_server_post_config("s_neverexisted", snap)
    assert result is None, (
        "stamp_server_post_config must return None when the session is "
        "missing — silent SessionResult shell would mislead callers into "
        "broadcasting `fit` SSE for a non-existent result."
    )


# ---------------------------------------------------------------------------
# 10b. stamp_server_post_config — content-check race guard (PR #122 fix)
# ---------------------------------------------------------------------------


def test_stamp_server_post_config_survives_concurrent_record_republish(
    monkeypatch, tmp_path,
):
    """Regression for PR #122 stamp_server_post_config content-check.

    The pre-#122 code re-read `self.results.get(sid)` after `store_result`
    and compared `stored is not updated` (identity). A benign concurrent
    `record()` between the store_result and the re-read publishes a
    FRESHER SessionResult object that ALREADY carries our stamp (record()
    reads from disk where store_result just wrote, then republishes a
    different SessionResult instance). Identity comparison treated those
    as "race tripped" and silently returned None — caller skipped the
    SSE `fit` broadcast even though the stamp had actually landed.

    Now: content check via `snapshot.algorithm_id in
    stored.config_used_by_algorithm`. A benign republish that preserves
    the stamp must return the stored object, not None."""
    import main
    from detection_paths import stamp_server_post_run
    from schemas import (
        BlobCandidate,
        DetectionConfigSnapshotPayload,
        FramePayload,
        PitchPayload,
    )

    s = main.State(data_dir=tmp_path)
    s.heartbeat("A", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    s.heartbeat("B", time_synced=True, time_sync_id="sy_deadbeef",
                sync_anchor_timestamp_s=0.0)
    snap = DetectionConfigSnapshotPayload(
        algorithm_id="v11_hsv_cc",
        params={
            "hsv": {"h_min": 10, "h_max": 20, "s_min": 30,
                    "s_max": 200, "v_min": 40, "v_max": 210},
            "shape_gate": {"aspect_min": 0.7, "fill_min": 0.55},
        },
        preset_name=None,
    )
    frame = FramePayload(
        frame_index=1, timestamp_s=0.1, ball_detected=True,
        candidates=[BlobCandidate(
            px=10.0, py=20.0, area=100, area_score=1.0,
            aspect=1.0, fill=0.68,
        )],
    )
    for cam in ("A", "B"):
        p = PitchPayload(
            camera_id=cam, session_id="s_deadbeef",
            sync_id="sy_deadbeef", sync_anchor_timestamp_s=0.0,
            video_start_pts_s=0.0,
        )
        stamp_server_post_run(p, snap, [frame])
        s.record(p)
    # Sanity: result is in the store with our stamp already present.
    assert "v11_hsv_cc" in s.results["s_deadbeef"].config_used_by_algorithm

    # Inject a benign race: when stamp_server_post_config calls
    # store_result, simulate a concurrent record() that publishes a
    # FRESH SessionResult instance into self.results. The fresh result
    # carries the stamp (record() reads the just-written snapshot via
    # rebuild). Identity comparison would treat this as a race trip and
    # return None even though the snapshot DID land in self.results.
    original_store_result = s.store_result

    def racing_store_result(result):
        original_store_result(result)
        # Replace self.results[sid] with a fresh instance carrying the
        # same snapshot to simulate a benign concurrent record() that
        # republished a new SessionResult object.
        with s._lock:
            current = s.results.get(result.session_id)
            if current is not None:
                # model_copy(deep=True) yields a NEW object that is
                # `is not` the one store_result wrote. Pre-fix identity
                # check would trip here and return None.
                s.results[result.session_id] = current.model_copy(deep=True)

    monkeypatch.setattr(s, "store_result", racing_store_result)

    result = s.stamp_server_post_config("s_deadbeef", snap)
    assert result is not None, (
        "content-check race guard regressed: a benign concurrent record() "
        "republish that preserves the stamp must NOT cause stamp_server_post_config "
        "to silently return None (which skips the SSE fit broadcast)"
    )
    assert "v11_hsv_cc" in result.config_used_by_algorithm


# ---------------------------------------------------------------------------
# Originally-positioned: state_runtime range guard (kept below).
# ---------------------------------------------------------------------------


def test_runtime_settings_out_of_range_value_raises(tmp_path):
    """A field present but out of the validated range must raise —
    silent clamp or revert would hide operator misconfig."""
    import json
    import pytest
    from state_runtime import RuntimeSettingsStore

    settings_path = tmp_path / "runtime_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "chirp_detect_threshold": 5.0,  # out of [0.01, 1.0]
                "mutual_sync_threshold": 0.10,
                "heartbeat_interval_s": 1.0,
                "sync_params": {
                    "emit_a_at_s": [0.3],
                    "emit_b_at_s": [1.8],
                    "record_duration_s": 4.0,
                    "search_window_s": 0.3,
                },
                "capture_height_px": 1080,
                "tracking_exposure_cap": "frame_duration",
                "strike_zone": {"batter_height_cm": 175},
                "default_paths": ["live"],
            }
        )
    )

    def _atomic_write(path, payload):
        path.write_text(payload)

    with pytest.raises(ValueError, match="chirp_detect_threshold"):
        RuntimeSettingsStore(settings_path, atomic_write=_atomic_write)
