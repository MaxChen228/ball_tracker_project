from __future__ import annotations

import json
import time
from typing import Any, Callable

import numpy as np
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from control_routes import settings_message_for, wants_html


def build_preview_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    preview_request_ttl_s: float,
    time_sync_max_age_s: float,
) -> APIRouter:
    router = APIRouter()

    @router.post("/camera/{camera_id}/calibration_frame")
    async def camera_calibration_frame(camera_id: str, request: Request) -> dict[str, Any]:
        _validate_camera_id_or_422(camera_id)
        state = get_state()
        if not state.is_calibration_frame_requested(camera_id):
            raise HTTPException(
                status_code=409,
                detail="calibration frame not requested for this camera",
            )
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
        _validate_camera_id_or_422(camera_id)
        state = get_state()
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
    def camera_preview_latest(camera_id: str, annotate: int = 0) -> Response:
        _validate_camera_id_or_422(camera_id)
        got = get_state()._preview.latest(camera_id)
        if got is None:
            raise HTTPException(status_code=404, detail="no preview frame")
        jpeg_bytes, _ = got
        if annotate:
            jpeg_bytes = _annotate_preview_jpeg(jpeg_bytes)
        return Response(
            content=jpeg_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @router.get("/camera/{camera_id}/preview.mjpeg")
    def camera_preview_mjpeg(camera_id: str) -> Response:
        _validate_camera_id_or_422(camera_id)
        boundary = "ballpreviewframe"
        state = get_state()

        def stream():
            last_ts: float | None = None
            idle_deadline: float | None = None
            tick_s = 1.0 / 10.0
            try:
                while True:
                    if not state._preview.is_requested(camera_id):
                        break
                    got = state._preview.latest(camera_id)
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
                                idle_deadline = now + preview_request_ttl_s * 2
                            elif now > idle_deadline:
                                break
                    else:
                        if idle_deadline is None:
                            idle_deadline = now + preview_request_ttl_s * 2
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
        _validate_camera_id_or_422(camera_id)
        state = get_state()
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
        await get_device_ws().send(
            camera_id,
            settings_message_for(
                camera_id=camera_id,
                state=state,
                device_ws=get_device_ws(),
                time_sync_max_age_s=time_sync_max_age_s,
            ),
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return Response(
            json.dumps({"ok": True, "enabled": flag}),
            media_type="application/json",
        )

    return router


_CAMERA_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,16}$")


def _validate_camera_id_or_422(camera_id: str) -> None:
    if not _CAMERA_ID_RE.match(camera_id):
        raise HTTPException(status_code=422, detail="invalid camera_id")


def _annotate_preview_jpeg(jpeg_bytes: bytes) -> bytes:
    import cv2  # noqa: WPS433

    try:
        from calibration_solver import PLATE_MARKER_WORLD, detect_all_markers_in_dict

        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return jpeg_bytes
        for m in detect_all_markers_in_dict(bgr):
            is_plate = m.id in PLATE_MARKER_WORLD
            colour = (60, 200, 60) if is_plate else (230, 160, 60)
            pts = m.corners.astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(bgr, [pts], isClosed=True, color=colour, thickness=3)
            cx, cy = m.corners.mean(axis=0)
            label = f"ID {m.id}"
            (tw, th), _base = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2
            )
            tx, ty = int(cx) - tw // 2, int(cy) + th // 2
            cv2.rectangle(
                bgr,
                (tx - 4, ty - th - 4),
                (tx + tw + 4, ty + 6),
                colour,
                -1,
            )
            cv2.putText(
                bgr,
                label,
                (tx, ty),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 0),
                2,
                cv2.LINE_AA,
            )
        ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return bytes(encoded.tobytes()) if ok else jpeg_bytes
    except Exception:
        return jpeg_bytes
