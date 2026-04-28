"""GT labelling + distillation routes.

Two coexisting workflows live in this module (mini-plan v4):

  **Labelling** — operator picks a session, marks a video-relative
    time window per cam, and adds a queue item. The persistent FIFO
    in `state.gt_queue` is consumed by the in-process GTQueueWorker
    (started in main.py lifespan) which spawns `label_with_sam3.py`
    as a subprocess. All endpoints under `/gt/queue/*` and the
    page/listing endpoints (`/gt`, `/gt/sessions`, `/gt/timeline/*`,
    `/gt/preview/*`, `/gt/sessions/{sid}/skip`) drive this surface.

  **Distillation** — fit HSV / shape_gate / selector params from the
    pooled GT mask statistics. Distillation never touches the
    labelling queue (mini-plan v4 cut auto-pause); operator presses
    [Pause] manually if they want to interleave a long fit-eval. The
    distillation routes (`/gt/distill`, `/gt/cancel_distill`,
    `/gt/proposals`, `/gt/apply_proposal`) and `GET /report/{sid}`
    plus `POST /sessions/{sid}/run_validation` are preserved here.

The dropped surface (mini-plan v4):
  * `POST /sessions/{sid}/run_gt_labelling` → use `POST /gt/queue`.
  * `POST /sessions/{sid}/cancel_gt` → use `DELETE /gt/queue/{id}`.

These removed endpoints share no code path with the new queue (the
old per-session `state._gt_processing` "label" jobs were a tracker,
not a queue). They have no callers in the dashboard after Phase 5.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")
_QUEUE_ID_RE = re.compile(r"^q_[0-9a-f]{8}$")


# ----- helpers -----------------------------------------------------


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def _validate_sid(session_id: str, request: Request) -> None:
    """Raise 422 (or HTML redirect) on a bad session id."""
    if _SESSION_ID_RE.match(session_id):
        return
    from main import _wants_html
    if _wants_html(request):
        raise HTTPException(status_code=422, detail="invalid session_id")
    raise HTTPException(status_code=422, detail="invalid session_id")


# ----- background workers (kept from v1: validation + distillation) ---


def _run_validation_inproc(session_id: str, camera_id: str) -> None:
    """Direct in-process call into validate_three_way (server venv has
    all deps). Uses `state._gt_processing` purely for cancel/error
    tracking — the new `state.gt_queue` is for labelling only."""
    from main import state
    proc = state._gt_processing
    job_key = ("validate", session_id, camera_id)
    sys.path.insert(0, str(_scripts_dir()))
    try:
        from validate_three_way import validate_session_cam, _write_outputs  # type: ignore[import-not-found]
        gt_path = state.data_dir / "gt" / "sam3" / f"session_{session_id}_{camera_id}.json"
        pitch_path = state.data_dir / "pitches" / f"session_{session_id}_{camera_id}.json"
        if not gt_path.is_file():
            proc.finish_job(job_key, status="error", error="GT JSON missing — run labelling first")
            return
        if not pitch_path.is_file():
            proc.finish_job(job_key, status="error", error="pitch JSON missing")
            return
        if proc.is_canceled(job_key):
            proc.finish_job(job_key, status="canceled")
            return
        report, rows = validate_session_cam(
            pitch_path=pitch_path,
            gt_path=gt_path,
            match_radius_px=8.0,
        )
        _write_outputs(report, rows, out_dir=state.data_dir / "gt" / "validation")
        # Validation just rewrote per-session JSON — invalidate the
        # /gt index entry so the next session-list fetch reflects it.
        try:
            state.gt_index.invalidate(session_id)
        except Exception:
            pass
        proc.finish_job(job_key, status="completed")
    except Exception as e:
        logger.exception("validate job failed: %s", e)
        proc.finish_job(job_key, status="error", error=str(e))


def _run_distill_inproc() -> None:
    from main import state
    proc = state._gt_processing
    job_key = ("distill", "global", "global")
    sys.path.insert(0, str(_scripts_dir()))
    try:
        from distill_all import main as distill_main  # type: ignore[import-not-found]
        out_path = state.data_dir / "gt" / "fit_proposals.json"
        rc = distill_main(
            [
                "--data-dir", str(state.data_dir),
                "--out", str(out_path),
            ],
            should_cancel=lambda: proc.is_canceled(job_key),
        )
        if rc == 130:
            proc.finish_job(job_key, status="canceled")
            return
        if rc != 0:
            proc.finish_job(job_key, status="error", error=f"distill_all exit={rc}")
            return
        state._gt_proposals = json.loads(out_path.read_text())
        proc.finish_job(job_key, status="completed")
    except Exception as e:
        logger.exception("distill job failed: %s", e)
        proc.finish_job(job_key, status="error", error=str(e))


# ----- validation + distill routes (preserved) -----------------------


@router.post("/sessions/{session_id}/run_validation")
async def run_validation(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Queue three-way validation for both cameras of a session.

    NOTE: triggered manually from the operator's CLI / `/gt` page after
    GT exists; never auto-fires. The /gt page renders a [Validate]
    button on the session detail header that POSTs here."""
    from main import state, _wants_html
    _validate_sid(session_id, request)
    cams = ["A", "B"]
    queued = []
    for cam in cams:
        if not state._gt_processing.start_job(("validate", session_id, cam)):
            continue
        background_tasks.add_task(_run_validation_inproc, session_id, cam)
        queued.append(cam)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session_id": session_id, "queued": queued}


@router.post("/gt/distill")
async def gt_distill(request: Request, background_tasks: BackgroundTasks):
    """Trigger fit_*.py + distill_all.py over every GT record."""
    from main import state, _wants_html
    if not state._gt_processing.start_job(("distill", "global", "global")):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="distillation already running")
    background_tasks.add_task(_run_distill_inproc)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "queued": "distill"}


@router.post("/gt/cancel_distill")
async def gt_cancel_distill(request: Request):
    from main import state, _wants_html
    flagged = state._gt_processing.cancel_distill()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "flagged": flagged}


@router.get("/gt/proposals")
async def gt_proposals():
    from main import state
    proposals_path = state.data_dir / "gt" / "fit_proposals.json"
    if state._gt_proposals is not None:
        return state._gt_proposals
    if not proposals_path.is_file():
        return {"available": False, "message": "no proposals yet — run /gt/distill first"}
    payload = json.loads(proposals_path.read_text())
    state._gt_proposals = payload
    return payload


@router.post("/gt/apply_proposal")
async def gt_apply_proposal(request: Request):
    """Apply a category of proposed params to data/*.json + WS push.

    Body JSON: {"category": "hsv_range" | "shape_gate" | "selector_tuning"}
    """
    from main import state, device_ws, _settings_message_for, _wants_html

    body = await request.json() if request.headers.get("content-type", "").lower().startswith("application/json") else {}
    category = body.get("category")
    if category not in {"hsv_range", "shape_gate", "selector_tuning"}:
        raise HTTPException(status_code=400, detail="category must be one of hsv_range/shape_gate/selector_tuning")

    proposals_path = state.data_dir / "gt" / "fit_proposals.json"
    if not proposals_path.is_file():
        raise HTTPException(status_code=409, detail="no proposals to apply — run /gt/distill first")
    payload = json.loads(proposals_path.read_text())
    proposed = payload.get("params", {}).get("proposed", {})
    values = proposed.get(category)
    if values is None:
        raise HTTPException(status_code=409, detail=f"proposal payload missing {category}")

    if category == "hsv_range":
        from detection import HSVRange
        rng = HSVRange(
            h_min=int(values["h_min"]), h_max=int(values["h_max"]),
            s_min=int(values["s_min"]), s_max=int(values["s_max"]),
            v_min=int(values["v_min"]), v_max=int(values["v_max"]),
        )
        state.set_hsv_range(rng)
        await device_ws.broadcast({c.camera_id: _settings_message_for() for c in state.online_devices()})
    elif category == "shape_gate":
        from detection import ShapeGate
        gate = ShapeGate(
            aspect_min=float(values["aspect_min"]),
            fill_min=float(values["fill_min"]),
        )
        state.set_shape_gate(gate)
        await device_ws.broadcast({c.camera_id: _settings_message_for() for c in state.online_devices()})
    elif category == "selector_tuning":
        from candidate_selector import CandidateSelectorTuning
        tuning = CandidateSelectorTuning(
            r_px_expected=float(values["r_px_expected"]),
            w_area=float(values["w_area"]),
            w_dist=float(values["w_dist"]),
            dist_cost_sat_radii=float(values["dist_cost_sat_radii"]),
        )
        state.set_candidate_selector_tuning(tuning)

    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "category": category}


@router.get("/report/{session_id}")
async def report(session_id: str):
    """SSR three-way report page (live vs server_post vs SAM 3 GT)."""
    from main import state  # noqa: F401  (kept for symmetry)
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    val_dir = state.data_dir / "gt" / "validation"
    cam_payloads: dict[str, dict] = {}
    for cam in ("A", "B"):
        path = val_dir / f"session_{session_id}_{cam}.json"
        if path.is_file():
            cam_payloads[cam] = json.loads(path.read_text())
    if not cam_payloads:
        raise HTTPException(
            status_code=404,
            detail=f"no validation report for {session_id} — run /sessions/{{sid}}/run_validation first",
        )

    from render_report import render_report_page
    html = render_report_page(session_id, cam_payloads)
    return HTMLResponse(html)


# ----- /gt page + queue endpoints (mini-plan v4) ---------------------


class QueueAddBody(BaseModel):
    """`POST /gt/queue` body. Field-level validation lives here; range +
    MOV-existence checks happen in the handler against State so we get
    a 422 with detail rather than a Pydantic 422 with a generic message."""
    session_id: str = Field(pattern=r"^s_[0-9a-f]{4,32}$")
    camera_id: Literal["A", "B"]
    time_range: tuple[float, float]
    prompt: str = Field(min_length=1, max_length=200)


@router.get("/gt", response_class=HTMLResponse)
async def gt_page(request: Request):
    """SSR /gt page. Phase 4 fills in the renderer; for now we return
    a placeholder so the route shape is exercised by tests."""
    try:
        from render_gt_page import render_gt_page
    except ImportError:
        return HTMLResponse(
            "<html><body><h1>/gt</h1><p>page renderer not yet wired</p></body></html>",
            status_code=200,
        )
    from main import state
    return HTMLResponse(render_gt_page(state))


@router.get("/gt/sessions")
async def gt_sessions():
    """JSON: every session with a pitch JSON, sorted by recency.

    Front-end polls this every 5 s to refresh row tints / glyphs after
    GT / validation / skip writes."""
    from main import state
    states = state.gt_index.get_all()
    return {"sessions": [s.to_dict() for s in states]}


@router.get("/gt/timeline/{session_id}/{camera_id}.json")
async def gt_timeline(session_id: str, camera_id: str):
    """Detection-density heatmap for the editor timeline.

    Returns 100 ms-bucket counts of frames where px ≠ None. Source
    preference: `frames_live` → `frames_server_post` → empty. Buckets
    are returned as a `[start_s, count]` list; the renderer normalises
    to 0-1 by dividing by the bucket max."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    if camera_id not in ("A", "B"):
        raise HTTPException(status_code=422, detail="camera_id must be A or B")
    from main import state
    pitch_path = state.data_dir / "pitches" / f"session_{session_id}_{camera_id}.json"
    if not pitch_path.is_file():
        raise HTTPException(status_code=404, detail="no pitch JSON for that (session, cam)")
    payload = json.loads(pitch_path.read_text())
    video_start = float(payload.get("video_start_pts_s", 0.0))
    # Source selection: live first; if empty, fall back to server_post.
    chosen_source = "frames_live"
    frames = [
        f for f in (payload.get("frames_live") or [])
        if isinstance(f, dict) and f.get("px") is not None
    ]
    if not frames:
        frames = [
            f for f in (payload.get("frames_server_post") or [])
            if isinstance(f, dict) and f.get("px") is not None
        ]
        chosen_source = "frames_server_post"

    # Bucket size 100 ms. Find duration first so the renderer knows
    # the timeline span even when there are no detections.
    duration_s = 0.0
    last_ts = None
    for source in (payload.get("frames_server_post") or [], payload.get("frames_live") or []):
        if source:
            last = source[-1]
            if isinstance(last, dict) and isinstance(last.get("timestamp_s"), (int, float)):
                last_ts = float(last["timestamp_s"])
                break
    if last_ts is not None:
        duration_s = max(0.0, last_ts - video_start)

    bucket_size_s = 0.1
    n_buckets = max(1, int(round(duration_s / bucket_size_s)))
    counts = [0] * n_buckets
    for f in frames:
        ts = f.get("timestamp_s")
        if not isinstance(ts, (int, float)):
            continue
        t_video = float(ts) - video_start
        if t_video < 0:
            continue
        idx = int(t_video / bucket_size_s)
        if 0 <= idx < n_buckets:
            counts[idx] += 1

    return {
        "session_id": session_id,
        "camera_id": camera_id,
        "source": chosen_source if frames else "empty",
        "bucket_size_s": bucket_size_s,
        "duration_s": duration_s,
        "buckets": counts,
    }


@router.post("/gt/queue")
async def gt_queue_add(body: QueueAddBody):
    """Add an item to the GT labelling queue.

    Returns 422 if:
      * range invalid (start ≥ end, start < 0, end > video_duration + 0.5s slack)
      * (sid, cam) has no MOV on disk
      * sid not in the GTIndex (no pitch JSON)
    Otherwise returns the minted queue id."""
    from main import state
    t_start, t_end = body.time_range
    if not (0.0 <= t_start < t_end):
        raise HTTPException(
            status_code=422,
            detail=f"time_range must satisfy 0 <= start < end (got {t_start}, {t_end})",
        )
    try:
        sgt = state.gt_index.get(body.session_id)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"session not in index: {e}")
    if not sgt.cams_present.get(body.camera_id):
        raise HTTPException(
            status_code=422,
            detail=f"no pitch JSON for {body.session_id}/{body.camera_id}",
        )
    if not sgt.has_mov.get(body.camera_id):
        raise HTTPException(
            status_code=422,
            detail=f"no MOV on disk for {body.session_id}/{body.camera_id}",
        )
    duration = sgt.video_duration_s.get(body.camera_id)
    if duration is not None and t_end > duration + 0.5:
        raise HTTPException(
            status_code=422,
            detail=f"time_range end ({t_end}) exceeds video duration ({duration:.3f}+0.5)",
        )

    qid = state.gt_queue.add(
        session_id=body.session_id,
        camera_id=body.camera_id,
        time_range=(t_start, t_end),
        prompt=body.prompt,
    )
    # Persist last-used prompt globally so the next add prefills it.
    try:
        last_prompt_path = state.data_dir / "gt" / "last_prompt.json"
        last_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = last_prompt_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"prompt": body.prompt}))
        os.replace(tmp, last_prompt_path)
    except Exception:
        pass
    return {"id": qid, "session_id": body.session_id, "camera_id": body.camera_id}


@router.get("/gt/queue")
async def gt_queue_get():
    """Full queue state for 1 Hz front-end poll."""
    from main import state
    items = state.gt_queue.get_all()
    return {
        "items": [item.to_dict() for item in items],
        "paused": state.gt_queue.paused(),
    }


@router.delete("/gt/queue/{queue_id}")
async def gt_queue_cancel(queue_id: str):
    if not _QUEUE_ID_RE.match(queue_id):
        raise HTTPException(status_code=422, detail="invalid queue_id")
    from main import state
    ok = state.gt_queue.cancel(queue_id)
    return {"ok": ok}


@router.post("/gt/queue/{queue_id}/retry")
async def gt_queue_retry(queue_id: str):
    if not _QUEUE_ID_RE.match(queue_id):
        raise HTTPException(status_code=422, detail="invalid queue_id")
    from main import state
    new_id = state.gt_queue.retry(queue_id)
    if new_id is None:
        raise HTTPException(
            status_code=409,
            detail="item is not in a retryable state (must be error/canceled/done)",
        )
    return {"id": new_id}


@router.delete("/gt/queue/done")
async def gt_queue_clear_done():
    from main import state
    n = state.gt_queue.clear_done()
    return {"removed": n}


@router.delete("/gt/queue/errors")
async def gt_queue_clear_errors():
    from main import state
    n = state.gt_queue.clear_errors()
    return {"removed": n}


@router.post("/gt/queue/run")
async def gt_queue_run():
    from main import state
    state.gt_queue.resume()
    return {"ok": True, "paused": False}


@router.post("/gt/queue/pause")
async def gt_queue_pause():
    from main import state
    state.gt_queue.pause()
    return {"ok": True, "paused": True}


@router.get("/gt/preview/{queue_id}.jpg")
async def gt_preview(queue_id: str):
    """Serve the worker-written mask preview JPEG.

    Regex-validate the queue id BEFORE Path joining (path traversal
    hardening). Miss returns **204** rather than 404 because the front
    end polls this URL aggressively from the moment a job goes
    `running` — the very first PROGRESS line lands before the first
    JPEG is written, and a 404 in DevTools console for that brief
    window is just noise."""
    if not _QUEUE_ID_RE.match(queue_id):
        raise HTTPException(status_code=422, detail="invalid queue_id")
    from main import state
    path = state.data_dir / "gt" / "preview" / f"{queue_id}.jpg"
    if not path.is_file():
        return Response(status_code=204)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/gt/sessions/{session_id}/skip")
async def gt_skip(session_id: str):
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    from main import state
    state.gt_index.add_skip(session_id)
    return {"ok": True, "session_id": session_id, "skipped": True}


@router.post("/gt/sessions/{session_id}/unskip")
async def gt_unskip(session_id: str):
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    from main import state
    state.gt_index.remove_skip(session_id)
    return {"ok": True, "session_id": session_id, "skipped": False}
