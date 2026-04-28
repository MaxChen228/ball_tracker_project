"""GT-driven distillation routes.

Phase E2 of the GT-driven distillation pipeline. Provides operator-
facing HTTP surfaces for:

  POST /sessions/{sid}/run_gt_labelling   queue SAM 3 GT labelling job
  POST /sessions/{sid}/run_validation     queue three-way validation
  POST /sessions/{sid}/cancel_gt          cancel a queued/running job
  POST /gt/distill                        run fit pipeline on all GT
  POST /gt/apply_proposal                 apply per-category proposals
                                          to data/*.json + WS broadcast
  GET  /gt/proposals                      return latest fit_proposals
  GET  /report/{sid}                      SSR three-way report page

Background tasks:
  - label_with_sam3.py is invoked via subprocess so the heavy SAM 3 +
    torch deps stay in tools/.venv. Production server venv never
    imports torch.
  - distill_all.py + validate_three_way.py run in-process — they only
    need numpy / opencv (server venv has both). Avoids subprocess
    startup overhead which dominates over the actual fit cost.

Cancellation:
  Each GT job lives in `state._gt_processing` keyed by job kind +
  session id. `should_cancel` callback flips when the operator hits
  /sessions/{sid}/cancel_gt. SAM 3 subprocess gets killed via
  Popen.terminate(); in-process distill / validate sees the cancel
  flag at the start of each per-record loop iteration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


# ----- helpers -----------------------------------------------------


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def _validate_sid(session_id: str, request: Request) -> None:
    """Raise 422 (or HTML redirect) on a bad session id."""
    if _SESSION_ID_RE.match(session_id):
        return
    from main import _wants_html
    if _wants_html(request):
        # We can't raise from a sync helper for the HTML path — leave
        # the redirect to the caller. But also block the bad id from
        # reaching state.* by raising HTTPException; the route handler
        # catches it for HTML and redirects.
        raise HTTPException(status_code=422, detail="invalid session_id")
    raise HTTPException(status_code=422, detail="invalid session_id")


# ----- background workers ------------------------------------------


def _run_sam3_label_subprocess(
    session_id: str,
    camera_id: str,
    prompt: str,
    min_confidence: float,
) -> None:
    """Invoke `label_with_sam3.py` via the tools venv. Runs in a
    BackgroundTask thread (not async); blocking subprocess is fine.

    The script writes its own GT JSON to data/gt/sam3/. We just
    propagate completion/cancel flags through state._gt_processing so
    the dashboard can show progress."""
    from main import state
    proc = state._gt_processing
    job_key = ("label", session_id, camera_id)
    cmd = [
        "uv", "run", "--project", "../tools",
        "python", str(_scripts_dir() / "label_with_sam3.py"),
        "--session", session_id,
        "--cam", camera_id,
        "--prompt", prompt,
        "--min-confidence", str(min_confidence),
    ]
    log_path = state.data_dir / "gt" / "sam3" / f"session_{session_id}_{camera_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-cancel check: if the operator hit Cancel between start_job
    # (request thread) and now (BackgroundTask thread spinning up), bail
    # without ever spawning the subprocess. Without this we'd burn a
    # full subprocess startup + ~5GB model load before noticing.
    if proc.is_canceled(job_key):
        proc.finish_job(job_key, status="canceled")
        return
    try:
        with log_path.open("w") as logf:
            popen = subprocess.Popen(
                cmd,
                cwd=Path(__file__).resolve().parent.parent,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
            proc.set_subprocess_pid(job_key, popen.pid)
            while popen.poll() is None:
                if proc.is_canceled(job_key):
                    popen.terminate()
                    try:
                        popen.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        popen.kill()
                    break
                # We can't asyncio.sleep in a BackgroundTask thread —
                # use blocking sleep. ~1 s polling means cancel takes
                # at most 1 s to reach SIGTERM.
                try:
                    popen.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            rc = popen.returncode
        if rc == 0:
            proc.finish_job(job_key, status="completed")
        else:
            proc.finish_job(
                job_key,
                status="canceled" if proc.is_canceled(job_key) else "error",
                error=f"label_with_sam3 exit={rc} (see {log_path.name})" if rc != 0 else None,
            )
    except Exception as e:
        logger.exception("sam3 label job failed: %s", e)
        proc.finish_job(job_key, status="error", error=str(e))


def _run_validation_inproc(session_id: str, camera_id: str) -> None:
    """Direct in-process call into validate_three_way (server venv
    has all deps it needs)."""
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
        # `--skip-eval` would speed this up, but we want the eval; the
        # operator-side button explicitly chose this.
        rc = distill_main(
            [
                "--data-dir", str(state.data_dir),
                "--out", str(out_path),
            ],
            should_cancel=lambda: proc.is_canceled(job_key),
        )
        if rc == 130:
            # distill_all returns 130 (SIGINT-style) on operator cancel.
            proc.finish_job(job_key, status="canceled")
            return
        if rc != 0:
            proc.finish_job(job_key, status="error", error=f"distill_all exit={rc}")
            return
        # Cache the freshly written proposals on State so the dashboard
        # doesn't have to re-read the file on every /status tick.
        state._gt_proposals = json.loads(out_path.read_text())
        proc.finish_job(job_key, status="completed")
    except Exception as e:
        logger.exception("distill job failed: %s", e)
        proc.finish_job(job_key, status="error", error=str(e))


# ----- routes ------------------------------------------------------


@router.post("/sessions/{session_id}/run_gt_labelling")
async def run_gt_labelling(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Queue SAM 3 GT labelling for both cameras of a session.

    Body (optional JSON): {"prompt": "blue ball", "min_confidence": 0.5,
                           "cams": ["A", "B"]}"""
    from main import state, _wants_html
    _validate_sid(session_id, request)
    prompt = "blue ball"
    min_confidence = 0.5
    cams = ["A", "B"]
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        body = await request.json()
        prompt = str(body.get("prompt") or prompt)
        min_confidence = float(body.get("min_confidence") or min_confidence)
        if isinstance(body.get("cams"), list):
            cams = [str(c) for c in body["cams"]]

    queued = []
    for cam in cams:
        clip = next(
            (p for p in (state.data_dir / "videos").glob(f"session_{session_id}_{cam}.*")
             if p.suffix.lower() in (".mov", ".mp4", ".m4v")),
            None,
        )
        if clip is None:
            logger.warning("skip GT label %s/%s: no MOV", session_id, cam)
            continue
        if not state._gt_processing.start_job(("label", session_id, cam)):
            logger.info("GT label %s/%s already running, skipping", session_id, cam)
            continue
        background_tasks.add_task(
            _run_sam3_label_subprocess,
            session_id, cam, prompt, min_confidence,
        )
        queued.append(cam)

    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session_id": session_id, "queued": queued}


@router.post("/sessions/{session_id}/run_validation")
async def run_validation(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Queue three-way validation for both cameras of a session."""
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


@router.post("/sessions/{session_id}/cancel_gt")
async def cancel_gt(session_id: str, request: Request):
    from main import state, _wants_html
    _validate_sid(session_id, request)
    n_canceled = state._gt_processing.cancel_session(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session_id": session_id, "n_canceled": n_canceled}


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
    """Flag the running distillation job for cancellation. The eval
    loop in distill_all polls between holdout records / between frames
    and bails with exit 130 within ~1 frame's decode time. Idempotent
    — calling when no distill is running returns ok=True, flagged=False."""
    from main import state, _wants_html
    flagged = state._gt_processing.cancel_distill()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "flagged": flagged}


@router.get("/gt/proposals")
async def gt_proposals():
    """Latest fit_proposals.json contents — cached on State to avoid
    file IO on every dashboard tick."""
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
        # selector_tuning is server-only; no WS broadcast (per existing
        # convention in routes/settings.py).

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
