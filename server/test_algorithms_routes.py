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


# ----- helpers --------------------------------------------------------


def _list_entry(client: TestClient, algorithm_id: str) -> dict:
    """Fetch one entry from the list endpoint by id (single endpoint
    `/algorithms/{id}` retired; list is the only schema surface)."""
    for a in client.get("/algorithms").json()["algorithms"]:
        if a["algorithm_id"] == algorithm_id:
            return a
    raise AssertionError(f"{algorithm_id!r} missing from /algorithms list")


# ----- field flattening (v11_hsv_cc) ---------------------------------


def test_v11_fields_flattens_nested_hsv_and_shape_gate(
    tmp_path, monkeypatch,
):
    """V11Params has `hsv: HSVRangePayload` + `shape_gate: ShapeGatePayload`,
    each a nested Pydantic model. The exporter must dot-flatten them
    into 8 leaf fields (6 HSV axes + 2 shape thresholds)."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    v11 = _list_entry(client, "v11_hsv_cc")
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
    fields = {f["path"]: f for f in _list_entry(client, "v11_hsv_cc")["fields"]}
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
    fields = _list_entry(client, "hybrid_28d")["fields"]
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
    fields = {f["path"]: f for f in _list_entry(client, "hybrid_28d")["fields"]}
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
    assert fields["match_px"]["minimum"] == 2.0
    assert fields["match_px"]["maximum"] == 50.0
    assert fields["match_px"]["default"] == 5.0
    assert fields["match_px"]["type"] == "float"


def test_v11_hsv_shape_fields_expose_pydantic_bounds(tmp_path, monkeypatch):
    """`HSVRangePayload` / `ShapeGatePayload` now carry `Field(ge=, le=)`
    bounds (OpenCV 8-bit HSV space + [0,1] gate ranges). The exporter
    must surface those bounds so the dashboard slider lands on the
    correct range instead of free-form number input — and the
    preset POST / `runs/{algorithm_id} {params}` path can no longer
    sneak out-of-range values past the schema validator."""
    main = _fresh_main(tmp_path, monkeypatch)
    client = TestClient(main.app)
    fields = {f["path"]: f for f in _list_entry(client, "v11_hsv_cc")["fields"]}
    assert fields["hsv.h_min"]["minimum"] == 0
    assert fields["hsv.h_min"]["maximum"] == 179
    assert fields["hsv.h_max"]["maximum"] == 179
    assert fields["hsv.s_min"]["maximum"] == 255
    assert fields["hsv.v_max"]["maximum"] == 255
    assert fields["shape_gate.aspect_min"]["minimum"] == 0.0
    assert fields["shape_gate.aspect_min"]["maximum"] == 1.0
    assert fields["shape_gate.fill_min"]["maximum"] == 1.0


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


def test_export_fields_optional_field_raises():
    """`Optional[int]` emits `anyOf: [int, null]` in Pydantic v2's
    JSON Schema — without explicit handling the exporter would silently
    treat it as a leaf with `type=None`. Reject loud so a future
    detector with optional params doesn't ship a broken UI."""
    from pydantic import BaseModel
    from algorithms._form_schema import export_fields

    class HasOptional(BaseModel):
        maybe: int | None = None

    with pytest.raises(ValueError, match="anyOf"):
        export_fields(HasOptional)


def test_export_fields_literal_field_raises():
    """`Literal["a", "b"]` is a finite-choice param — needs a select
    widget, not a slider. Until we extend `FormField.type` + the JS
    dispatch table together, reject loud."""
    from typing import Literal
    from pydantic import BaseModel
    from algorithms._form_schema import export_fields

    class HasLiteral(BaseModel):
        choice: Literal["a", "b"] = "a"

    # Pydantic emits Literal as `enum` on a string leaf — falls through
    # to the unsupported-type branch since the type is "string".
    with pytest.raises(ValueError, match="unsupported field type"):
        export_fields(HasLiteral)


def test_export_fields_list_field_raises():
    """`list[int]` is a variable-length param — no slider analogue.
    Reject loud rather than silently emitting a broken field record."""
    from pydantic import BaseModel
    from algorithms._form_schema import export_fields

    class HasList(BaseModel):
        items: list[int] = []

    with pytest.raises(ValueError, match="unsupported field type"):
        export_fields(HasList)


def test_export_fields_empty_model_raises():
    """A `BaseModel` with zero declared fields would silently produce
    zero `FormField`s — exactly the silent-drop the exporter exists to
    prevent. The failure should name the empty container so the source
    model is identifiable."""
    from pydantic import BaseModel
    from algorithms._form_schema import export_fields

    class Empty(BaseModel):
        pass

    with pytest.raises(ValueError, match="empty properties"):
        export_fields(Empty)


def test_walk_rejects_allof_container():
    """`allOf` wraps schemas under several Pydantic `Field(...)` +
    nested-model combinations across versions. The exporter doesn't
    unwrap them today; reject loud at the path so a future Pydantic
    upgrade that starts emitting allOf surfaces immediately, not as
    a silently-dropped form widget. Tested by feeding a hand-crafted
    schema directly into `_walk` so the test is robust against
    Pydantic's exact emit choices."""
    from algorithms._form_schema import _walk

    schema = {
        "type": "object",
        "properties": {
            "x": {"allOf": [{"$ref": "#/$defs/Inner"}]},
        },
    }
    defs = {"Inner": {"properties": {"h": {"type": "integer"}}}}
    with pytest.raises(ValueError, match="allOf"):
        _walk(schema, prefix="", defs=defs)


def test_walk_rejects_oneof_container():
    """Same defensive reject for `oneOf` (Pydantic uses it for tagged
    unions). The exporter doesn't render unions today."""
    from algorithms._form_schema import _walk

    schema = {
        "type": "object",
        "properties": {
            "x": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        },
    }
    with pytest.raises(ValueError, match="oneOf"):
        _walk(schema, prefix="", defs={})


def test_resolve_ref_rejects_non_defs_root():
    """`$ref` outside `#/$defs/` (e.g. a remote URL) would
    coincidentally tail-match a `$defs` entry by name. Reject loud."""
    from algorithms._form_schema import _walk

    schema = {
        "type": "object",
        "properties": {
            "x": {"$ref": "http://example.com/schema#/Inner"},
        },
    }
    defs = {"Inner": {"properties": {"h": {"type": "integer"}}}}
    with pytest.raises(ValueError, match="unsupported \\$ref root"):
        _walk(schema, prefix="", defs=defs)
