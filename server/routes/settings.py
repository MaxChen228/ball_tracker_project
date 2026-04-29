from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate
from presets import PRESETS as _HSV_PRESETS
from schemas import TrackingExposureCapMode

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


@router.post("/detection/hsv")
async def detection_hsv(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html

    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        preset = body.get("preset")
        if isinstance(preset, str) and preset in _HSV_PRESETS and not any(
            key in body for key in ("h_min", "h_max", "s_min", "s_max", "v_min", "v_max")
        ):
            hsv = _HSV_PRESETS[preset].hsv
        else:
            hsv = _validated_hsv_range(body)
    else:
        form = await request.form()
        preset = form.get("preset")
        if isinstance(preset, str) and preset in _HSV_PRESETS and not any(
            form.get(key) is not None for key in ("h_min", "h_max", "s_min", "s_max", "v_min", "v_max")
        ):
            hsv = _HSV_PRESETS[preset].hsv
        else:
            hsv = _validated_hsv_range({key: form.get(key) for key in ("h_min", "h_max", "s_min", "s_max", "v_min", "v_max")})

    applied = state.set_hsv_range(hsv)
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    payload = {"ok": True, "hsv_range": applied.__dict__}
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return payload


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


@router.post("/detection/shape_gate")
async def detection_shape_gate(request: Request):
    """Operator-tunable aspect/fill gates for HSV blob filter.

    Both gates ∈ [0, 1]. Pushed to iOS over WS `settings` so the `live`
    path applies the same thresholds as `server_post`. Body accepts JSON
    `{"aspect_min": 0.7, "fill_min": 0.55}` or form fields.
    """
    from main import state, device_ws, _settings_message_for, _wants_html

    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        gate = _validated_shape_gate(body)
    else:
        form = await request.form()
        gate = _validated_shape_gate({k: form.get(k) for k in ("aspect_min", "fill_min")})
    applied = state.set_shape_gate(gate)
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    payload = {"ok": True, "shape_gate": {"aspect_min": applied.aspect_min, "fill_min": applied.fill_min}}
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return payload


def _validated_candidate_selector_tuning(values: dict[str, object]) -> CandidateSelectorTuning:
    """Parse + range-check the two shape-prior knobs."""
    def _float_field(name: str, lo: float, hi: float) -> float:
        raw = values.get(name)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"missing or invalid '{name}'")
        if not (lo <= value <= hi):
            raise HTTPException(status_code=400, detail=f"'{name}' out of range [{lo}, {hi}]")
        return value

    return CandidateSelectorTuning(
        w_aspect=_float_field("w_aspect", 0.0, 1.0),
        w_fill=_float_field("w_fill", 0.0, 1.0),
    )


@router.post("/detection/candidate_selector")
async def detection_candidate_selector(request: Request):
    """Operator-tunable shape-prior weights for `select_best_candidate`.

    Server-side only — applied in both `live_pairing._resolve_candidates`
    (live path) and `detect_pitch` (server_post path). Body accepts JSON
    `{"w_aspect": 0.6, "w_fill": 0.4}` or equivalent form fields.
    """
    from main import state, _wants_html

    fields = ("w_aspect", "w_fill")
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        tuning = _validated_candidate_selector_tuning(body)
    else:
        form = await request.form()
        tuning = _validated_candidate_selector_tuning(
            {k: form.get(k) for k in fields}
        )
    applied = state.set_candidate_selector_tuning(tuning)
    payload = {
        "ok": True,
        "candidate_selector_tuning": {
            "w_aspect": applied.w_aspect,
            "w_fill": applied.w_fill,
        },
    }
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return payload


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
