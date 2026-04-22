from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from preview import FRAME_MAX_AGE_S as _PREVIEW_FRAME_MAX_AGE_S

router = APIRouter()

_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _validate_camera_id_or_422(camera_id: str) -> None:
    if not _CAMERA_ID_RE.match(camera_id):
        raise HTTPException(status_code=422, detail="invalid camera_id")


@router.post("/camera/{camera_id}/calibration_frame")
async def camera_calibration_frame(camera_id: str, request: Request) -> dict[str, Any]:
    from main import state
    _validate_camera_id_or_422(camera_id)
    if not state.is_calibration_frame_requested(camera_id):
        raise HTTPException(status_code=409, detail="calibration frame not requested for this camera")
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        file_field = form.get("file")
        if file_field is None or not hasattr(file_field, "read"):
            raise HTTPException(status_code=422, detail="missing `file` part")
        body = await file_field.read()
    else:
        body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="empty body")
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="calibration frame too large")
    state.store_calibration_frame(camera_id, bytes(body))
    return {"ok": True, "bytes": len(body)}


@router.post("/camera/{camera_id}/preview_frame")
async def camera_preview_frame(camera_id: str, request: Request) -> dict[str, Any]:
    from main import state
    _validate_camera_id_or_422(camera_id)
    if not state._preview.is_requested(camera_id):
        raise HTTPException(status_code=409, detail="preview not requested")
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        file_field = form.get("file")
        if file_field is None or not hasattr(file_field, "read"):
            raise HTTPException(status_code=422, detail="missing `file` part")
        body = await file_field.read()
    else:
        body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="empty body")
    ok = state._preview.push(camera_id, bytes(body), ts=time.time())
    if not ok:
        raise HTTPException(status_code=413, detail="preview frame too large")
    return {"ok": True, "bytes": len(body)}


@router.get("/camera/{camera_id}/preview")
def camera_preview_latest(camera_id: str) -> Response:
    from main import state
    _validate_camera_id_or_422(camera_id)
    got = state._preview.latest(camera_id, max_age_s=_PREVIEW_FRAME_MAX_AGE_S)
    if got is None:
        raise HTTPException(status_code=404, detail="no preview frame")
    jpeg_bytes, _ = got
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@router.get("/camera/{camera_id}/preview.mjpeg")
def camera_preview_mjpeg(camera_id: str) -> Response:
    from main import state
    _validate_camera_id_or_422(camera_id)
    boundary = "ballpreviewframe"

    def stream():
        last_ts: float | None = None
        idle_deadline: float | None = None
        tick_s = 1.0 / 10.0
        try:
            while True:
                if not state._preview.is_requested(camera_id):
                    break
                got = state._preview.latest(camera_id, max_age_s=_PREVIEW_FRAME_MAX_AGE_S)
                now = time.time()
                if got is not None:
                    jpeg_bytes, ts = got
                    if ts != last_ts:
                        last_ts = ts
                        idle_deadline = None
                        header = (
                            f"--{boundary}\r\n"
                            f"Content-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
                        ).encode()
                        yield header + jpeg_bytes + b"\r\n"
                    else:
                        if idle_deadline is None:
                            idle_deadline = now + 10.0
                        elif now > idle_deadline:
                            break
                else:
                    if idle_deadline is None:
                        idle_deadline = now + 10.0
                    elif now > idle_deadline:
                        break
                time.sleep(tick_s)
        except GeneratorExit:
            return

    return Response(
        stream(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/camera/{camera_id}/preview_request")
async def camera_preview_request(
    camera_id: str,
    request: Request,
    enabled: str | None = Form(default=None),
) -> Response:
    from main import state, device_ws, _settings_message_for, _wants_html
    _validate_camera_id_or_422(camera_id)
    raw: Any = enabled
    if raw is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = body.get("enabled")
        except Exception:
            raw = None
    if isinstance(raw, bool):
        flag = raw
    elif isinstance(raw, str):
        flag = raw.strip().lower() not in ("", "false", "0", "off", "no")
    elif raw is None:
        flag = True
    else:
        flag = bool(raw)
    state._preview.request(camera_id, enabled=flag)
    await device_ws.send(camera_id, _settings_message_for(camera_id))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    import json as _stdjson
    return Response(
        _stdjson.dumps({"ok": True, "enabled": flag}),
        media_type="application/json",
    )
