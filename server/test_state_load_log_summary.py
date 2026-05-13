"""Loader log compression: `_load_from_disk` formerly logged the
full `ValidationError.__str__` per corrupt file, which dumps four
lines per offending field. With 117 legacy pitches × ~6 None fields
each, the startup INFO lines and the final "N file(s) failed" summary
get scrolled off-screen.

`_summarise_load_error` collapses pydantic detail to a single line —
operator-readable, single grep-able row per file. These tests pin the
single-line invariant so future schema strictening doesn't silently
reintroduce the multi-line dump.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from state import _summarise_load_error


class _ToyModel(BaseModel):
    a: float
    b: float
    c: float


def _capture(model_cls, payload):
    try:
        model_cls.model_validate(payload)
    except ValidationError as e:
        return e
    raise AssertionError("expected ValidationError")


def test_summarise_single_line_for_validation_error():
    err = _capture(_ToyModel, {"a": None, "b": None, "c": None})
    summary = _summarise_load_error(err)
    assert "\n" not in summary, summary
    assert "validation error" in summary
    assert "first fields" in summary


def test_summarise_includes_error_count():
    err = _capture(_ToyModel, {"a": None, "b": None, "c": None})
    summary = _summarise_load_error(err)
    assert summary.startswith("3 validation error(s)")


def test_summarise_lists_first_three_field_paths():
    err = _capture(_ToyModel, {"a": None, "b": None, "c": None})
    summary = _summarise_load_error(err)
    assert "a" in summary
    assert "b" in summary
    assert "c" in summary


def test_summarise_truncates_when_more_than_three_errors():
    class _Wide(BaseModel):
        a: float
        b: float
        c: float
        d: float
        e: float

    err = _capture(_Wide, {"a": None, "b": None, "c": None, "d": None, "e": None})
    summary = _summarise_load_error(err)
    assert summary.startswith("5 validation error(s)")
    assert "+2 more" in summary


def test_summarise_nested_field_path():
    class _Inner(BaseModel):
        x: float

    class _Outer(BaseModel):
        inner: _Inner

    err = _capture(_Outer, {"inner": {"x": None}})
    summary = _summarise_load_error(err)
    assert "\n" not in summary
    assert "inner.x" in summary


def test_summarise_json_decode_error_first_line_only():
    try:
        json.loads("{ this is not json\nmultiline error\n}")
    except json.JSONDecodeError as e:
        summary = _summarise_load_error(e)
    assert "\n" not in summary
    assert summary  # non-empty


def test_summarise_oserror_falls_back_to_type_name_when_message_empty():
    err = OSError("")
    summary = _summarise_load_error(err)
    assert summary == "OSError"
