"""Shared fixtures for the server test suite.

Previously each test module declared its own `_reset_state` autouse — that
worked when there was only one test file. Now that `test_viewer.py` also
hits the global `main.state`, the fixture lives here so both files get
the same isolated-state guarantee without duplicating code.
"""
from __future__ import annotations

import pytest

import main
import pipeline


@pytest.fixture(autouse=True)
def _reset_main_state(tmp_path, monkeypatch):
    """Replace `main.state` with a fresh per-test State rooted at tmp_path.

    Route modules in routes/* use `from main import state` inside function
    bodies (late import), so they always read the current `main.state`
    value — no extra patching needed."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    yield


@pytest.fixture(autouse=True)
def _disable_bg_sub_warmup(monkeypatch):
    """Zero MOG2 warm-up during tests. Real pipeline skips the first 30
    frames (125 ms @ 240 fps) because the subtractor's initial mask is
    unreliable — but synthetic test clips ship only 3 static frames with
    a ground-truth ball, so a warm-up window would force all detections
    to None and break E2E triangulation asserts. On the first frame MOG2
    emits an all-foreground mask anyway, so zeroing warm-up is harmless
    for the static-ball case the tests exercise."""
    monkeypatch.setattr(pipeline, "_BG_SUBTRACTOR_WARMUP_FRAMES", 0)
    yield


def sid(n: int | str) -> str:
    """Session-id helper for tests — returns a value that matches the
    server's `s_[0-9a-f]{4,32}` regex. Use a stable int or a readable
    suffix so assertions on session identity stay legible."""
    if isinstance(n, int):
        return f"s_{n:08x}"
    return f"s_{n}"
