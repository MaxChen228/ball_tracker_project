"""Per-camera chain filter that annotates ``FramePayload.filter_status``.

Two noise classes are targeted (see analysis in the repo's memory):

- **Flicker**: a detection that appears in 1-2 frames then disappears.
  Surfaces as a "chain" shorter than ``min_run_len``.
- **Ray-direction jump**: consecutive detections whose pixel jump exceeds
  ``max_jump_px`` or whose frame-index gap exceeds ``max_frame_gap``. The
  filter starts a new chain at the jump; whichever side ends up below
  ``min_run_len`` is then flagged as ``rejected_jump`` (the jump is what
  stranded it, not its intrinsic length).

Stationary false positives (a third noise class seen in live sessions) are
NOT filtered here; they need a spatial-spread gate that we've deferred.

Non-detected frames (``ball_detected=False``) are skipped entirely and keep
``filter_status = None`` so the viewer can distinguish "the detector saw
nothing" from "the detector saw something and we rejected it"."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from schemas import FramePayload


@dataclass(frozen=True)
class ChainFilterParams:
    max_frame_gap: int = 15
    max_jump_px: float = 160.0
    min_run_len: int = 10


DEFAULT_PARAMS = ChainFilterParams()


def annotate(frames: Sequence[FramePayload], params: ChainFilterParams = DEFAULT_PARAMS) -> None:
    """Mutate each detection frame's ``filter_status`` in place.

    The input list may contain non-detections; order does not matter — we
    re-sort by ``frame_index`` for chain walking. Non-detection frames are
    left untouched."""
    detections = [f for f in frames if f.ball_detected and f.px is not None and f.py is not None]
    detections.sort(key=lambda f: f.frame_index)

    # Walk and split into chains. Each break records WHY it broke so the
    # short side of the break can be tagged rejected_jump vs rejected_flicker.
    chains: list[list[FramePayload]] = []
    break_reasons: list[str] = []  # reason that ENDED the previous chain
    cur: list[FramePayload] = []
    for f in detections:
        if cur:
            prev = cur[-1]
            gap = f.frame_index - prev.frame_index
            jump = math.hypot((f.px or 0) - (prev.px or 0), (f.py or 0) - (prev.py or 0))
            if gap > params.max_frame_gap or jump > params.max_jump_px:
                chains.append(cur)
                # If the split was caused by a pixel jump (not just a long
                # quiet gap), the short side is "rejected_jump". A pure gap
                # (detector just stopped firing for a while) doesn't implicate
                # jump; short sides there are flickers.
                break_reasons.append("jump" if jump > params.max_jump_px else "gap")
                cur = []
        cur.append(f)
    if cur:
        chains.append(cur)

    # Tag each chain.
    for i, chain in enumerate(chains):
        if len(chain) >= params.min_run_len:
            status = "kept"
        else:
            # Either side of a jump-split → rejected_jump. If bounded only
            # by gaps (or by a session edge), it's a pure flicker.
            left_reason  = break_reasons[i - 1] if i > 0 else None
            right_reason = break_reasons[i]     if i < len(break_reasons) else None
            if "jump" in (left_reason, right_reason):
                status = "rejected_jump"
            else:
                status = "rejected_flicker"
        for f in chain:
            f.filter_status = status


def counts(frames: Iterable[FramePayload]) -> dict[str, int]:
    """Return a kept/rejected_flicker/rejected_jump/unscored tally for UI."""
    out = {"kept": 0, "rejected_flicker": 0, "rejected_jump": 0, "unscored": 0}
    for f in frames:
        if not f.ball_detected:
            continue
        key = f.filter_status or "unscored"
        out[key] = out.get(key, 0) + 1
    return out
