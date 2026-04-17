"""FastAPI ingest + triangulation server for ball_tracker iPhone app.

Endpoints:
  GET  /                       — events index (HTML table → /viewer/{cycle})
  GET  /status                 — health + received-cycle summary
  POST /pitch                  — ingest one iPhone pitch payload (multipart/form-data
                                  with required `payload` JSON part and optional
                                  `video` MOV/MP4 clip)
  GET  /chirp.wav              — reference sync chirp for the 時間校正 step
  GET  /events                 — one row per cycle: cameras, status, counts,
                                  received_at, triangulation stats
  GET  /results/latest         — latest fully-triangulated cycle
  GET  /results/{cycle}        — specific cycle result
  GET  /reconstruction/{cycle} — 3D scene (cameras + rays + optional
                                  triangulated trajectory) as JSON
  GET  /viewer/{cycle}         — same scene rendered as a self-contained
                                  Plotly HTML page
  POST /reset                  — clear all cached state
"""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, ValidationError

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
    # OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Optional so
    # payloads without distortion still validate and fall back to the angle
    # path.
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
    # Constrained so we can safely interpolate into filenames (clips,
    # pitch json). Matches the iOS-side values ("A" / "B") with slack for
    # future role additions but blocks path-traversal attempts.
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    # Shared time anchor for A/B pairing, recovered from an audio-chirp
    # matched-filter hit on the 時間校正 step. Server uses
    # `sync_anchor_timestamp_s` as the per-cycle clock origin and pairs
    # frames within an 8 ms window of the relative time.
    sync_anchor_frame_index: int
    sync_anchor_timestamp_s: float
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


def _ray_for_frame(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
) -> np.ndarray:
    """Per-frame ray choice. Prefer undistorting raw pixels if available,
    otherwise fall back to the angle ray computed on-device."""
    if dist_coeffs is not None and px is not None and py is not None:
        return undistorted_ray_cam(px, py, K, np.asarray(dist_coeffs, dtype=float))
    if theta_x is None or theta_z is None:
        raise ValueError("frame has neither usable angles nor pixels")
    return angle_ray_cam(theta_x, theta_z)


def _valid_frame(f: FramePayload) -> bool:
    has_angles = f.theta_x_rad is not None and f.theta_z_rad is not None
    has_pixels = f.px is not None and f.py is not None
    return f.ball_detected and (has_angles or has_pixels)


def _frame_items(p: PitchPayload):
    """Ball-bearing frames as `(t_rel, θx, θz, px, py)`, sorted by
    anchor-relative time. `t_rel = timestamp_s − sync_anchor_timestamp_s`."""
    anchor = p.sync_anchor_timestamp_s
    out = [
        (f.timestamp_s - anchor, f.theta_x_rad, f.theta_z_rad, f.px, f.py)
        for f in p.frames if _valid_frame(f)
    ]
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(a: PitchPayload, b: PitchPayload) -> list[TriangulatedPoint]:
    """Pair A and B frames within an 8 ms window of anchor-relative time and
    run ray-midpoint triangulation. Requires intrinsics + homography on both
    cameras."""
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a)
    items_b = _frame_items(b)
    if not items_a or not items_b:
        return []

    b_times = np.array([x[0] for x in items_b])
    max_dt = 1.0 / 120.0  # 8 ms tolerance at 240 fps

    dist_a = a.intrinsics.distortion
    dist_b = b.intrinsics.distortion

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
        self._video_dir = data_dir / "videos"
        self._pitch_dir.mkdir(parents=True, exist_ok=True)
        self._result_dir.mkdir(parents=True, exist_ok=True)
        self._video_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    @property
    def video_dir(self) -> Path:
        return self._video_dir

    def save_clip(
        self, camera_id: str, cycle: int, data: bytes, ext: str = "mov"
    ) -> Path:
        """Persist a cycle's H.264 clip to disk. Writes atomically so a
        partial transfer cannot leave a corrupt file visible to downstream
        tools. Overwrites any existing clip for (camera_id, cycle)."""
        safe_ext = (ext or "mov").lstrip(".").lower()
        if not safe_ext or "/" in safe_ext or "\\" in safe_ext:
            safe_ext = "mov"
        path = self._video_dir / f"cycle_{cycle:06d}_{camera_id}.{safe_ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

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

    def pitches_for_cycle(self, cycle: int) -> dict[str, PitchPayload]:
        """Snapshot of all pitches currently stored for `cycle`, keyed by
        camera_id. Returns an empty dict if the cycle has not been seen."""
        with self._lock:
            return {
                cam_id: p
                for (cam_id, c), p in self.pitches.items()
                if c == cycle
            }

    def events(self) -> list[dict[str, Any]]:
        """Summary row per cycle for the events panel — one entry per
        cycle_number, collapsing A/B uploads into a single event.

        `received_at` is derived from the pitch file's mtime so we don't have
        to extend the Pydantic payload with server-side timestamps.
        """
        with self._lock:
            cycles = sorted({c for _, c in self.pitches.keys()})
            events: list[dict[str, Any]] = []
            for cyc in cycles:
                cams_present = sorted(
                    cam for (cam, c) in self.pitches.keys() if c == cyc
                )
                cam_frame_counts: dict[str, int] = {}
                latest_mtime: float | None = None
                for cam in cams_present:
                    pitch = self.pitches[(cam, cyc)]
                    cam_frame_counts[cam] = sum(
                        1 for f in pitch.frames if f.ball_detected
                    )
                    path = self._pitch_path(cam, cyc)
                    try:
                        mtime = path.stat().st_mtime
                    except FileNotFoundError:
                        mtime = None
                    if mtime is not None and (
                        latest_mtime is None or mtime > latest_mtime
                    ):
                        latest_mtime = mtime

                result = self.results.get(cyc)
                n_triangulated = len(result.points) if result is not None else 0
                error = result.error if result is not None else None

                if error:
                    status = "error"
                elif len(cams_present) >= 2 and n_triangulated > 0:
                    status = "paired"
                elif len(cams_present) >= 2:
                    status = "paired_no_points"
                else:
                    status = "partial"

                peak_z: float | None = None
                mean_res: float | None = None
                duration: float | None = None
                if result is not None and result.points:
                    zs = [p.z_m for p in result.points]
                    peak_z = float(max(zs))
                    mean_res = float(
                        sum(p.residual_m for p in result.points)
                        / len(result.points)
                    )
                    ts = [p.t_rel_s for p in result.points]
                    duration = float(ts[-1] - ts[0])

                events.append(
                    {
                        "cycle_number": cyc,
                        "cameras": cams_present,
                        "status": status,
                        "received_at": latest_mtime,
                        "n_ball_frames": cam_frame_counts,
                        "n_triangulated": n_triangulated,
                        "peak_z_m": peak_z,
                        "mean_residual_m": mean_res,
                        "duration_s": duration,
                        "error": error,
                    }
                )
            # Latest events first — matches the UI expectation.
            events.sort(key=lambda e: e["cycle_number"], reverse=True)
            return events

    def reset(self, purge_disk: bool = False) -> None:
        with self._lock:
            self.pitches.clear()
            self.results.clear()
            if purge_disk:
                for path in self._pitch_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._result_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._video_dir.glob("cycle_*"):
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
async def pitch(
    payload: str = Form(...),
    video: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest one cycle as multipart/form-data.

    Required form field `payload`: JSON-encoded `PitchPayload`.
    Optional form field `video`:   MOV/MP4 clip of the cycle. Stored under
                                    `data/videos/cycle_XXXXXX_{cam}.{ext}` and
                                    not yet consumed by triangulation — Phase
                                    1 raw-video experiment.
    """
    try:
        payload_obj = PitchPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    clip_info: dict[str, Any] | None = None
    if video is not None:
        data = await video.read()
        if data:
            ext = "mov"
            if video.filename:
                suffix = Path(video.filename).suffix.lstrip(".").lower()
                if suffix:
                    ext = suffix
            clip_path = state.save_clip(
                payload_obj.camera_id, payload_obj.cycle_number, data, ext
            )
            clip_info = {"filename": clip_path.name, "bytes": len(data)}

    result = state.record(payload_obj)
    ball_frames = sum(1 for f in payload_obj.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s cycle=%d frames=%d ball=%d triangulated=%d%s%s",
        payload_obj.camera_id,
        payload_obj.cycle_number,
        len(payload_obj.frames),
        ball_frames,
        len(result.points),
        f" clip={clip_info['bytes']}B" if clip_info else "",
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
    response: dict[str, Any] = {"ok": True, **_summarize_result(result)}
    if clip_info is not None:
        response["clip"] = clip_info
    return response


@app.get("/chirp.wav")
def chirp_wav() -> Response:
    """Reference sync chirp for the 時間校正 step.

    Users download this on any device (browser) and play it near the two
    iPhones. Each phone's AudioChirpDetector runs matched filtering and
    pins the session-clock PTS of the peak as the per-cycle anchor.

    Signal: linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, surrounded by
    0.5 s of silence either side so the phones can catch it mid-stream.
    """
    sr = 44100
    f0 = 2000.0
    f1 = 8000.0
    duration = 0.1
    n = int(sr * duration)
    t = np.arange(n) / sr
    phase = 2.0 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2.0 * duration))
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(n) / (n - 1)))
    chirp = np.sin(phase) * window

    silence = np.zeros(int(sr * 0.5), dtype=np.float64)
    full = np.concatenate([silence, chirp, silence])
    pcm = np.clip(full * 0.8, -1.0, 1.0)
    pcm_int = (pcm * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_int.tobytes())
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"Content-Disposition": 'inline; filename="chirp.wav"'},
    )


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


def _scene_for_cycle(cycle: int):
    """Shared fetch+build for the two scene endpoints. Raises 404 when no
    pitches have been received for this cycle yet."""
    # Local imports so the FastAPI app still boots when plotly is missing
    # (the JSON endpoint doesn't need it; the HTML one will surface a 500).
    from reconstruct import build_scene

    pitches = state.pitches_for_cycle(cycle)
    if not pitches:
        raise HTTPException(404, f"cycle {cycle} has no pitches")
    result = state.get(cycle)
    triangulated = result.points if result is not None else []
    return build_scene(cycle, pitches, triangulated)


@app.get("/reconstruction/{cycle}")
def reconstruction(cycle: int) -> dict[str, Any]:
    scene = _scene_for_cycle(cycle)
    return scene.to_dict()


@app.get("/viewer/{cycle}", response_class=HTMLResponse)
def viewer(cycle: int) -> HTMLResponse:
    from viewer import render_scene_html

    scene = _scene_for_cycle(cycle)
    return HTMLResponse(render_scene_html(scene))


@app.get("/events")
def events() -> list[dict[str, Any]]:
    return state.events()


@app.get("/", response_class=HTMLResponse)
def events_index() -> HTMLResponse:
    from viewer import render_events_index_html

    return HTMLResponse(render_events_index_html(state.events()))


@app.post("/reset")
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
