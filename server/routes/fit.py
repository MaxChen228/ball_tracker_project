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
    path: str | None = Query(None),
) -> HTMLResponse:
    """Render the multi-segment fit page for a session.

    Path selection rules (no silent fallback — explicit auto-pick):
      - `?path=` omitted → pick `server_post` if it has triangulated
        points, else `live` if it has, else 404
      - `?path=<value>` with `<value>` not in `_VALID_PATHS` → 422
      - `?path=<value>` with no triangulated points on `<value>` → 404
        with the available paths spelled out so the user can switch
    """
    if path is not None and path not in _VALID_PATHS:
        raise HTTPException(422, f"path must be one of {_VALID_PATHS}")

    from main import state
    from routes.viewer import _scene_for_session

    result = state.get(session_id)
    if result is None:
        raise HTTPException(404, f"session {session_id} not found")

    available = [
        p for p in _VALID_PATHS  # _VALID_PATHS order = (server_post, live)
        if result.triangulated_by_path.get(p)
    ]
    if path is None:
        if not available:
            raise HTTPException(
                404,
                f"session {session_id} has no triangulated points on any path",
            )
        path = available[0]
    pts_in = result.triangulated_by_path.get(path, [])
    if not pts_in:
        if available:
            raise HTTPException(
                404,
                f"session {session_id} has no triangulated points on path "
                f"'{path}'; available: {','.join(available)}",
            )
        raise HTTPException(
            404,
            f"session {session_id} has no triangulated points on any path",
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
