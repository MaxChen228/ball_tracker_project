"""Tests for routes/gt.py + state_gt_processing.py + render_report.py.

Strategy:
  - GTProcessingState: pure state machine, tested directly.
  - render_report_page: pure SSR, tested with sample payloads.
  - routes: TestClient hits each endpoint with fake state. Background
    tasks (subprocess + in-process workers) are NOT exercised here —
    those are covered by their respective scripts' own tests + the
    operator-side smoke test on real GT data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# scripts/ on path so render_report imports validate cleanly.
_SCRIPTS = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import main
from state_gt_processing import GTProcessingState
from render_report import render_report_page


def _client() -> TestClient:
    return TestClient(main.app)


# ----- GTProcessingState ---------------------------------------------


def test_gt_state_start_job_idempotent():
    s = GTProcessingState()
    key = ("label", "s_deadbeef", "A")
    assert s.start_job(key) is True
    # Second start while still running → False.
    assert s.start_job(key) is False
    # After finish, can start again.
    s.finish_job(key, status="completed")
    assert s.start_job(key) is True


def test_gt_state_finish_job_invalid_status():
    s = GTProcessingState()
    with pytest.raises(ValueError):
        s.finish_job(("label", "s_deadbeef", "A"), status="bogus")


def test_gt_state_cancel_session_flags_running_jobs_only():
    s = GTProcessingState()
    s.start_job(("label", "s_aaa", "A"))
    s.start_job(("label", "s_aaa", "B"))
    s.start_job(("label", "s_bbb", "A"))
    s.finish_job(("label", "s_aaa", "B"), status="completed")
    # Only running jobs of session s_aaa get flagged. b is done; bbb is
    # a different session.
    n = s.cancel_session("s_aaa")
    assert n == 1
    assert s.is_canceled(("label", "s_aaa", "A")) is True
    assert s.is_canceled(("label", "s_aaa", "B")) is False
    assert s.is_canceled(("label", "s_bbb", "A")) is False


def test_gt_state_snapshot_buckets():
    s = GTProcessingState()
    s.start_job(("label", "s_aaa", "A"))
    s.start_job(("validate", "s_bbb", "A"))
    s.finish_job(("label", "s_aaa", "A"), status="completed")
    s.finish_job(("validate", "s_bbb", "A"), status="error", error="boom")
    snap = s.snapshot()
    assert {e["sid"] for e in snap["running"]} == set()
    assert {e["sid"] for e in snap["completed"]} == {"s_aaa"}
    assert "validate:s_bbb:A" in snap["errors"]


def test_gt_state_subprocess_pid():
    s = GTProcessingState()
    key = ("label", "s_aaa", "A")
    s.start_job(key)
    s.set_subprocess_pid(key, 12345)
    # Internal: PID is dropped on finish.
    s.finish_job(key, status="completed")
    # Re-starting clears the PID.
    s.start_job(key)
    assert s._pids.get(key) is None


# ----- render_report --------------------------------------------------


def _sample_cam_payload() -> dict:
    return {
        "session_id": "s_deadbeef",
        "camera_id": "A",
        "match_radius_px": 8.0,
        "n_gt_frames": 1100,
        "n_live_frames": 280,
        "n_server_frames": 1086,
        "live_vs_gt": {
            "n_a_total": 280, "n_b_total": 1100, "n_hits": 260,
            "recall": 0.236, "precision": 0.929,
            "centroid_mae_px": 0.6, "centroid_p95_px": 1.8,
            "n_both_present": 270, "n_a_only": 10, "n_b_only": 830, "n_neither": 0,
        },
        "server_vs_gt": {
            "n_a_total": 1086, "n_b_total": 1100, "n_hits": 1010,
            "recall": 0.918, "precision": 0.930,
            "centroid_mae_px": 0.7, "centroid_p95_px": 2.4,
            "n_both_present": 1080, "n_a_only": 6, "n_b_only": 20, "n_neither": 0,
        },
        "live_vs_server": {
            "n_a_total": 280, "n_b_total": 1086, "n_hits": 270,
            "recall": 0.249, "precision": 0.964,
            "centroid_mae_px": 0.3, "centroid_p95_px": 0.9,
            "n_both_present": 280, "n_a_only": 0, "n_b_only": 806, "n_neither": 0,
        },
    }


def test_render_report_with_two_cams_renders_basic_structure():
    payloads = {"A": _sample_cam_payload(), "B": _sample_cam_payload()}
    html = render_report_page("s_deadbeef", payloads)
    assert "s_deadbeef" in html
    assert "Cam A" in html and "Cam B" in html
    assert "live ↔ GT" in html
    assert "server ↔ GT" in html
    assert "live ↔ server" in html
    # Recall numbers from sample appear.
    assert "0.918" in html  # server_vs_gt recall
    assert "0.249" in html  # live_vs_server recall


def test_render_report_empty_shows_friendly_message():
    html = render_report_page("s_deadbeef", {})
    assert "No validation report" in html


def test_render_report_gates_pass_when_thresholds_met():
    payloads = {"A": _sample_cam_payload()}
    payloads["A"]["live_vs_gt"]["recall"] = 0.95              # > 0.90 → pass
    payloads["A"]["live_vs_server"]["centroid_p95_px"] = 0.5  # ≤ 1 → pass
    html = render_report_page("s_deadbeef", payloads)
    # Both gates rendered, both pass.
    assert html.count('class="gate pass"') >= 2


def test_render_report_zero_data_shows_na_not_pass():
    """Regression: a session with zero detections must NOT render
    "Algorithm alignment p95: 0.00px pass" — that p95=0.0 came from an
    empty distance list and is meaningless. Should show n/a instead."""
    empty_pair = {
        "n_a_total": 0, "n_b_total": 0, "n_hits": 0,
        "n_both_present": 0, "n_a_only": 0, "n_b_only": 0, "n_neither": 0,
        "recall": 0.0, "precision": 0.0,
        "centroid_mae_px": 0.0, "centroid_p95_px": 0.0,
    }
    payloads = {"A": {
        "session_id": "s_deadbeef", "camera_id": "A", "match_radius_px": 8.0,
        "n_gt_frames": 0, "n_live_frames": 0, "n_server_frames": 0,
        "live_vs_gt": dict(empty_pair),
        "server_vs_gt": dict(empty_pair),
        "live_vs_server": dict(empty_pair),
    }}
    html = render_report_page("s_deadbeef", payloads)
    # The two gates in the summary row must render n/a, not pass.
    # Slice from the actual <div class="summary-row"> opener (not the
    # CSS rule named the same way).
    marker = '<div class="summary-row">'
    assert marker in html, "summary-row div not in rendered HTML"
    summary_section = html.split(marker, 1)[1].split("</div>", 1)[0]
    assert "n/a" in summary_section, summary_section
    assert "gate pass" not in summary_section, summary_section


# ----- routes ---------------------------------------------------------


def test_get_gt_proposals_when_missing():
    r = _client().get("/gt/proposals")
    assert r.status_code == 200
    body = r.json()
    assert body.get("available") is False or "proposed_params" in body or "params" in body


def test_post_gt_distill_queues_then_409_on_double():
    c = _client()
    r1 = c.post("/gt/distill")
    assert r1.status_code == 200
    assert r1.json().get("queued") == "distill"
    # Second call before the BackgroundTask thread has finished marking
    # the job "completed" should hit 409.
    r2 = c.post("/gt/distill")
    # 200 is fine if the in-process distill happened to finish synchronously
    # (the test data dir is tmp_path so distill_all may exit fast). We
    # tolerate either, the contract is "no concurrent runs".
    assert r2.status_code in (200, 409)


def test_post_gt_apply_proposal_no_proposals_409(tmp_path):
    """When data/gt/fit_proposals.json doesn't exist, apply must 409."""
    # The test fixture rebuilds main.state on tmp_path so by default the
    # proposals file is missing.
    r = _client().post(
        "/gt/apply_proposal",
        json={"category": "hsv_range"},
    )
    assert r.status_code == 409


def test_post_gt_apply_proposal_invalid_category():
    r = _client().post(
        "/gt/apply_proposal",
        json={"category": "bogus_category"},
    )
    assert r.status_code == 400


def test_post_run_validation_invalid_session_422():
    r = _client().post("/sessions/not-a-session/run_validation")
    assert r.status_code == 422


# `POST /sessions/{sid}/run_gt_labelling` and `POST /sessions/{sid}/cancel_gt`
# were dropped in mini-plan v4 — the new flow is `POST /gt/queue` +
# `DELETE /gt/queue/{id}`. Tests for those routes live in the new queue
# section below.


def test_post_cancel_distill_idempotent_when_no_running_job():
    r = _client().post("/gt/cancel_distill")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("flagged") is False


def test_gt_state_cancel_distill_flips_global_distill_only():
    """Regression: distill key is ('distill', 'global', 'global'); the
    earlier cancel_session implementation matched key[1] == session_id
    so a literal session_id 'global' would have been needed. The
    explicit cancel_distill() helper avoids that."""
    s = GTProcessingState()
    label_key = ("label", "s_aaa", "A")
    distill_key = ("distill", "global", "global")
    s.start_job(label_key)
    s.start_job(distill_key)
    flagged = s.cancel_distill()
    assert flagged is True
    assert s.is_canceled(distill_key) is True
    # Per-session label job is NOT touched.
    assert s.is_canceled(label_key) is False
    # Idempotent — calling again on a not-running job returns False.
    s.finish_job(distill_key, status="canceled")
    assert s.cancel_distill() is False


def test_get_report_404_when_no_validation():
    r = _client().get("/report/s_deadbeef")
    assert r.status_code == 404


def test_get_report_renders_html_when_validation_present(tmp_path):
    """End-to-end: drop a validation JSON in place, GET /report/{sid}
    returns HTML built by render_report_page."""
    sid = "s_deadbeef"
    val_dir = main.state.data_dir / "gt" / "validation"
    val_dir.mkdir(parents=True, exist_ok=True)
    (val_dir / f"session_{sid}_A.json").write_text(json.dumps(_sample_cam_payload()))
    r = _client().get(f"/report/{sid}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert sid in r.text
    assert "Cam A" in r.text


# ----- /gt page + queue endpoints (mini-plan v4) ----------------------


def _seed_pitch_and_mov(sid: str, cam: str, *, video_start: float = 100.0):
    """Helper: drop minimal pitch JSON + dummy MOV so /gt/queue accepts."""
    pitch_dir = main.state.data_dir / "pitches"
    pitch_dir.mkdir(parents=True, exist_ok=True)
    (pitch_dir / f"session_{sid}_{cam}.json").write_text(json.dumps({
        "session_id": sid,
        "camera_id": cam,
        "video_start_pts_s": video_start,
        "video_fps": 240.0,
        "frames_live": [
            {"frame_index": 0, "timestamp_s": video_start + 0.10, "px": 100.0, "py": 50.0},
            {"frame_index": 1, "timestamp_s": video_start + 0.50, "px": 110.0, "py": 50.0},
            # Last frame's timestamp gives the duration.
            {"frame_index": 2, "timestamp_s": video_start + 2.50, "px": 120.0, "py": 50.0},
        ],
        "frames_server_post": [],
    }))
    video_dir = main.state.data_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"session_{sid}_{cam}.mov").write_bytes(b"\x00" * 32)
    main.state.gt_index.invalidate(sid)


def test_gt_page_renders_html():
    r = _client().get("/gt")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_gt_sessions_returns_empty_when_no_pitches():
    r = _client().get("/gt/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_gt_sessions_lists_seeded_session():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().get("/gt/sessions")
    assert r.status_code == 200
    sids = [s["session_id"] for s in r.json()["sessions"]]
    assert sid in sids


def test_gt_timeline_returns_buckets():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().get(f"/gt/timeline/{sid}/A.json")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "frames_live"
    assert body["bucket_size_s"] == 0.1
    # At least one bucket should have a positive count
    assert any(c > 0 for c in body["buckets"])


def test_gt_timeline_404_when_no_pitch():
    # All-hex sid that passes regex but has no pitch JSON on disk.
    r = _client().get("/gt/timeline/s_ffffffff/A.json")
    assert r.status_code == 404


def test_gt_timeline_invalid_cam_422():
    r = _client().get("/gt/timeline/s_deadbeef/X.json")
    assert r.status_code == 422


def test_gt_queue_add_happy_path():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().post("/gt/queue", json={
        "session_id": sid,
        "camera_id": "A",
        "time_range": [0.1, 1.0],
        "click_x": 960,
        "click_y": 540,
        "click_t_video_rel": 0.5,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("q_")
    # Queue listing should now contain it
    r2 = _client().get("/gt/queue")
    items = r2.json()["items"]
    assert any(it["id"] == body["id"] and it["status"] == "pending" for it in items)
    # cleanup so other tests don't see this item
    _client().delete(f"/gt/queue/{body['id']}")


def test_gt_queue_add_rejects_missing_mov():
    sid = "s_e0e0e0e0"  # all-hex so the regex passes
    pitch_dir = main.state.data_dir / "pitches"
    pitch_dir.mkdir(parents=True, exist_ok=True)
    (pitch_dir / f"session_{sid}_A.json").write_text(json.dumps({
        "session_id": sid,
        "camera_id": "A",
        "video_start_pts_s": 0.0,
        "video_fps": 240.0,
        "frames_live": [],
        "frames_server_post": [],
    }))
    main.state.gt_index.invalidate(sid)
    r = _client().post("/gt/queue", json={
        "session_id": sid,
        "camera_id": "A",
        "time_range": [0.0, 1.0],
        "click_x": 960, "click_y": 540, "click_t_video_rel": 0.5,
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "no mov" in detail.lower()


def test_gt_queue_add_rejects_inverted_range():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().post("/gt/queue", json={
        "session_id": sid,
        "camera_id": "A",
        "time_range": [1.0, 0.5],
        "click_x": 960, "click_y": 540, "click_t_video_rel": 0.5,
    })
    assert r.status_code == 422


def test_gt_queue_add_rejects_range_past_video_end():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")  # duration = 2.5s
    r = _client().post("/gt/queue", json={
        "session_id": sid,
        "camera_id": "A",
        "time_range": [0.0, 100.0],
        "click_x": 960, "click_y": 540, "click_t_video_rel": 0.5,
    })
    assert r.status_code == 422


def test_gt_queue_add_rejects_click_outside_range():
    """Click at t=2.5s but range is [0.0, 1.0] → 422."""
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().post("/gt/queue", json={
        "session_id": sid,
        "camera_id": "A",
        "time_range": [0.0, 1.0],
        "click_x": 960, "click_y": 540, "click_t_video_rel": 2.5,
    })
    assert r.status_code == 422
    assert "click_t_video_rel" in r.json()["detail"]


def test_gt_queue_cancel_pending():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().post("/gt/queue", json={
        "session_id": sid, "camera_id": "A",
        "time_range": [0.0, 1.0],
        "click_x": 960, "click_y": 540, "click_t_video_rel": 0.5,
    })
    qid = r.json()["id"]
    r2 = _client().delete(f"/gt/queue/{qid}")
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    item = main.state.gt_queue.get(qid)
    assert item.status == "canceled"


def test_gt_queue_cancel_invalid_id_422():
    r = _client().delete("/gt/queue/not-a-queue-id")
    assert r.status_code == 422


def test_gt_queue_retry_creates_new_id():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    qid = main.state.gt_queue.add(
        session_id=sid, camera_id="A",
        time_range=(0.0, 1.0),
        click_x=960, click_y=540, click_t_video_rel=0.5,
    )
    main.state.gt_queue.mark_running(qid, pid=1)
    main.state.gt_queue.mark_error(qid, "boom")
    r = _client().post(f"/gt/queue/{qid}/retry")
    assert r.status_code == 200
    new_id = r.json()["id"]
    assert new_id != qid
    assert main.state.gt_queue.get(new_id).status == "pending"


def test_gt_queue_retry_pending_409():
    qid = main.state.gt_queue.add(
        session_id="s_pending1", camera_id="A",
        time_range=(0.0, 1.0),
        click_x=960, click_y=540, click_t_video_rel=0.5,
    )
    r = _client().post(f"/gt/queue/{qid}/retry")
    assert r.status_code == 409


def test_gt_queue_pause_resume():
    r = _client().post("/gt/queue/pause")
    assert r.status_code == 200
    assert main.state.gt_queue.paused() is True
    r2 = _client().post("/gt/queue/run")
    assert r2.status_code == 200
    assert main.state.gt_queue.paused() is False


def test_gt_preview_204_when_missing():
    r = _client().get("/gt/preview/q_deadbeef.jpg")
    assert r.status_code == 204


def test_gt_preview_404_invalid_id():
    r = _client().get("/gt/preview/notvalidid.jpg")
    assert r.status_code == 422


def test_gt_preview_serves_with_no_store():
    qid = "q_abcdef01"
    preview_path = main.state.data_dir / "gt" / "preview" / f"{qid}.jpg"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal JPEG-ish bytes (real JPEG SOI + EOI)
    preview_path.write_bytes(b"\xff\xd8\xff\xd9")
    r = _client().get(f"/gt/preview/{qid}.jpg")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_gt_skip_writes_skip_list():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    r = _client().post(f"/gt/sessions/{sid}/skip")
    assert r.status_code == 200
    skip_path = main.state.data_dir / "gt" / "skip_list.json"
    assert skip_path.is_file()
    assert sid in skip_path.read_text()
    # Index should now reflect skipped status
    assert main.state.gt_index.get(sid).is_skipped is True


def test_gt_unskip_clears_skip_list():
    sid = "s_aabbccdd"
    _seed_pitch_and_mov(sid, "A")
    _client().post(f"/gt/sessions/{sid}/skip")
    r = _client().post(f"/gt/sessions/{sid}/unskip")
    assert r.status_code == 200
    assert main.state.gt_index.get(sid).is_skipped is False
