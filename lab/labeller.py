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

from lab.propagator import PropagationCancelled

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
    """fps / duration only. total_frames is NOT taken from ffprobe nb_read_frames
    because that count is only useful as a sanity check; the authoritative frame
    index space comes from the dense PTS list built later in `build_pts_table`.
    Mirroring the server-side viewer (`unionTimes` in
    `server/static/viewer/20_filters.js`) — frame index = position in real-data
    timestamp list, not `round(pts * avg_fps)` which collides on variable-fps."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    stream = json.loads(result.stdout)["streams"][0]
    num, den = stream["avg_frame_rate"].split("/")
    fps = float(num) / float(den)
    return {"fps": fps, "duration_s": float(stream["duration"])}


class ManifestStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        if not MANIFEST_PATH.exists():
            MANIFEST_PATH.write_text(json.dumps({"items": []}, indent=2), encoding="utf-8")
        cfg_path = WORKSPACE / "workspace_config.json"
        self._extra_source_dirs: list[Path] = []
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for d in cfg.get("extra_source_dirs", []):
                self._extra_source_dirs.append((WORKSPACE / d).resolve())

    def _read(self) -> dict[str, Any]:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _write(self, payload: dict[str, Any]) -> None:
        MANIFEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def scan_sources(self) -> None:
        """Pick up any new files dropped into source_videos/ or extra_source_dirs.
        Slug = filename stem. Files from extra dirs are symlinked into SOURCES_DIR
        so all downstream code can keep treating source_video as a SOURCES_DIR-relative name."""
        # Symlink any files from extra dirs into SOURCES_DIR before scanning.
        for src_dir in self._extra_source_dirs:
            if not src_dir.is_dir():
                continue
            for video in src_dir.iterdir():
                if video.is_dir() or video.suffix.lower() not in VIDEO_EXTS:
                    continue
                link = SOURCES_DIR / video.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(video)
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
                # Build the dense PTS list now so total_frames is authoritative
                # (= count of decoded frames). On variable-fps slo-mo MOVs this
                # is ≠ ffprobe nb_read_frames; whichever way we decide later,
                # the manifest must agree with the PTS table or scrubber.max
                # ends up off by ~200 on iPhone 240fps clips.
                pts_payload = _build_and_cache_pts(video, slug, meta["fps"])
                payload["items"].append({
                    "slug": slug,
                    "source_video": video.name,
                    "fps": meta["fps"],
                    "total_frames": pts_payload["total_frames"],
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

    def delete(self, slug: str) -> None:
        """Remove item from manifest and delete its workspace files.
        Source video is left intact (might be a symlink to user's data dir)."""
        with self._lock:
            payload = self._read()
            before = len(payload["items"])
            payload["items"] = [it for it in payload["items"] if it["slug"] != slug]
            if len(payload["items"]) == before:
                raise KeyError(slug)
            self._write(payload)
        idir = ITEMS_DIR / slug
        if idir.exists():
            shutil.rmtree(idir)
        pts_file = PTS_CACHE_DIR / f"{slug}.json"
        if pts_file.exists():
            pts_file.unlink()


def item_dir(slug: str) -> Path:
    d = ITEMS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def frames_dir_for(slug: str) -> Path:
    return item_dir(slug) / "frames"


def masks_dir_for(slug: str) -> Path:
    return item_dir(slug) / "masks"


PTS_CACHE_DIR = WORKSPACE / "pts_cache"


def _decode_pts_seconds(source_path: Path) -> list[float]:
    """Walk all decoded frames once, return their PTS in seconds, sorted ASC.

    Output is the canonical frame index space: position `i` in the returned
    list IS frame index `i`. No nulls, no synthetic `round(pts*fps)`. This is
    the same idea as `unionTimes` in `server/static/viewer/20_filters.js`."""
    import av  # type: ignore

    times: list[float] = []
    container = av.open(str(source_path))
    try:
        for frame in container.decode(video=0):
            if frame.pts is not None:
                t = float(frame.pts * frame.time_base)
            elif frame.time is not None:
                t = float(frame.time)
            else:
                continue
            times.append(t)
    finally:
        container.close()
    times.sort()
    return times


def _build_and_cache_pts(source_path: Path, slug: str, fps: float) -> dict[str, Any]:
    PTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    times = _decode_pts_seconds(source_path)
    payload = {
        "fps": float(fps),
        "total_frames": len(times),
        "source_mtime": source_path.stat().st_mtime,
        "pts": times,
    }
    (PTS_CACHE_DIR / f"{slug}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def get_pts_table(slug: str) -> dict[str, Any]:
    PTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = PTS_CACHE_DIR / f"{slug}.json"
    item = STORE.get(slug)
    source = SOURCES_DIR / item["source_video"]
    src_mtime = source.stat().st_mtime
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("source_mtime") == src_mtime:
            # Trust cache as the authoritative frame count; sync manifest if it
            # disagrees (cheap idempotent reconcile so old manifests with the
            # legacy ffprobe nb_read_frames don't keep showing wrong scrubber).
            if cached.get("total_frames") != item.get("total_frames"):
                STORE.update(slug, total_frames=cached["total_frames"])
            return cached
    payload = _build_and_cache_pts(source, slug, float(item["fps"]))
    if payload["total_frames"] != item.get("total_frames"):
        STORE.update(slug, total_frames=payload["total_frames"])
    return payload


def _pts_idx_for(time_s: float, pts_list: list[float], eps: float = 1e-6) -> int | None:
    """Position of a PyAV frame's PTS in the dense list. None if not found."""
    import bisect

    pos = bisect.bisect_left(pts_list, time_s - eps)
    if pos < 0 or pos >= len(pts_list):
        return None
    if abs(pts_list[pos] - time_s) > eps:
        return None
    return pos


def extract_one_frame(source_path: Path, frame_index: int, pts_list: list[float]):
    """Decode through the source until we find the frame whose PTS == pts_list[frame_index]."""
    import av  # type: ignore

    target = pts_list[frame_index]
    container = av.open(str(source_path))
    try:
        for frame in container.decode(video=0):
            t = float(frame.pts * frame.time_base) if frame.pts is not None else (
                float(frame.time) if frame.time is not None else None
            )
            if t is None:
                continue
            if abs(t - target) < 1e-6:
                return frame.to_ndarray(format="bgr24")
    finally:
        container.close()
    raise IndexError(f"frame {frame_index} (pts={target}) not found in {source_path}")


def extract_range_to_dir(
    source_path: Path, in_frame: int, out_frame: int, dest: Path, pts_list: list[float],
) -> list[int]:
    """Extract every decoded frame whose source index ∈ [in_frame, out_frame].

    Indices are positions in the dense `pts_list` (built by `_decode_pts_seconds`).
    Decode order may differ from PTS order (B-frames), so we stage files under
    src-keyed names and rename to sequential 00000.jpg afterwards. SAM 2 expects
    sequential filenames; `local_to_source[local] == source_idx` lets the
    propagator translate back to mask filenames the browser understands.
    """
    import av  # type: ignore
    import cv2  # type: ignore

    dest.mkdir(parents=True, exist_ok=True)
    sidecar = dest.parent / "local_to_source.json"

    if sidecar.exists():
        cached = json.loads(sidecar.read_text(encoding="utf-8"))
        if cached.get("in_frame") == in_frame and cached.get("out_frame") == out_frame:
            mapping = cached["local_to_source"]
            if all((dest / f"{i:05d}.jpg").exists() for i in range(len(mapping))):
                return mapping

    for old in dest.glob("*.jpg"):
        old.unlink()
    for old in dest.glob("src_*.tmp.jpg"):
        old.unlink()

    src_to_path: dict[int, Path] = {}
    container = av.open(str(source_path))
    try:
        for frame in container.decode(video=0):
            t = float(frame.pts * frame.time_base) if frame.pts is not None else (
                float(frame.time) if frame.time is not None else None
            )
            if t is None:
                continue
            pos = _pts_idx_for(t, pts_list)
            if pos is None:
                continue
            if not (in_frame <= pos <= out_frame):
                continue
            arr = frame.to_ndarray(format="bgr24")
            tmp = dest / f"src_{pos:08d}.tmp.jpg"
            cv2.imwrite(str(tmp), arr, [cv2.IMWRITE_JPEG_QUALITY, 90])
            src_to_path[pos] = tmp
    finally:
        container.close()
    if not src_to_path:
        raise RuntimeError(f"no frames extracted in source-idx range [{in_frame}, {out_frame}]")
    local_to_source: list[int] = []
    for local, src in enumerate(sorted(src_to_path.keys())):
        os.replace(src_to_path[src], dest / f"{local:05d}.jpg")
        local_to_source.append(src)
    sidecar.write_text(
        json.dumps({"in_frame": in_frame, "out_frame": out_frame,
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

AVAILABLE_MODELS = [
    "facebook/sam2-hiera-tiny",
    "facebook/sam2-hiera-small",
    "facebook/sam2-hiera-base-plus",
    "facebook/sam2-hiera-large",
]
_ACTIVE_MODELS = {
    "seed": os.environ.get("SAM2_IMAGE_MODEL", "facebook/sam2-hiera-large"),
    "prop": os.environ.get("SAM2_VIDEO_MODEL", "facebook/sam2-hiera-base-plus"),
}


def get_seeder():
    global _SEEDER
    with _MODEL_LOCK:
        target = _ACTIVE_MODELS["seed"]
        if _SEEDER is not None and getattr(_SEEDER, "model_id", None) != target:
            print(f"[labeller] image predictor model changed → unloading {_SEEDER.model_id}", flush=True)
            _SEEDER = None
        if _SEEDER is None:
            from lab.seeder import Seeder
            print(f"[labeller] loading SAM2 image predictor ({target})...", flush=True)
            t0 = time.time()
            _SEEDER = Seeder(model_id=target)
            print(f"[labeller] image predictor ready on {_SEEDER.device} in {time.time()-t0:.1f}s", flush=True)
    return _SEEDER


def get_propagator():
    global _PROPAGATOR
    with _MODEL_LOCK:
        target = _ACTIVE_MODELS["prop"]
        if _PROPAGATOR is not None and getattr(_PROPAGATOR, "model_id", None) != target:
            print(f"[labeller] video predictor model changed → unloading {_PROPAGATOR.model_id}", flush=True)
            _PROPAGATOR = None
        if _PROPAGATOR is None:
            from lab.propagator import Propagator
            print(f"[labeller] loading SAM2 video predictor ({target})...", flush=True)
            t0 = time.time()
            _PROPAGATOR = Propagator(model_id=target)
            print(f"[labeller] video predictor ready on {_PROPAGATOR.device} in {time.time()-t0:.1f}s", flush=True)
    return _PROPAGATOR


def unload_seeder() -> None:
    """Drop the SAM2 image predictor and free MPS/CUDA cache. Used before a
    queue propagate run to claw back the ~few-GB resident large model."""
    global _SEEDER
    with _MODEL_LOCK:
        if _SEEDER is None:
            return
        print(f"[labeller] unloading image predictor ({_SEEDER.model_id})", flush=True)
        _SEEDER = None
    import gc
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def unload_propagator() -> None:
    """Drop the SAM2 video predictor (mirror of unload_seeder)."""
    global _PROPAGATOR
    with _MODEL_LOCK:
        if _PROPAGATOR is None:
            return
        print(f"[labeller] unloading video predictor ({_PROPAGATOR.model_id})", flush=True)
        _PROPAGATOR = None
    import gc
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


STORE = ManifestStore()
BUS = SseBus()
PROP_THREADS: dict[str, threading.Thread] = {}


# Per-slug locks so concurrent extract requests for the same slug serialize
# rather than racing on the on-disk frames dir.
_EXTRACT_LOCKS: dict[str, threading.Lock] = {}
_EXTRACT_LOCKS_GUARD = threading.Lock()


def _get_extract_lock(slug: str) -> threading.Lock:
    with _EXTRACT_LOCKS_GUARD:
        lk = _EXTRACT_LOCKS.get(slug)
        if lk is None:
            lk = threading.Lock()
            _EXTRACT_LOCKS[slug] = lk
        return lk


def _ensure_range_extracted(slug: str, in_f: int, out_f: int) -> None:
    """Run extract_range_to_dir if its sidecar isn't already populated for
    this exact range. Single PyAV pass decodes the whole [in,out] interval
    so propagate has frames ready on disk."""
    try:
        item = STORE.get(slug)
    except KeyError:
        return
    source = SOURCES_DIR / item["source_video"]
    fdir = frames_dir_for(slug)
    sidecar = fdir.parent / "local_to_source.json"
    if sidecar.is_file():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
            if cached.get("in_frame") == in_f and cached.get("out_frame") == out_f:
                return
        except Exception:
            pass
    lock = _get_extract_lock(slug)
    if not lock.acquire(blocking=False):
        return  # another thread already extracting
    try:
        pts_payload = get_pts_table(slug)
        extract_range_to_dir(source, in_f, out_f, fdir, pts_payload["pts"])
    except Exception as e:
        print(f"[labeller] background extract {slug} failed: {e}", flush=True)
    finally:
        lock.release()


def _kick_background_extract(slug: str, in_f: int, out_f: int) -> None:
    threading.Thread(
        target=_ensure_range_extracted, args=(slug, in_f, out_f), daemon=True
    ).start()


def _bootstrap_extract_all() -> None:
    """At startup, eagerly extract every seeded session's [in,out] range so
    warmUp on first selection hits pre-extracted JPEGs from disk instead of
    triggering per-frame cv2 decodes."""
    for it in STORE.list():
        in_f = it.get("in_frame")
        out_f = it.get("out_frame")
        if isinstance(in_f, int) and isinstance(out_f, int) and in_f < out_f:
            _kick_background_extract(it["slug"], in_f, out_f)


def _recover_crashed_propagations() -> None:
    """If the previous server died mid-propagate, items remain stamped
    `running` and the queue's `idle`-filter would skip them forever. Reset
    those to `idle` so Run Queue picks up where we left off."""
    recovered: list[str] = []
    for it in STORE.list():
        if it.get("propagate_status") == "running":
            STORE.update(it["slug"], propagate_status="idle")
            recovered.append(it["slug"])
    if recovered:
        print(f"[labeller] recovered {len(recovered)} crashed propagations: {recovered}", flush=True)


# Queue state — one global queue runner thread; cancel flag is set by user.
_QUEUE_LOCK = threading.Lock()
_QUEUE_THREAD: threading.Thread | None = None
_QUEUE_CANCEL = threading.Event()
_QUEUE_CURRENT: str | None = None  # slug of the item _queue_runner is currently propagating


def _queue_snapshot() -> dict[str, Any]:
    with _QUEUE_LOCK:
        running = _QUEUE_THREAD is not None and _QUEUE_THREAD.is_alive()
        current = _QUEUE_CURRENT
    items = STORE.list()
    seeded = [it for it in items if it.get("seed_frame") is not None]
    done = sum(1 for it in seeded if it.get("propagate_status") == "done")
    ready = sum(1 for it in seeded
                if it.get("propagate_status") == "idle"
                and it["slug"] != current)
    return {
        "running": running,
        "current": current,
        "done": done,
        "ready": ready,
        "total": len(seeded),
    }


def _queue_runner() -> None:
    """Iterate ready items (seed set, status idle), propagate one at a time.
    Re-queries manifest each iteration so newly-readied items get picked up."""
    global _QUEUE_CURRENT
    print("[labeller] queue: starting", flush=True)
    unload_seeder()
    BUS.publish("__queue__", "queue", _queue_snapshot())
    try:
        while not _QUEUE_CANCEL.is_set():
            items = STORE.list()
            ready = [it for it in items
                     if it.get("seed_frame") is not None
                     and it.get("propagate_status") == "idle"]
            if not ready:
                break
            slug = ready[0]["slug"]
            print(f"[labeller] queue: propagate {slug} ({len(ready)} ready)", flush=True)
            STORE.update(slug, propagate_status="running")
            with _QUEUE_LOCK:
                _QUEUE_CURRENT = slug
            BUS.publish("__queue__", "queue", _queue_snapshot())
            try:
                run_propagate(slug)  # blocks until done / failed / cancelled
            except Exception as e:
                print(f"[labeller] queue: {slug} failed: {e}", flush=True)
                STORE.update(slug, propagate_status="failed")
            # Drop the predictor between queue items. SAM2's reset_state +
            # torch.mps.empty_cache inside Propagator.propagate() do not fully
            # release the per-session activation buffers + MPS pool fragments;
            # over 3 consecutive sessions RSS climbs past 10GB. Reloading the
            # model costs ~1.5s on M-series MPS, paid once per item.
            unload_propagator()
            if _QUEUE_CANCEL.is_set():
                # Stop-Queue cancelled this item mid-run (run_propagate caught
                # the exception and stamped "failed"). Reset to idle so the
                # next Run Queue resumes from this exact session.
                cur = STORE.get(slug)
                if cur.get("propagate_status") != "done":
                    STORE.update(slug, propagate_status="idle")
                break
        print("[labeller] queue: drained", flush=True)
    finally:
        with _QUEUE_LOCK:
            _QUEUE_CURRENT = None
        BUS.publish("__queue__", "queue", _queue_snapshot())
        _QUEUE_CANCEL.clear()


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

    expected = out_f - in_f + 1
    BUS.publish(slug, "phase", {
        "phase": "extracting", "expected_frames": expected, "in_frame": in_f, "out_frame": out_f,
    })
    t_extract = time.time()
    try:
        pts_payload = get_pts_table(slug)
        local_to_source = extract_range_to_dir(source, in_f, out_f, fdir, pts_payload["pts"])
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
    last_queue_pub = 0.0
    frames_emitted = 0
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
            frames_emitted += 1
            # Throttle queue-channel progress to ~1 update / 1.5s. SAM 2's
            # forward pass emits (expected - seed_local) frames and the reverse
            # pass emits (seed_local + 1), so total = expected + 1 (seed visited
            # in both passes).
            if _QUEUE_CURRENT == slug:
                now = time.time()
                if now - last_queue_pub >= 1.5:
                    last_queue_pub = now
                    snap = _queue_snapshot()
                    snap["frame_done"] = frames_emitted
                    snap["frame_total"] = expected + 1
                    snap["elapsed_s"] = round(now - t_prop, 2)
                    BUS.publish("__queue__", "queue", snap)
    except PropagationCancelled:
        # User pressed Stop. Wipe the partial masks and reset to idle so the
        # next Run Queue (or single propagate) starts from scratch — leaving
        # the mid-run masks on disk would let a half-done session masquerade
        # as complete on the next reload.
        for png in mdir.glob("*.png"):
            png.unlink()
        STORE.update(slug, propagate_status="idle")
        BUS.publish(slug, "error", {"msg": "cancelled"})
        return
    except Exception as e:
        BUS.publish(slug, "error", {"msg": f"propagate failed: {e}"})
        STORE.update(slug, propagate_status="failed")
        return

    STORE.update(slug, propagate_status="done")
    BUS.publish(slug, "done", {"elapsed_s": round(time.time() - t_prop, 2)})


SLUG_RE = re.compile(r"^/api/items/([A-Za-z0-9_\-]+)/(trim|seed|propagate|propagate/cancel|events|pts|masks|delete)$")
MASK_RE = re.compile(r"^/mask/([A-Za-z0-9_\-]+)/(\d{5})\.png$")
CLIP_RE = re.compile(r"^/clip/([A-Za-z0-9_\-]+)\.mp4$")


class Handler(BaseHTTPRequestHandler):
    server_version = "lab-labeller/1.0"
    # HTTP/1.1 keep-alive: reuse TCP across SSE + clip range requests. (The
    # legacy per-JPEG /frame/<NNNNN>.jpg path is gone; clips now stream as one
    # MP4 + WebCodecs decode in the browser.)
    protocol_version = "HTTP/1.1"

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
        # __queue__ is a reserved channel for global queue events; no manifest entry.
        if slug != "__queue__":
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
        if p == "/api/queue/status":
            self._send_json(HTTPStatus.OK, _queue_snapshot())
            return
        if p == "/api/models":
            self._send_json(HTTPStatus.OK, {
                "available": AVAILABLE_MODELS,
                "active": dict(_ACTIVE_MODELS),
                "loaded": {
                    "seed": getattr(_SEEDER, "model_id", None) if _SEEDER else None,
                    "prop": getattr(_PROPAGATOR, "model_id", None) if _PROPAGATOR else None,
                },
            })
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
        if p.startswith("/static/") or p in ("/", "/index.html", "/app.js", "/style.css", "/frame_source.js") or p.startswith("/vendor/"):
            self._serve_static(p[len("/static"):] if p.startswith("/static/") else p)
            return
        self._send_text(HTTPStatus.NOT_FOUND, f"no route: {p}")

    def do_POST(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/items/rescan":
            STORE.scan_sources()
            self._send_json(HTTPStatus.OK, {"items": [self._public_item(it) for it in STORE.list()]})
            return
        if url.path == "/api/queue/run":
            global _QUEUE_THREAD
            with _QUEUE_LOCK:
                if _QUEUE_THREAD is not None and _QUEUE_THREAD.is_alive():
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "msg": "queue already running"})
                    return
                _QUEUE_CANCEL.clear()
                _QUEUE_THREAD = threading.Thread(target=_queue_runner, daemon=True)
                _QUEUE_THREAD.start()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if url.path == "/api/queue/cancel":
            _QUEUE_CANCEL.set()
            if _PROPAGATOR is not None:
                _PROPAGATOR.cancel()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if url.path == "/api/queue/status":
            self._send_json(HTTPStatus.OK, _queue_snapshot())
            return
        if url.path == "/api/models/unload_seed":
            unload_seeder()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if url.path == "/api/models":
            body = self._read_json()
            kind = body["kind"]
            model_id = body["model_id"]
            if kind not in ("seed", "prop"):
                self._send_text(HTTPStatus.BAD_REQUEST, "kind must be 'seed' or 'prop'")
                return
            if model_id not in AVAILABLE_MODELS:
                self._send_text(HTTPStatus.BAD_REQUEST, f"unknown model_id: {model_id}")
                return
            with _MODEL_LOCK:
                _ACTIVE_MODELS[kind] = model_id
            # Don't pre-load; next /seed or /propagate triggers reload.
            self._send_json(HTTPStatus.OK, {"ok": True, "active": dict(_ACTIVE_MODELS)})
            return
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
            # Kick the single-pass PyAV extract for the new range. Returns
            # immediately; warmUp's /frame fetches will then hit pre-decoded
            # JPEGs on disk instead of forcing per-frame decode.
            _kick_background_extract(slug, in_f, out_f)
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
                pts_payload = get_pts_table(slug)
                arr = extract_one_frame(source, frame_index, pts_payload["pts"])
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
            # Only fire global cancel if THIS slug is the one actively running.
            # _PROPAGATOR is a singleton — calling cancel() while another slug
            # owns it (queue runner on a different item) would interrupt the
            # wrong propagation.
            t = PROP_THREADS.get(slug)
            owns_propagator = (t is not None and t.is_alive()) or _QUEUE_CURRENT == slug
            if owns_propagator and _PROPAGATOR is not None:
                _PROPAGATOR.cancel()
            STORE.update(slug, propagate_status="idle")
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if action == "delete":
            t = PROP_THREADS.get(slug)
            owns_propagator = (t is not None and t.is_alive()) or _QUEUE_CURRENT == slug
            if owns_propagator and _PROPAGATOR is not None:
                _PROPAGATOR.cancel()
            STORE.delete(slug)
            PROP_THREADS.pop(slug, None)
            _EXTRACT_LOCKS.pop(slug, None)
            with BUS._lock:
                BUS._subs.pop(slug, None)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        self._send_text(HTTPStatus.NOT_FOUND, f"unknown action: {action}")

    @staticmethod
    def _public_item(it: dict[str, Any]) -> dict[str, Any]:
        return dict(it)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress the noisy traceback stdlib emits when a browser aborts a
    fetch / closes an EventSource mid-request — that surfaces as
    ConnectionResetError or BrokenPipeError before handle_one_request
    can even read the request line. Real exceptions still propagate."""

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    STORE.scan_sources()
    _recover_crashed_propagations()
    _bootstrap_extract_all()
    port = int(os.environ.get("LABELLER_PORT", "8876"))
    addr = ("127.0.0.1", port)
    server = _QuietThreadingHTTPServer(addr, Handler)
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
