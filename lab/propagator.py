from __future__ import annotations

import io
from pathlib import Path
from threading import Lock
from typing import Iterator

import numpy as np
from PIL import Image


def _pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class Propagator:
    """Wraps SAM 2 video predictor as a generator yielding (local_frame_idx, mask_png_bytes)."""

    def __init__(self, model_id: str = "facebook/sam2-hiera-tiny") -> None:
        from sam2.build_sam import build_sam2_video_predictor_hf

        self.device = _pick_device()
        self.model_id = model_id
        self._predictor = build_sam2_video_predictor_hf(model_id, device=self.device)
        self._lock = Lock()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def reset_cancel(self) -> None:
        self._cancel = False

    def propagate(
        self,
        frames_dir: Path,
        seed_local_idx: int,
        seed_point: tuple[int, int],
        offload_video_to_cpu: bool = True,
    ) -> Iterator[tuple[int, bytes]]:
        """
        Run forward then reverse propagation from the seed frame.

        Yields (local_frame_idx, mask_png_bytes) for each propagated frame in the order
        SAM 2 emits them. `frames_dir` must contain JPEGs named 00000.jpg, 00001.jpg, ...
        """
        x, y = seed_point
        with self._lock:
            self.reset_cancel()
            state = self._predictor.init_state(
                video_path=str(frames_dir),
                offload_video_to_cpu=offload_video_to_cpu,
            )
            self._predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=seed_local_idx,
                obj_id=1,
                points=np.array([[x, y]], dtype=np.float32),
                labels=np.array([1], dtype=np.int32),
            )

            for reverse in (False, True):
                for out_frame_idx, _obj_ids, out_mask_logits in self._predictor.propagate_in_video(
                    state, start_frame_idx=seed_local_idx, reverse=reverse
                ):
                    if self._cancel:
                        return
                    mask = (out_mask_logits[0] > 0).cpu().numpy().astype(np.uint8) * 255
                    if mask.ndim == 3:
                        mask = mask[0]
                    buf = io.BytesIO()
                    Image.fromarray(mask, mode="L").save(buf, format="PNG", optimize=False)
                    yield int(out_frame_idx), buf.getvalue()
