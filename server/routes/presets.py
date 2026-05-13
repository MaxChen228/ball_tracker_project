"""CRUD endpoints for the disk-backed preset library.

A preset = `{name, label, algorithm_id, params}` JSON file under
`<data_dir>/presets/<name>.json`. The slug `name` is the URL key and
filename — restricted to `[a-z0-9_]{1,32}` for portability. `label` is
operator-facing and can be anything; the dashboard / viewer escape it
on render. `algorithm_id` is a runnable detector id from `algorithms`
(non-runnable data sources like `ios_capture_time` are rejected at
write time). `params` is the algorithm's `Detector.params_schema`
shape — round-trip-validated on POST so a malformed body fails fast
with the schema's own error.

Built-in tennis / blue_ball / hybrid_28d_blue_ball seeds are written
on first boot by `presets.seed_builtins`; deleting a built-in and
restarting recreates it.

Active-config side effect (current state, pre-phase-3):
- v11_hsv_cc preset POST / activate → updates live `DetectionConfig`
  and broadcasts WS settings to online cameras.
- non-v11 preset POST → save to disk only; live config unchanged.
- non-v11 activate → 422 (dual active live/server_post is phase 3).

Identity validation on `POST /detection/config` (the live-config setter
in `routes/settings.py`) compares the submitted pair against the
on-disk preset, so a freshly-saved v11 preset becomes claimable as
`preset=<name>` immediately without a server restart.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

import algorithms
from detection_config import DetectionConfig
from presets import Preset, validate_slug

router = APIRouter()


def _preset_to_wire(p: Preset) -> dict[str, Any]:
    """Wire shape: canonical `{algorithm_id, name, label, params}`.
    `params` is opaque per-algorithm — frontend reads
    `params.hsv.h_min` etc when `algorithm_id == "v11_hsv_cc"`. No
    legacy v11 flat-key surface (CLAUDE.md no-backcompat: clients are
    in-tree and updated in lockstep)."""
    return {
        "algorithm_id": p.algorithm_id,
        "name": p.name,
        "label": p.label,
        "params": p.params,
    }


def _read_preset_body(body: object, *, name_from_url: str | None) -> Preset:
    """Strict body parse for the canonical preset shape `{name, label,
    algorithm_id, params}`. Every field is required (CLAUDE.md
    no-silent-fallback); `params` is round-trip-validated through the
    detector's `Detector.params_schema`, so a missing key inside
    `params` surfaces as the schema's own ValidationError rather than
    a silent default. The normalised dict (`model_dump()`) is what
    persists, so disk preset files always carry exactly the fields
    the schema declares — extras dropped, defaults filled.

    `algorithm_id` MUST be runnable: non-runnable data sources like
    `ios_capture_time` are valid wire ids but have no `Detector` so
    there's no schema to validate `params` against and no detector to
    re-run with. Caught here at the system boundary so a typo doesn't
    persist a dangling preset.

    When `name_from_url` is set, a body-level `name` is rejected as
    ambiguous (URL is canonical for PUT)."""
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

    algorithm_id = body.get("algorithm_id")
    if not isinstance(algorithm_id, str) or not algorithm_id:
        raise HTTPException(
            status_code=400,
            detail="missing required field 'algorithm_id'",
        )
    # Split structural (400) vs semantic (422) algorithm_id errors so
    # the dashboard can distinguish "client typo / wrong case" from
    # "id is well-formed but the server doesn't know it / it's a
    # non-runnable data source". Without this split a malformed slug
    # `"V11_HSV_CC"` and an unknown `"v999"` both fall through to 422.
    if not algorithms.is_valid_id_format(algorithm_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid 'algorithm_id' format: {algorithm_id!r} — "
                "must match [a-z0-9_]{1,32}"
            ),
        )
    try:
        algorithms.validate_runnable_id(algorithm_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    params = body.get("params")
    if not isinstance(params, dict):
        raise HTTPException(
            status_code=400,
            detail="missing or invalid 'params' (must be a JSON object)",
        )

    schema = algorithms.get(algorithm_id).detector.params_schema
    try:
        typed = schema.model_validate(params)
    except ValidationError as e:
        # Surface Pydantic's structured errors verbatim — the dashboard
        # form generator can highlight the offending dotted path. 422
        # so the operator distinguishes "schema mismatch" (fixable in
        # the form) from 400 "missing top-level field".
        raise HTTPException(status_code=422, detail=e.errors())

    return Preset(
        name=name,
        label=label,
        algorithm_id=algorithm_id,
        # Round-tripped dict — extras dropped (schema has extra='forbid'
        # for hybrid_28d, allow-by-default for v11), defaults filled.
        # Storing model_dump() instead of the raw body ensures disk
        # files always conform to the schema's serialised shape.
        params=typed.model_dump(),
    )


def _is_v11(algorithm_id: str) -> bool:
    return algorithm_id == algorithms.V11_HSV_CC


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
    exists — preset files are immutable by name (the dashboard's
    Apply button = "Save as new"); rename if you want to update.

    Side effect varies by `algorithm_id`:
    - v11_hsv_cc → atomically activates the new preset (updates live
      `DetectionConfig` + broadcasts WS settings to every online
      camera). The dashboard's Apply path expects this so iOS sees
      the new HSV/shape_gate immediately.
    - any other algorithm_id → save to disk only; live config and WS
      broadcast are left alone. Activating a non-v11 preset for the
      server_post path is a separate concern handled by phase-3 dual
      active state — this route just persists the preset file.
    """
    from main import state, device_ws, _settings_message_for

    body = await request.json()
    preset = _read_preset_body(body, name_from_url=None)
    if state.preset_exists(preset.name):
        raise HTTPException(
            status_code=409,
            detail=(
                f"preset already exists: {preset.name!r} — preset filenames "
                f"are immutable; choose a different name or delete the "
                f"existing one first"
            ),
        )
    state.save_preset(preset)
    if _is_v11(preset.algorithm_id):
        cfg = DetectionConfig(
            hsv=preset.hsv,
            shape_gate=preset.shape_gate,
            preset=preset.name,
            last_applied_at=None,
            algorithm_id=preset.algorithm_id,
        )
        try:
            state.set_detection_config(cfg)
        except KeyError:
            # set_detection_config validates preset existence within
            # its lock; a concurrent DELETE between save_preset above
            # and this set call would land us here.
            raise HTTPException(
                status_code=409,
                detail=f"preset {preset.name!r} was deleted concurrently",
            )
        await device_ws.broadcast(
            {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
        )
    return _preset_to_wire(preset)


_ALLOWED_TARGETS = ("live", "server_post")


@router.post("/presets/active")
async def presets_set_active(request: Request) -> dict[str, Any]:
    """Switch one of the two active preset slots — no file write.

    Body (JSON or form): `name` + `target` — both required.

    `target="live"`: drives the iOS live path (HSV+CC detection).
    Loads the preset, snaps `DetectionConfig` to it, broadcasts WS
    settings. v11_hsv_cc-only — a non-v11 preset returns 422 because
    iOS can't run any other algorithm.

    `target="server_post"`: drives the server-side post-pass `Run
    server` button (and reprocess defaults). Accepts any registered
    algorithm. Does NOT touch live config and does NOT broadcast WS
    — operators on iOS see no change.

    Persisted across restart (live: `detection_config.json` preset
    field; server_post: `active_server_post_preset.json` sidecar)."""
    from main import state, device_ws, _settings_message_for

    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        name = body.get("name")
        target = body.get("target")
    else:
        form = await request.form()
        name = form.get("name")
        target = form.get("target")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="missing required field 'name'")
    if not isinstance(target, str) or not target:
        raise HTTPException(status_code=400, detail="missing required field 'target'")
    if target not in _ALLOWED_TARGETS:
        raise HTTPException(
            status_code=422,
            detail=f"'target' must be one of {list(_ALLOWED_TARGETS)}, got {target!r}",
        )
    try:
        p = state.load_preset(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {name!r}")

    if target == "live":
        if not _is_v11(p.algorithm_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"preset {name!r} targets {p.algorithm_id!r}; only "
                    f"{algorithms.V11_HSV_CC!r} can drive the live path "
                    "(iOS detection is HSV+CC hardcoded)"
                ),
            )
        cfg = DetectionConfig(
            hsv=p.hsv,
            shape_gate=p.shape_gate,
            preset=name,
            last_applied_at=None,
            algorithm_id=p.algorithm_id,
        )
        try:
            state.set_detection_config(cfg)
        except KeyError:
            # set_detection_config re-validates preset existence within
            # its lock; a concurrent DELETE between the load_preset
            # above and this set call would land us here.
            raise HTTPException(
                status_code=409,
                detail=f"preset {name!r} was deleted concurrently",
            )
        await device_ws.broadcast(
            {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
        )
    else:
        # target == "server_post"
        try:
            state.set_active_server_post_preset(name)
        except KeyError:
            raise HTTPException(
                status_code=409,
                detail=f"preset {name!r} was deleted concurrently",
            )
    return {"ok": True, "active": name, "target": target}


@router.delete("/presets/{name}")
def presets_delete(name: str) -> dict[str, Any]:
    """Unlink a preset file. Returns 404 if the preset doesn't exist;
    409 if the preset is currently bound to either active slot — live
    or server_post (operator must switch the relevant slot first via
    `POST /presets/active`). Built-in seeds (tennis, blue_ball,
    hybrid_28d_blue_ball) are deletable — restart will re-seed any
    built-in whose file is missing.

    Sessions whose `live_config_used.preset_name` /
    `server_post_config_used.preset_name` references the deleted preset
    render the chip with a "(deleted)" suffix at the dashboard /
    viewer, but still use the frozen snapshot's HSV + shape-gate values.
    The underlying detection results on disk are untouched.
    """
    from main import state
    if state.active_server_post_preset_name() == name:
        raise HTTPException(
            status_code=409,
            detail=(
                f"preset {name!r} is currently active in the server_post "
                "slot — switch active to another preset first via "
                "POST /presets/active with target=server_post"
            ),
        )
    if state.detection_config().preset == name:
        raise HTTPException(
            status_code=409,
            detail=(
                f"preset {name!r} is currently active — switch active to "
                f"another preset first via POST /presets/active"
            ),
        )
    try:
        state.delete_preset(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {name!r}")
    except RuntimeError as e:
        # state.delete_preset re-checks the active slots under its
        # lock — the route's two checks above are best-effort and a
        # concurrent activate could race. Surface the lock-side reject
        # as 409 too so the operator sees a consistent error class.
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "deleted": name}
