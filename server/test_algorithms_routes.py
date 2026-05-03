"""GET /algorithms — read-only schema export for the dashboard form
generator. Pin the wire shape and the field-flattening behavior of
`algorithms._form_schema.export_fields` so a future params_schema
edit can't silently drop dashboard widgets.

The dashboard reads `fields` to decide what UI to render; missing /
mistyped entries here would manifest as silently un-editable params,
which is precisely the failure mode this whole platform-widening
effort exists to prevent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import main
    monkeypatch.setattr(main, "state", main.State(data_dir=tmp_path))
    return main


# ----- list endpoint -------------------------------------------------


def test_list_algorithms_returns_every_registered_detector(
    tmp_path, monkeypatch,
):
    """Every entry in `algorithms._REGISTRY` must surface in
    `GET /algorithms`. Sourced from the registry so a new shipped
    detector adds itself without a test edit."""
    import algorithms as _algos
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/algorithms")
    assert r.status_code == 200, r.text
    ids = sorted(a["algorithm_id"] for a in r.json()["algorithms"])
    expected = sorted(e.algorithm_id for e in _algos.list_all())
    assert ids == expected


def test_list_algorithms_excludes_non_runnable_data_sources(
    tmp_path, monkeypatch,
):
    """`ios_capture_time` is a wire-id-only data source with no
    Detector. The dashboard form generator has nothing to render for
    it, so it MUST NOT appear in this endpoint."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/algorithms")
    ids = [a["algorithm_id"] for a in r.json()["algorithms"]]
    assert "ios_capture_time" not in ids


def test_list_algorithms_entry_carries_label_description_threshold(
    tmp_path, monkeypatch,
):
    """Wire-shape pin per entry — these are what the dashboard renders
    in the algorithm picker (label) + tooltip (description). cost_threshold
    travels too because the viewer's per-point gate display reads it."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    entries = client.get("/algorithms").json()["algorithms"]
    v11 = next(e for e in entries if e["algorithm_id"] == "v11_hsv_cc")
    assert isinstance(v11["label"], str) and v11["label"]
    assert isinstance(v11["description"], str) and v11["description"]
    assert v11["cost_threshold"] == 0.5
    assert isinstance(v11["fields"], list) and v11["fields"]


# ----- single endpoint -----------------------------------------------


def test_get_single_algorithm_matches_list_entry(tmp_path, monkeypatch):
    """`GET /algorithms/{id}` returns the same shape as one entry of
    the list endpoint — single source of truth for the dashboard."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    list_v11 = next(
        a for a in client.get("/algorithms").json()["algorithms"]
        if a["algorithm_id"] == "v11_hsv_cc"
    )
    single = client.get("/algorithms/v11_hsv_cc").json()
    assert single == list_v11


def test_get_unknown_algorithm_returns_404(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/algorithms/v999_not_real")
    assert r.status_code == 404
    assert "v999_not_real" in r.json()["detail"]


def test_get_ios_capture_time_returns_404(tmp_path, monkeypatch):
    """Non-runnable data source has no Detector → no schema → 404
    (consistent with `algorithms.get` which raises KeyError)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    r = client.get("/algorithms/ios_capture_time")
    assert r.status_code == 404


# ----- field flattening (v11_hsv_cc) ---------------------------------


def test_v11_fields_flattens_nested_hsv_and_shape_gate(
    tmp_path, monkeypatch,
):
    """V11Params has `hsv: HSVRangePayload` + `shape_gate: ShapeGatePayload`,
    each a nested Pydantic model. The exporter must dot-flatten them
    into 8 leaf fields (6 HSV axes + 2 shape thresholds)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    v11 = client.get("/algorithms/v11_hsv_cc").json()
    paths = sorted(f["path"] for f in v11["fields"])
    assert paths == sorted([
        "hsv.h_min", "hsv.h_max",
        "hsv.s_min", "hsv.s_max",
        "hsv.v_min", "hsv.v_max",
        "shape_gate.aspect_min",
        "shape_gate.fill_min",
    ])


def test_v11_fields_typed_int_and_float(tmp_path, monkeypatch):
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    fields = {f["path"]: f for f in client.get("/algorithms/v11_hsv_cc").json()["fields"]}
    for hsv_path in ("hsv.h_min", "hsv.h_max", "hsv.s_min", "hsv.s_max", "hsv.v_min", "hsv.v_max"):
        assert fields[hsv_path]["type"] == "int", hsv_path
    assert fields["shape_gate.aspect_min"]["type"] == "float"
    assert fields["shape_gate.fill_min"]["type"] == "float"


# ----- field flattening (hybrid_28d) ---------------------------------


def test_hybrid_28d_fields_includes_all_leaves(tmp_path, monkeypatch):
    """Hybrid28dParams has 9 top-level fields, two of which are
    HSVRangePayload and two ShapeGatePayload — flatten to 22 leaves
    (6+6+2+2 from nested models, +6 scalar params)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    fields = client.get("/algorithms/hybrid_28d").json()["fields"]
    paths = sorted(f["path"] for f in fields)
    assert paths == sorted([
        # PROD pool — tight HSV + shape + ball-sized area floor
        "prod_hsv.h_min", "prod_hsv.h_max",
        "prod_hsv.s_min", "prod_hsv.s_max",
        "prod_hsv.v_min", "prod_hsv.v_max",
        "prod_shape.aspect_min", "prod_shape.fill_min",
        "prod_area_min",
        # V11 pool — loose HSV + morph CLOSE + small-blob rescue floor
        "v11_hsv.h_min", "v11_hsv.h_max",
        "v11_hsv.s_min", "v11_hsv.s_max",
        "v11_hsv.v_min", "v11_hsv.v_max",
        "v11_shape.aspect_min", "v11_shape.fill_min",
        "v11_area_min",
        "v11_close_kernel",
        # Temporal-persistence rerank knobs
        "neigh_half",
        "match_px",
    ])


def test_hybrid_28d_field_bounds_and_defaults_match_pydantic(
    tmp_path, monkeypatch,
):
    """Field(ge=, le=, default=) on Hybrid28dParams must round-trip
    through the schema export. Drift here = dashboard slider with
    wrong range / wrong starting value."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    fields = {f["path"]: f for f in client.get("/algorithms/hybrid_28d").json()["fields"]}
    # Spot-check the explicitly-bounded scalar params.
    assert fields["prod_area_min"]["minimum"] == 1
    assert fields["prod_area_min"]["maximum"] == 10_000
    assert fields["prod_area_min"]["default"] == 20
    assert fields["v11_area_min"]["default"] == 3
    assert fields["v11_close_kernel"]["minimum"] == 1
    assert fields["v11_close_kernel"]["maximum"] == 9
    assert fields["v11_close_kernel"]["default"] == 3
    assert fields["neigh_half"]["minimum"] == 1
    assert fields["neigh_half"]["maximum"] == 30
    assert fields["neigh_half"]["default"] == 6
    assert fields["match_px"]["minimum"] == 0.5
    assert fields["match_px"]["maximum"] == 50.0
    assert fields["match_px"]["default"] == 5.0
    assert fields["match_px"]["type"] == "float"


def test_v11_fields_have_no_bounds_when_pydantic_didnt_set_any(
    tmp_path, monkeypatch,
):
    """V11Params doesn't put `Field(ge=, le=)` on its HSV/shape leaves
    (they're plain `int` / `float` inside the nested payload models),
    so `minimum` / `maximum` must come back as `None` — the dashboard
    falls back to free number input rather than rendering a bogus
    [0, ?] slider."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    fields = {f["path"]: f for f in client.get("/algorithms/v11_hsv_cc").json()["fields"]}
    assert fields["hsv.h_min"]["minimum"] is None
    assert fields["hsv.h_min"]["maximum"] is None
    assert fields["shape_gate.aspect_min"]["minimum"] is None


# ----- exporter unit tests -------------------------------------------


def test_export_fields_unsupported_leaf_type_raises():
    """Strict exporter — an unsupported leaf type (e.g. bool, str,
    list) must raise so the failure surfaces at boot / first /algorithms
    fetch, not as a silently-dropped UI widget."""
    from pydantic import BaseModel
    from algorithms._form_schema import export_fields

    class HasBool(BaseModel):
        flag: bool = True

    with pytest.raises(ValueError, match="unsupported field type"):
        export_fields(HasBool)
