"""SAM 3 video labeller wrapper.

Bridges the offline `tools/` venv (torch + transformers main @ Nov 2025+)
to the production `server/` schemas and video decoder. Loaded only by
the scripts under `server/scripts/`; production server boot never
imports this module — torch / transformers are NOT in
`server/pyproject.toml`.

Why not import facebookresearch/sam3 directly:
  Official repo hard-requires Triton (CUDA-only) for Euclidean Distance
  Transform. Refuses to load on Apple Silicon. The HuggingFace
  transformers port has no Triton / flash-attn deps and works on MPS.
  See `tools/README.md` for the full rationale.

Why monkey-patch the MPS pin_memory bug at runtime:
  `transformers.models.sam3_video.processing_sam3_video.batched_mask_to_box`
  (or its caller) does `tensor.pin_memory().to(device)` which silently
  fails on MPS. The fix is one line — drop the `pin_memory()` call.
  Patching at runtime here means operators don't have to maintain a
  fork or remember to edit `site-packages/`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from schemas import SAM3GTFrame, SAM3GTRecord
from video import iter_frames, probe_dims

logger = logging.getLogger(__name__)


# ----- Device selection ------------------------------------------------


def _select_device(requested: str = "auto") -> str:
    """Resolve "auto" → best available device for the local box.

    Order: cuda → mps → cpu. We don't try to be clever about multi-GPU;
    SAM 3 inference is single-device anyway. Explicit non-"auto" values
    short-circuit before importing torch — that lets the test suite
    construct a `Sam3VideoLabeller(device="cpu")` in the server venv
    (no torch installed) without tripping ImportError."""
    if requested != "auto":
        return requested
    import torch  # local: torch is a tools-venv dep, not a server-venv dep

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _apply_mps_patch_if_needed(device: str) -> None:
    """Monkey-patch `pin_memory()` calls on MPS — no-op on cuda / cpu.

    The bug: `processing_sam3_video.py` (HF transformers main @ Nov 2025)
    calls `keep_idx.pin_memory().to(device)`. On CUDA pin_memory pages
    the host buffer for fast async copies. On MPS pin_memory returns a
    tensor whose subsequent `.to()` raises a device-mismatch error
    because the storage is interpreted as CPU but the device is MPS.

    Workaround: replace `Tensor.pin_memory` with a no-op identity
    function ONLY on MPS. CUDA users keep the fast path."""
    if device != "mps":
        return
    import torch

    if getattr(torch.Tensor, "_ball_tracker_pin_memory_patched", False):
        return
    original_pin_memory = torch.Tensor.pin_memory

    def _no_op_pin_memory(self, *args: Any, **kwargs: Any) -> "torch.Tensor":  # type: ignore[name-defined]
        return self

    torch.Tensor.pin_memory = _no_op_pin_memory  # type: ignore[method-assign]
    torch.Tensor._ball_tracker_pin_memory_patched = True  # type: ignore[attr-defined]
    logger.info("Sam3VideoLabeller: applied MPS pin_memory() no-op patch")


# ----- Mask analysis ---------------------------------------------------


@dataclass
class _MaskStats:
    """Derived per-frame mask quantities. Kept as a plain dataclass so
    the test suite can construct stats without instantiating Pydantic."""
    bbox: tuple[float, float, float, float]
    centroid_px: tuple[float, float]
    area_px: int
    aspect: float
    fill: float
    hue_mean: float
    hue_std: float
    sat_mean: float
    val_mean: float


def analyze_mask(mask: np.ndarray, bgr: np.ndarray) -> _MaskStats | None:
    """Compute the per-frame statistics our distillation pipeline needs.

    `mask` is HxW uint8 / bool (255 / True = inside ball). `bgr` is the
    matching HxWx3 uint8 frame (same resolution). Returns None when the
    mask is empty — caller should treat this as a non-detection.

    HSV is computed on the **same H.264-decoded BGR** that server_post
    sees, so the (h, s, v) distributions can be fit directly into
    `data/hsv_range.json` without an extra colour-space conversion gap."""
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    # Normalise to {0, 255} so opencv ops behave uniformly regardless
    # of whether the caller passed a bool mask or a 0/1 mask.
    mask_bin = np.where(mask > 0, np.uint8(255), np.uint8(0))
    area = int(np.count_nonzero(mask_bin))
    if area == 0:
        return None
    ys, xs = np.where(mask_bin > 0)
    x_min, x_max = float(xs.min()), float(xs.max())
    y_min, y_max = float(ys.min()), float(ys.max())
    bbox_w = max(x_max - x_min + 1.0, 1.0)
    bbox_h = max(y_max - y_min + 1.0, 1.0)
    aspect = float(min(bbox_w, bbox_h) / max(bbox_w, bbox_h))
    fill = float(area / (bbox_w * bbox_h))
    centroid = (float(xs.mean()), float(ys.mean()))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue_pixels = hsv[..., 0][mask_bin > 0]
    sat_pixels = hsv[..., 1][mask_bin > 0]
    val_pixels = hsv[..., 2][mask_bin > 0]
    return _MaskStats(
        bbox=(x_min, y_min, x_max, y_max),
        centroid_px=centroid,
        area_px=area,
        aspect=aspect,
        fill=fill,
        hue_mean=float(hue_pixels.mean()),
        hue_std=float(hue_pixels.std()),
        sat_mean=float(sat_pixels.mean()),
        val_mean=float(val_pixels.mean()),
    )


# ----- Labeller --------------------------------------------------------


class Sam3VideoLabeller:
    """Wraps `Sam3VideoModel` + `Sam3VideoProcessor` with our project's
    BGR frame iterator + GT JSON output schema.

    The model + processor are loaded lazily on first `label_video()` call
    so importing this module is cheap (no torch graph build, no weights
    download). Keeps unit tests fast.
    """

    DEFAULT_MODEL_ID = "facebook/sam3"

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = "auto",
        image_size: int = 1008,
    ):
        self.model_id = model_id
        self.device = _select_device(device)
        self.image_size = image_size
        self._model: Any | None = None
        self._processor: Any | None = None
        self._model_version: str | None = None

    def load(self) -> None:
        """Lazy load. Idempotent — second call is a no-op."""
        if self._model is not None and self._processor is not None:
            return
        _apply_mps_patch_if_needed(self.device)
        # Imports are local: tests stub the module BEFORE this method
        # is called, so torch / transformers stay un-imported in the
        # test path.
        import torch
        from transformers import (
            Sam3VideoConfig,
            Sam3VideoModel,
            Sam3VideoProcessor,
        )

        dtype = torch.bfloat16 if self.device != "cpu" else torch.float32
        config = Sam3VideoConfig.from_pretrained(self.model_id)
        if self.image_size != getattr(config, "image_size", None):
            config.image_size = self.image_size
        model = Sam3VideoModel.from_pretrained(self.model_id, config=config)
        model = model.to(self.device, dtype=dtype)
        model.eval()
        processor = Sam3VideoProcessor.from_pretrained(
            self.model_id,
            size={"height": self.image_size, "width": self.image_size},
        )
        self._model = model
        self._processor = processor
        self._model_version = (
            f"{self.model_id} (image_size={self.image_size}, dtype={dtype}, device={self.device})"
        )
        logger.info("Sam3VideoLabeller loaded: %s", self._model_version)

    def label_video(
        self,
        mov_path: Path,
        video_start_pts_s: float,
        session_id: str,
        camera_id: str,
        prompt: str = "blue ball",
        min_confidence: float = 0.5,
        max_frames: int | None = None,
        time_range: tuple[float, float] | None = None,
        progress_callback: Callable[[int, int, float], None] | None = None,
        preview_callback: Callable[[int, np.ndarray, np.ndarray], None] | None = None,
    ) -> SAM3GTRecord:
        """Decode the MOV, run SAM 3 video propagation with the text
        prompt, and return a `SAM3GTRecord`.

        - SAM 3 may detect multiple objects per frame (open-vocab text
          prompts can hit on similar-coloured background). For our
          single-ball scene we pick the **highest-score object per
          frame** above `min_confidence`. Lower-score detections are
          silently dropped.
        - Frames where no object meets the threshold are omitted from
          `frames` — distillation treats absence as ground-truth miss.
        - `max_frames` clamps the propagation window for dev iteration.
          None means full video.
        - `time_range` is **video-relative seconds** (`[t_start, t_end]`,
          where `t = absolute_pts_s − video_start_pts_s`). Only frames
          whose video-relative PTS falls inside the window are kept.
          Mutually exclusive with `max_frames` (raises ValueError).
        - `progress_callback(current, total, ms_per_frame)` is invoked
          synchronously after each propagation step. The CLI driver
          turns it into stderr `PROGRESS:` lines for the queue worker.
        - `preview_callback(frame_idx, bgr, mask)` is invoked
          synchronously after each propagation step that produced a
          mask above `min_confidence`. `mask` is the chosen object's
          binary mask (uint8 0/255). The CLI driver writes a JPEG
          overlay so the operator can confirm SAM 3 is tracking the
          right object before the run finishes.
        """
        if max_frames is not None and time_range is not None:
            raise ValueError("max_frames and time_range are mutually exclusive")

        self.load()
        assert self._model is not None and self._processor is not None

        dims = probe_dims(mov_path)
        if dims is None:
            raise RuntimeError(f"could not probe dims for {mov_path}")

        # Pre-load all frames + their absolute PTS. We need the BGR
        # buffers anyway to compute mask_hue_* etc, so caching them is
        # not extra cost. `time_range` filters here; `max_frames` clamps.
        t_start_abs = (
            video_start_pts_s + time_range[0] if time_range is not None else None
        )
        t_end_abs = (
            video_start_pts_s + time_range[1] if time_range is not None else None
        )
        frame_bgrs: list[np.ndarray] = []
        frame_pts: list[float] = []
        for absolute_pts_s, bgr in iter_frames(mov_path, video_start_pts_s):
            if time_range is not None:
                if absolute_pts_s < t_start_abs:
                    continue
                if absolute_pts_s > t_end_abs:
                    break
            frame_bgrs.append(bgr)
            frame_pts.append(absolute_pts_s)
            if max_frames is not None and len(frame_bgrs) >= max_frames:
                break

        if not frame_bgrs:
            raise RuntimeError(
                f"no decodable frames in {mov_path}"
                + (f" within time_range {time_range}" if time_range else "")
            )

        video_fps = (
            (len(frame_pts) - 1) / (frame_pts[-1] - frame_pts[0])
            if len(frame_pts) > 1 and frame_pts[-1] > frame_pts[0]
            else 0.0
        )

        # SAM 3 video processor expects RGB frames (via PIL). Convert
        # once; iter_frames gives us BGR for cv2 compatibility.
        frame_rgbs = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frame_bgrs]

        session = self._processor.init_video_session(
            video=frame_rgbs,
            inference_device=self.device,
            processing_device="cpu",
            video_storage_device="cpu",
            dtype=self._dtype_for_device(),
        )
        self._processor.add_text_prompt(
            inference_session=session,
            text=prompt,
        )

        gt_frames: list[SAM3GTFrame] = []
        total_frames = len(frame_rgbs)
        ema_ms_per_frame: float | None = None
        last_tick = time.monotonic()
        for model_outputs in self._model.propagate_in_video_iterator(
            inference_session=session,
            max_frame_num_to_track=total_frames - 1,
            show_progress_bar=False,  # CLI emits its own PROGRESS lines
        ):
            processed = self._processor.postprocess_outputs(
                session, model_outputs
            )
            frame_idx = int(model_outputs.frame_idx)

            # Per-frame elapsed → exponentially-weighted moving avg so
            # the CLI's `ms_per_frame` field doesn't yo-yo on warmup.
            now = time.monotonic()
            sample_ms = (now - last_tick) * 1000.0
            last_tick = now
            if ema_ms_per_frame is None:
                ema_ms_per_frame = sample_ms
            else:
                ema_ms_per_frame = 0.7 * ema_ms_per_frame + 0.3 * sample_ms

            scores = processed.get("scores")
            masks = processed.get("masks")
            if scores is None or masks is None or len(scores) == 0:
                if progress_callback is not None:
                    progress_callback(frame_idx + 1, total_frames, ema_ms_per_frame)
                continue
            scores_np = scores.detach().cpu().float().numpy()
            best_idx = int(np.argmax(scores_np))
            best_score = float(scores_np[best_idx])
            if best_score < min_confidence:
                if progress_callback is not None:
                    progress_callback(frame_idx + 1, total_frames, ema_ms_per_frame)
                continue
            best_mask = masks[best_idx].detach().cpu().numpy()
            stats = analyze_mask(best_mask, frame_bgrs[frame_idx])
            if stats is None:
                # Mask was non-empty in score sort but degenerated after
                # binary threshold (rare edge). Treat as miss.
                if progress_callback is not None:
                    progress_callback(frame_idx + 1, total_frames, ema_ms_per_frame)
                continue
            gt_frames.append(SAM3GTFrame(
                frame_idx=frame_idx,
                t_pts_s=frame_pts[frame_idx],
                bbox=stats.bbox,
                centroid_px=stats.centroid_px,
                mask_area_px=stats.area_px,
                mask_aspect=stats.aspect,
                mask_fill=stats.fill,
                mask_hue_mean=stats.hue_mean,
                mask_hue_std=stats.hue_std,
                mask_sat_mean=stats.sat_mean,
                mask_val_mean=stats.val_mean,
                confidence=best_score,
            ))
            if preview_callback is not None:
                # Pass a normalised 0/255 uint8 mask so callers don't
                # have to second-guess dtype; matches what analyze_mask
                # internally normalises to.
                m_u8 = best_mask.astype(np.uint8)
                m_u8 = np.where(m_u8 > 0, np.uint8(255), np.uint8(0))
                try:
                    preview_callback(frame_idx, frame_bgrs[frame_idx], m_u8)
                except Exception as e:
                    logger.warning("preview_callback raised: %s", e)
            if progress_callback is not None:
                progress_callback(frame_idx + 1, total_frames, ema_ms_per_frame)

        gt_frames.sort(key=lambda f: f.frame_idx)
        return SAM3GTRecord(
            session_id=session_id,
            camera_id=camera_id,
            model_version=self._model_version or self.model_id,
            labelled_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            prompt_strategy=f"text:{prompt!r}",
            video_fps=video_fps,
            video_dims=dims,
            frames=gt_frames,
            frames_decoded=len(frame_bgrs),
            frames_labelled=len(gt_frames),
            min_confidence=min_confidence,
        )

    def _dtype_for_device(self) -> "Any":
        """Pick the appropriate torch dtype for the active device.
        Local import so tests don't need torch installed."""
        import torch
        return torch.bfloat16 if self.device != "cpu" else torch.float32
