"""Pure-Python equivalents of three small algorithms that live as inline JS
inside cam-view consumers (markers `compareRows` + `handleCamClick`,
viewer `_drawDetectionForPath`).

The browser is the production runtime — these functions exist so we can
pin their behaviour with pytest without spinning up jsdom / playwright
for the project. Any change to one of these algorithms MUST also be
mirrored to the JS sibling listed below the function; otherwise viewer /
markers will drift out from under the regression tests.

JS siblings:
  * compare_rows_collapse  ->  render_markers.py::_MARKERS_JS::compareRows
  * find_detection_index   ->  viewer_page.py::_drawDetectionForPath
  * hit_test_nearest       ->  render_markers.py::_MARKERS_JS::handleCamClick
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

# --- compareRows priority collapse ----------------------------------------

# Higher priority wins on marker_id collisions. The runtime fields rendered
# off these origins are intentionally distinct: candidate gets a dashed
# outline + detail-save / detail-delete buttons; stored gets a solid green
# outline + detail-delete; known is read-only (no edit path), which is why
# the prior three-segment build silently broke when stored collided with a
# previously-pushed known row.
_PRIORITY = {"known": 0, "stored": 1, "candidate": 2}


def compare_rows_collapse(
    known: Iterable[dict[str, Any]],
    stored: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Single-pass upsert keyed by ``marker_id`` with explicit priority.

    Each row is shallow-copied with ``origin`` (and, for stored/candidate,
    ``kind``) stamped in. Iteration order across the inputs determines
    nothing; priority alone wins.
    """
    by_id: dict[Any, dict[str, Any]] = {}

    def upsert(row: dict[str, Any], origin: str, *, force_kind: str | None = None) -> None:
        out = dict(row)
        out["origin"] = origin
        if force_kind is not None:
            out["kind"] = force_kind
        out["_priority"] = _PRIORITY[origin]
        marker_id = _coerce_id(row.get("marker_id"))
        prev = by_id.get(marker_id)
        if prev is None or prev["_priority"] < out["_priority"]:
            by_id[marker_id] = out

    for row in known:
        upsert(row, "known")
    for row in stored:
        upsert(row, "stored", force_kind="stored")
    for row in candidates:
        upsert(row, "candidate", force_kind="candidate")

    out_rows = []
    for row in by_id.values():
        emit = dict(row)
        emit.pop("_priority", None)
        out_rows.append(emit)
    return out_rows


# --- detection time-lookup with left-scan-on-gap --------------------------


def find_detection_index(
    ts: Sequence[float],
    det: Sequence[bool],
    t: float,
    tol: float,
) -> int | None:
    """Return the index of the nearest detected sample to ``t`` within
    ``tol`` seconds, walking back from the binary-search hit if it lands
    on a not-detected entry.

    Mirrors the viewer's `_drawDetectionForPath` algorithm: server_post
    frame gaps leave sequential `det=False` rows. Without the left-scan,
    scrubbing across a gap blanks the dot and at 240 fps reads as flicker.
    """
    n = len(ts)
    if n == 0 or len(det) != n:
        return None
    if n == 1:
        if det[0] and abs(ts[0] - t) <= tol:
            return 0
        return None
    lo, hi = 0, n - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid
        else:
            hi = mid
    idx = lo if abs(ts[lo] - t) <= abs(ts[hi] - t) else hi
    while idx >= 0 and not det[idx] and abs(ts[idx] - t) <= tol:
        idx -= 1
    if idx < 0 or not det[idx] or abs(ts[idx] - t) > tol:
        return None
    return idx


# --- markers click hit-test ------------------------------------------------


def hit_test_nearest(
    rows: Sequence[dict[str, Any]],
    click_uv: tuple[float, float],
    project: Callable[[dict[str, Any]], tuple[float, float] | None],
    tol_px: float,
) -> dict[str, Any] | None:
    """Pick the row closest to the click within ``tol_px``, breaking ties
    in favour of higher rank (candidate beats stored beats known).

    The runtime supplies a ``project`` callable that maps a row to its
    image-space (u, v) — that's where the click point is too. Returns
    None if every row projects out-of-frame or beyond tol.
    """
    cu, cv = click_uv
    best: dict[str, Any] | None = None
    best_dist = float(tol_px)
    best_rank = -1
    for row in rows:
        proj = project(row)
        if proj is None:
            continue
        u, v = proj
        d = ((u - cu) ** 2 + (v - cv) ** 2) ** 0.5
        if d > tol_px:
            continue
        rank = _rank(row)
        if rank > best_rank or (rank == best_rank and d < best_dist):
            best = row
            best_dist = d
            best_rank = rank
    return best


def _rank(row: dict[str, Any]) -> int:
    if row.get("origin") == "candidate" or row.get("kind") == "candidate":
        return 2
    return 1


def _coerce_id(value: Any) -> Any:
    """JS-side compares with `Number(...)`; mirror that for tests so a row
    with marker_id="3" collides with marker_id=3."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
