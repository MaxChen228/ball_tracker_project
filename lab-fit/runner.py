"""Run the frozen segmenter on a result file's triangulated points.

Result JSON stores per-algorithm point clouds in
`triangulated_by_algorithm` (keys: `ios_capture_time` = LIVE, plus the
active server-post algorithm id). Convert to the input shape segmenter
expects, run, return Segments + pts.

Important: server-side `session_results.py` filters triangulated points
by `gap_threshold_m` BEFORE passing to find_segments. We replicate that
behavior here so lab analyses match production.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from algo.segmenter import find_segments, Segment


@dataclass
class TriPoint:
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


def _to_tri_points(raw: list[dict]) -> list[TriPoint]:
    return [
        TriPoint(p["t_rel_s"], p["x_m"], p["y_m"], p["z_m"], p["residual_m"])
        for p in raw
    ]


def run_segmenter(
    triangulated: list[dict],
    *,
    gap_threshold_m: float,
    apply_residual_gate: bool = True,
    **segmenter_kwargs,
) -> tuple[list[Segment], np.ndarray]:
    """Run frozen segmenter on a list of triangulated dicts.

    `apply_residual_gate=True` mirrors what the server does: drop any
    point with residual_m > gap_threshold_m before segmentation. Set to
    False to study what the segmenter would do with full data.
    """
    pts = _to_tri_points(triangulated)
    if apply_residual_gate:
        pts = [p for p in pts if p.residual_m <= gap_threshold_m]
    return find_segments(pts, **segmenter_kwargs)


def run_for_result(
    result: dict,
    *,
    path: str = "server_post",
    apply_residual_gate: bool = True,
    **segmenter_kwargs,
) -> tuple[list[Segment], np.ndarray]:
    """Run on a specific path ('live' or 'server_post') of a result dict.

    Resolves the logical path to its algorithm id via
    `algorithm_id_for_path`, then reads `triangulated_by_algorithm`.
    Returns empty if the path has no points for this session.
    """
    from data_loader import algorithm_id_for_path

    alg = algorithm_id_for_path(result, path)
    if alg is None:
        return [], np.empty((0, 5))
    raw = result.get("triangulated_by_algorithm", {}).get(alg, []) or []
    return run_segmenter(
        raw,
        gap_threshold_m=result["gap_threshold_m"],
        apply_residual_gate=apply_residual_gate,
        **segmenter_kwargs,
    )
