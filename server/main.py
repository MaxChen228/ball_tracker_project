"""FastAPI ingest + triangulation server for ball_tracker iPhone app.

Endpoints:
  GET  /status          — health + received-cycle summary
  POST /pitch           — ingest one iPhone pitch payload
  GET  /results/latest  — latest fully-triangulated cycle
  GET  /results/{cycle} — specific cycle result
  POST /reset           — clear all cached state
"""
from __future__ import annotations

import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
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
    undistorted_ray_cam,
)

logger = logging.getLogger("ball_tracker")


class IntrinsicsPayload(BaseModel):
    fx: float
    fz: float
    cx: float
    cy: float
    # OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Optional so old
    # payloads without distortion still validate.
    distortion: list[float] | None = None


class FramePayload(BaseModel):
    frame_index: int
    timestamp_s: float
    theta_x_rad: float | None = None
    theta_z_rad: float | None = None
    # Raw (distorted) ball pixel coords. When present AND the camera's
    # intrinsics.distortion is present, the server undistorts these instead
    # of using the angles. Nil when no ball was detected.
    px: float | None = None
    py: float | None = None
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
    """Emit (t_rel_s, theta_x, theta_z, px, py) per valid ball-detected frame.

    A frame is "valid" if either:
      - theta_x_rad and theta_z_rad are both present, or
      - px and py are both present.
    px/py may be None (angles-only fallback) and theta_*_rad may be None
    (pixels-only). Both must not be None simultaneously.
    """
    out = []
    for f in p.frames:
        if not f.ball_detected:
            continue
        has_angles = f.theta_x_rad is not None and f.theta_z_rad is not None
        has_pixels = f.px is not None and f.py is not None
        if not (has_angles or has_pixels):
            continue
        out.append(
            (
                f.timestamp_s - p.flash_timestamp_s,
                f.theta_x_rad,
                f.theta_z_rad,
                f.px,
                f.py,
            )
        )
    out.sort(key=lambda x: x[0])
    return out


def _ray_for_frame(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
) -> np.ndarray:
    """Choose undistortion path when possible, else fall back to angle path.

    Per-frame per-camera decision: if intrinsics.distortion is present for
    this camera AND this frame has px/py, undistort. Otherwise use angles.
    """
    if dist_coeffs is not None and px is not None and py is not None:
        return undistorted_ray_cam(px, py, K, np.asarray(dist_coeffs, dtype=float))
    # Angle fallback requires both angles.
    if theta_x is None or theta_z is None:
        raise ValueError("frame has neither usable angles nor pixels")
    return angle_ray_cam(theta_x, theta_z)


def triangulate_cycle(a: PitchPayload, b: PitchPayload) -> list[TriangulatedPoint]:
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)
    dist_a = a.intrinsics.distortion
    dist_b = b.intrinsics.distortion

    items_a = _frame_items(a)
    items_b = _frame_items(b)
    if not items_a or not items_b:
        return []

    b_times = np.array([x[0] for x in items_b])
    max_dt = 1.0 / 120.0  # 8ms tolerance at 240fps

    results: list[TriangulatedPoint] = []
    for t_rel, tx_a, tz_a, px_a, py_a in items_a:
        idx = int(np.argmin(np.abs(b_times - t_rel)))
        if abs(b_times[idx] - t_rel) > max_dt:
            continue
        _, tx_b, tz_b, px_b, py_b = items_b[idx]

        d_a_cam = _ray_for_frame(tx_a, tz_a, px_a, py_a, K_a, dist_a)
        d_b_cam = _ray_for_frame(tx_b, tz_b, px_b, py_b, K_b, dist_b)
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


_DEFAULT_DATA_DIR = Path(os.environ.get("BALL_TRACKER_DATA_DIR", "data"))


class State:
    def __init__(self, data_dir: Path = _DEFAULT_DATA_DIR) -> None:
        self._lock = Lock()
        self.pitches: dict[tuple[str, int], PitchPayload] = {}
        self.results: dict[int, CycleResult] = {}
        self._data_dir = data_dir
        self._pitch_dir = data_dir / "pitches"
        self._result_dir = data_dir / "results"
        self._pitch_dir.mkdir(parents=True, exist_ok=True)
        self._result_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def _pitch_path(self, camera_id: str, cycle: int) -> Path:
        return self._pitch_dir / f"cycle_{cycle:06d}_{camera_id}.json"

    def _result_path(self, cycle: int) -> Path:
        return self._result_dir / f"cycle_{cycle:06d}.json"

    def _load_from_disk(self) -> None:
        for path in sorted(self._pitch_dir.glob("cycle_*.json")):
            try:
                obj = json.loads(path.read_text())
                pitch = PitchPayload.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt pitch file %s: %s", path.name, e)
                continue
            self.pitches[(pitch.camera_id, pitch.cycle_number)] = pitch

        seen_cycles = {cyc for _, cyc in self.pitches.keys()}
        for cycle in sorted(seen_cycles):
            a = self.pitches.get(("A", cycle))
            b = self.pitches.get(("B", cycle))
            result = CycleResult(
                cycle_number=cycle,
                camera_a_received=a is not None,
                camera_b_received=b is not None,
            )
            if a is not None and b is not None:
                try:
                    result.points = triangulate_cycle(a, b)
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
            self.results[cycle] = result

        if self.pitches:
            logger.info(
                "restored %d pitch payloads across %d cycles from %s",
                len(self.pitches),
                len(seen_cycles),
                self._data_dir,
            )

    def _atomic_write(self, path: Path, payload: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    def record(self, pitch: PitchPayload) -> CycleResult:
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.cycle_number)] = pitch
            self._atomic_write(
                self._pitch_path(pitch.camera_id, pitch.cycle_number),
                pitch.model_dump_json(),
            )

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
            self._atomic_write(
                self._result_path(pitch.cycle_number),
                result.model_dump_json(),
            )
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

    def reset(self, purge_disk: bool = False) -> None:
        with self._lock:
            self.pitches.clear()
            self.results.clear()
            if purge_disk:
                for path in self._pitch_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._result_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)


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


def _summarize_result(result: CycleResult) -> dict[str, Any]:
    paired = result.camera_a_received and result.camera_b_received
    summary: dict[str, Any] = {
        "cycle": result.cycle_number,
        "paired": paired,
        "triangulated_points": len(result.points),
        "error": result.error,
    }
    if result.points:
        residuals = [p.residual_m for p in result.points]
        zs = [p.z_m for p in result.points]
        ts = [p.t_rel_s for p in result.points]
        summary["mean_residual_m"] = float(np.mean(residuals))
        summary["max_residual_m"] = float(np.max(residuals))
        summary["peak_z_m"] = float(max(zs))
        summary["duration_s"] = float(ts[-1] - ts[0])
    return summary


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
    return {"ok": True, **_summarize_result(result)}


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
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
