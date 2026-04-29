"""Standalone fit page — runs multi-segment ballistic extraction live on
the session's triangulated points and renders a Plotly 3D figure with
per-segment colors. Path source picked via `?path=live|server_post`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from render_fit import build_fit_figure, render_fit_html
from segmenter import find_segments

router = APIRouter()

_VALID_PATHS = ("server_post", "live")


@router.get("/fit/{session_id}", response_class=HTMLResponse)
def fit_page(
    session_id: str,
    path: str = Query("server_post"),
) -> HTMLResponse:
    if path not in _VALID_PATHS:
        raise HTTPException(422, f"path must be one of {_VALID_PATHS}")

    from main import state
    from routes.viewer import _scene_for_session

    result = state.get(session_id)
    if result is None:
        raise HTTPException(404, f"session {session_id} not found")

    pts_in = result.triangulated_by_path.get(path, [])
    available = sorted(
        p for p in _VALID_PATHS
        if result.triangulated_by_path.get(p)
    )

    scene = _scene_for_session(session_id)
    segments, pts_sorted, kept_mask = find_segments(pts_in)

    fig = build_fit_figure(scene, pts_in, pts_sorted, kept_mask, segments)
    fig_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    html = render_fit_html(
        session_id=session_id,
        path=path,
        available_paths=available or [path],
        n_input=len(pts_in),
        n_kept=int(kept_mask.sum()) if kept_mask.size else 0,
        segments=segments,
        fig_html=fig_html,
    )
    return HTMLResponse(html)
