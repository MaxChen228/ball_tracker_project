"""Read-only introspection endpoints for the algorithm registry.

Surfaces what the dashboard needs to render an algorithm-agnostic
params editor: which detectors exist, their human label, and a
flattened form-field list derived from each detector's
`params_schema`. The dashboard JS fetches `/algorithms` once at boot
and dispatches form rendering off the returned shape — no per-
algorithm hand-coded UI.

Non-runnable data sources (`ios_capture_time`) are excluded — they
have no `Detector` and no editable params. The dashboard only needs
the runnable set.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

import algorithms
from algorithms._form_schema import export_fields, field_to_wire

router = APIRouter()


def _entry_to_wire(entry: algorithms.AlgorithmEntry) -> dict[str, Any]:
    fields = export_fields(entry.detector.params_schema)
    return {
        "algorithm_id": entry.algorithm_id,
        "label": entry.label,
        "description": entry.description,
        "cost_threshold": entry.cost_threshold,
        "fields": [field_to_wire(f) for f in fields],
    }


@router.get("/algorithms")
def list_algorithms() -> dict[str, Any]:
    """Return every runnable detector sorted by id (matches
    `algorithms.list_all`). Shape: `{algorithms: [{algorithm_id, label,
    description, cost_threshold, fields: [{path, type, minimum,
    maximum, default, title}, ...]}, ...]}`. The dashboard binds form
    widgets to `fields` by `path` (dotted, e.g. `prod_hsv.h_min`) —
    same path the preset POST body + `state.update_param` accept."""
    return {"algorithms": [_entry_to_wire(e) for e in algorithms.list_all()]}
