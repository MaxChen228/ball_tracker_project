"""FastAPI ingest + triangulation server for ball_tracker iPhone app.

Endpoints:
  GET  /status          — health + received-cycle summary
  POST /pitch           — ingest one iPhone pitch payload
  GET  /results/latest  — latest fully-triangulated cycle
  GET  /results/{cycle} — specific cycle result
  POST /reset           — clear all cached state
"""
from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
)

logger = logging.getLogger("ball_tracker")


class IntrinsicsPayload(BaseModel):
    fx: float
    fz: float
    cx: float
    cy: float


class FramePayload(BaseModel):
    frame_index: int
    timestamp_s: float
    theta_x_rad: float | None = None
    theta_z_rad: float | None = None
    ball_detected: bool


class PitchPayload(BaseModel):
    camera_id: str
    flash_frame_index: int
    flash_timestamp_s: float
    cycle_number: int
    frames: list[FramePayload]
    intrinsics: IntrinsicsPayload | None = None
    homography: list[float] | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None


class TriangulatedPoint(BaseModel):
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


class CycleResult(BaseModel):
    cycle_number: int
    camera_a_received: bool
    camera_b_received: bool
    points: list[TriangulatedPoint] = []
    error: str | None = None


def _camera_pose(intr: IntrinsicsPayload, H_list: list[float]):
    K = build_K(intr.fx, intr.fz, intr.cx, intr.cy)
    H = np.array(H_list, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, R, t, C


def _frame_items(p: PitchPayload):
    out = []
    for f in p.frames:
        if f.ball_detected and f.theta_x_rad is not None and f.theta_z_rad is not None:
            out.append((f.timestamp_s - p.flash_timestamp_s, f.theta_x_rad, f.theta_z_rad))
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(a: PitchPayload, b: PitchPayload) -> list[TriangulatedPoint]:
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    _, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    _, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a)
    items_b = _frame_items(b)
    if not items_a or not items_b:
        return []

    b_times = np.array([x[0] for x in items_b])
    max_dt = 1.0 / 120.0  # 8ms tolerance at 240fps

    results: list[TriangulatedPoint] = []
    for t_rel, tx_a, tz_a in items_a:
        idx = int(np.argmin(np.abs(b_times - t_rel)))
        if abs(b_times[idx] - t_rel) > max_dt:
            continue
        _, tx_b, tz_b = items_b[idx]

        d_a_cam = angle_ray_cam(tx_a, tz_a)
        d_b_cam = angle_ray_cam(tx_b, tz_b)
        d_a_world = R_a.T @ d_a_cam
        d_b_world = R_b.T @ d_b_cam

        P, gap = triangulate_rays(C_a, d_a_world, C_b, d_b_world)
        results.append(
            TriangulatedPoint(
                t_rel_s=t_rel,
                x_m=float(P[0]),
                y_m=float(P[1]),
                z_m=float(P[2]),
                residual_m=gap,
            )
        )
    return results


class State:
    def __init__(self) -> None:
        self._lock = Lock()
        self.pitches: dict[tuple[str, int], PitchPayload] = {}
        self.results: dict[int, CycleResult] = {}

    def record(self, pitch: PitchPayload) -> CycleResult:
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.cycle_number)] = pitch
            a = self.pitches.get(("A", pitch.cycle_number))
            b = self.pitches.get(("B", pitch.cycle_number))
            result = CycleResult(
                cycle_number=pitch.cycle_number,
                camera_a_received=a is not None,
                camera_b_received=b is not None,
            )
            if a is not None and b is not None:
                try:
                    result.points = triangulate_cycle(a, b)
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
            self.results[pitch.cycle_number] = result
            return result

    def summary(self) -> dict[str, Any]:
        with self._lock:
            cycles = sorted({c for _, c in self.pitches.keys()})
            completed = [
                k for k, r in self.results.items()
                if r.camera_a_received and r.camera_b_received and not r.error
            ]
            return {
                "state": "receiving" if self.pitches else "idle",
                "received_cycles": cycles,
                "completed_cycles": sorted(completed),
            }

    def latest(self) -> CycleResult | None:
        with self._lock:
            if not self.results:
                return None
            return self.results[max(self.results.keys())]

    def get(self, cycle: int) -> CycleResult | None:
        with self._lock:
            return self.results.get(cycle)

    def reset(self) -> None:
        with self._lock:
            self.pitches.clear()
            self.results.clear()


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ip = _lan_ip()
    logger.info("LAN IP: %s  →  set iPhone Settings → Server IP = %s, Port = 8765", ip, ip)
    yield


app = FastAPI(title="ball_tracker server", lifespan=lifespan)
state = State()


@app.get("/status")
def status() -> dict[str, Any]:
    return state.summary()


@app.post("/pitch")
def pitch(payload: PitchPayload) -> dict[str, Any]:
    result = state.record(payload)
    ball_frames = sum(1 for f in payload.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s cycle=%d frames=%d ball=%d triangulated=%d%s",
        payload.camera_id,
        payload.cycle_number,
        len(payload.frames),
        ball_frames,
        len(result.points),
        f" err={result.error}" if result.error else "",
    )
    if result.points:
        zs = [p.z_m for p in result.points]
        logger.info(
            "  cycle %d → %d pts, duration %.2fs, peak z = %.2fm",
            result.cycle_number,
            len(result.points),
            result.points[-1].t_rel_s - result.points[0].t_rel_s,
            max(zs),
        )
    return {"ok": True, "cycle": result.cycle_number, "triangulated_points": len(result.points)}


@app.get("/results/latest")
def results_latest() -> CycleResult:
    r = state.latest()
    if r is None:
        raise HTTPException(404, "no results yet")
    return r


@app.get("/results/{cycle}")
def results_cycle(cycle: int) -> CycleResult:
    r = state.get(cycle)
    if r is None:
        raise HTTPException(404, f"cycle {cycle} not found")
    return r


@app.post("/reset")
def reset() -> dict[str, bool]:
    state.reset()
    return {"ok": True}
