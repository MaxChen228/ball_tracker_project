from __future__ import annotations

import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LAB_DIR = Path(__file__).resolve().parent
STATIC_DIR = LAB_DIR / "static"
WORKSPACE = LAB_DIR / "standalone_workspace"
SOURCES_DIR = WORKSPACE / "source_videos"
ITEMS_DIR = WORKSPACE / "items"
MANIFEST_PATH = WORKSPACE / "manifest.json"

VIDEO_EXTS = (".mp4", ".mov", ".m4v")


def _slugify(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch.lower())
        else:
            out.append("_")
    s = "".join(out).strip("_")
    if not s:
        raise ValueError("slug resolved to empty")
    return s


def _video_meta(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries",
        "stream=nb_read_frames,avg_frame_rate,duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    stream = json.loads(result.stdout)["streams"][0]
    num, den = stream["avg_frame_rate"].split("/")
    fps = float(num) / float(den)
    return {
        "fps": fps,
        "total_frames": int(stream["nb_read_frames"]),
        "duration_s": float(stream["duration"]),
    }


class ManifestStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        if not MANIFEST_PATH.exists():
            MANIFEST_PATH.write_text(json.dumps({"items": []}, indent=2), encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _write(self, payload: dict[str, Any]) -> None:
        MANIFEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def scan_sources(self) -> None:
        """Pick up any new files dropped into source_videos/. Slug = filename stem."""
        with self._lock:
            payload = self._read()
            existing = {it["slug"] for it in payload["items"]}
            existing_sources = {it["source_video"] for it in payload["items"]}
            for video in sorted(SOURCES_DIR.iterdir()):
                if video.is_dir() or video.suffix.lower() not in VIDEO_EXTS:
                    continue
                if video.name in existing_sources:
                    continue
                slug = _slugify(video.stem)
                while slug in existing:
                    slug = f"{slug}_{secrets.token_hex(2)}"
                meta = _video_meta(video)
                payload["items"].append({
                    "slug": slug,
                    "source_video": video.name,
                    "fps": meta["fps"],
                    "total_frames": meta["total_frames"],
                    "in_frame": None,
                    "out_frame": None,
                    "seed_frame": None,
                    "seed_point": None,
                    "propagate_status": "idle",
                })
                existing.add(slug)
                existing_sources.add(video.name)
            self._write(payload)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read()["items"])

    def get(self, slug: str) -> dict[str, Any]:
        with self._lock:
            for it in self._read()["items"]:
                if it["slug"] == slug:
                    return it
        raise KeyError(slug)

    def update(self, slug: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            payload = self._read()
            for it in payload["items"]:
                if it["slug"] == slug:
                    it.update(fields)
                    self._write(payload)
                    return it
        raise KeyError(slug)


def item_dir(slug: str) -> Path:
    d = ITEMS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def frames_dir_for(slug: str) -> Path:
    return item_dir(slug) / "frames"


def masks_dir_for(slug: str) -> Path:
    return item_dir(slug) / "masks"


def _pts_to_frame_index(frame, fps: float) -> int:
    """Map a PyAV frame's PTS to the same frame index the browser uses.

    The browser-side `currentFrame = round(mediaTime * fps)` is the canonical
    source of truth for 'which frame is this'. We mirror that formula here
    using `frame.pts * frame.time_base` (== exact PTS in seconds), so backend
    file naming and frontend lookups agree even on variable-fps videos where
    decode-order index does NOT equal PTS-derived index.
    """
    if frame.pts is None:
        # Some containers omit PTS. Fall back to frame.time which PyAV synthesizes.
        if frame.time is None:
            raise RuntimeError("frame has neither pts nor time; cannot index")
        t = float(frame.time)
    else:
        t = float(frame.pts * frame.time_base)
    return int(round(t * fps))


PTS_CACHE_DIR = WORKSPACE / "pts_cache"


def build_pts_table(source_path: Path, total_frames: int, fps: float) -> list[float | None]:
    """One PTS-second value per source frame index. None where no decoded frame
    rounds to that index (variable-fps gap). The browser uses these to seek to
    the exact mid-point of frame N, so arrow-key step lands on the visually-
    next frame even when avg-fps is a lie."""
    import av  # type: ignore

    table: list[float | None] = [None] * total_frames
    container = av.open(str(source_path))
    try:
        for frame in container.decode(video=0):
            if frame.pts is None:
                if frame.time is None:
                    continue
                t = float(frame.time)
            else:
                t = float(frame.pts * frame.time_base)
            idx = int(round(t * fps))
            if 0 <= idx < total_frames:
                table[idx] = t
    finally:
        container.close()
    return table


def get_pts_table(slug: str) -> dict[str, Any]:
    PTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = PTS_CACHE_DIR / f"{slug}.json"
    item = STORE.get(slug)
    source = SOURCES_DIR / item["source_video"]
    src_mtime = source.stat().st_mtime
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("source_mtime") == src_mtime and cached.get("total_frames") == item["total_frames"]:
            return cached
    table = build_pts_table(source, int(item["total_frames"]), float(item["fps"]))
    payload = {
        "fps": float(item["fps"]),
        "total_frames": int(item["total_frames"]),
        "source_mtime": src_mtime,
        "pts": table,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def extract_one_frame(source_path: Path, frame_index: int, fps: float):
    """Decode the frame whose PTS-derived index == frame_index. Returns BGR ndarray.

    Uses the same `round(pts * fps)` formula the browser uses for its
    `currentFrame` so the seed click on browser frame N hits source frame N.
    """
    import av  # type: ignore

    container = av.open(str(source_path))
    try:
        last_below = None
        for frame in container.decode(video=0):
            idx = _pts_to_frame_index(frame, fps)
            if idx == frame_index:
                return frame.to_ndarray(format="bgr24")
            if idx < frame_index:
                last_below = (idx, frame)
                continue
            # idx > frame_index: PTS overshot without an exact match (variable fps gap).
            # Use the closest frame on either side.
            arr_after = frame.to_ndarray(format="bgr24")
            if last_below is not None and (frame_index - last_below[0]) <= (idx - frame_index):
                return last_below[1].to_ndarray(format="bgr24")
            return arr_after
        if last_below is not None:
            return last_below[1].to_ndarray(format="bgr24")
        raise IndexError(f"frame {frame_index} not found in {source_path}")
    finally:
        container.close()


def extract_range_to_dir(
    source_path: Path, in_frame: int, out_frame: int, dest: Path, fps: float,
) -> list[int]:
    """Extract source frames whose PTS-index ∈ [in_frame, out_frame] into dest.

    Files are written sequentially (00000.jpg, 00001.jpg, ...) because SAM 2
    video predictor expects sequential local indexing. Returns the local→source
    map (`local_to_source[local_idx] == source_idx`) and persists it as
    `local_to_source.json` next to the frames so subsequent calls (propagate
    seed lookup, mask filenames) can translate between local and source coords
    without re-decoding.
    """
    import av  # type: ignore
    import cv2  # type: ignore

    dest.mkdir(parents=True, exist_ok=True)
    sidecar = dest.parent / "local_to_source.json"

    # Cache check: same range + sidecar present and consistent → skip work.
    if sidecar.exists():
        cached = json.loads(sidecar.read_text(encoding="utf-8"))
        if cached.get("in_frame") == in_frame and cached.get("out_frame") == out_frame:
            mapping = cached["local_to_source"]
            if all((dest / f"{i:05d}.jpg").exists() for i in range(len(mapping))):
                return mapping

    for old in dest.glob("*.jpg"):
        old.unlink()

    local_to_source: list[int] = []
    container = av.open(str(source_path))
    try:
        for frame in container.decode(video=0):
            source_idx = _pts_to_frame_index(frame, fps)
            if source_idx < in_frame:
                continue
            if source_idx > out_frame:
                break
            arr = frame.to_ndarray(format="bgr24")
            local = len(local_to_source)
            cv2.imwrite(str(dest / f"{local:05d}.jpg"), arr, [cv2.IMWRITE_JPEG_QUALITY, 90])
            local_to_source.append(source_idx)
    finally:
        container.close()
    if not local_to_source:
        raise RuntimeError(f"no frames extracted in PTS range [{in_frame}, {out_frame}] @ fps={fps}")
    sidecar.write_text(
        json.dumps({"in_frame": in_frame, "out_frame": out_frame, "fps": fps,
                    "local_to_source": local_to_source}, indent=2),
        encoding="utf-8",
    )
    return local_to_source


class SseBus:
    """Per-slug pub/sub for SSE. New listeners register; publishers fan out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = {}

    def subscribe(self, slug: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2048)
        with self._lock:
            self._subs.setdefault(slug, []).append(q)
        return q

    def unsubscribe(self, slug: str, q: queue.Queue) -> None:
        with self._lock:
            lst = self._subs.get(slug, [])
            if q in lst:
                lst.remove(q)

    def publish(self, slug: str, event: str, data: dict[str, Any]) -> None:
        msg = (event, data)
        with self._lock:
            lst = list(self._subs.get(slug, []))
        for q in lst:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


# Lazy globals — populated on first use to keep boot fast and surface clear errors.
_SEEDER = None
_PROPAGATOR = None
_MODEL_LOCK = threading.Lock()


def get_seeder():
    global _SEEDER
    with _MODEL_LOCK:
        if _SEEDER is None:
            from lab.seeder import Seeder
            print("[labeller] loading SAM2 image predictor (sam2-hiera-tiny)...", flush=True)
            t0 = time.time()
            _SEEDER = Seeder()
            print(f"[labeller] image predictor ready on {_SEEDER.device} in {time.time()-t0:.1f}s", flush=True)
    return _SEEDER


def get_propagator():
    global _PROPAGATOR
    with _MODEL_LOCK:
        if _PROPAGATOR is None:
            from lab.propagator import Propagator
            print("[labeller] loading SAM2 video predictor (sam2-hiera-tiny)...", flush=True)
            t0 = time.time()
            _PROPAGATOR = Propagator()
            print(f"[labeller] video predictor ready on {_PROPAGATOR.device} in {time.time()-t0:.1f}s", flush=True)
    return _PROPAGATOR


STORE = ManifestStore()
BUS = SseBus()
PROP_THREADS: dict[str, threading.Thread] = {}


def run_propagate(slug: str) -> None:
    item = STORE.get(slug)
    in_f = item["in_frame"]
    out_f = item["out_frame"]
    seed_f = item["seed_frame"]
    seed_p = item["seed_point"]
    if None in (in_f, out_f, seed_f, seed_p):
        BUS.publish(slug, "error", {"msg": "missing in/out/seed/point"})
        STORE.update(slug, propagate_status="failed")
        return
    if not (in_f <= seed_f <= out_f):
        BUS.publish(slug, "error", {"msg": "seed_frame outside [in,out]"})
        STORE.update(slug, propagate_status="failed")
        return

    source = SOURCES_DIR / item["source_video"]
    fdir = frames_dir_for(slug)
    mdir = masks_dir_for(slug)
    mdir.mkdir(parents=True, exist_ok=True)
    for old in mdir.glob("*.png"):
        old.unlink()

    fps = float(item["fps"])
    expected = out_f - in_f + 1
    BUS.publish(slug, "phase", {
        "phase": "extracting", "expected_frames": expected, "in_frame": in_f, "out_frame": out_f,
    })
    t_extract = time.time()
    try:
        local_to_source = extract_range_to_dir(source, in_f, out_f, fdir, fps)
    except Exception as e:
        BUS.publish(slug, "error", {"msg": f"frame extract failed: {e}"})
        STORE.update(slug, propagate_status="failed")
        return
    BUS.publish(slug, "phase", {
        "phase": "extracted", "expected_frames": len(local_to_source),
        "elapsed_s": round(time.time() - t_extract, 2),
    })

    # Find local index whose source idx is closest to the seed frame.
    seed_local = min(range(len(local_to_source)), key=lambda i: abs(local_to_source[i] - seed_f))
    if local_to_source[seed_local] != seed_f:
        BUS.publish(slug, "phase", {
            "phase": "seed_remapped", "requested": seed_f, "matched": local_to_source[seed_local],
        })
    BUS.publish(slug, "phase", {"phase": "model_loading"})
    t_model = time.time()
    prop = get_propagator()
    BUS.publish(slug, "phase", {
        "phase": "model_ready", "device": prop.device, "model": prop.model_id,
        "elapsed_s": round(time.time() - t_model, 2),
    })

    BUS.publish(slug, "phase", {
        "phase": "propagating", "expected_frames": expected, "seed_frame": seed_f,
    })
    t_prop = time.time()
    try:
        for local_idx, mask_png in prop.propagate(fdir, seed_local, (seed_p[0], seed_p[1])):
            # Translate SAM 2's local sequential idx back to the canonical source
            # frame index the browser uses, via the local_to_source map. This
            # keeps mask filenames aligned with `round(mediaTime * fps)` so the
            # frontend overlay lines up with the ball, not 1-2 frames offset.
            source_idx = local_to_source[local_idx]
            (mdir / f"{source_idx:05d}.png").write_bytes(mask_png)
            BUS.publish(slug, "mask", {
                "frame": source_idx,
                "mask_url": f"/mask/{slug}/{source_idx:05d}.png",
            })
    except Exception as e:
        BUS.publish(slug, "error", {"msg": f"propagate failed: {e}"})
        STORE.update(slug, propagate_status="failed")
        return

    STORE.update(slug, propagate_status="done")
    BUS.publish(slug, "done", {"elapsed_s": round(time.time() - t_prop, 2)})


SLUG_RE = re.compile(r"^/api/items/([A-Za-z0-9_\-]+)/(trim|seed|propagate|propagate/cancel|events|pts|masks)$")
MASK_RE = re.compile(r"^/mask/([A-Za-z0-9_\-]+)/(\d{5})\.png$")
CLIP_RE = re.compile(r"^/clip/([A-Za-z0-9_\-]+)\.mp4$")


class Handler(BaseHTTPRequestHandler):
    server_version = "lab-labeller/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, msg: str) -> None:
        self._send_bytes(code, msg.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _serve_static(self, rel: str) -> None:
        # Map "/" to index.html
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            self._send_text(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not target.is_file():
            self._send_text(HTTPStatus.NOT_FOUND, f"not found: {rel}")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        self._send_bytes(HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream")

    def _serve_clip(self, slug: str) -> None:
        try:
            item = STORE.get(slug)
        except KeyError:
            self._send_text(HTTPStatus.NOT_FOUND, "no such slug")
            return
        src = SOURCES_DIR / item["source_video"]
        if not src.is_file():
            self._send_text(HTTPStatus.NOT_FOUND, f"missing source {src.name}")
            return
        # Range support is needed for HTML5 <video> seeking.
        size = src.stat().st_size
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                start_s, end_s = rng[6:].split("-", 1)
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            except ValueError:
                self._send_text(HTTPStatus.BAD_REQUEST, "bad range")
                return
            end = min(end, size - 1)
            length = end - start + 1
            with src.open("rb") as f:
                f.seek(start)
                body = f.read(length)
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with src.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def _serve_mask(self, slug: str, idx: str) -> None:
        path = masks_dir_for(slug) / f"{idx}.png"
        if not path.is_file():
            self._send_text(HTTPStatus.NOT_FOUND, "no mask")
            return
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), "image/png")

    def _serve_sse(self, slug: str) -> None:
        try:
            STORE.get(slug)
        except KeyError:
            self._send_text(HTTPStatus.NOT_FOUND, "no such slug")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe(slug)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
                self.wfile.write(msg)
                self.wfile.flush()
                if event in ("done", "error"):
                    # keep the connection so frontend sees later events too
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            BUS.unsubscribe(slug, q)

    # --- routes ---

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        p = url.path
        if p == "/api/items":
            STORE.scan_sources()
            self._send_json(HTTPStatus.OK, {"items": [self._public_item(it) for it in STORE.list()]})
            return
        m = SLUG_RE.match(p)
        if m and m.group(2) == "events":
            self._serve_sse(m.group(1))
            return
        if m and m.group(2) == "pts":
            try:
                payload = get_pts_table(m.group(1))
            except KeyError:
                self._send_text(HTTPStatus.NOT_FOUND, "no such slug")
                return
            except Exception as e:
                self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"pts build failed: {e}")
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if m and m.group(2) == "masks":
            slug = m.group(1)
            try:
                STORE.get(slug)
            except KeyError:
                self._send_text(HTTPStatus.NOT_FOUND, "no such slug")
                return
            mdir = masks_dir_for(slug)
            frames: list[int] = []
            if mdir.is_dir():
                for png in mdir.glob("*.png"):
                    try:
                        frames.append(int(png.stem))
                    except ValueError:
                        continue
            frames.sort()
            self._send_json(HTTPStatus.OK, {"frames": frames})
            return
        m = MASK_RE.match(p)
        if m:
            self._serve_mask(m.group(1), m.group(2))
            return
        m = CLIP_RE.match(p)
        if m:
            self._serve_clip(m.group(1))
            return
        if p.startswith("/static/") or p in ("/", "/index.html", "/app.js", "/style.css"):
            self._serve_static(p[len("/static"):] if p.startswith("/static/") else p)
            return
        self._send_text(HTTPStatus.NOT_FOUND, f"no route: {p}")

    def do_POST(self) -> None:
        url = urllib.parse.urlparse(self.path)
        m = SLUG_RE.match(url.path)
        if not m:
            self._send_text(HTTPStatus.NOT_FOUND, f"no route: {url.path}")
            return
        slug, action = m.group(1), m.group(2)
        try:
            STORE.get(slug)
        except KeyError:
            self._send_text(HTTPStatus.NOT_FOUND, "no such slug")
            return

        if action == "trim":
            body = self._read_json()
            in_f = body["in_frame"]
            out_f = body["out_frame"]
            if not (isinstance(in_f, int) and isinstance(out_f, int) and 0 <= in_f < out_f):
                self._send_text(HTTPStatus.BAD_REQUEST, "in_frame/out_frame must be int with in<out")
                return
            STORE.update(slug, in_frame=in_f, out_frame=out_f)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if action == "seed":
            body = self._read_json()
            frame_index = body["frame_index"]
            x = body["x"]
            y = body["y"]
            if not all(isinstance(v, int) for v in (frame_index, x, y)):
                self._send_text(HTTPStatus.BAD_REQUEST, "frame_index/x/y must be int")
                return
            item = STORE.get(slug)
            source = SOURCES_DIR / item["source_video"]
            try:
                arr = extract_one_frame(source, frame_index, float(item["fps"]))
            except Exception as e:
                self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"extract failed: {e}")
                return
            try:
                seeder = get_seeder()
                png = seeder.seed_at(arr, x, y)
            except Exception as e:
                self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"seed failed: {e}")
                return
            (item_dir(slug) / "seed_mask.png").write_bytes(png)
            STORE.update(slug, seed_frame=frame_index, seed_point=[x, y])
            self._send_bytes(HTTPStatus.OK, png, "image/png")
            return

        if action == "propagate":
            t = PROP_THREADS.get(slug)
            if t and t.is_alive():
                self._send_text(HTTPStatus.CONFLICT, "propagate already running")
                return
            STORE.update(slug, propagate_status="running")
            t = threading.Thread(target=run_propagate, args=(slug,), daemon=True)
            PROP_THREADS[slug] = t
            t.start()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if action == "propagate/cancel":
            if _PROPAGATOR is not None:
                _PROPAGATOR.cancel()
            STORE.update(slug, propagate_status="idle")
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        self._send_text(HTTPStatus.NOT_FOUND, f"unknown action: {action}")

    @staticmethod
    def _public_item(it: dict[str, Any]) -> dict[str, Any]:
        out = dict(it)
        out["status"] = it["propagate_status"]
        return out


def main() -> None:
    STORE.scan_sources()
    port = int(os.environ.get("LABELLER_PORT", "8876"))
    addr = ("127.0.0.1", port)
    server = ThreadingHTTPServer(addr, Handler)
    print(f"[labeller] http://{addr[0]}:{addr[1]}", flush=True)
    print(f"[labeller] static={STATIC_DIR}", flush=True)
    print(f"[labeller] workspace={WORKSPACE}", flush=True)
    print(f"[labeller] {len(STORE.list())} item(s) in manifest", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[labeller] shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
