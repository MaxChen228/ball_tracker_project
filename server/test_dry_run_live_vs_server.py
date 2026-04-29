"""End-to-end sanity for `dry_run_live_vs_server`.

Builds a synthetic pitch JSON in a tmp data dir, runs the script's
top-level `run_session`, and asserts the counts + centroid Δ stats
line up with what we hand-constructed.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def script(tmp_path, monkeypatch):
    """Re-import the module pointed at a tmp data dir so file IO is sandboxed."""
    import dry_run_live_vs_server as mod

    importlib.reload(mod)  # reset module-level state per-test
    pitch_dir = tmp_path / "pitches"
    report_dir = tmp_path / "alignment_reports"
    pitch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "PITCH_DIR", pitch_dir)
    monkeypatch.setattr(mod, "REPORT_DIR", report_dir)
    return mod, pitch_dir, report_dir


def _frame(idx: int, ts: float, *, px=None, py=None, detected=False, n_cands=0):
    return {
        "frame_index": idx,
        "timestamp_s": ts,
        "px": px,
        "py": py,
        "ball_detected": detected,
        "candidates": [{"px": 0, "py": 0, "area": 1, "area_score": 1.0}] * n_cands,
    }


def _write_pitch(pitch_dir: Path, sid: str, cam: str, live, server):
    obj = {
        "camera_id": cam,
        "session_id": sid,
        "frames_live": live,
        "frames_server_post": server,
    }
    (pitch_dir / f"session_{sid}_{cam}.json").write_text(json.dumps(obj))


def test_pair_frames_window(script):
    mod, _, _ = script
    live = [
        mod.FrameLite(0, 0.000, None, None, False, 0),
        mod.FrameLite(1, 0.010, 100.0, 100.0, True, 1),
    ]
    server = [
        mod.FrameLite(0, 0.001, None, None, False, 0),
        mod.FrameLite(1, 0.020, 100.0, 100.0, True, 1),  # > 4ms from live[1]
    ]
    pairs, ul, us = mod.pair_frames(live, server, window_s=0.004)
    assert len(pairs) == 1
    assert pairs[0][0].frame_index == 0 and pairs[0][1].frame_index == 0
    assert len(ul) == 1 and ul[0].frame_index == 1
    assert len(us) == 1 and us[0].frame_index == 1


def test_run_session_end_to_end(script, capsys):
    mod, pitch_dir, report_dir = script
    sid = "s_deadbeef"
    # Cam A: 4 paired frames covering all 4 outcomes + a centroid Δ.
    live_a = [
        _frame(0, 0.000, detected=False, n_cands=0),                       # neither
        _frame(1, 0.004, px=100.0, py=200.0, detected=True, n_cands=2),    # both
        _frame(2, 0.008, px=300.0, py=400.0, detected=True, n_cands=1),    # only_live
        _frame(3, 0.012, detected=False, n_cands=0),                       # only_server
    ]
    server_a = [
        _frame(0, 0.0005, detected=False, n_cands=0),
        _frame(1, 0.0045, px=102.5, py=197.0, detected=True, n_cands=3),
        _frame(2, 0.0085, detected=False, n_cands=4),
        _frame(3, 0.0125, px=500.0, py=500.0, detected=True, n_cands=1),
    ]
    _write_pitch(pitch_dir, sid, "A", live_a, server_a)

    rc, reports = mod.run_session(sid, window_s=0.004)
    assert rc == mod.EXIT_OK
    assert len(reports) == 1
    rep = reports[0]
    assert rep.camera_id == "A"
    assert rep.paired == 4
    assert rep.both == 1
    assert rep.only_live == 1
    assert rep.only_server == 1
    assert rep.neither == 1
    assert rep.unmatched_live == 0 and rep.unmatched_server == 0
    # centroid Δ: dx = 100 - 102.5 = -2.5, dy = 200 - 197 = 3.0
    assert rep.centroid_dx == pytest.approx([-2.5])
    assert rep.centroid_dy == pytest.approx([3.0])
    assert rep.centroid_dist[0] == pytest.approx(((2.5 ** 2 + 3.0 ** 2) ** 0.5))
    # candidates delta: (0,0)=0, (2,3)=-1, (1,4)=-3, (0,1)=-1
    assert rep.cand_delta_hist == {0: 1, -1: 2, -3: 1}
    # report file written
    out = report_dir / f"{sid}.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["session_id"] == sid
    assert payload["window_ms"] == 4.0
    assert payload["cameras"][0]["counts"]["both"] == 1
    # markdown emitted on stdout
    captured = capsys.readouterr().out
    assert "cam A" in captured
    assert "both detected" in captured


def test_run_session_missing_server_post_loud(script, capsys):
    mod, pitch_dir, _ = script
    sid = "s_cafebabe"
    live = [_frame(0, 0.0, px=10.0, py=10.0, detected=True, n_cands=1)]
    _write_pitch(pitch_dir, sid, "A", live, server=[])
    rc, reports = mod.run_session(sid, window_s=0.004)
    assert rc == mod.EXIT_NO_SERVER_POST
    assert reports == []
    err = capsys.readouterr().err
    assert "server_post not run" in err


def test_run_session_no_pitch(script, capsys):
    mod, _, _ = script
    rc, reports = mod.run_session("s_nope", window_s=0.004)
    assert rc == mod.EXIT_NOT_FOUND
    assert reports == []


def test_arg_parser_help(script):
    mod, _, _ = script
    p = mod.build_arg_parser()
    # smoke: --session is accepted, --all is accepted, --since is accepted
    ns = p.parse_args(["--session", "s_xxx", "--window-ms", "8"])
    assert ns.session == ["s_xxx"]
    assert ns.window_ms == 8.0


def test_discover_sessions_dedups(script):
    mod, pitch_dir, _ = script
    _write_pitch(pitch_dir, "s_aaaa1111", "A", [], [])
    _write_pitch(pitch_dir, "s_aaaa1111", "B", [], [])
    _write_pitch(pitch_dir, "s_bbbb2222", "A", [], [])

    class _Args:
        session = None
        all = True
        since = None

    sids = mod.discover_sessions(_Args())
    assert sorted(sids) == ["s_aaaa1111", "s_bbbb2222"]
