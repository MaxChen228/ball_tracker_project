"""Shared path constants — robust against folder reorganisation.

Searches upward from this file for a repo-root marker (`.git` or `server/`),
so scripts work regardless of nesting depth or cwd. Use::

    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
    from _paths import ROOT, WS, OUT, NOTES, RESEARCH
"""
from __future__ import annotations
from pathlib import Path


def _find_repo_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists() or (parent / "server").is_dir():
            return parent
    raise RuntimeError(f"repo root not found from {p}")


ROOT = _find_repo_root()
RESEARCH = ROOT / "lab" / "research"
WS = ROOT / "lab" / "standalone_workspace"
OUT = RESEARCH / "outputs"
NOTES = RESEARCH / "notes"


# Populated by load_manifest(): slug -> segment_id of the chosen segment.
# Schema v2 nests masks under masks/<segment_id>/, but legacy scripts wrote
# masks/<src>.png. Use this to bridge: `WS/"items"/slug/"masks"/SEG_BY_SLUG[slug]`.
SEG_BY_SLUG: dict[str, str] = {}


def read_mask(path) -> "np.ndarray | None":  # noqa: F821
    """Load a SAM2 ball mask as a 2D uint8 array (0/255).

    Workspace migrated to alpha-channel PNGs (RGB empty, alpha holds the mask).
    Legacy scripts that called cv2.imread(..., IMREAD_GRAYSCALE) silently got
    all-zero masks — use this helper instead to be format-agnostic.
    """
    import cv2  # local import: keep _paths.py importable without cv2
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return img[:, :, 3]
    if img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img[:, :, 0]


def load_manifest():
    """Load standalone_workspace manifest with v1-compat flattening.

    Schema v2 moved propagate_status / in_frame / out_frame / seed_frame /
    seed_point onto per-segment dicts. Most legacy research scripts expect
    those fields at the item level. We flatten by promoting fields from the
    item's active_segment (or first segment with propagate_status="done")
    onto the item itself, so both old and new scripts can read uniformly.

    Returns the parsed manifest dict (with items mutated in place to carry
    item-level shadow fields).
    """
    import json
    m = json.loads((WS / "manifest.json").read_text())
    for it in m.get("items", []):
        segs = it.get("segments") or []
        if not segs:
            continue
        active_id = it.get("active_segment_id")
        chosen = None
        if active_id:
            chosen = next((s for s in segs if s.get("id") == active_id), None)
        if chosen is None:
            chosen = next((s for s in segs if s.get("propagate_status") == "done"), None)
        if chosen is None:
            chosen = segs[0]
        for k in ("propagate_status", "in_frame", "out_frame", "seed_frame", "seed_point"):
            if k in chosen and k not in it:
                it[k] = chosen[k]
        seg_id = chosen.get("id")
        if seg_id:
            SEG_BY_SLUG[it["slug"]] = seg_id
            it.setdefault("_segment_id", seg_id)
    return m
