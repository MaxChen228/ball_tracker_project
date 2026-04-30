"""CRUD endpoints for the disk-backed preset library.

A preset = `{name, label, hsv, shape_gate}` JSON file under
`<data_dir>/presets/<name>.json`. The slug `name` is the URL key and
filename — restricted to `[a-z0-9_]{1,32}` for portability. `label` is
operator-facing and can be anything; the dashboard / viewer escape it
on render. Built-in tennis / blue_ball seeds are written on first boot
by `presets.seed_builtins`; an operator deleting a built-in and
restarting recreates it.

Identity validation on `POST /detection/config` (the live-config setter
in `routes/settings.py`) compares the submitted pair against the
on-disk preset, so a freshly-saved preset becomes claimable as
`preset=<name>` immediately without a server restart.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from detection import HSVRange, ShapeGate
from presets import Preset, validate_slug

router = APIRouter()


def _preset_to_wire(p: Preset) -> dict[str, Any]:
    return {
        "name": p.name,
        "label": p.label,
        "hsv": {
            "h_min": p.hsv.h_min, "h_max": p.hsv.h_max,
            "s_min": p.hsv.s_min, "s_max": p.hsv.s_max,
            "v_min": p.hsv.v_min, "v_max": p.hsv.v_max,
        },
        "shape_gate": {
            "aspect_min": p.shape_gate.aspect_min,
            "fill_min": p.shape_gate.fill_min,
        },
    }


def _validated_hsv(values: dict[str, object]) -> HSVRange:
    def _int_field(key: str, upper: int) -> int:
        raw = values.get(key)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid 'hsv.{key}'")
        if not (0 <= v <= upper):
            raise HTTPException(
                status_code=400,
                detail=f"'hsv.{key}' out of range [0, {upper}]",
            )
        return v

    h_min = _int_field("h_min", 179)
    h_max = _int_field("h_max", 179)
    s_min = _int_field("s_min", 255)
    s_max = _int_field("s_max", 255)
    v_min = _int_field("v_min", 255)
    v_max = _int_field("v_max", 255)
    if h_min > h_max:
        raise HTTPException(status_code=400, detail="'hsv.h_min' must be <= 'hsv.h_max'")
    if s_min > s_max:
        raise HTTPException(status_code=400, detail="'hsv.s_min' must be <= 'hsv.s_max'")
    if v_min > v_max:
        raise HTTPException(status_code=400, detail="'hsv.v_min' must be <= 'hsv.v_max'")
    return HSVRange(h_min=h_min, h_max=h_max, s_min=s_min, s_max=s_max, v_min=v_min, v_max=v_max)


def _validated_shape_gate(values: dict[str, object]) -> ShapeGate:
    def _float_field(key: str, lo: float, hi: float) -> float:
        raw = values.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid 'shape_gate.{key}'")
        if not (lo <= v <= hi):
            raise HTTPException(
                status_code=400,
                detail=f"'shape_gate.{key}' out of range [{lo}, {hi}]",
            )
        return v

    return ShapeGate(
        aspect_min=_float_field("aspect_min", 0.0, 1.0),
        fill_min=_float_field("fill_min", 0.0, 1.0),
    )


def _read_preset_body(body: object, *, name_from_url: str | None) -> Preset:
    """Strict body parse — every field required, no per-field defaults
    (per CLAUDE.md no-silent-fallback). When `name_from_url` is set, a
    body-level `name` is rejected as ambiguous (URL is canonical for
    PUT)."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    if name_from_url is not None:
        if "name" in body and body["name"] != name_from_url:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"body 'name' ({body['name']!r}) disagrees with URL "
                    f"({name_from_url!r}); URL is canonical for PUT"
                ),
            )
        name = name_from_url
    else:
        raw_name = body.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            raise HTTPException(status_code=400, detail="missing required field 'name'")
        name = raw_name

    try:
        validate_slug(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="missing or invalid 'label'")

    hsv_raw = body.get("hsv")
    if not isinstance(hsv_raw, dict):
        raise HTTPException(status_code=400, detail="missing or invalid 'hsv'")
    sg_raw = body.get("shape_gate")
    if not isinstance(sg_raw, dict):
        raise HTTPException(status_code=400, detail="missing or invalid 'shape_gate'")

    return Preset(
        name=name,
        label=label,
        hsv=_validated_hsv(hsv_raw),
        shape_gate=_validated_shape_gate(sg_raw),
    )


@router.get("/presets")
def presets_list() -> dict[str, Any]:
    """All presets sorted by slug. Built-in seeds and operator-created
    entries are returned identically — the dashboard does not currently
    distinguish them at the API layer (a future `builtin: true` flag
    can be added if a "lock" affordance becomes desired)."""
    from main import state
    return {"presets": [_preset_to_wire(p) for p in state.list_presets()]}


@router.get("/presets/{name}")
def presets_get(name: str) -> dict[str, Any]:
    from main import state
    try:
        preset = state.load_preset(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {name!r}")
    return _preset_to_wire(preset)


@router.post("/presets")
async def presets_create(request: Request) -> dict[str, Any]:
    """Create a new preset. 409 if a preset with that slug already
    exists (use PUT to overwrite). Body fields all required."""
    from main import state

    body = await request.json()
    preset = _read_preset_body(body, name_from_url=None)
    if state.preset_exists(preset.name):
        raise HTTPException(
            status_code=409,
            detail=f"preset already exists: {preset.name!r} (use PUT to overwrite)",
        )
    state.save_preset(preset)
    return _preset_to_wire(preset)


@router.put("/presets/{name}")
async def presets_replace(name: str, request: Request) -> dict[str, Any]:
    """Overwrite an existing preset. Slug comes from URL; `name` in body
    is optional but if present must match. PUT must target an existing
    preset (404 if missing) — creation goes through POST so the
    operator can never accidentally upsert under the wrong slug."""
    from main import state

    if not state.preset_exists(name):
        raise HTTPException(status_code=404, detail=f"unknown preset: {name!r}")
    body = await request.json()
    preset = _read_preset_body(body, name_from_url=name)
    state.save_preset(preset)
    return _preset_to_wire(preset)


@router.delete("/presets/{name}")
def presets_delete(name: str) -> dict[str, Any]:
    """Unlink a preset file. Returns 404 if the preset doesn't exist.
    Built-in seeds (tennis, blue_ball) are deletable — restart will
    re-seed any built-in whose file is missing.

    A preset that is currently bound as the live `detection_config.json`
    `preset` field becomes a dangling reference; the dashboard surfaces
    that state visually and the next `set_detection_config` clears it.
    No server-side cascade — keeping the in-memory `preset` string
    untouched preserves operator intent ("I last claimed blue_ball")
    until they explicitly Apply a new config.
    """
    from main import state
    try:
        state.delete_preset(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {name!r}")
    return {"ok": True, "deleted": name}
