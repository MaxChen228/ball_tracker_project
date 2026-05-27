"""Pydantic params_schema → flat form-field list for dashboard rendering.

Walks a detector's `params_schema.model_json_schema()` and flattens
nested models (e.g. `HSVRangePayload`) into `FormField` records keyed
by dotted path (e.g. `prod_hsv.h_min`). The dashboard JS reads this
list and renders one slider+number widget per field — no algorithm-
specific UI code, no JSON-Schema spec parsing on the client.

Scope is deliberately narrow: only `int` and `float` leaves are
supported, because that's all every shipped detector uses today
(HSV bounds, shape thresholds, area floors, kernel sizes, temporal
hyperparams). When a detector grows a `bool` toggle or a
`Literal["a", "b"]` choice we'll extend `FormField.type` and the JS
dispatch table together.

The exported shape is intentionally not Pydantic's raw JSON Schema:
- nested `$ref` resolved server-side; client doesn't need a $ref walker
- only the fields we actually render (path, type, bounds, default,
  title) — no schema cruft (`$schema`, `definitions`, `additionalProperties`)
- dotted-path keys mirror how the dashboard POSTs param updates
  (`POST /presets` body and `state.update_param` both speak dotted
  path), so the form generator's output is what gets sent back
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


_INT_TYPE = "integer"
_NUMBER_TYPE = "number"


@dataclass(frozen=True)
class FormField:
    """One leaf field in a flattened params schema.

    `path` is the dotted access path from the params root — e.g.
    `hsv.h_min` for `V11Params.hsv.h_min`. `type` is
    the simplified UI type (`int` or `float`); `minimum` / `maximum`
    come from `Field(ge=, le=)` on the source model and are `None`
    when the field has no bound (renders as a free number input).
    `default` is the schema's default value if any; `title` is the
    human-friendly label Pydantic auto-derives from the field name."""
    path: str
    type: str
    minimum: int | float | None
    maximum: int | float | None
    default: Any
    title: str | None


def export_fields(model: type[BaseModel]) -> list[FormField]:
    """Flatten `model`'s JSON Schema into a list of `FormField`s.

    Nested Pydantic models (referenced via `$ref` in the schema) are
    walked recursively; their fields appear with dotted paths under the
    parent field name. Order follows the source model's field
    declaration order (Pydantic preserves it).

    Raises `ValueError` for an unsupported leaf type — keeps the
    exporter strict so a future bool / enum field doesn't silently
    drop out of the dashboard."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    return _walk(schema, prefix="", defs=defs)


def _walk(
    schema: dict[str, Any],
    *,
    prefix: str,
    defs: dict[str, Any],
) -> list[FormField]:
    props = schema.get("properties", {})
    if not props:
        # An object schema with zero properties produces zero leaves —
        # exactly the silent-drop the strict exporter exists to prevent.
        # Either the source model is empty (no params to render → caller
        # shouldn't be exporting it) or the schema uses
        # `additionalProperties` / `patternProperties` which the
        # dashboard can't render. Fail loud at the path of the empty
        # container so the source model is identifiable.
        raise ValueError(
            f"empty properties at {prefix or '<root>'!r} — exporter "
            "needs at least one declared field per object schema"
        )
    out: list[FormField] = []
    for name, sub in props.items():
        path = f"{prefix}.{name}" if prefix else name
        resolved = _resolve_ref(sub, defs, path=path)
        if "properties" in resolved:
            out.extend(_walk(resolved, prefix=path, defs=defs))
            continue
        out.append(_leaf_to_field(path, resolved))
    return out


def _resolve_ref(
    sub: dict[str, Any],
    defs: dict[str, Any],
    *,
    path: str,
) -> dict[str, Any]:
    """Inline a `$ref` to its `$defs` target. Pydantic v2 emits nested
    BaseModel as `{"$ref": "#/$defs/HSVRangePayload"}`; we follow it
    once. Multi-hop refs aren't used by any detector today.

    Strict on schema features the exporter doesn't implement:
    - `allOf` / `anyOf` / `oneOf` containers — Pydantic v2 wraps these
      around `Optional[T]` (anyOf: [T, null]) and `Field(default=...,
      description=...)` on nested models (allOf: [{$ref}]). Silently
      treating these as leaves would crash later in `_leaf_to_field`
      with a confusing "unsupported field type: None" once a future
      detector grows an Optional / decorated nested field.
    - Non-`#/$defs/` ref roots (e.g. an http URL) — the lookup would
      coincidentally match by tail name and return the wrong def.
    """
    for unsupported in ("allOf", "anyOf", "oneOf"):
        if unsupported in sub:
            raise ValueError(
                f"{unsupported!r} at {path!r} not supported — exporter "
                "doesn't render Optional / Union / decorated-nested-model "
                "schemas; restructure the params model or extend the "
                "exporter alongside the JS form generator"
            )
    if "$ref" not in sub:
        return sub
    ref = sub["$ref"]
    if not ref.startswith("#/$defs/"):
        raise ValueError(
            f"unsupported $ref root at {path!r}: {ref!r} (only "
            "'#/$defs/<name>' supported)"
        )
    target_name = ref[len("#/$defs/"):]
    if target_name not in defs:
        raise ValueError(f"unresolved $ref {ref!r} (have: {sorted(defs)})")
    return defs[target_name]


def _leaf_to_field(path: str, sub: dict[str, Any]) -> FormField:
    json_type = sub.get("type")
    if json_type == _INT_TYPE:
        ui_type = "int"
    elif json_type == _NUMBER_TYPE:
        ui_type = "float"
    else:
        raise ValueError(
            f"unsupported field type at {path!r}: {json_type!r} "
            "(only int/float leaves render today; extend FormField.type "
            "+ JS dispatch table together when adding bool/enum)"
        )
    return FormField(
        path=path,
        type=ui_type,
        minimum=sub.get("minimum"),
        maximum=sub.get("maximum"),
        default=sub.get("default"),
        title=sub.get("title"),
    )


def field_to_wire(f: FormField) -> dict[str, Any]:
    """Serialise a `FormField` to the wire shape returned by
    `GET /algorithms`. Always emits all five UI keys (`type`, `minimum`,
    `maximum`, `default`, `title`) — `None` carries the meaning "no
    constraint / no default" and the JS form generator dispatches on
    presence-of-key, not value. Keeping the shape rectangular avoids
    a `if "minimum" in f` branch in every JS render path."""
    return {
        "path": f.path,
        "type": f.type,
        "minimum": f.minimum,
        "maximum": f.maximum,
        "default": f.default,
        "title": f.title,
    }
