from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from control_routes import wants_html
from schemas import SyncLogBody, SyncReport


def build_sync_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    sync_start_status_for_reason: dict[str, int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/sync/start")
    async def sync_start(request: Request) -> dict[str, Any]:
        state = get_state()
        device_ws = get_device_ws()
        run, reason = state.start_sync()
        if reason is not None:
            status_code = sync_start_status_for_reason.get(reason, 409)
            raise HTTPException(
                status_code=status_code,
                detail={"ok": False, "error": reason},
            )
        assert run is not None
        await device_ws.broadcast(
            {
                cam.camera_id: {"type": "sync_run", "sync_id": run.id}
                for cam in state.online_devices()
            }
        )
        return {"ok": True, "sync": run.to_dict()}

    @router.post("/sync/report")
    async def sync_report(report: SyncReport) -> dict[str, Any]:
        state = get_state()
        run_after, result, reason = state.record_sync_report(report)
        if reason == "no_sync":
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": "no_sync"},
            )
        if reason == "stale_sync_id":
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": "stale_sync_id"},
            )
        resp: dict[str, Any] = {"ok": True, "solved": result is not None}
        if result is not None:
            resp["result"] = result.model_dump()
        elif run_after is not None:
            resp["run"] = run_after.to_dict()
        return resp

    @router.get("/sync/state")
    def sync_state(log_limit: int = 200) -> dict[str, Any]:
        state = get_state()
        run = state.current_sync()
        last = state.last_sync_result()
        logs = state.sync_logs(limit=log_limit)
        return {
            "sync": run.to_dict() if run is not None else None,
            "last_sync": last.model_dump() if last is not None else None,
            "cooldown_remaining_s": state.sync_cooldown_remaining_s(),
            "logs": [entry.model_dump() for entry in logs],
        }

    @router.post("/sync/trigger")
    async def sync_trigger(request: Request) -> Any:
        state = get_state()
        ctype = request.headers.get("content-type", "").lower()
        is_form = (
            "application/x-www-form-urlencoded" in ctype
            or "multipart/form-data" in ctype
        )
        camera_ids: list[str] | None = None
        if "application/json" in ctype:
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                raw = body.get("camera_ids")
                if isinstance(raw, list):
                    camera_ids = [str(c) for c in raw]
                elif raw is not None:
                    raise HTTPException(
                        status_code=422,
                        detail="camera_ids must be a list of strings",
                    )
        elif is_form:
            form = await request.form()
            raw = form.get("camera_ids")
            if raw is not None:
                camera_ids = [
                    c for c in (str(raw).replace(",", " ").split()) if c
                ]

        dispatched = state.trigger_sync_command(camera_ids)
        pending = state.pending_sync_commands()
        ws_messages = {
            cam: {"type": "sync_command", "command": "start", "sync_command_id": sid}
            for cam, sid in pending.items()
            if cam in dispatched
        }
        if ws_messages:
            await get_device_ws().broadcast(ws_messages)
        if is_form:
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "dispatched_to": dispatched}

    @router.post("/sync/claim")
    def sync_claim() -> dict[str, Any]:
        intent = get_state().claim_time_sync_intent()
        return {
            "ok": True,
            "sync_id": intent.id,
            "started_at": intent.started_at,
            "expires_at": intent.expires_at,
        }

    @router.post("/sync/log")
    async def sync_log_post(body: SyncLogBody) -> dict[str, Any]:
        get_state().log_sync_event(
            source=body.camera_id,
            event=body.event,
            detail=body.detail,
        )
        return {"ok": True}

    return router
