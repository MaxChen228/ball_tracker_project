"""Shared fixtures for the server test suite.

Previously each test module declared its own `_reset_state` autouse — that
worked when there was only one test file. Now that `test_viewer.py` also
hits the global `main.state`, the fixture lives here so both files get
the same isolated-state guarantee without duplicating code.
"""
from __future__ import annotations

import pytest

import main


@pytest.fixture(autouse=True)
def _reset_main_state(tmp_path, monkeypatch):
    """Replace `main.state` with a fresh per-test State rooted at tmp_path.

    Keeps test ordering-safe: no pitch, result, or clip file leaks across
    tests, and no interference with the developer's real `server/data/`."""
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    yield
