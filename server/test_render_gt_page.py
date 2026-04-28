"""Tests for `render_gt_page.render_gt_page`.

We don't render via TestClient here — those tests live in
`test_routes_gt.py::test_gt_page_renders_html`. This file targets the
pure SSR helpers + the integration with State.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from render_gt_page import (
    _glyph_for,
    _label_for_queue_item,
    _summary_text,
    _tint_for,
    render_gt_page,
)
from state import State
from state_gt_index import SessionGTState
from state_gt_queue import GTQueueItem


def _mk_state(tmp_path: Path) -> State:
    return State(data_dir=tmp_path)


def _mk_session_state(**overrides) -> SessionGTState:
    base = dict(
        session_id="s_aaaaaaaa",
        cams_present={"A": True, "B": True},
        has_mov={"A": True, "B": True},
        has_gt={"A": False, "B": False},
        n_live_dets={"A": 100, "B": 90},
        t_first_video_rel={"A": 0.5, "B": 0.6},
        t_last_video_rel={"A": 1.5, "B": 1.6},
        video_duration_s={"A": 2.0, "B": 2.0},
        is_skipped=False,
        recency=1000.0,
    )
    base.update(overrides)
    return SessionGTState(**base)


# ----- glyph / tint helpers ----------------------------------------


def test_glyph_no_gt_is_dot():
    assert _glyph_for(_mk_session_state(has_gt={"A": False, "B": False})) == "(·)"


def test_glyph_one_cam_is_filled_dot():
    assert _glyph_for(_mk_session_state(has_gt={"A": True, "B": False})) == "(●)"


def test_glyph_both_cams_is_check():
    assert _glyph_for(_mk_session_state(has_gt={"A": True, "B": True})) == "(✓)"


def test_glyph_skipped_overrides():
    s = _mk_session_state(has_gt={"A": True, "B": True}, is_skipped=True)
    assert _glyph_for(s) == "(⊘)"


def test_tint_skipped_takes_priority():
    s = _mk_session_state(has_gt={"A": True, "B": True}, is_skipped=True)
    assert _tint_for(s) == "gt-row-skipped"


def test_tint_full_gt():
    assert _tint_for(_mk_session_state(has_gt={"A": True, "B": True})) == "gt-row-passed"


def test_tint_partial_gt():
    assert _tint_for(_mk_session_state(has_gt={"A": True, "B": False})) == "gt-row-warn"


def test_tint_none_gt():
    assert _tint_for(_mk_session_state(has_gt={"A": False, "B": False})) == "gt-row-neutral"


# ----- queue label -------------------------------------------------


def _mk_item(**overrides) -> GTQueueItem:
    base = dict(
        id="q_deadbeef",
        session_id="s_aaaaaaaa",
        camera_id="A",
        time_range=(0.5, 1.5),
        click_x=960,
        click_y=540,
        click_t_video_rel=0.5,
        status="pending",
        created_at="2026-04-28T12:00:00Z",
    )
    base.update(overrides)
    return GTQueueItem(**base)


def test_queue_label_pending():
    s = _label_for_queue_item(_mk_item(status="pending"))
    assert s.startswith("⏳")
    assert "s_aaaaaaaa/A" in s
    assert "0.50–1.50s" in s


def test_queue_label_running_with_progress():
    s = _label_for_queue_item(_mk_item(
        status="running",
        progress={"current_frame": 50, "total_frames": 200, "ms_per_frame": 1500.0},
    ))
    assert s.startswith("▶")
    assert "frame 50/200" in s
    assert "25%" in s


def test_queue_label_done():
    s = _label_for_queue_item(_mk_item(status="done", n_labelled=87, n_decoded=110))
    assert s.startswith("✓")
    assert "87/110 frames" in s


def test_queue_label_error():
    s = _label_for_queue_item(_mk_item(status="error", error="cuda OOM\nstack trace blah"))
    assert s.startswith("✗")
    assert "cuda OOM" in s
    assert "stack trace" not in s  # only first line


def test_queue_label_canceled():
    s = _label_for_queue_item(_mk_item(status="canceled"))
    assert s.startswith("⊘")


def test_summary_text_includes_paused_marker():
    items = [_mk_item(status="pending"), _mk_item(id="q_deadbef0", status="running")]
    s = _summary_text(items, paused=True)
    assert "PAUSED" in s
    assert "running: 1" in s
    assert "queued: 1" in s


def test_summary_text_no_paused_when_running():
    s = _summary_text([], paused=False)
    assert "PAUSED" not in s
    assert "total: 0" in s


# ----- full SSR with State -----------------------------------------


def test_render_gt_page_html_contains_panels(tmp_path: Path):
    state = _mk_state(tmp_path)
    html = render_gt_page(state)
    assert "BALL_TRACKER" in html
    assert "Sessions" in html
    assert "Queue idle" in html  # empty-state message
    # Initial state is injected
    assert "__GT_INITIAL_STATE__" in html


def test_render_gt_page_renders_pending_queue_items(tmp_path: Path):
    state = _mk_state(tmp_path)
    state.gt_queue.add(
        session_id="s_deadbeef",
        camera_id="A",
        time_range=(0.5, 1.5),
        click_x=960,
        click_y=540,
        click_t_video_rel=0.5,
    )
    html = render_gt_page(state)
    assert "s_deadbeef/A" in html
    assert "[0.50–1.50s]" in html
    assert "click=(960,540)@0.50" in html
    # The "Queue idle" string lives both in the SSR empty state and in
    # the JS bundle as the empty-state fallback. Slice the queue panel
    # list to test the SSR markup specifically.
    queue_list_start = html.find('id="gt-queue-list"')
    assert queue_list_start != -1
    queue_list_chunk = html[queue_list_start: queue_list_start + 2000]
    assert "Queue idle" not in queue_list_chunk


def test_render_gt_page_uses_glyph_in_session_row(tmp_path: Path):
    state = _mk_state(tmp_path)
    # Seed a pitch JSON + GT JSON for session sid
    sid = "s_aaaaaaaa"
    pitches = tmp_path / "pitches"
    pitches.mkdir(parents=True, exist_ok=True)
    (pitches / f"session_{sid}_A.json").write_text(json.dumps({
        "session_id": sid, "camera_id": "A",
        "video_start_pts_s": 0.0, "video_fps": 240.0,
        "frames_live": [], "frames_server_post": [],
    }))
    (tmp_path / "gt" / "sam3").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gt" / "sam3" / f"session_{sid}_A.json").write_text("{}")
    state.gt_index.invalidate(sid)
    html = render_gt_page(state)
    assert sid in html
    # One cam with GT → partial glyph (●)
    assert "(●)" in html or "(✓)" in html
