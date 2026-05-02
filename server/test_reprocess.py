"""Reprocess CLI contract: the offline `reprocess_sessions.py` script
defaults to per-pitch frozen preset lookup so a mixed-history corpus
can be safely batch-reprocessed under each session's own preset
(blue_ball sessions rerun under today's blue_ball, tennis under tennis,
etc.). `--use-frozen-snapshot` replays the exact stamp on each pitch
for reproducibility audits. `--force-preset NAME` and
`--params <file>` are explicit overrides.

The frozen stamps themselves are always preserved on disk (re-stamped at
the end of every detection run with whatever config was actually used),
so the "what was X originally detected with" question stays answerable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from detection import HSVRange, ShapeGate
from schemas import (
    DetectionConfigSnapshotPayload,
    HSVRangePayload,
    PitchPayload,
    ShapeGatePayload,
)


def _snapshot(
    *,
    h_min: int,
    h_max: int,
    s_min: int,
    s_max: int,
    v_min: int,
    v_max: int,
    aspect_min: float,
    fill_min: float,
    preset_name: str | None = "tennis",
    algorithm_id: str | None = None,
) -> DetectionConfigSnapshotPayload:
    import algorithms as _algorithms
    return DetectionConfigSnapshotPayload(
        algorithm_id=algorithm_id or _algorithms.DEFAULT_ALGORITHM_ID,
        hsv=HSVRangePayload(
            h_min=h_min, h_max=h_max,
            s_min=s_min, s_max=s_max,
            v_min=v_min, v_max=v_max,
        ),
        shape_gate=ShapeGatePayload(
            aspect_min=aspect_min,
            fill_min=fill_min,
        ),
        preset_name=preset_name,
    )


def _make_pitch(
    *,
    server_post_used: DetectionConfigSnapshotPayload | None = None,
    live_used: DetectionConfigSnapshotPayload | None = None,
    sid: str = "s_abcd1234",
) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id=sid,
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        server_post_config_used=server_post_used,
        live_config_used=live_used,
    )


def _write_pitch(path: Path, pitch: PitchPayload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pitch.model_dump_json())


def _write_preset(presets_dir: Path, name: str, snap: DetectionConfigSnapshotPayload) -> None:
    """Write a disk preset file matching `presets._read_with_migration`'s
    expected canonical shape (no algorithm_id/preset_name fields needed
    on the snapshot itself — the preset file IS the source of truth)."""
    presets_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "label": name,
        "algorithm_id": snap.algorithm_id,
        "hsv": {
            "h_min": snap.hsv.h_min, "h_max": snap.hsv.h_max,
            "s_min": snap.hsv.s_min, "s_max": snap.hsv.s_max,
            "v_min": snap.hsv.v_min, "v_max": snap.hsv.v_max,
        },
        "shape_gate": {
            "aspect_min": snap.shape_gate.aspect_min,
            "fill_min": snap.shape_gate.fill_min,
        },
    }
    (presets_dir / f"{name}.json").write_text(json.dumps(payload))


def _capture_run_detection_args():
    """Capture every `algorithms.run_detection` call's args/kwargs and
    return [].  Replaces the prior `detect_pitch` capture; reprocess
    now dispatches via the registry."""
    captured: list[dict] = []

    def fake_run_detection(
        algorithm_id, video_path, video_start_pts_s, params, **kwargs,
    ):
        captured.append({
            "algorithm_id": algorithm_id,
            "video_path": video_path,
            "video_start_pts_s": video_start_pts_s,
            "params": params,
            **kwargs,
        })
        return []

    return fake_run_detection, captured


# ---------------------------------------------------------------------------
# rerun_detection — the inner per-pitch detection driver. Snapshot is
# pre-resolved by the caller; rerun_detection just runs detect_pitch
# with the supplied values and stamps the result back.
# ---------------------------------------------------------------------------


def test_rerun_detection_routes_through_registry(tmp_path, monkeypatch):
    """rerun_detection dispatches via `algorithms.run_detection` with
    the snapshot's algorithm_id + params. No direct detect_pitch call."""
    import algorithms
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=_snapshot(
        h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        aspect_min=0.61, fill_min=0.62,
    ))
    pitch_path = tmp_path / "pitches" / "session_s_abcd1234_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_run, captured = _capture_run_detection_args()
    monkeypatch.setattr(algorithms, "run_detection", fake_run)

    snap = _snapshot(
        h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150,
        aspect_min=0.99, fill_min=0.99,
    )
    R.rerun_detection(pitch_path, snap, dry_run=True)

    call = captured[0]
    assert call["algorithm_id"] == snap.algorithm_id
    assert call["params"]["hsv"].h_min == 100 and call["params"]["hsv"].h_max == 110
    assert call["params"]["shape_gate"].aspect_min == pytest.approx(0.99)


def test_rerun_detection_stamps_snapshot_back(tmp_path, monkeypatch):
    """After reprocessing, server_post_config_used is overwritten with
    the snapshot that produced the new frames, so the next reprocess
    can honour the freeze."""
    import algorithms
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_legacy02_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_run, _ = _capture_run_detection_args()
    monkeypatch.setattr(algorithms, "run_detection", fake_run)

    snap = _snapshot(
        h_min=42, h_max=43, s_min=44, s_max=45, v_min=46, v_max=47,
        aspect_min=0.55, fill_min=0.66,
    )
    R.rerun_detection(pitch_path, snap, dry_run=False)

    written = PitchPayload.model_validate_json(pitch_path.read_text())
    assert written.server_post_config_used is not None
    assert written.server_post_config_used.hsv.h_min == 42
    assert written.server_post_config_used.shape_gate.aspect_min == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# resolve_snapshot_for_pitch — the dispatch layer that decides which
# snapshot a pitch gets based on flags. Returns None to signal "skip".
# ---------------------------------------------------------------------------


def test_resolve_default_uses_per_pitch_frozen_preset(tmp_path, monkeypatch):
    """Default (no flags): pitch's frozen preset_name → load that preset
    from disk → return its CURRENT values. blue_ball pitch reruns under
    today's blue_ball, NOT today's dashboard active preset."""
    import reprocess_sessions as R

    # Disk preset has been edited to NEW values since the pitch was frozen.
    blue_today = _snapshot(
        h_min=105, h_max=115, s_min=140, s_max=255, v_min=40, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="blue_ball",
    )
    presets_dir = tmp_path / "presets"
    _write_preset(presets_dir, "blue_ball", blue_today)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)

    # Pitch was frozen under OLD values, but with preset_name="blue_ball".
    blue_old = _snapshot(
        h_min=99, h_max=99, s_min=99, s_max=99, v_min=99, v_max=99,
        aspect_min=0.5, fill_min=0.5, preset_name="blue_ball",
    )
    pitch = _make_pitch(server_post_used=blue_old)

    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=None,
        algorithm_id_override=None,
    )
    assert snap is not None
    assert snap.preset_name == "blue_ball"
    assert snap.hsv.h_min == 105  # disk values, not the pitch's frozen 99
    assert snap.shape_gate.aspect_min == pytest.approx(0.7)


def test_resolve_default_falls_through_to_live_preset_name(tmp_path, monkeypatch):
    """First-time reprocess (no server_post run yet): falls through to
    live_config_used.preset_name. This is the path a freshly-arrived
    pitch takes — it has live frames but no server_post yet."""
    import reprocess_sessions as R

    tennis = _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
    )
    presets_dir = tmp_path / "presets"
    _write_preset(presets_dir, "tennis", tennis)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)

    live_only = _snapshot(
        h_min=1, h_max=1, s_min=1, s_max=1, v_min=1, v_max=1,
        aspect_min=0.5, fill_min=0.5, preset_name="tennis",
    )
    pitch = _make_pitch(server_post_used=None, live_used=live_only)

    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=None,
        algorithm_id_override=None,
    )
    assert snap is not None
    assert snap.preset_name == "tennis"
    assert snap.hsv.h_min == 25  # disk tennis, not the pitch's frozen 1


def test_resolve_default_skips_pitch_without_any_frozen_preset_name(
    tmp_path, monkeypatch, caplog,
):
    """Pre-preset-identity pitches (server_post and live both None or
    both with preset_name=None) get skipped with a warning that points
    at --force-preset."""
    import logging
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    pitch = _make_pitch(server_post_used=None, live_used=None)

    with caplog.at_level(logging.WARNING, logger="reprocess"):
        snap = R.resolve_snapshot_for_pitch(
            pitch,
            use_frozen_snapshot=False,
            params_snapshot=None,
            force_preset_snapshot=None,
            algorithm_id_override=None,
        )
    assert snap is None
    msgs = " ".join(r.message for r in caplog.records)
    assert "no frozen preset_name" in msgs
    assert "--force-preset" in msgs


def test_resolve_default_skips_when_frozen_preset_deleted(
    tmp_path, monkeypatch, caplog,
):
    """Frozen preset_name claims a preset, but file is gone. Skip with
    warning instead of silently picking the wrong preset."""
    import logging
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    (tmp_path / "presets").mkdir(exist_ok=True)  # empty presets dir

    snap_old = _snapshot(
        h_min=1, h_max=2, s_min=3, s_max=4, v_min=5, v_max=6,
        aspect_min=0.5, fill_min=0.5, preset_name="ghost_preset",
    )
    pitch = _make_pitch(server_post_used=snap_old)

    with caplog.at_level(logging.WARNING, logger="reprocess"):
        snap = R.resolve_snapshot_for_pitch(
            pitch,
            use_frozen_snapshot=False,
            params_snapshot=None,
            force_preset_snapshot=None,
            algorithm_id_override=None,
        )
    assert snap is None
    msgs = " ".join(r.message for r in caplog.records)
    assert "ghost_preset" in msgs
    assert "no longer exists" in msgs


def test_resolve_use_frozen_snapshot_replays_pitch_stamp(tmp_path, monkeypatch):
    """--use-frozen-snapshot returns the pitch's stored
    server_post_config_used verbatim — no disk lookup."""
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    frozen = _snapshot(
        h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        aspect_min=0.61, fill_min=0.62, preset_name="blue_ball",
    )
    pitch = _make_pitch(server_post_used=frozen)

    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=True,
        params_snapshot=None,
        force_preset_snapshot=None,
        algorithm_id_override=None,
    )
    assert snap is frozen


def test_resolve_use_frozen_snapshot_skips_legacy_pitch(
    tmp_path, monkeypatch, caplog,
):
    """Pre-freeze pitch under --use-frozen-snapshot: skip with warning,
    NO silent fallback to disk (CLAUDE.md §experimental)."""
    import logging
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    pitch = _make_pitch(server_post_used=None)

    with caplog.at_level(logging.WARNING, logger="reprocess"):
        snap = R.resolve_snapshot_for_pitch(
            pitch,
            use_frozen_snapshot=True,
            params_snapshot=None,
            force_preset_snapshot=None,
            algorithm_id_override=None,
        )
    assert snap is None
    msgs = " ".join(r.message for r in caplog.records)
    assert "no server_post_config_used" in msgs


def test_resolve_force_preset_overrides_frozen_lookup(tmp_path, monkeypatch):
    """--force-preset wins over the per-pitch frozen preset_name."""
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)

    blue = _snapshot(
        h_min=99, h_max=99, s_min=99, s_max=99, v_min=99, v_max=99,
        aspect_min=0.5, fill_min=0.5, preset_name="blue_ball",
    )
    pitch = _make_pitch(server_post_used=blue)

    forced = _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
    )
    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=forced,
        algorithm_id_override=None,
    )
    assert snap is forced


def test_resolve_params_overrides_frozen_lookup(tmp_path, monkeypatch):
    """--params (loaded snapshot) wins over per-pitch frozen lookup."""
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    pitch = _make_pitch(server_post_used=_snapshot(
        h_min=99, h_max=99, s_min=99, s_max=99, v_min=99, v_max=99,
        aspect_min=0.5, fill_min=0.5, preset_name="blue_ball",
    ))
    params = _snapshot(
        h_min=1, h_max=2, s_min=3, s_max=4, v_min=5, v_max=6,
        aspect_min=0.7, fill_min=0.55, preset_name="custom_run",
    )
    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=params,
        force_preset_snapshot=None,
        algorithm_id_override=None,
    )
    assert snap is params


def test_resolve_default_server_post_with_null_preset_falls_through_to_live(
    tmp_path, monkeypatch,
):
    """Boundary case: server_post_config_used is set but its preset_name
    is None (e.g. legacy server_post run before preset identity
    landed). Must fall through to live_config_used.preset_name rather
    than treat the None as a legitimate preset_name lookup."""
    import reprocess_sessions as R

    tennis = _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
    )
    _write_preset(tmp_path / "presets", "tennis", tennis)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)

    server_post_no_name = _snapshot(
        h_min=99, h_max=99, s_min=99, s_max=99, v_min=99, v_max=99,
        aspect_min=0.5, fill_min=0.5, preset_name=None,
    )
    live_with_name = _snapshot(
        h_min=1, h_max=1, s_min=1, s_max=1, v_min=1, v_max=1,
        aspect_min=0.5, fill_min=0.5, preset_name="tennis",
    )
    pitch = _make_pitch(
        server_post_used=server_post_no_name, live_used=live_with_name,
    )

    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=None,
        algorithm_id_override=None,
    )
    assert snap is not None
    assert snap.preset_name == "tennis"
    assert snap.hsv.h_min == 25  # disk tennis values, not the 99/1 stamps


def test_resolve_force_preset_combines_with_algorithm_id_override(
    tmp_path, monkeypatch,
):
    """--force-preset + --algorithm-id is allowed; algorithm_id_override
    rewrites the id slot of the loaded preset's snapshot, leaving the
    HSV / shape_gate values intact."""
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    forced = _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
        algorithm_id="v11_hsv_cc",
    )
    pitch = _make_pitch(server_post_used=None, live_used=forced)

    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=forced,
        algorithm_id_override="v11_hsv_cc",
    )
    assert snap is not None
    assert snap.algorithm_id == "v11_hsv_cc"
    assert snap.hsv.h_min == 25
    assert snap.preset_name == "tennis"


def test_resolve_algorithm_id_override_combines_with_default(tmp_path, monkeypatch):
    """--algorithm-id with default mode rewrites only the algorithm_id
    slot of the per-pitch resolved snapshot — preset values stay."""
    import reprocess_sessions as R

    blue_today = _snapshot(
        h_min=105, h_max=115, s_min=140, s_max=255, v_min=40, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="blue_ball",
    )
    _write_preset(tmp_path / "presets", "blue_ball", blue_today)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)

    pitch = _make_pitch(server_post_used=blue_today)
    snap = R.resolve_snapshot_for_pitch(
        pitch,
        use_frozen_snapshot=False,
        params_snapshot=None,
        force_preset_snapshot=None,
        algorithm_id_override="v11_hsv_cc",
    )
    assert snap is not None
    assert snap.algorithm_id == "v11_hsv_cc"
    assert snap.hsv.h_min == 105


# ---------------------------------------------------------------------------
# CLI mutex — the parse-layer guards that prevent ambiguous flag combos.
# ---------------------------------------------------------------------------


def test_use_frozen_snapshot_mutex_with_algorithm_id(tmp_path, monkeypatch):
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--use-frozen-snapshot",
         "--algorithm-id", "v11_hsv_cc"],
    )
    with pytest.raises(SystemExit, match="--use-frozen-snapshot"):
        R.main()


def test_use_frozen_snapshot_mutex_with_force_preset(tmp_path, monkeypatch):
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--use-frozen-snapshot",
         "--force-preset", "tennis"],
    )
    with pytest.raises(SystemExit, match="--use-frozen-snapshot"):
        R.main()


def test_use_frozen_snapshot_mutex_with_params(tmp_path, monkeypatch):
    import sys
    import reprocess_sessions as R

    params_path = tmp_path / "p.json"
    params_path.write_text(_snapshot(
        h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
        aspect_min=0.5, fill_min=0.5,
    ).model_dump_json())

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--use-frozen-snapshot",
         "--params", str(params_path)],
    )
    with pytest.raises(SystemExit, match="--use-frozen-snapshot"):
        R.main()


def test_force_preset_mutex_with_params(tmp_path, monkeypatch):
    import sys
    import reprocess_sessions as R

    params_path = tmp_path / "p.json"
    params_path.write_text(_snapshot(
        h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
        aspect_min=0.5, fill_min=0.5,
    ).model_dump_json())

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all",
         "--force-preset", "tennis",
         "--params", str(params_path)],
    )
    with pytest.raises(SystemExit, match="mutually exclusive"):
        R.main()


def test_algorithm_id_and_params_are_mutually_exclusive(tmp_path, monkeypatch):
    import sys
    import reprocess_sessions as R

    params_path = tmp_path / "p.json"
    params_path.write_text(_snapshot(
        h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
        aspect_min=0.5, fill_min=0.5,
    ).model_dump_json())

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all",
         "--algorithm-id", "v11_hsv_cc",
         "--params", str(params_path)],
    )
    with pytest.raises(SystemExit, match="mutually exclusive"):
        R.main()


def test_force_preset_unknown_name_systemexit(tmp_path, monkeypatch):
    """--force-preset that doesn't exist on disk → actionable SystemExit
    before any pitch processing starts."""
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    (tmp_path / "presets").mkdir(exist_ok=True)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    (tmp_path / "pitches").mkdir(exist_ok=True)

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--force-preset", "nonexistent"],
    )
    with pytest.raises(SystemExit, match="nonexistent"):
        R.main()


def test_algorithm_id_override_validates_against_registry(tmp_path, monkeypatch):
    """Bad --algorithm-id → SystemExit before any pitch work."""
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    (tmp_path / "pitches").mkdir(exist_ok=True)
    monkeypatch.setattr(R, "select_pitch_files", lambda args: [])

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--algorithm-id", "v999_not_registered"],
    )
    with pytest.raises(SystemExit, match="v999_not_registered"):
        R.main()


# ---------------------------------------------------------------------------
# --params snapshot file loader — strict parse, actionable errors.
# ---------------------------------------------------------------------------


def test_load_snapshot_from_file_strict_parse(tmp_path):
    import reprocess_sessions as R

    good = _snapshot(
        h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        aspect_min=0.7, fill_min=0.55,
    )
    good_path = tmp_path / "good.json"
    good_path.write_text(good.model_dump_json())
    loaded = R._load_snapshot_from_file(good_path)
    assert loaded.hsv.h_min == 10
    assert loaded.shape_gate.aspect_min == pytest.approx(0.7)

    bad_path = tmp_path / "bad_algo.json"
    bad_path.write_text(json.dumps({
        "algorithm_id": "v999_not_registered",
        "hsv": {"h_min": 0, "h_max": 1, "s_min": 0, "s_max": 1, "v_min": 0, "v_max": 1},
        "shape_gate": {"aspect_min": 0.5, "fill_min": 0.5},
        "preset_name": None,
    }))
    with pytest.raises(SystemExit, match="v999_not_registered"):
        R._load_snapshot_from_file(bad_path)


def test_load_snapshot_from_file_missing_path_raises_actionable_systemexit(tmp_path):
    import reprocess_sessions as R

    with pytest.raises(SystemExit, match="does not exist"):
        R._load_snapshot_from_file(tmp_path / "nope.json")


def test_load_snapshot_from_file_malformed_json_raises_actionable_systemexit(tmp_path):
    import reprocess_sessions as R

    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{ not valid json")
    with pytest.raises(SystemExit, match=str(bad_path.name)):
        R._load_snapshot_from_file(bad_path)


# ---------------------------------------------------------------------------
# --strict — non-zero exit on per-pitch failure.
# ---------------------------------------------------------------------------


def test_strict_flag_exit_code_is_nonzero(tmp_path, monkeypatch):
    """Pin the contract: --strict's exit code MUST be non-zero so
    automation pipelines can `set -e` against it."""
    import sys
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_strictcode_A.json"
    _write_pitch(pitch_path, pitch)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    monkeypatch.setattr(R, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "results").mkdir(exist_ok=True)
    _write_preset(tmp_path / "presets", "tennis", _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
    ))
    monkeypatch.setattr(R, "load_pairing_tuning", lambda: R.PairingTuning.default())
    monkeypatch.setattr(R, "load_calibrations", lambda: {})

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic fail")

    monkeypatch.setattr(R, "rerun_detection", boom)

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--force-preset", "tennis", "--strict"],
    )
    with pytest.raises(SystemExit) as exc:
        R.main()
    code = exc.value.code
    assert code != 0 and code is not None, (
        f"--strict must exit non-zero on failure; got {code!r}"
    )


def test_strict_flag_propagates_failure_to_exit_code(tmp_path, monkeypatch):
    """--strict turns "any pitch failed" into a non-zero exit; default
    behaviour returns 0 so a single bad MOV doesn't sink an automation
    sweep over a hundred sessions."""
    import sys
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_strict01_A.json"
    _write_pitch(pitch_path, pitch)
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    monkeypatch.setattr(R, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "results").mkdir(exist_ok=True)
    _write_preset(tmp_path / "presets", "tennis", _snapshot(
        h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255,
        aspect_min=0.7, fill_min=0.55, preset_name="tennis",
    ))
    monkeypatch.setattr(R, "load_pairing_tuning", lambda: R.PairingTuning.default())
    monkeypatch.setattr(R, "load_calibrations", lambda: {})

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic fail")

    monkeypatch.setattr(R, "rerun_detection", boom)

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--force-preset", "tennis"],
    )
    R.main()  # default → completes despite failure

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--force-preset", "tennis", "--strict"],
    )
    with pytest.raises(SystemExit):
        R.main()
