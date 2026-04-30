from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from detection import HSVRange, ShapeGate
from detection_config import (
    DetectionConfig,
    to_dict as _detection_config_to_dict,
)
from schemas import TrackingExposureCapMode
from strike_zone import strike_zone_geometry_for_height

router = APIRouter()


def _validated_hsv_range(values: dict[str, object]) -> HSVRange:
    def _int_field(name: str, upper: int) -> int:
        raw = values.get(name)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid '{name}'")
        if not (0 <= value <= upper):
            raise HTTPException(status_code=400, detail=f"'{name}' out of range [0, {upper}]")
        return value

    h_min = _int_field("h_min", 179)
    h_max = _int_field("h_max", 179)
    s_min = _int_field("s_min", 255)
    s_max = _int_field("s_max", 255)
    v_min = _int_field("v_min", 255)
    v_max = _int_field("v_max", 255)
    if h_min > h_max:
        raise HTTPException(status_code=400, detail="'h_min' must be <= 'h_max'")
    if s_min > s_max:
        raise HTTPException(status_code=400, detail="'s_min' must be <= 's_max'")
    if v_min > v_max:
        raise HTTPException(status_code=400, detail="'v_min' must be <= 'v_max'")
    return HSVRange(
        h_min=h_min,
        h_max=h_max,
        s_min=s_min,
        s_max=s_max,
        v_min=v_min,
        v_max=v_max,
    )


def _validated_shape_gate(values: dict[str, object]) -> ShapeGate:
    def _float_field(name: str, lo: float, hi: float) -> float:
        raw = values.get(name)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid '{name}'")
        if not (lo <= value <= hi):
            raise HTTPException(status_code=400, detail=f"'{name}' out of range [{lo}, {hi}]")
        return value

    aspect_min = _float_field("aspect_min", 0.0, 1.0)
    fill_min = _float_field("fill_min", 0.0, 1.0)
    return ShapeGate(aspect_min=aspect_min, fill_min=fill_min)


@router.get("/detection/config")
async def detection_config_get():
    """Return the full detection-config pair plus preset identity and
    the list of fields that diverge from the bound preset (empty when
    preset-pure or `preset` is None / custom). The dashboard's phase 3
    unified card calls this on render to populate the identity header
    ("Active: blue_ball · modified") and the per-section sliders.
    """
    from main import state

    cfg = state.detection_config()
    return {
        **_detection_config_to_dict(cfg),
        "modified_fields": state.modified_fields_for(cfg),
    }


@router.post("/detection/config")
async def detection_config_post(request: Request):
    """Atomic update of the full detection-config pair.

    Body (JSON) — all required, no per-field defaulting (per CLAUDE.md
    no-silent-fallback):
      - `hsv`: `{h_min, h_max, s_min, s_max, v_min, v_max}` (uint8 range)
      - `shape_gate`: `{aspect_min, fill_min}` (each ∈ [0, 1])
      - `preset`: optional string. If supplied AND non-null, the
        pair MUST exactly match the on-disk preset of that name —
        otherwise the operator's "I'm setting blue_ball" claim is
        contradicted by their values, which is exactly the silent-drift
        failure mode this redesign exists to prevent. Caller can pass
        `preset=null` explicitly to mean "custom config".

    Persists atomically to `data/detection_config.json` and pushes the
    new config to all connected cameras over WS in a single broadcast
    (vs the legacy two-endpoint dance which fired two pushes for one
    logical edit).
    """
    from main import state, device_ws, _settings_message_for

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    for key in ("hsv", "shape_gate"):
        if key not in body:
            raise HTTPException(
                status_code=400,
                detail=f"missing required field '{key}'",
            )
    hsv = _validated_hsv_range(body["hsv"])
    gate = _validated_shape_gate(body["shape_gate"])

    preset_name = body.get("preset")
    if preset_name is not None:
        if not isinstance(preset_name, str):
            raise HTTPException(
                status_code=400,
                detail="'preset' must be a string or null",
            )
        try:
            ref = state.load_preset(preset_name)
        except KeyError:
            known = sorted(pp.name for pp in state.list_presets())
            raise HTTPException(
                status_code=400,
                detail=f"unknown preset: {preset_name!r} (known: {known})",
            )
        # Identity claim must match the actual values — anything else
        # is the kind of silent drift the unified-config redesign is
        # built to prevent. Operator-facing UI computes the diff client-
        # side and either submits matching values or sets preset=null.
        if hsv != ref.hsv or gate != ref.shape_gate:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"preset='{preset_name}' but supplied pair differs from "
                    f"the on-disk preset {preset_name!r}. Either submit "
                    "values matching the preset exactly, or pass "
                    "preset=null for custom."
                ),
            )

    cfg = DetectionConfig(
        hsv=hsv,
        shape_gate=gate,
        preset=preset_name,
        last_applied_at=None,  # state stamps under lock
    )
    applied = state.set_detection_config(cfg)
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    return {
        "ok": True,
        **_detection_config_to_dict(applied),
        "modified_fields": state.modified_fields_for(applied),
    }


@router.post("/settings/chirp_threshold")
async def settings_chirp_threshold(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            threshold = float(body.get("threshold"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'threshold'")
    else:
        form = await request.form()
        raw = form.get("threshold")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'threshold'")
        try:
            threshold = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'threshold'")
    try:
        applied = state.set_chirp_detect_threshold(threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/mutual_sync_threshold")
async def settings_mutual_sync_threshold(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            threshold = float(body.get("threshold"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'threshold'")
    else:
        form = await request.form()
        raw = form.get("threshold")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'threshold'")
        try:
            threshold = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'threshold'")
    try:
        applied = state.set_mutual_sync_threshold(threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/heartbeat_interval")
async def settings_heartbeat_interval(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            interval = float(body.get("interval_s"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'interval_s'")
    else:
        form = await request.form()
        raw = form.get("interval_s")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'interval_s'")
        try:
            interval = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'interval_s'")
    try:
        applied = state.set_heartbeat_interval_s(interval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/tracking_exposure_cap")
async def settings_tracking_exposure_cap(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        mode_raw = body.get("mode")
    else:
        form = await request.form()
        mode_raw = form.get("mode")
    if mode_raw is None:
        raise HTTPException(status_code=400, detail="missing 'mode'")
    try:
        mode = TrackingExposureCapMode(str(mode_raw))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid 'mode'; expected one of {[m.value for m in TrackingExposureCapMode]}",
        )
    applied = state.set_tracking_exposure_cap(mode)
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied.value}


@router.post("/settings/capture_height")
async def settings_capture_height(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        height_raw = body.get("height")
    else:
        form = await request.form()
        height_raw = form.get("height")
    if height_raw is None:
        raise HTTPException(status_code=400, detail="missing 'height'")
    try:
        height = int(height_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid 'height'")
    try:
        applied = state.set_capture_height_px(height)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/strike_zone")
async def settings_strike_zone(request: Request):
    from main import state, _wants_html

    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        height_raw = body.get("height_cm")
    else:
        form = await request.form()
        height_raw = form.get("height_cm")
    if height_raw is None:
        raise HTTPException(status_code=400, detail="missing 'height_cm'")
    try:
        height_cm = int(height_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid 'height_cm'")
    try:
        applied = state.set_batter_height_cm(height_cm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    strike_zone = strike_zone_geometry_for_height(applied).to_dict()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "strike_zone": strike_zone}
