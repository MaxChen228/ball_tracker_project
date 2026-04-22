from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile

from pitch_ingest import attach_pitch_analysis, ingest_pitch
from schemas import PitchAnalysisPayload


def build_pitch_router(
    *,
    get_state: Callable[[], Any],
    get_max_pitch_upload_bytes: Callable[[], int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/pitch")
    async def pitch(
        request: Request,
        background_tasks: BackgroundTasks,
        payload: str = Form(...),
        video: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        return await ingest_pitch(
            request=request,
            background_tasks=background_tasks,
            payload=payload,
            video=video,
            state=get_state(),
            max_pitch_upload_bytes=get_max_pitch_upload_bytes(),
        )

    @router.post("/pitch_analysis")
    async def pitch_analysis(payload: PitchAnalysisPayload) -> dict[str, Any]:
        return await attach_pitch_analysis(payload=payload, state=get_state())

    return router
