"""Reprocess two-mode contract: the offline `reprocess_sessions.py` script
defaults to **current** `data/*.json` so the operator's tuning workflow
("I tweaked HSV, rerun this session, see if it improves") works as
expected. Opt-in `--use-frozen-snapshot` reuses
`PitchPayload.server_post_config_used` for reproducibility audits —
falling back to disk for legacy pitches that pre-date the stamp, with a
logged warning.

The frozen stamps themselves are always preserved on disk (re-stamped at
the end of every detection run with whatever config was actually used),
so the "what was X originally detected with" question stays answerable
even when default reprocess overwrites the run.

Selector cost weights (`_W_ASPECT` / `_W_FILL`) are now module
constants in `candidate_selector.py` rather than a runtime tunable, so
they're not part of the freeze schema.
"""
from __future__ import annotations

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
    server_post_used: DetectionConfigSnapshotPayload | None,
) -> PitchPayload:
    return PitchPayload(
        camera_id="A",
        session_id="s_abcd1234",
        sync_anchor_timestamp_s=0.0,
        video_start_pts_s=0.0,
        server_post_config_used=server_post_used,
    )


def _write_pitch(path: Path, pitch: PitchPayload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pitch.model_dump_json())


def _capture_detect_pitch_args():
    """Return (mock, captured_kwargs_list) — patches reprocess.detect_pitch
    with a stub that records every call's kwargs and returns []."""
    captured: list[dict] = []

    def fake_detect_pitch(*args, **kwargs):
        captured.append(kwargs)
        return []

    return fake_detect_pitch, captured


def test_rerun_detection_default_uses_current_disk_config(tmp_path, monkeypatch):
    """The discriminating test for the operator's tuning workflow:
    pitch was originally detected under HSV/gate/tuning X; operator
    edited disk to Y; default reprocess MUST call detect_pitch with Y
    (so the tweak shows up in the rerun), not X."""
    import reprocess_sessions as R

    # Pitch was originally frozen under X
    frozen = _snapshot(
        h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        aspect_min=0.61, fill_min=0.62,
    )
    pitch = _make_pitch(server_post_used=frozen)
    pitch_path = tmp_path / "pitches" / "session_s_abcd1234_A.json"
    _write_pitch(pitch_path, pitch)

    # Stub video discovery + detect_pitch
    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    # "Current disk" config — Y. Different from X.
    current = _snapshot(
        h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150,
        aspect_min=0.99, fill_min=0.99,
    )

    R.rerun_detection(
        pitch_path, current,
        dry_run=True,
    )

    assert len(captured) == 1
    kw = captured[0]
    # Default = current disk Y wins, not frozen X.
    assert kw["hsv_range"].h_min == 100 and kw["hsv_range"].h_max == 110
    assert kw["shape_gate"].aspect_min == pytest.approx(0.99)
    assert kw["shape_gate"].fill_min == pytest.approx(0.99)


def test_rerun_detection_use_frozen_snapshot_replays_original(tmp_path, monkeypatch):
    """For the reproducibility-audit case: pitch frozen under X, disk
    holds Y, `use_frozen_snapshot=True` MUST replay X (ignore disk)."""
    import reprocess_sessions as R

    frozen = _snapshot(
        h_min=10, h_max=20, s_min=30, s_max=40, v_min=50, v_max=60,
        aspect_min=0.61, fill_min=0.62,
    )
    pitch = _make_pitch(server_post_used=frozen)
    pitch_path = tmp_path / "pitches" / "session_s_abcd1234_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current = _snapshot(
        h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150,
        aspect_min=0.99, fill_min=0.99,
    )

    R.rerun_detection(
        pitch_path, current,
        dry_run=True,
        use_frozen_snapshot=True,
    )

    kw = captured[0]
    # Frozen X wins, not current Y.
    assert kw["hsv_range"].h_min == 10 and kw["hsv_range"].h_max == 20
    assert kw["hsv_range"].s_min == 30 and kw["hsv_range"].v_max == 60
    assert kw["shape_gate"].aspect_min == pytest.approx(0.61)
    assert kw["shape_gate"].fill_min == pytest.approx(0.62)


def test_rerun_detection_use_frozen_snapshot_legacy_falls_back_with_warning(
    tmp_path, monkeypatch, caplog,
):
    """When `use_frozen_snapshot=True` but pitch lacks `*_used` stamps
    (legacy from before the freeze landed), MUST fall back to disk
    values AND log a warning on each missing field so the operator
    knows the rerun isn't an unambiguous historical reproduction."""
    import logging

    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_legacy01_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, captured = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current = _snapshot(
        h_min=100, h_max=110, s_min=120, s_max=130, v_min=140, v_max=150,
        aspect_min=0.99, fill_min=0.99,
    )

    with caplog.at_level(logging.WARNING, logger="reprocess"):
        R.rerun_detection(
            pitch_path, current,
            dry_run=True,
            use_frozen_snapshot=True,
        )

    kw = captured[0]
    # Fell back to current disk — Y wins.
    assert kw["hsv_range"].h_min == 100
    assert kw["shape_gate"].fill_min == pytest.approx(0.99)
    # Legacy-fallback warning fired.
    msgs = " ".join(r.message for r in caplog.records)
    assert "lacks server_post_config_used" in msgs


def test_load_snapshot_from_file_strict_parse(tmp_path):
    """`--params <json>` reads the file via Pydantic strict parse: any
    schema mismatch raises rather than silently falling back to default
    values. Round-trip is intentional — the file format equals the wire
    format for `DetectionConfigSnapshotPayload`."""
    import json
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

    # Unknown algorithm_id rejected by the model validator.
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
    """Operator running `--params /tmp/typo.json` deserves a one-line
    `--params <path>: file does not exist`, not a Pydantic stack trace."""
    import reprocess_sessions as R

    with pytest.raises(SystemExit, match="does not exist"):
        R._load_snapshot_from_file(tmp_path / "nope.json")


def test_load_snapshot_from_file_malformed_json_raises_actionable_systemexit(tmp_path):
    """Same actionable-error contract for malformed JSON content."""
    import reprocess_sessions as R

    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{ not valid json")
    with pytest.raises(SystemExit, match=str(bad_path.name)):
        R._load_snapshot_from_file(bad_path)


def test_use_frozen_snapshot_with_algorithm_id_override_is_rejected(
    tmp_path, monkeypatch,
):
    """`--use-frozen-snapshot` ignores the disk/CLI snapshot, so
    combining it with `--algorithm-id` would silently drop the
    override. Reject up front."""
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--use-frozen-snapshot",
         "--algorithm-id", "v11_hsv_cc"],
    )
    with pytest.raises(SystemExit, match="--use-frozen-snapshot"):
        R.main()


def test_strict_flag_exit_code_is_nonzero(tmp_path, monkeypatch):
    """Pin the contract: --strict's exit code MUST be non-zero so
    automation pipelines can `set -e` against it. A change that left
    SystemExit(0) on failure would silently break CI."""
    import sys
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_strictcode_A.json"
    _write_pitch(pitch_path, pitch)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    monkeypatch.setattr(R, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "results").mkdir(exist_ok=True)
    monkeypatch.setattr(
        R, "load_detection_config_snapshot",
        lambda: _snapshot(
            h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
            aspect_min=0.5, fill_min=0.5,
        ),
    )
    monkeypatch.setattr(R, "load_pairing_tuning", lambda: R.PairingTuning.default())
    monkeypatch.setattr(R, "load_calibrations", lambda: {})

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic fail")

    monkeypatch.setattr(R, "rerun_detection", boom)

    monkeypatch.setattr(sys, "argv", ["reprocess_sessions", "--all", "--strict"])
    with pytest.raises(SystemExit) as exc:
        R.main()
    code = exc.value.code
    assert code != 0 and code is not None, (
        f"--strict must exit non-zero on failure; got {code!r}"
    )


def test_strict_flag_propagates_failure_to_exit_code(tmp_path, monkeypatch):
    """`--strict` turns "any pitch failed" into a non-zero exit; default
    behaviour returns 0 so a single bad MOV doesn't sink an automation
    sweep over a hundred sessions."""
    import sys
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_strict01_A.json"
    _write_pitch(pitch_path, pitch)
    monkeypatch.setattr(R, "PITCH_DIR", tmp_path / "pitches")
    monkeypatch.setattr(R, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "results").mkdir(exist_ok=True)
    monkeypatch.setattr(
        R, "load_detection_config_snapshot",
        lambda: _snapshot(
            h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
            aspect_min=0.5, fill_min=0.5,
        ),
    )
    monkeypatch.setattr(R, "load_pairing_tuning", lambda: R.PairingTuning.default())
    monkeypatch.setattr(R, "load_calibrations", lambda: {})

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic fail")

    monkeypatch.setattr(R, "rerun_detection", boom)

    monkeypatch.setattr(sys, "argv", ["reprocess_sessions", "--all"])
    R.main()  # default → completes despite failure

    monkeypatch.setattr(sys, "argv", ["reprocess_sessions", "--all", "--strict"])
    with pytest.raises(SystemExit):
        R.main()


def test_algorithm_id_override_validates_against_registry(tmp_path, monkeypatch):
    """`--algorithm-id` rewrites the snapshot's id but rejects an id
    that isn't in the registry. Bad id → SystemExit before any work."""
    import sys
    import reprocess_sessions as R

    monkeypatch.setattr(
        R, "load_detection_config_snapshot",
        lambda: _snapshot(
            h_min=0, h_max=1, s_min=0, s_max=1, v_min=0, v_max=1,
            aspect_min=0.5, fill_min=0.5,
        ),
    )
    monkeypatch.setattr(R, "select_pitch_files", lambda args: [])

    monkeypatch.setattr(
        sys, "argv",
        ["reprocess_sessions", "--all", "--algorithm-id", "v999_not_registered"],
    )
    with pytest.raises(SystemExit, match="v999_not_registered"):
        R.main()


def test_algorithm_id_and_params_are_mutually_exclusive(tmp_path, monkeypatch):
    """`--params` already carries its own algorithm_id, so combining
    with `--algorithm-id` is ambiguous — abort early so the operator
    can pick one source of truth."""
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


def test_rerun_detection_stamps_used_values_back_on_legacy(tmp_path, monkeypatch):
    """After reprocessing a legacy pitch, the freshly-used values are
    stamped back so the next reprocess can honour the freeze."""
    import reprocess_sessions as R

    pitch = _make_pitch(server_post_used=None)
    pitch_path = tmp_path / "pitches" / "session_s_legacy02_A.json"
    _write_pitch(pitch_path, pitch)

    monkeypatch.setattr(R, "find_video", lambda sid, cam: tmp_path / "fake.mov")
    fake_dp, _ = _capture_detect_pitch_args()
    monkeypatch.setattr(R, "detect_pitch", fake_dp)

    current = _snapshot(
        h_min=42, h_max=43, s_min=44, s_max=45, v_min=46, v_max=47,
        aspect_min=0.55, fill_min=0.66,
    )

    R.rerun_detection(
        pitch_path, current,
        dry_run=False,  # write back so we can re-read
    )

    # Re-read and verify the freeze stamp landed
    written = PitchPayload.model_validate_json(pitch_path.read_text())
    assert written.server_post_config_used is not None
    assert written.server_post_config_used.hsv.h_min == 42
    assert written.server_post_config_used.shape_gate.aspect_min == pytest.approx(0.55)
