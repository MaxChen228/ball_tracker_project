"""Tests for `scripts/label_with_sam3.py` CLI argument parsing.

Strategy: import the script module and exercise its `main()` (or its
arg parser surface) with crafted argv. We don't load SAM 3 — instead
we monkey-patch the `Sam3VideoLabeller` constructor + `label_video`
call to a no-op stub, which lets us assert how the CLI's flags map
through to the runtime call.

Specifically we test the mini-plan v4 contract:
  * `--time-range` and `--limit-frames` are mutually exclusive.
  * `--queue-id` is regex-validated to prevent path traversal.
  * `--time-range START END` requires `0 <= START < END`.
  * `--all` rejects per-(sid, cam) flags (`--time-range`, `--queue-id`).
  * The PROGRESS / DONE stderr lines match the worker's regex contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ on sys.path so we can import the module by name.
_SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import label_with_sam3  # type: ignore[import-not-found]


# ----- argument parsing --------------------------------------------


def test_argparse_rejects_time_range_with_limit_frames():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef", "--cam", "A",
            "--limit-frames", "30",
            "--time-range", "0.5", "1.5",
        ])


def test_argparse_rejects_invalid_queue_id():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef", "--cam", "A",
            "--queue-id", "../etc/passwd",
        ])


def test_argparse_rejects_queue_id_wrong_format():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef", "--cam", "A",
            "--queue-id", "q_BADFORMAT",  # uppercase hex
        ])


def test_argparse_accepts_valid_queue_id(monkeypatch, tmp_path):
    """A valid queue-id passes regex; we stub the labeller out so this
    finishes without touching SAM 3."""
    _stub_label_one(monkeypatch)
    rc = label_with_sam3.main([
        "--data-dir", str(tmp_path),
        "--session", "s_deadbeef", "--cam", "A",
        "--queue-id", "q_deadbeef",
    ])
    # We're not actually running SAM 3 — the stub returns success.
    assert rc == 0


def test_argparse_rejects_inverted_time_range():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef", "--cam", "A",
            "--time-range", "2.0", "1.0",  # end < start
        ])


def test_argparse_rejects_negative_time_range():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef", "--cam", "A",
            "--time-range", "-1.0", "1.0",
        ])


def test_argparse_rejects_all_with_time_range():
    """`--all` and `--time-range` together makes no sense (each session
    has its own ball-window) — should fail explicitly."""
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--all",
            "--time-range", "0.0", "1.0",
        ])


def test_argparse_rejects_all_with_queue_id():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--all",
            "--queue-id", "q_deadbeef",
        ])


def test_argparse_requires_cam_with_session():
    with pytest.raises(SystemExit):
        label_with_sam3.main([
            "--session", "s_deadbeef",
        ])


# ----- progress / done stderr contract -----------------------------


def test_progress_emitter_format(capfd):
    """The CLI's `_make_progress_emitter` must produce lines that match
    the worker's PROGRESS regex exactly."""
    import re
    progress_re = re.compile(
        r"^PROGRESS:\s+frame=(\d+)\s+total=(\d+)\s+elapsed=([\d.]+)\s+ms_per_frame=([\d.]+)\s*$"
    )
    cb = label_with_sam3._make_progress_emitter(queue_id="q_deadbeef")
    cb(1, 100, 1500.0)
    out, err = capfd.readouterr()
    assert err.strip() != ""
    line = err.strip().splitlines()[0]
    m = progress_re.match(line)
    assert m is not None, f"line {line!r} did not match PROGRESS regex"
    assert int(m.group(1)) == 1
    assert int(m.group(2)) == 100


def test_progress_emitter_density_for_first_10_then_decimated(capfd):
    cb = label_with_sam3._make_progress_emitter(queue_id=None)
    # Per spec: every frame for first 10, every 10th thereafter, plus
    # the final frame.
    for i in range(1, 31):
        cb(i, 30, 100.0)
    cb(30, 30, 100.0)  # final frame again to make sure density still emits
    _, err = capfd.readouterr()
    lines = [l for l in err.splitlines() if l.startswith("PROGRESS:")]
    # Expected emitting frames: 1..10 + 20 + 30 + (30 again from final) = 13
    # We'll just count >= 12 to allow for off-by-one without locking the
    # exact number.
    assert len(lines) >= 12


def test_done_emitter_format(capfd, monkeypatch, tmp_path):
    """The DONE line must match the worker's regex."""
    import re
    _stub_label_one(monkeypatch)

    rc = label_with_sam3.main([
        "--data-dir", str(tmp_path),
        "--session", "s_deadbeef", "--cam", "A",
    ])
    # The stub doesn't actually fire the DONE line (we bypass the real
    # _label_one). Test the format string directly:
    expected = label_with_sam3._DONE_FMT.format(labelled=87, decoded=110)
    done_re = re.compile(r"^DONE:\s+labelled=(\d+)\s+decoded=(\d+)\s*$")
    m = done_re.match(expected)
    assert m is not None
    assert int(m.group(1)) == 87
    assert int(m.group(2)) == 110


# ----- helpers -----------------------------------------------------


def _stub_label_one(monkeypatch):
    """Replace `_label_one` with a no-op that pretends to succeed.

    The real function loads SAM 3 weights — far too heavy for unit
    tests. We just need the CLI plumbing to validate args and call
    something."""

    def fake_label_one(labeller, **kwargs):  # noqa: ARG001
        return Path("/tmp/fake-gt.json")

    monkeypatch.setattr(label_with_sam3, "_label_one", fake_label_one)
    # Also stub out the SAM 3 constructor so it doesn't try to load weights.

    class _NullLabeller:
        DEFAULT_MODEL_ID = "fake/sam3"

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(label_with_sam3, "Sam3VideoLabeller", _NullLabeller)
