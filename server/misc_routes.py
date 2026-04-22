from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import Response

from chirp import chirp_wav_bytes


def build_misc_router(
    *,
    get_state: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/chirp.wav")
    def chirp_wav() -> Response:
        return Response(
            content=chirp_wav_bytes(),
            media_type="audio/wav",
            headers={"Content-Disposition": 'inline; filename="chirp.wav"'},
        )

    @router.post("/reset")
    def reset(purge: bool = False) -> dict[str, bool]:
        get_state().reset(purge_disk=purge)
        return {"ok": True, "purged": purge}

    return router
