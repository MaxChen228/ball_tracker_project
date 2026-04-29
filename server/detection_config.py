"""Atomic detection-config bundle (HSV + shape gate + preset
identity) — phase 2 of the unified-config redesign.

Earlier each sub-knob lived in its own file (`hsv_range.json`,
`shape_gate.json`, `candidate_selector_tuning.json`) with its own
load / persist path on `State`. That made "switch to blue_ball preset"
a multi-step write that could half-succeed, and made "is the current
config still equal to a known preset?" un-answerable without
hand-diffing three records. This module collapses the pair into a
single `data/detection_config.json` with explicit preset identity.

Selector cost weights were a third sub-knob until the selector
retirement; they're now `_W_ASPECT` / `_W_FILL` module constants in
`candidate_selector` and no longer participate in the disk schema.

Migration: `load_or_migrate` reads the new file when present;
otherwise reconstructs from the legacy `hsv_range.json` /
`shape_gate.json` / `candidate_selector_tuning.json` files and rewrites
atomically + deletes them. Subsequent boots only see the new file.
Per CLAUDE.md no-backcompat the legacy load path is removed once
migration runs once on every operator's data dir; the migration
function is itself a one-shot system-boundary translation, not a
runtime fallback. A `selector` key surviving in an existing
`detection_config.json` (written by a pre-retirement build) is
stripped on first load; the file is rewritten in canonical shape.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from detection import HSVRange, ShapeGate
from presets import PRESETS

logger = logging.getLogger("ball_tracker")


class _Sentinel:
    """Distinguishes "argument not supplied" from `None` in `with_`. We
    can't use `None` directly because `preset` and `last_applied_at`
    accept `None` as a meaningful value (custom config / never applied)."""
    pass


_SENTINEL = _Sentinel()


@dataclass(frozen=True)
class DetectionConfig:
    """The detection-time pair plus the preset it was last derived from
    (None = custom). `last_applied_at` is a unix timestamp used by the UI
    to show "synced X ago" — None means never explicitly applied (i.e.
    boot defaults). Mirrors the on-disk schema 1:1."""
    hsv: HSVRange
    shape_gate: ShapeGate
    preset: str | None
    last_applied_at: float | None

    def with_(
        self,
        *,
        hsv: HSVRange | None = None,
        shape_gate: ShapeGate | None = None,
        preset: "str | None | _Sentinel" = _SENTINEL,
        last_applied_at: "float | None | _Sentinel" = _SENTINEL,
    ) -> "DetectionConfig":
        """Functional replace; sentinel-distinguishes "leave unchanged"
        from "set to None". The `set_hsv_range` / `set_shape_gate`
        adapters use this to mutate one section while explicitly clearing
        `preset` (editing any sub-knob means leaving the named preset)."""
        return replace(
            self,
            hsv=self.hsv if hsv is None else hsv,
            shape_gate=self.shape_gate if shape_gate is None else shape_gate,
            preset=self.preset if isinstance(preset, _Sentinel) else preset,
            last_applied_at=(
                self.last_applied_at
                if isinstance(last_applied_at, _Sentinel)
                else last_applied_at
            ),
        )


def _hsv_to_dict(h: HSVRange) -> dict[str, int]:
    return {
        "h_min": h.h_min, "h_max": h.h_max,
        "s_min": h.s_min, "s_max": h.s_max,
        "v_min": h.v_min, "v_max": h.v_max,
    }


def _hsv_from_dict(d: dict) -> HSVRange:
    return HSVRange(
        h_min=int(d["h_min"]), h_max=int(d["h_max"]),
        s_min=int(d["s_min"]), s_max=int(d["s_max"]),
        v_min=int(d["v_min"]), v_max=int(d["v_max"]),
    )


def _shape_gate_to_dict(sg: ShapeGate) -> dict[str, float]:
    return {"aspect_min": sg.aspect_min, "fill_min": sg.fill_min}


def _shape_gate_from_dict(d: dict) -> ShapeGate:
    return ShapeGate(
        aspect_min=float(d["aspect_min"]),
        fill_min=float(d["fill_min"]),
    )


def to_dict(cfg: DetectionConfig) -> dict:
    """Wire / disk shape. Symmetric with `from_dict`."""
    return {
        "preset": cfg.preset,
        "hsv": _hsv_to_dict(cfg.hsv),
        "shape_gate": _shape_gate_to_dict(cfg.shape_gate),
        "last_applied_at": cfg.last_applied_at,
    }


def from_dict(d: dict) -> DetectionConfig:
    """Strict load — every required key must be present. Per CLAUDE.md
    no-silent-fallback: a corrupt file should fail loudly at boot rather
    than silently drop back to defaults masking a config-file bug.

    A residual `selector` key (from a pre-retirement build) is ignored
    here; `load_or_migrate` rewrites the file in canonical shape on
    first boot post-retirement so the stale field is gone within one
    cycle."""
    return DetectionConfig(
        hsv=_hsv_from_dict(d["hsv"]),
        shape_gate=_shape_gate_from_dict(d["shape_gate"]),
        preset=d.get("preset"),
        last_applied_at=d.get("last_applied_at"),
    )


def modified_fields(cfg: DetectionConfig) -> list[str]:
    """If `cfg.preset` is set, return the dotted paths within the triple
    that differ from `PRESETS[cfg.preset]`. Empty list = preset-pure;
    non-empty = "modified" indicator on the dashboard.

    If `cfg.preset` is None (custom), return empty list — no preset to
    diff against. The dashboard shows "custom" rather than "modified"
    for that state.
    """
    if cfg.preset is None or cfg.preset not in PRESETS:
        return []
    base = PRESETS[cfg.preset]
    diff: list[str] = []
    if cfg.hsv != base.hsv:
        for k, v in _hsv_to_dict(cfg.hsv).items():
            if getattr(base.hsv, k) != v:
                diff.append(f"hsv.{k}")
    if cfg.shape_gate != base.shape_gate:
        for k in ("aspect_min", "fill_min"):
            if getattr(cfg.shape_gate, k) != getattr(base.shape_gate, k):
                diff.append(f"shape_gate.{k}")
    return diff


# ---- on-disk paths -------------------------------------------------------


_NEW_FILENAME = "detection_config.json"
_LEGACY_HSV_FILENAME = "hsv_range.json"
_LEGACY_SHAPE_GATE_FILENAME = "shape_gate.json"
_LEGACY_SELECTOR_FILENAME = "candidate_selector_tuning.json"


def _default_config() -> DetectionConfig:
    """Boot default when neither the new file nor any legacy file exists.
    Tennis preset is the canonical default — `HSVRange.default()` /
    `ShapeGate.default()` are bound to its values via the preset
    registry, so this is the self-consistent zero state."""
    p = PRESETS["tennis"]
    return DetectionConfig(
        hsv=p.hsv,
        shape_gate=p.shape_gate,
        preset="tennis",
        last_applied_at=None,
    )


def _load_legacy_triple(data_dir: Path) -> DetectionConfig | None:
    """If either legacy file exists, rebuild a DetectionConfig using
    whichever ones do exist (missing slot → module default — preserves
    pre-migration boot semantics). Returns None only if zero legacy
    files exist — caller treats that as "fresh install" and takes
    `_default_config()`.

    The legacy `candidate_selector_tuning.json` is **not parsed** here
    (selector weights are now `_W_ASPECT` / `_W_FILL` module constants)
    and the file itself is unlinked by `load_or_migrate`'s cleanup loop
    after migration succeeds. Operator-edited values from before the
    retirement are intentionally dropped (CLAUDE.md no-backcompat).

    Preset is intentionally left None on migration: the operator may
    have hand-edited the legacy files, so we cannot safely claim
    "this config IS the blue_ball preset" without diffing — and even if
    they happen to match, "custom that happens to equal blue_ball" is
    safer to label as custom than to retroactively bind to a preset
    name they never selected. The dashboard's first explicit preset
    click after migration restores identity.
    """
    hsv_path = data_dir / _LEGACY_HSV_FILENAME
    sg_path = data_dir / _LEGACY_SHAPE_GATE_FILENAME
    sel_path = data_dir / _LEGACY_SELECTOR_FILENAME
    if not (hsv_path.exists() or sg_path.exists() or sel_path.exists()):
        return None

    def _load_or_default(path: Path, parser, default):
        if not path.exists():
            return default
        try:
            return parser(json.loads(path.read_text()))
        except Exception as e:
            logger.warning("legacy %s corrupt during migration: %s", path.name, e)
            return default

    hsv = _load_or_default(hsv_path, _hsv_from_dict, HSVRange.default())
    sg = _load_or_default(sg_path, _shape_gate_from_dict, ShapeGate.default())
    return DetectionConfig(
        hsv=hsv, shape_gate=sg,
        preset=None, last_applied_at=None,
    )


def load_or_migrate(
    data_dir: Path,
    *,
    atomic_write: Callable[[Path, str], None],
) -> DetectionConfig:
    """Boot-time loader. Order:
      1. New `detection_config.json` exists → load it (strict).
      2. Legacy file(s) exist → migrate to new, delete legacy, return.
      3. Nothing exists → return Tennis-preset default (does NOT write
         disk yet — first explicit `set_detection_config` writes).
    """
    new_path = data_dir / _NEW_FILENAME
    if new_path.exists():
        try:
            raw = json.loads(new_path.read_text())
            cfg = from_dict(raw)
        except Exception as e:
            # Strict: a corrupt new file is a real bug, not "fall back
            # to defaults". Re-raise so boot fails loudly. Recovery is
            # to delete the file by hand — the next boot will fall
            # through to the fresh-Tennis-default branch (legacy
            # files were already cleaned up on the prior migrating
            # boot, so re-migration is not on the menu).
            raise RuntimeError(
                f"detection_config.json corrupt at {new_path}: {e}"
            ) from e
        if "selector" in raw:
            # Pre-retirement build wrote a `selector` block; rewrite the
            # file in canonical (no-selector) shape so the next boot
            # reads a clean record. One-shot, idempotent: subsequent
            # boots see no `selector` key and skip this branch.
            atomic_write(new_path, json.dumps(to_dict(cfg), indent=2))
            logger.info(
                "stripped legacy `selector` key from %s "
                "(weights now hardcoded `_W_ASPECT` / `_W_FILL`)",
                new_path,
            )
        return cfg

    legacy = _load_legacy_triple(data_dir)
    if legacy is not None:
        atomic_write(new_path, json.dumps(to_dict(legacy), indent=2))
        for name in (_LEGACY_HSV_FILENAME, _LEGACY_SHAPE_GATE_FILENAME,
                     _LEGACY_SELECTOR_FILENAME):
            p = data_dir / name
            if p.exists():
                try:
                    p.unlink()
                except OSError as e:
                    logger.warning("could not unlink legacy %s: %s", p, e)
        logger.info(
            "migrated legacy detection config triple → %s "
            "(preset cleared; first dashboard preset click will restore identity)",
            new_path,
        )
        return legacy

    return _default_config()


def persist(
    cfg: DetectionConfig,
    data_dir: Path,
    *,
    atomic_write: Callable[[Path, str], None],
) -> None:
    """Atomic single-file write. Caller owns the lock."""
    atomic_write(data_dir / _NEW_FILENAME, json.dumps(to_dict(cfg), indent=2))
