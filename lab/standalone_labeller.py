from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LAB_DIR = Path(__file__).resolve().parent
REPO_ROOT = LAB_DIR.parent
WORKSPACE_DIR = LAB_DIR / "standalone_workspace"
SOURCE_VIDEOS_DIR = WORKSPACE_DIR / "source_videos"
ITEMS_DIR = WORKSPACE_DIR / "items"
MANIFEST_PATH = WORKSPACE_DIR / "manifest.json"
TRIM_SCRIPT = LAB_DIR / "trim_video_clip.py"
SEED_SCRIPT = LAB_DIR / "run_clip_seed.py"
TRACK_SCRIPT = LAB_DIR / "run_seeded_sam2.py"
VIDEO_META_CACHE: dict[str, tuple[float, dict[str, float | int]]] = {}


def _video_meta(video_path: Path) -> dict[str, float | int]:
    stat = video_path.stat()
    cached = VIDEO_META_CACHE.get(str(video_path))
    if cached is not None and cached[0] == stat.st_mtime:
        return cached[1]
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames,avg_frame_rate,duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    rate = stream["avg_frame_rate"]
    num, den = rate.split("/")
    fps = float(num) / float(den)
    meta = {
        "frames": int(stream["nb_read_frames"]),
        "fps": fps,
        "duration_s": float(stream["duration"]),
    }
    VIDEO_META_CACHE[str(video_path)] = (stat.st_mtime, meta)
    return meta


def _slugify(text: str) -> str:
    clean = []
    for ch in text:
        if ch.isalnum() or ch in {"_", "-"}:
            clean.append(ch.lower())
        else:
            clean.append("_")
    slug = "".join(clean).strip("_")
    if not slug:
        raise ValueError("slug resolved to empty")
    return slug


class LabellerStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        SOURCE_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        if not MANIFEST_PATH.exists():
            self._write({"items": []})
        self._sanitize_after_restart()

    def _read(self) -> dict[str, Any]:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _write(self, payload: dict[str, Any]) -> None:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _mutate(self, fn):
        with self._lock:
            payload = self._read()
            result = fn(payload)
            self._write(payload)
            return result

    def _sanitize_after_restart(self) -> None:
        def mutate(payload: dict[str, Any]):
            for item in payload["items"]:
                if item["status"] in {"seeding", "tracking"}:
                    item["status"] = "error"
                    item["running_job"] = None
                    item["error"] = "tool restarted during background job"
        self._mutate(mutate)

    def import_video(self, source_path: Path) -> dict[str, Any]:
        if not source_path.exists():
            raise FileNotFoundError(f"source video missing: {source_path}")
        if not source_path.is_file():
            raise ValueError(f"source path is not a file: {source_path}")
        suffix = source_path.suffix.lower()
        if suffix not in {".mov", ".mp4", ".m4v"}:
            raise ValueError("only .mov/.mp4/.m4v are supported")
        target = SOURCE_VIDEOS_DIR / source_path.name
        if target.exists():
            stem = _slugify(source_path.stem)
            target = SOURCE_VIDEOS_DIR / f"{stem}_{secrets.token_hex(3)}{suffix}"
        shutil.copy2(source_path, target)
        meta = _video_meta(target)
        return {
            "name": target.name,
            "path": str(target),
            "frames": int(meta["frames"]),
            "fps": meta["fps"],
            "duration_s": meta["duration_s"],
        }

    def list_videos(self) -> list[dict[str, Any]]:
        videos = []
        for path in sorted(SOURCE_VIDEOS_DIR.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".mov", ".mp4", ".m4v"}:
                continue
            meta = _video_meta(path)
            videos.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "frames": int(meta["frames"]),
                    "fps": meta["fps"],
                    "duration_s": meta["duration_s"],
                }
            )
        return videos

    def create_item(self, *, source_video: Path, start_frame: int, end_frame: int, slug: str | None) -> dict[str, Any]:
        if source_video.parent != SOURCE_VIDEOS_DIR:
            raise ValueError("source video must be imported into lab workspace first")
        meta = _video_meta(source_video)
        if end_frame < start_frame:
            raise ValueError("end_frame must be >= start_frame")
        if start_frame < 0 or end_frame >= int(meta["frames"]):
            raise ValueError(f"frame range must be within 0..{int(meta['frames']) - 1}")
        item_id = f"clip_{secrets.token_hex(4)}"
        item_slug = _slugify(slug or f"{source_video.stem}_{start_frame}_{end_frame}")
        item_dir = ITEMS_DIR / item_id
        item_dir.mkdir(parents=True, exist_ok=True)
        clip_path = item_dir / f"{item_slug}.mp4"
        trim_cmd = [
            sys.executable,
            str(TRIM_SCRIPT),
            "--video",
            str(source_video),
            "--output",
            str(clip_path),
            "--start-frame",
            str(start_frame),
            "--end-frame",
            str(end_frame),
            "--overwrite",
        ]
        subprocess.run(trim_cmd, check=True, cwd=str(REPO_ROOT))
        item = {
            "id": item_id,
            "slug": item_slug,
            "status": "trimmed",
            "source_video": str(source_video),
            "source_video_name": source_video.name,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "clip_path": str(clip_path),
            "seed_run_dir": str(item_dir / "seed_run"),
            "running_job": None,
            "error": None,
            "seed_frame_override": None,
        }

        def mutate(payload: dict[str, Any]):
            payload["items"].insert(0, item)
            return item

        self._mutate(mutate)
        return item

    def delete_item(self, item_id: str) -> None:
        with self._lock:
            payload = self._read()
            kept = []
            removed = None
            for item in payload["items"]:
                if item["id"] == item_id:
                    removed = item
                    continue
                kept.append(item)
            if removed is None:
                raise KeyError(item_id)
            if removed["status"] in {"seeding", "tracking"}:
                raise RuntimeError("cannot delete running item")
            payload["items"] = kept
            self._write(payload)
        shutil.rmtree(Path(removed["clip_path"]).parent, ignore_errors=True)

    def get_item(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self._read()
        for item in payload["items"]:
            if item["id"] == item_id:
                return dict(item)
        raise KeyError(item_id)

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            payload = self._read()
        return [self._present_item(item) for item in payload["items"]]

    def _present_item(self, item: dict[str, Any]) -> dict[str, Any]:
        presented = dict(item)
        clip_path = Path(item["clip_path"])
        seed_run_dir = Path(item["seed_run_dir"])
        if clip_path.exists():
            presented["clip_url"] = f"/media/item/{item['id']}/clip"
        if (seed_run_dir / "previews" / "seed_grounding.jpg").exists():
            presented["seed_preview_url"] = f"/media/item/{item['id']}/seed"
        if (seed_run_dir / "tracking_overlay.mp4").exists():
            presented["overlay_url"] = f"/media/item/{item['id']}/overlay"
        meta_path = clip_path.with_suffix(clip_path.suffix + ".json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            duration_s = float(meta["duration_seconds"])
            frames = item["end_frame"] - item["start_frame"] + 1
            presented["clip_summary"] = f"{frames} frames | {duration_s:.3f}s"
        log_tail = self._log_tail(item)
        if log_tail:
            presented["log_tail"] = log_tail
        stats_path = seed_run_dir / "stats.json"
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            if item["status"] == "seeded":
                selected = stats.get("selected_seed")
                if selected is not None:
                    presented["summary_line"] = (
                        f"seed frame={selected['frame_index']} score={selected['score']:.3f}"
                    )
            elif item["status"] == "tracked":
                presented["summary_line"] = (
                    f"tracked_frames={stats.get('tracked_frames')} overlay ready"
                )
        return presented

    def _log_tail(self, item: dict[str, Any]) -> str:
        seed_run_dir = Path(item["seed_run_dir"])
        if item.get("running_job") is not None:
            candidates = [seed_run_dir / f"{item['running_job']}.log"]
        else:
            candidates = [seed_run_dir / "track.log", seed_run_dir / "seed.log"]
        for candidate in candidates:
            if candidate.exists():
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[-20:])
        return ""

    def _update_item(self, item_id: str, **fields: Any) -> None:
        def mutate(payload: dict[str, Any]):
            for item in payload["items"]:
                if item["id"] == item_id:
                    item.update(fields)
                    return
            raise KeyError(item_id)
        self._mutate(mutate)

    def start_seed(self, item_id: str, seed_frame: int | None) -> None:
        item = self.get_item(item_id)
        if item["status"] in {"seeding", "tracking"}:
            raise RuntimeError("item already running")
        seed_run_dir = Path(item["seed_run_dir"])
        seed_run_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(SEED_SCRIPT),
            "--clip",
            item["clip_path"],
            "--output-dir",
            item["seed_run_dir"],
            "--overwrite",
        ]
        if seed_frame is not None:
            cmd.extend(["--seed-frame", str(seed_frame)])
        self._update_item(
            item_id,
            status="seeding",
            error=None,
            running_job="seed",
            seed_frame_override=seed_frame,
        )
        self._spawn_background(item_id, "seed", cmd)

    def start_track(self, item_id: str) -> None:
        item = self.get_item(item_id)
        if item["status"] != "seeded":
            raise RuntimeError("item must be seeded before SAM2")
        cmd = [
            sys.executable,
            str(TRACK_SCRIPT),
            "--seed-run-dir",
            item["seed_run_dir"],
            "--offload-video-to-cpu",
            "--render-overlay",
        ]
        self._update_item(item_id, status="tracking", error=None, running_job="track")
        self._spawn_background(item_id, "track", cmd)

    def _spawn_background(self, item_id: str, job_name: str, cmd: list[str]) -> None:
        thread = threading.Thread(
            target=self._run_background_job,
            args=(item_id, job_name, cmd),
            daemon=True,
        )
        thread.start()

    def _run_background_job(self, item_id: str, job_name: str, cmd: list[str]) -> None:
        item = self.get_item(item_id)
        seed_run_dir = Path(item["seed_run_dir"])
        seed_run_dir.mkdir(parents=True, exist_ok=True)
        log_path = seed_run_dir / f"{job_name}.log"
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
            )
            code = proc.wait()
        if code != 0:
            self._update_item(
                item_id,
                status="error",
                error=f"{job_name} failed; inspect log",
                running_job=None,
            )
            return
        if job_name == "seed":
            self._update_item(item_id, status="seeded", error=None, running_job=None)
        elif job_name == "track":
            self._update_item(item_id, status="tracked", error=None, running_job=None)
        else:
            raise AssertionError(job_name)


STORE = LabellerStore()


def render_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Standalone Labeller</title>
  <style>
    :root {
      --bg: #f4efe5;
      --panel: #fffdf8;
      --ink: #171411;
      --muted: #6b6258;
      --line: #d7ccbc;
      --accent: #0e5b78;
      --ok: #2f6f43;
      --err: #8c2f2f;
      --warn: #8a5a10;
      --track: #1f1b16;
      --sel: #d6b25e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .wrap {
      max-width: 1560px;
      margin: 0 auto;
      padding: 20px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 30px;
    }
    .sub {
      color: var(--muted);
      margin: 0 0 18px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 18px;
      margin-bottom: 18px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(520px, 1.4fr) minmax(360px, 0.8fr);
      gap: 18px;
    }
    .stack {
      display: grid;
      gap: 12px;
    }
    video, img {
      width: 100%;
      display: block;
      background: #000;
      border-radius: 10px;
    }
    .toolbar, .microbar, .picker-row, .summary-grid, .item-grid, .item-actions {
      display: grid;
      gap: 10px;
    }
    .toolbar {
      grid-template-columns: repeat(8, minmax(0, 1fr));
    }
    .microbar {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .picker-row {
      grid-template-columns: minmax(280px, 1.2fr) 160px 160px 180px auto;
      align-items: end;
    }
    .summary-grid {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .timecode-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: 1fr 1fr 1fr;
    }
    .item-grid {
      grid-template-columns: 1fr 1fr 1fr;
      margin-top: 12px;
      margin-bottom: 12px;
    }
    .item-actions {
      grid-template-columns: 140px 160px 160px auto;
      align-items: end;
      margin-bottom: 12px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 12px;
      color: var(--muted);
    }
    input, select, button {
      font: inherit;
    }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 10px 12px;
      color: var(--ink);
    }
    button {
      border: 1px solid var(--track);
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--track);
      color: #fff;
      cursor: pointer;
    }
    button.alt {
      background: #fff;
      color: var(--track);
    }
    button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .timeline-wrap {
      display: grid;
      gap: 10px;
    }
    .timeline-track {
      position: relative;
      height: 14px;
      border-radius: 999px;
      background: linear-gradient(
        to right,
        #dbd2c4 0%,
        #dbd2c4 var(--sel-start, 0%),
        var(--sel) var(--sel-start, 0%),
        var(--sel) var(--sel-end, 100%),
        #dbd2c4 var(--sel-end, 100%),
        #dbd2c4 100%
      );
      overflow: hidden;
    }
    .timeline-track::after {
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: var(--playhead, 0%);
      width: 2px;
      background: var(--accent);
    }
    .timeline-badge {
      position: absolute;
      top: -26px;
      transform: translateX(-50%);
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 11px;
      color: #fff;
      background: var(--track);
      white-space: nowrap;
      pointer-events: none;
    }
    .timeline-badge.in {
      background: #7a5a12;
    }
    .timeline-badge.out {
      background: #7a5a12;
    }
    .timeline-badge.playhead {
      background: var(--accent);
      top: 18px;
    }
    .slider-stack {
      position: relative;
      height: 34px;
    }
    .slider-stack input[type="range"] {
      position: absolute;
      inset: 0;
      width: 100%;
      margin: 0;
      padding: 0;
      height: 34px;
      background: transparent;
      border: none;
      -webkit-appearance: none;
      appearance: none;
      pointer-events: none;
    }
    .slider-stack input[type="range"]::-webkit-slider-runnable-track {
      height: 34px;
      background: transparent;
    }
    .slider-stack input[type="range"]::-moz-range-track {
      height: 34px;
      background: transparent;
      border: none;
    }
    .slider-stack input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      margin-top: 7px;
      height: 20px;
      width: 20px;
      border-radius: 999px;
      border: 2px solid #fff;
      background: var(--track);
      box-shadow: 0 0 0 1px rgba(0,0,0,0.22);
      pointer-events: auto;
      cursor: pointer;
    }
    .slider-stack input[type="range"]::-moz-range-thumb {
      height: 20px;
      width: 20px;
      border-radius: 999px;
      border: 2px solid #fff;
      background: var(--track);
      box-shadow: 0 0 0 1px rgba(0,0,0,0.22);
      pointer-events: auto;
      cursor: pointer;
    }
    #playhead-slider::-webkit-slider-thumb {
      background: var(--accent);
      width: 14px;
      height: 24px;
      margin-top: 5px;
    }
    #playhead-slider::-moz-range-thumb {
      background: var(--accent);
      width: 14px;
      height: 24px;
    }
    #in-slider::-webkit-slider-thumb,
    #out-slider::-webkit-slider-thumb {
      background: var(--sel);
      border-color: #5d4b1e;
    }
    #in-slider::-moz-range-thumb,
    #out-slider::-moz-range-thumb {
      background: var(--sel);
      border-color: #5d4b1e;
    }
    .preview-strip {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    canvas {
      width: 100%;
      display: block;
      border-radius: 8px;
      background: #000;
    }
    .readout, .media-card, .item {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 12px;
    }
    .readout .k {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    .readout .v {
      font-size: 18px;
    }
    .hint, .meta {
      color: var(--muted);
      white-space: pre-wrap;
    }
    .err {
      color: var(--err);
      white-space: pre-wrap;
    }
    .items {
      display: grid;
      gap: 16px;
    }
    .item-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      margin-bottom: 6px;
    }
    .item h2 {
      margin: 0;
      font-size: 22px;
    }
    .chip {
      display: inline-block;
      padding: 4px 8px;
      border: 1px solid currentColor;
      border-radius: 999px;
      font-size: 12px;
      text-transform: uppercase;
    }
    .st-trimmed, .st-seeded, .st-tracked { color: var(--ok); }
    .st-seeding, .st-tracking { color: var(--accent); }
    .st-error { color: var(--err); }
    .log {
      margin: 0;
      min-height: 120px;
      max-height: 280px;
      overflow: auto;
      border-radius: 10px;
      background: #181410;
      color: #f4efe5;
      padding: 12px;
      white-space: pre-wrap;
    }
    @media (max-width: 1200px) {
      .grid, .toolbar, .microbar, .picker-row, .summary-grid, .item-grid, .item-actions { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Standalone Human Loop Labeller</h1>
    <p class="sub">import video into lab -> trim with in/out -> create clip -> run seed -> verify -> run SAM2 -> review overlay</p>

    <section class="panel">
      <div class="picker-row">
        <label>Import source video path
          <input type="text" id="import-path" placeholder="/absolute/path/to/video.mov">
        </label>
        <button id="import-video" type="button">Import Video</button>
      </div>
      <div class="hint">Only video copying may read outside `lab/`. After import, all operations stay inside `lab/standalone_workspace`.</div>
      <div class="err" id="import-error"></div>
    </section>

    <section class="panel">
      <div class="grid">
        <div class="stack">
          <div class="media-card">
            <div class="meta">Source Preview</div>
            <video id="source-video" preload="metadata"></video>
            <video id="thumb-video" preload="metadata" muted playsinline style="display:none"></video>
          </div>

          <div class="preview-strip">
            <div class="media-card">
              <div class="meta">In Preview</div>
              <canvas id="in-canvas" width="320" height="180"></canvas>
              <div class="meta" id="in-canvas-label">-</div>
            </div>
            <div class="media-card">
              <div class="meta">Out Preview</div>
              <canvas id="out-canvas" width="320" height="180"></canvas>
              <div class="meta" id="out-canvas-label">-</div>
            </div>
          </div>

          <div class="timeline-wrap">
            <div class="timeline-track" id="timeline-visual">
              <div class="timeline-badge in" id="in-badge">IN</div>
              <div class="timeline-badge out" id="out-badge">OUT</div>
              <div class="timeline-badge playhead" id="playhead-badge">PLAY</div>
            </div>
            <div class="slider-stack">
              <input id="in-slider" type="range" min="0" max="0" step="1" value="0">
              <input id="out-slider" type="range" min="0" max="0" step="1" value="0">
              <input id="playhead-slider" type="range" min="0" max="0" step="1" value="0">
            </div>
          </div>

          <div class="toolbar">
            <button class="alt" id="toggle-play" type="button">Play / Pause</button>
            <button class="alt" id="prev10" type="button">-10f</button>
            <button class="alt" id="prev1" type="button">-1f</button>
            <button class="alt" id="next1" type="button">+1f</button>
            <button class="alt" id="next10" type="button">+10f</button>
            <button id="mark-in" type="button">Mark In [I]</button>
            <button id="mark-out" type="button">Mark Out [O]</button>
            <button class="alt" id="reset-io" type="button">Reset In/Out</button>
          </div>

          <div class="microbar">
            <button class="alt" id="go-in" type="button">Go In</button>
            <button class="alt" id="go-out" type="button">Go Out</button>
            <button class="alt" id="nudge-in" type="button">In = Playhead</button>
            <button class="alt" id="nudge-out" type="button">Out = Playhead</button>
            <button class="alt" id="play-around-in" type="button">Play Around In</button>
            <button class="alt" id="play-around-out" type="button">Play Around Out</button>
          </div>

          <div class="hint">Shortcuts: `Space` play/pause, `J/L` -1/+1 frame, `Shift+J/Shift+L` -10/+10 frames, `I` mark in, `O` mark out.</div>
        </div>

        <div class="stack">
          <div class="picker-row">
            <label>Imported source video
              <select id="source-select"></select>
            </label>
            <label>In frame
              <input id="in-frame" type="number" min="0">
            </label>
            <label>Out frame
              <input id="out-frame" type="number" min="0">
            </label>
            <label>Clip slug
              <input id="clip-slug" type="text" placeholder="optional">
            </label>
            <button id="create-clip" type="button">Create Clip</button>
          </div>

          <div class="summary-grid">
            <div class="readout"><div class="k">Playhead</div><div class="v" id="playhead-readout">-</div></div>
            <div class="readout"><div class="k">In</div><div class="v" id="in-readout">-</div></div>
            <div class="readout"><div class="k">Out</div><div class="v" id="out-readout">-</div></div>
            <div class="readout"><div class="k">Clip</div><div class="v" id="clip-readout">-</div></div>
          </div>

          <div class="timecode-grid">
            <label>Playhead timecode
              <input id="playhead-timecode" type="text" placeholder="00:00:00.000">
            </label>
            <label>In timecode
              <input id="in-timecode" type="text" placeholder="00:00:00.000">
            </label>
            <label>Out timecode
              <input id="out-timecode" type="text" placeholder="00:00:00.000">
            </label>
          </div>

          <div class="meta" id="video-meta"></div>
          <div class="err" id="create-error"></div>
        </div>
      </div>
    </section>

    <section class="items" id="items"></section>
  </div>

  <script>
    const sourceVideoEl = document.getElementById("source-video");
    const thumbVideoEl = document.getElementById("thumb-video");
    const sourceSelectEl = document.getElementById("source-select");
    const importPathEl = document.getElementById("import-path");
    const importErrorEl = document.getElementById("import-error");
    const createErrorEl = document.getElementById("create-error");
    const videoMetaEl = document.getElementById("video-meta");
    const playheadSliderEl = document.getElementById("playhead-slider");
    const inSliderEl = document.getElementById("in-slider");
    const outSliderEl = document.getElementById("out-slider");
    const timelineVisualEl = document.getElementById("timeline-visual");
    const inBadgeEl = document.getElementById("in-badge");
    const outBadgeEl = document.getElementById("out-badge");
    const playheadBadgeEl = document.getElementById("playhead-badge");
    const inFrameEl = document.getElementById("in-frame");
    const outFrameEl = document.getElementById("out-frame");
    const clipSlugEl = document.getElementById("clip-slug");
    const playheadTimecodeEl = document.getElementById("playhead-timecode");
    const inTimecodeEl = document.getElementById("in-timecode");
    const outTimecodeEl = document.getElementById("out-timecode");
    const playheadReadoutEl = document.getElementById("playhead-readout");
    const inReadoutEl = document.getElementById("in-readout");
    const outReadoutEl = document.getElementById("out-readout");
    const clipReadoutEl = document.getElementById("clip-readout");
    const inCanvasEl = document.getElementById("in-canvas");
    const outCanvasEl = document.getElementById("out-canvas");
    const inCanvasLabelEl = document.getElementById("in-canvas-label");
    const outCanvasLabelEl = document.getElementById("out-canvas-label");
    const itemsEl = document.getElementById("items");
    let currentVideoMeta = null;
    let previewRenderToken = 0;
    let loopSelectionEnabled = false;
    let cutPreviewUntil = null;

    function esc(text) {
      return String(text ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `${res.status}`);
      }
      const ctype = res.headers.get("content-type") || "";
      if (ctype.includes("application/json")) return res.json();
      return null;
    }

    function formatTime(seconds) {
      const totalMs = Math.max(0, Math.round(seconds * 1000));
      const ms = totalMs % 1000;
      const totalSeconds = Math.floor(totalMs / 1000);
      const s = totalSeconds % 60;
      const totalMinutes = Math.floor(totalSeconds / 60);
      const m = totalMinutes % 60;
      const h = Math.floor(totalMinutes / 60);
      return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
    }

    function parseTimecode(text) {
      const trimmed = String(text || "").trim();
      const match = /^(\\d+):([0-5]\\d):([0-5]\\d)(?:\\.(\\d{1,3}))?$/.exec(trimmed);
      if (!match) return null;
      const hours = Number(match[1]);
      const minutes = Number(match[2]);
      const seconds = Number(match[3]);
      const millis = Number((match[4] || "0").padEnd(3, "0"));
      return (hours * 3600) + (minutes * 60) + seconds + (millis / 1000);
    }

    function frameToSeconds(frame) {
      if (!currentVideoMeta) return 0;
      return frame / currentVideoMeta.fps;
    }

    function secondsToFrame(seconds) {
      if (!currentVideoMeta) return 0;
      const frame = Math.round(seconds * currentVideoMeta.fps);
      return clampFrame(frame);
    }

    function clampFrame(frame) {
      if (!currentVideoMeta) return 0;
      return Math.max(0, Math.min(currentVideoMeta.frames - 1, frame));
    }

    function currentFrame() {
      return secondsToFrame(sourceVideoEl.currentTime || 0);
    }

    function getInFrame() {
      return clampFrame(Number(inFrameEl.value || 0));
    }

    function getOutFrame() {
      return clampFrame(Number(outFrameEl.value || 0));
    }

    function setInFrame(frame) {
      const next = clampFrame(frame);
      const out = getOutFrame();
      inFrameEl.value = String(Math.min(next, out));
    }

    function setOutFrame(frame) {
      const next = clampFrame(frame);
      const inp = getInFrame();
      outFrameEl.value = String(Math.max(next, inp));
    }

    function syncTimelineVisual() {
      if (!currentVideoMeta) return;
      const max = Math.max(1, currentVideoMeta.frames - 1);
      const playheadPct = (currentFrame() / max) * 100;
      const inPct = (getInFrame() / max) * 100;
      const outPct = (getOutFrame() / max) * 100;
      timelineVisualEl.style.setProperty("--playhead", `${playheadPct}%`);
      timelineVisualEl.style.setProperty("--sel-start", `${Math.min(inPct, outPct)}%`);
      timelineVisualEl.style.setProperty("--sel-end", `${Math.max(inPct, outPct)}%`);
      inBadgeEl.style.left = `${inPct}%`;
      outBadgeEl.style.left = `${outPct}%`;
      playheadBadgeEl.style.left = `${playheadPct}%`;
      inBadgeEl.textContent = `IN ${getInFrame()}`;
      outBadgeEl.textContent = `OUT ${getOutFrame()}`;
      playheadBadgeEl.textContent = `PLAY ${currentFrame()}`;
    }

    function frameLabel(frame) {
      return `f${frame} | ${formatTime(frameToSeconds(frame))}`;
    }

    function renderTrimSummary() {
      if (!currentVideoMeta) {
        playheadReadoutEl.textContent = "-";
        inReadoutEl.textContent = "-";
        outReadoutEl.textContent = "-";
        clipReadoutEl.textContent = "-";
        return;
      }
      const playhead = currentFrame();
      const inFrame = getInFrame();
      const outFrame = getOutFrame();
      inSliderEl.value = String(inFrame);
      outSliderEl.value = String(outFrame);
      playheadReadoutEl.textContent = frameLabel(playhead);
      inReadoutEl.textContent = frameLabel(inFrame);
      outReadoutEl.textContent = frameLabel(outFrame);
      playheadTimecodeEl.value = formatTime(frameToSeconds(playhead));
      inTimecodeEl.value = formatTime(frameToSeconds(inFrame));
      outTimecodeEl.value = formatTime(frameToSeconds(outFrame));
      if (outFrame >= inFrame) {
        const frames = outFrame - inFrame + 1;
        clipReadoutEl.textContent = `${frames}f | ${formatTime((frames - 1) / currentVideoMeta.fps)} | ${loopSelectionEnabled ? "loop on" : "loop off"}`;
      } else {
        clipReadoutEl.textContent = "invalid";
      }
      syncTimelineVisual();
      queueEndpointPreviewRefresh();
    }

    function seekFrame(frame) {
      if (!currentVideoMeta) return;
      const clamped = clampFrame(frame);
      sourceVideoEl.currentTime = frameToSeconds(clamped);
      playheadSliderEl.value = String(clamped);
      renderTrimSummary();
    }

    function offsetPlayhead(delta) {
      seekFrame(currentFrame() + delta);
    }

    function playAroundFrame(frame) {
      if (!currentVideoMeta) return;
      const start = clampFrame(frame - 12);
      const end = clampFrame(frame + 12);
      cutPreviewUntil = end;
      seekFrame(start);
      sourceVideoEl.play();
    }

    function seekEndpointPreview(which, frame) {
      const clamped = clampFrame(frame);
      if (which === "in") {
        inCanvasLabelEl.textContent = `${frameLabel(clamped)} | preview`;
      } else {
        outCanvasLabelEl.textContent = `${frameLabel(clamped)} | preview`;
      }
      queueEndpointPreviewRefresh();
    }

    function validClipSelection() {
      if (!currentVideoMeta) return false;
      return getOutFrame() >= getInFrame();
    }

    function updateCreateButtonState() {
      document.getElementById("create-clip").disabled = !validClipSelection();
    }

    function applyTimecodeToFrame(target, text) {
      const seconds = parseTimecode(text);
      if (seconds === null) return false;
      const frame = secondsToFrame(seconds);
      if (target === "playhead") seekFrame(frame);
      else if (target === "in") setInFrame(frame);
      else if (target === "out") setOutFrame(frame);
      renderTrimSummary();
      updateCreateButtonState();
      return true;
    }

    function renderItem(item) {
      const running = item.status === "seeding" || item.status === "tracking";
      const clipBody = item.clip_url
        ? `<video controls preload="metadata" src="${esc(item.clip_url)}"></video>`
        : `<div class="meta">clip pending</div>`;
      const seedBody = item.seed_preview_url
        ? `<img src="${esc(item.seed_preview_url)}?t=${Date.now()}" alt="seed preview">`
        : `<div class="meta">seed preview pending</div>`;
      const overlayBody = item.overlay_url
        ? `<video controls preload="metadata" src="${esc(item.overlay_url)}"></video>`
        : `<div class="meta">overlay pending</div>`;
      return `
        <article class="item">
          <div class="item-head">
            <div>
              <h2>${esc(item.slug)}</h2>
              <div class="meta">${esc(item.source_video_name)} | frames ${item.start_frame}-${item.end_frame} | ${esc(item.clip_summary ?? "")}</div>
            </div>
            <span class="chip st-${esc(item.status)}">${esc(item.status)}</span>
          </div>
          <div class="item-grid">
            <div class="media-card"><div class="meta">Clip</div>${clipBody}</div>
            <div class="media-card"><div class="meta">Seed</div>${seedBody}</div>
            <div class="media-card"><div class="meta">Overlay</div>${overlayBody}</div>
          </div>
          <div class="item-actions">
            <button data-action="seed" data-id="${esc(item.id)}" ${running ? "disabled" : ""}>Run Seed</button>
            <label>Seed frame override
              <input type="number" min="0" data-seed-frame="${esc(item.id)}" value="${item.seed_frame_override ?? ""}">
            </label>
            <button class="alt" data-action="track" data-id="${esc(item.id)}" ${item.status !== "seeded" ? "disabled" : ""}>Run SAM2</button>
            <div class="meta">${esc(item.summary_line ?? "")}<br><button class="alt" data-action="delete" data-id="${esc(item.id)}" ${running ? "disabled" : ""}>Delete</button></div>
          </div>
          <pre class="log">${esc(item.log_tail ?? "")}</pre>
          ${item.error ? `<div class="err">${esc(item.error)}</div>` : ""}
        </article>
      `;
    }

    async function refreshItems() {
      const data = await api("/api/state");
      itemsEl.innerHTML = data.items.map(renderItem).join("");
    }

    async function refreshVideos() {
      const data = await api("/api/videos");
      const previous = sourceSelectEl.value;
      sourceSelectEl.innerHTML = data.videos.map((row) =>
        `<option value="${esc(row.path)}">${esc(row.name)} | ${row.frames}f | ${row.duration_s.toFixed(3)}s</option>`
      ).join("");
      if (previous) sourceSelectEl.value = previous;
      if (!sourceSelectEl.value && data.videos.length > 0) sourceSelectEl.value = data.videos[0].path;
      await loadCurrentVideo();
    }

    async function loadCurrentVideo() {
      const path = sourceSelectEl.value;
      if (!path) {
        currentVideoMeta = null;
        sourceVideoEl.removeAttribute("src");
        videoMetaEl.textContent = "No imported videos yet.";
        renderTrimSummary();
        updateCreateButtonState();
        return;
      }
      currentVideoMeta = await api(`/api/video_meta?path=${encodeURIComponent(path)}`);
      sourceVideoEl.src = `/media/source?path=${encodeURIComponent(path)}`;
      thumbVideoEl.src = sourceVideoEl.src;
      const max = currentVideoMeta.frames - 1;
      playheadSliderEl.max = String(max);
      inSliderEl.max = String(max);
      outSliderEl.max = String(max);
      inFrameEl.max = String(max);
      outFrameEl.max = String(max);
      if (inFrameEl.value === "") inFrameEl.value = "0";
      if (outFrameEl.value === "") outFrameEl.value = String(max);
      setInFrame(Number(inFrameEl.value));
      setOutFrame(Number(outFrameEl.value));
      videoMetaEl.textContent = `${currentVideoMeta.name} | ${currentVideoMeta.frames} frames | ${currentVideoMeta.fps.toFixed(3)} fps | ${formatTime(currentVideoMeta.duration_s)} total`;
      playheadSliderEl.value = "0";
      sourceVideoEl.currentTime = 0;
      renderTrimSummary();
      updateCreateButtonState();
    }

    async function importVideo() {
      importErrorEl.textContent = "";
      try {
        await api("/api/import_video", {
          method: "POST",
          body: JSON.stringify({ source_path: importPathEl.value }),
        });
        importPathEl.value = "";
        await refreshVideos();
      } catch (err) {
        importErrorEl.textContent = err.message;
      }
    }

    async function createClip() {
      createErrorEl.textContent = "";
      try {
        await api("/api/create_clip", {
          method: "POST",
          body: JSON.stringify({
            source_video: sourceSelectEl.value,
            start_frame: getInFrame(),
            end_frame: getOutFrame(),
            slug: clipSlugEl.value || null,
          }),
        });
        clipSlugEl.value = "";
        await refreshItems();
      } catch (err) {
        createErrorEl.textContent = err.message;
      }
    }

    function blankCanvas(canvas) {
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }

    function drawFrameToCanvas(canvas, video) {
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    }

    function seekVideoOnce(video, seconds) {
      return new Promise((resolve) => {
        const onSeeked = () => {
          video.removeEventListener("seeked", onSeeked);
          resolve();
        };
        video.addEventListener("seeked", onSeeked, { once: true });
        video.currentTime = seconds;
      });
    }

    async function renderEndpointPreview(canvas, labelEl, frame) {
      if (!currentVideoMeta || !thumbVideoEl.src) {
        blankCanvas(canvas);
        labelEl.textContent = "-";
        return;
      }
      await seekVideoOnce(thumbVideoEl, frameToSeconds(frame));
      drawFrameToCanvas(canvas, thumbVideoEl);
      labelEl.textContent = frameLabel(frame);
    }

    async function queueEndpointPreviewRefresh() {
      const token = ++previewRenderToken;
      if (!currentVideoMeta) {
        blankCanvas(inCanvasEl);
        blankCanvas(outCanvasEl);
        inCanvasLabelEl.textContent = "-";
        outCanvasLabelEl.textContent = "-";
        return;
      }
      if (thumbVideoEl.readyState < 1) return;
      const inFrame = getInFrame();
      const outFrame = getOutFrame();
      await renderEndpointPreview(inCanvasEl, inCanvasLabelEl, inFrame);
      if (token !== previewRenderToken) return;
      await renderEndpointPreview(outCanvasEl, outCanvasLabelEl, outFrame);
    }

    document.getElementById("import-video").addEventListener("click", importVideo);
    document.getElementById("create-clip").addEventListener("click", createClip);
    sourceSelectEl.addEventListener("change", loadCurrentVideo);
    playheadSliderEl.addEventListener("input", () => seekFrame(Number(playheadSliderEl.value)));
    inSliderEl.addEventListener("input", () => {
      setInFrame(Number(inSliderEl.value));
      seekEndpointPreview("in", getInFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    outSliderEl.addEventListener("input", () => {
      setOutFrame(Number(outSliderEl.value));
      seekEndpointPreview("out", getOutFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    sourceVideoEl.addEventListener("timeupdate", () => {
      if (loopSelectionEnabled && currentVideoMeta && validClipSelection() && currentFrame() >= getOutFrame()) {
        sourceVideoEl.currentTime = frameToSeconds(getInFrame());
        playheadSliderEl.value = String(getInFrame());
      }
      if (cutPreviewUntil !== null && currentFrame() >= cutPreviewUntil) {
        sourceVideoEl.pause();
        cutPreviewUntil = null;
      }
      playheadSliderEl.value = String(currentFrame());
      renderTrimSummary();
    });
    thumbVideoEl.addEventListener("loadedmetadata", () => { queueEndpointPreviewRefresh(); });
    inFrameEl.addEventListener("input", () => {
      setInFrame(Number(inFrameEl.value));
      renderTrimSummary();
      updateCreateButtonState();
    });
    outFrameEl.addEventListener("input", () => {
      setOutFrame(Number(outFrameEl.value));
      renderTrimSummary();
      updateCreateButtonState();
    });
    document.getElementById("toggle-play").addEventListener("click", () => {
      if (sourceVideoEl.paused) sourceVideoEl.play();
      else sourceVideoEl.pause();
    });
    const loopBtn = document.createElement("button");
    loopBtn.type = "button";
    loopBtn.className = "alt";
    loopBtn.id = "toggle-loop";
    loopBtn.textContent = "Loop Selection";
    document.querySelector(".microbar").appendChild(loopBtn);
    loopBtn.addEventListener("click", () => {
      loopSelectionEnabled = !loopSelectionEnabled;
      loopBtn.textContent = loopSelectionEnabled ? "Loop Selection: On" : "Loop Selection";
      renderTrimSummary();
    });
    const reviewBtn = document.createElement("button");
    reviewBtn.type = "button";
    reviewBtn.className = "alt";
    reviewBtn.id = "review-selection";
    reviewBtn.textContent = "Review Selection";
    document.querySelector(".microbar").appendChild(reviewBtn);
    reviewBtn.addEventListener("click", () => {
      if (!currentVideoMeta || !validClipSelection()) return;
      loopSelectionEnabled = true;
      loopBtn.textContent = "Loop Selection: On";
      seekFrame(getInFrame());
      sourceVideoEl.play();
      renderTrimSummary();
    });
    document.getElementById("prev10").addEventListener("click", () => offsetPlayhead(-10));
    document.getElementById("prev1").addEventListener("click", () => offsetPlayhead(-1));
    document.getElementById("next1").addEventListener("click", () => offsetPlayhead(1));
    document.getElementById("next10").addEventListener("click", () => offsetPlayhead(10));
    document.getElementById("mark-in").addEventListener("click", () => {
      setInFrame(currentFrame());
      seekEndpointPreview("in", getInFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    document.getElementById("mark-out").addEventListener("click", () => {
      setOutFrame(currentFrame());
      seekEndpointPreview("out", getOutFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    document.getElementById("reset-io").addEventListener("click", () => {
      if (!currentVideoMeta) return;
      setInFrame(0);
      setOutFrame(currentVideoMeta.frames - 1);
      renderTrimSummary();
      updateCreateButtonState();
    });
    document.getElementById("go-in").addEventListener("click", () => seekFrame(getInFrame()));
    document.getElementById("go-out").addEventListener("click", () => seekFrame(getOutFrame()));
    document.getElementById("play-around-in").addEventListener("click", () => playAroundFrame(getInFrame()));
    document.getElementById("play-around-out").addEventListener("click", () => playAroundFrame(getOutFrame()));
    document.getElementById("nudge-in").addEventListener("click", () => {
      setInFrame(currentFrame());
      seekEndpointPreview("in", getInFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    document.getElementById("nudge-out").addEventListener("click", () => {
      setOutFrame(currentFrame());
      seekEndpointPreview("out", getOutFrame());
      renderTrimSummary();
      updateCreateButtonState();
    });
    playheadTimecodeEl.addEventListener("change", () => { applyTimecodeToFrame("playhead", playheadTimecodeEl.value); });
    inTimecodeEl.addEventListener("change", () => { applyTimecodeToFrame("in", inTimecodeEl.value); });
    outTimecodeEl.addEventListener("change", () => { applyTimecodeToFrame("out", outTimecodeEl.value); });

    document.addEventListener("keydown", (event) => {
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (event.code === "Space") {
        event.preventDefault();
        document.getElementById("toggle-play").click();
      } else if (event.key === "j") {
        event.preventDefault();
        offsetPlayhead(event.shiftKey ? -10 : -1);
      } else if (event.key === "l") {
        event.preventDefault();
        offsetPlayhead(event.shiftKey ? 10 : 1);
      } else if (event.key === "i") {
        event.preventDefault();
        document.getElementById("mark-in").click();
      } else if (event.key === "o") {
        event.preventDefault();
        document.getElementById("mark-out").click();
      }
    });

    itemsEl.addEventListener("click", async (event) => {
      const btn = event.target.closest("button[data-action]");
      if (!btn) return;
      const itemId = btn.dataset.id;
      const action = btn.dataset.action;
      if (action === "delete" && !confirm("Delete this clip and outputs?")) return;
      const seedInput = document.querySelector(`[data-seed-frame="${itemId}"]`);
      const payload = {};
      if (seedInput && seedInput.value !== "") payload.seed_frame = Number(seedInput.value);
      try {
        if (action === "delete") {
          await api(`/api/items/${itemId}`, { method: "DELETE" });
        } else {
          await api(`/api/items/${itemId}/${action}`, {
            method: "POST",
            body: JSON.stringify(payload),
          });
        }
        await refreshItems();
      } catch (err) {
        alert(err.message);
      }
    });

    refreshVideos().then(refreshItems);
    setInterval(refreshItems, 2000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict[str, Any]:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_file(self, path: Path) -> None:
        if not path.exists():
            self._text(HTTPStatus.NOT_FOUND, "missing file")
            return
        ctype = "application/octet-stream"
        suffix = path.suffix.lower()
        if suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif suffix in {".jpg", ".jpeg"}:
            ctype = "image/jpeg"
        elif suffix == ".png":
            ctype = "image/png"
        elif suffix == ".mov":
            ctype = "video/quicktime"
        elif suffix == ".mp4":
            ctype = "video/mp4"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._html(render_html())
                return
            if parsed.path == "/api/videos":
                self._json(HTTPStatus.OK, {"videos": STORE.list_videos()})
                return
            if parsed.path == "/api/video_meta":
                path = Path(query["path"][0])
                meta = _video_meta(path)
                self._json(
                    HTTPStatus.OK,
                    {
                        "name": path.name,
                        "path": str(path),
                        "frames": int(meta["frames"]),
                        "fps": meta["fps"],
                        "duration_s": meta["duration_s"],
                    },
                )
                return
            if parsed.path == "/api/state":
                self._json(HTTPStatus.OK, {"items": STORE.items()})
                return
            if parsed.path == "/media/source":
                path = Path(query["path"][0])
                if path.parent != SOURCE_VIDEOS_DIR:
                    self._text(HTTPStatus.BAD_REQUEST, "source video must live in standalone workspace")
                    return
                self._send_file(path)
                return
            if parsed.path.startswith("/media/item/"):
                parts = parsed.path.strip("/").split("/")
                item_id = parts[2]
                kind = parts[3]
                item = STORE.get_item(item_id)
                if kind == "clip":
                    self._send_file(Path(item["clip_path"]))
                    return
                if kind == "seed":
                    self._send_file(Path(item["seed_run_dir"]) / "previews" / "seed_grounding.jpg")
                    return
                if kind == "overlay":
                    self._send_file(Path(item["seed_run_dir"]) / "tracking_overlay.mp4")
                    return
            self._text(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self._text(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self._body_json()
            if parsed.path == "/api/import_video":
                imported = STORE.import_video(Path(payload["source_path"]))
                self._json(HTTPStatus.OK, imported)
                return
            if parsed.path == "/api/create_clip":
                item = STORE.create_item(
                    source_video=Path(payload["source_video"]),
                    start_frame=int(payload["start_frame"]),
                    end_frame=int(payload["end_frame"]),
                    slug=payload.get("slug"),
                )
                self._json(HTTPStatus.OK, {"item_id": item["id"]})
                return
            if parsed.path.startswith("/api/items/"):
                parts = parsed.path.strip("/").split("/")
                item_id = parts[2]
                action = parts[3]
                if action == "seed":
                    STORE.start_seed(item_id, payload.get("seed_frame"))
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if action == "track":
                    STORE.start_track(item_id)
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
            self._text(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self._text(HTTPStatus.BAD_REQUEST, str(exc))

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path.startswith("/api/items/"):
                item_id = parsed.path.strip("/").split("/")[2]
                STORE.delete_item(item_id)
                self._json(HTTPStatus.OK, {"ok": True})
                return
            self._text(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self._text(HTTPStatus.BAD_REQUEST, str(exc))

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    port = int(os.environ.get("LABELLER_PORT", "8876"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Standalone labeller: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
