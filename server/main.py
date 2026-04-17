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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from audio_sync import compute_audio_offset
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
    # Mac-sync clock offset: phone_monotonic_clock - server_monotonic_clock (seconds).
    # Server applies: aligned_ts = frame.timestamp_s - mac_clock_offset_s
    # to convert phone timestamps into server clock for cross-camera pairing.
    mac_clock_offset_s: float | None = None
    # Audio-sync anchor: session-clock PTS of the first sample in the sidecar
    # WAV. Combined with the correlation peak lag this recovers the A↔B clock
    # offset. Populated only when syncMode == "audio".
    audio_start_ts_s: float | None = None


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
    sync_method: str = "flash"  # "flash" | "mac" | "audio"
    # Measured A↔B clock offset when sync_method == "audio" (B − A, seconds
    # — add to B frame timestamps to express them in A's clock).
    audio_offset_s: float | None = None


def _camera_pose(intr: IntrinsicsPayload, H_list: list[float]):
    K = build_K(intr.fx, intr.fz, intr.cx, intr.cy)
    H = np.array(H_list, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, R, t, C


def _frame_items_flash(p: PitchPayload):
    """Frame items using flash-relative time (existing behavior)."""
    out = []
    for f in p.frames:
        if f.ball_detected and f.theta_x_rad is not None and f.theta_z_rad is not None:
            out.append((f.timestamp_s - p.flash_timestamp_s, f.theta_x_rad, f.theta_z_rad))
    out.sort(key=lambda x: x[0])
    return out


def _frame_items_mac(p: PitchPayload):
    """Frame items aligned to server clock via mac_clock_offset_s.

    mac_clock_offset_s = phone_clock - server_clock
    aligned_ts = phone_ts - mac_clock_offset_s  =>  server clock
    """
    offset = p.mac_clock_offset_s or 0.0
    out = []
    for f in p.frames:
        if f.ball_detected and f.theta_x_rad is not None and f.theta_z_rad is not None:
            aligned = f.timestamp_s - offset
            out.append((aligned, f.theta_x_rad, f.theta_z_rad))
    out.sort(key=lambda x: x[0])
    return out


def _frame_items_shifted(p: PitchPayload, ts_shift: float = 0.0):
    """Ball-bearing frame items with their raw session-clock timestamps
    optionally shifted. `ts_shift` > 0 moves the frames forward in time.
    Used for audio-sync alignment (shift B's frames by delta_s = clockA −
    clockB to express them in A's time base)."""
    out = []
    for f in p.frames:
        if f.ball_detected and f.theta_x_rad is not None and f.theta_z_rad is not None:
            out.append((f.timestamp_s + ts_shift, f.theta_x_rad, f.theta_z_rad))
    out.sort(key=lambda x: x[0])
    return out


def _do_triangulate(
    items_a: list, items_b: list,
    R_a: "np.ndarray", C_a: "np.ndarray",
    R_b: "np.ndarray", C_b: "np.ndarray",
) -> list[TriangulatedPoint]:
    """Core pairing + ray triangulation, shared by both sync flows."""
    if not items_a or not items_b:
        return []

    b_times = np.array([x[0] for x in items_b])
    max_dt = 1.0 / 120.0  # 8 ms tolerance at 240 fps

    results: list[TriangulatedPoint] = []
    for t_ref, tx_a, tz_a in items_a:
        idx = int(np.argmin(np.abs(b_times - t_ref)))
        if abs(b_times[idx] - t_ref) > max_dt:
            continue
        _, tx_b, tz_b = items_b[idx]

        d_a_cam = angle_ray_cam(tx_a, tz_a)
        d_b_cam = angle_ray_cam(tx_b, tz_b)
        d_a_world = R_a.T @ d_a_cam
        d_b_world = R_b.T @ d_b_cam

        P, gap = triangulate_rays(C_a, d_a_world, C_b, d_b_world)
        results.append(
            TriangulatedPoint(
                t_rel_s=t_ref,
                x_m=float(P[0]),
                y_m=float(P[1]),
                z_m=float(P[2]),
                residual_m=gap,
            )
        )
    return results


def triangulate_cycle(
    a: PitchPayload,
    b: PitchPayload,
    audio_offset_s: float | None = None,
) -> tuple[list[TriangulatedPoint], str]:
    """Returns (points, sync_method) where sync_method is 'mac' | 'audio' |
    'flash'. Priority order: mac > audio > flash.

    - `mac`: both payloads carry `mac_clock_offset_s` from NTP-style sync.
    - `audio`: caller pre-computed A↔B cross-correlation offset; pass it in
      as `audio_offset_s` (= clockA − clockB).
    - `flash`: degraded fallback — pair on per-camera time-since-flash.
    """
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    _, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    _, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    use_mac = (a.mac_clock_offset_s is not None) and (b.mac_clock_offset_s is not None)
    use_audio = (not use_mac) and (audio_offset_s is not None)

    if use_mac:
        items_a = _frame_items_mac(a)
        items_b = _frame_items_mac(b)
        sync_method = "mac"
    elif use_audio:
        # Shift B's frames by audio_offset_s (= clockA − clockB), putting
        # A and B on the same time base. A itself is unshifted.
        items_a = _frame_items_shifted(a, ts_shift=0.0)
        items_b = _frame_items_shifted(b, ts_shift=audio_offset_s or 0.0)
        sync_method = "audio"
    else:
        items_a = _frame_items_flash(a)
        items_b = _frame_items_flash(b)
        sync_method = "flash"

    return _do_triangulate(items_a, items_b, R_a, C_a, R_b, C_b), sync_method


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

    def _audio_path(self, camera_id: str, cycle: int) -> Path:
        return self._pitch_dir / f"cycle_{cycle:06d}_{camera_id}.wav"

    def _result_path(self, cycle: int) -> Path:
        return self._result_dir / f"cycle_{cycle:06d}.json"

    def _maybe_audio_offset(self, a: PitchPayload, b: PitchPayload) -> float | None:
        """Cross-correlate A and B WAVs if both are present with anchors.
        Returns delta_s = clockA − clockB (add to B frame ts to align to A)."""
        if a.audio_start_ts_s is None or b.audio_start_ts_s is None:
            return None
        a_wav = self._audio_path(a.camera_id, a.cycle_number)
        b_wav = self._audio_path(b.camera_id, b.cycle_number)
        if not (a_wav.exists() and b_wav.exists()):
            return None
        try:
            delta, _peak = compute_audio_offset(
                a_wav, a.audio_start_ts_s,
                b_wav, b.audio_start_ts_s,
            )
            return delta
        except Exception as e:
            logger.warning(
                "audio offset compute failed for cycle %d: %s",
                a.cycle_number, e
            )
            return None

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
                    audio_offset = self._maybe_audio_offset(a, b)
                    result.points, result.sync_method = triangulate_cycle(
                        a, b, audio_offset_s=audio_offset
                    )
                    if result.sync_method == "audio":
                        result.audio_offset_s = audio_offset
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

    def record(
        self,
        pitch: PitchPayload,
        audio_bytes: bytes | None = None,
    ) -> CycleResult:
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.cycle_number)] = pitch
            self._atomic_write(
                self._pitch_path(pitch.camera_id, pitch.cycle_number),
                pitch.model_dump_json(),
            )
            if audio_bytes is not None:
                self._audio_path(pitch.camera_id, pitch.cycle_number).write_bytes(audio_bytes)

            a = self.pitches.get(("A", pitch.cycle_number))
            b = self.pitches.get(("B", pitch.cycle_number))
            result = CycleResult(
                cycle_number=pitch.cycle_number,
                camera_a_received=a is not None,
                camera_b_received=b is not None,
            )
            if a is not None and b is not None:
                try:
                    audio_offset = self._maybe_audio_offset(a, b)
                    result.points, result.sync_method = triangulate_cycle(
                        a, b, audio_offset_s=audio_offset
                    )
                    if result.sync_method == "audio":
                        result.audio_offset_s = audio_offset
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
                for path in self._pitch_dir.glob("cycle_*.wav"):
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


@app.post("/sync/time")
def sync_time() -> dict[str, float]:
    """Ultra-low-latency time probe for NTP-style clock-offset estimation.

    Returns the server monotonic clock (time.monotonic()) the instant this
    request is handled.  No logging, no locking — straight clock read.
    """
    return {"server_time_s": time.monotonic()}


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
        "sync_method": result.sync_method,
        "audio_offset_s": result.audio_offset_s,
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
async def pitch(
    payload: str = Form(...),
    audio: UploadFile | None = File(None),
) -> dict[str, Any]:
    try:
        pitch_obj = PitchPayload.model_validate_json(payload)
    except Exception as e:
        raise HTTPException(400, f"invalid payload JSON: {e}")
    audio_bytes: bytes | None = None
    if audio is not None:
        audio_bytes = await audio.read()
        if not audio_bytes:
            audio_bytes = None
    result = state.record(pitch_obj, audio_bytes=audio_bytes)
    ball_frames = sum(1 for f in pitch_obj.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s cycle=%d frames=%d ball=%d sync=%s triangulated=%d%s",
        pitch_obj.camera_id,
        pitch_obj.cycle_number,
        len(pitch_obj.frames),
        ball_frames,
        result.sync_method,
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
