"""Atomic detection-config bundle (HSV + shape gate + selector + preset
identity) — phase 2 of the unified-config redesign.

Earlier each sub-knob lived in its own file (`hsv_range.json`,
`shape_gate.json`, `candidate_selector_tuning.json`) with its own
load / persist path on `State`. That made "switch to blue_ball preset"
a multi-step write that could half-succeed, and made "is the current
config still equal to a known preset?" un-answerable without
hand-diffing three records. This module collapses the triple into a
single `data/detection_config.json` with explicit preset identity.

Migration: `load_or_migrate` reads the new file when present;
otherwise reconstructs from the three legacy files and rewrites
atomically + deletes the legacy files. Subsequent boots only see the
new file. Per CLAUDE.md no-backcompat the legacy load path is removed
once migration runs once on every operator's data dir; the migration
function is itself a one-shot system-boundary translation, not a
runtime fallback.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from candidate_selector import CandidateSelectorTuning
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
    """The full detection-time triple plus the preset it was last
    derived from (None = custom). `last_applied_at` is a unix timestamp
    used by the UI to show "synced X ago" — None means never explicitly
    applied (i.e. boot defaults). Mirrors the on-disk schema 1:1."""
    hsv: HSVRange
    shape_gate: ShapeGate
    selector: CandidateSelectorTuning
    preset: str | None
    last_applied_at: float | None

    def with_(
        self,
        *,
        hsv: HSVRange | None = None,
        shape_gate: ShapeGate | None = None,
        selector: CandidateSelectorTuning | None = None,
        preset: "str | None | _Sentinel" = _SENTINEL,
        last_applied_at: "float | None | _Sentinel" = _SENTINEL,
    ) -> "DetectionConfig":
        """Functional replace; sentinel-distinguishes "leave unchanged"
        from "set to None". The legacy `set_hsv_range` / `set_shape_gate`
        / `set_candidate_selector_tuning` adapters use this to mutate
        one section while explicitly clearing `preset` (editing any
        sub-knob means leaving the named preset)."""
        return replace(
            self,
            hsv=self.hsv if hsv is None else hsv,
            shape_gate=self.shape_gate if shape_gate is None else shape_gate,
            selector=self.selector if selector is None else selector,
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


def _selector_to_dict(s: CandidateSelectorTuning) -> dict[str, float]:
    return {"w_aspect": s.w_aspect, "w_fill": s.w_fill}


def _selector_from_dict(d: dict) -> CandidateSelectorTuning:
    return CandidateSelectorTuning(
        w_aspect=float(d["w_aspect"]),
        w_fill=float(d["w_fill"]),
    )


def to_dict(cfg: DetectionConfig) -> dict:
    """Wire / disk shape. Symmetric with `from_dict`."""
    return {
        "preset": cfg.preset,
        "hsv": _hsv_to_dict(cfg.hsv),
        "shape_gate": _shape_gate_to_dict(cfg.shape_gate),
        "selector": _selector_to_dict(cfg.selector),
        "last_applied_at": cfg.last_applied_at,
    }


def from_dict(d: dict) -> DetectionConfig:
    """Strict load — every required key must be present. Per CLAUDE.md
    no-silent-fallback: a corrupt file should fail loudly at boot rather
    than silently drop back to defaults masking a config-file bug."""
    return DetectionConfig(
        hsv=_hsv_from_dict(d["hsv"]),
        shape_gate=_shape_gate_from_dict(d["shape_gate"]),
        selector=_selector_from_dict(d["selector"]),
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
    if cfg.selector != base.selector:
        for k in ("w_aspect", "w_fill"):
            if getattr(cfg.selector, k) != getattr(base.selector, k):
                diff.append(f"selector.{k}")
    return diff


# ---- on-disk paths -------------------------------------------------------


_NEW_FILENAME = "detection_config.json"
_LEGACY_HSV_FILENAME = "hsv_range.json"
_LEGACY_SHAPE_GATE_FILENAME = "shape_gate.json"
_LEGACY_SELECTOR_FILENAME = "candidate_selector_tuning.json"


def _default_config() -> DetectionConfig:
    """Boot default when neither the new file nor any legacy file exists.
    Tennis preset is the canonical default — `HSVRange.default()` /
    `ShapeGate.default()` / `CandidateSelectorTuning.default()` are all
    bound to its values via the preset registry, so this is the
    self-consistent zero state."""
    p = PRESETS["tennis"]
    return DetectionConfig(
        hsv=p.hsv,
        shape_gate=p.shape_gate,
        selector=p.selector,
        preset="tennis",
        last_applied_at=None,
    )


def _load_legacy_triple(data_dir: Path) -> DetectionConfig | None:
    """If any of the three legacy files exists, rebuild a DetectionConfig
    using whichever ones do exist (each missing slot falls to its
    module default — that's the legacy load semantics from `state.py`,
    preserved verbatim here so migration is exactly equivalent to a
    pre-migration boot). Returns None only if zero legacy files exist
    — caller treats that as "fresh install" and takes `_default_config()`.

    Preset is intentionally left None on migration: the operator may
    have hand-edited any of the three files, so we cannot safely claim
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
    sel = _load_or_default(
        sel_path,
        # candidate_selector legacy file may carry old `r_px_expected` /
        # `w_size` keys from the size_pen era; tolerate by reading only
        # the two keys we care about.
        lambda d: CandidateSelectorTuning(
            w_aspect=float(d.get("w_aspect", CandidateSelectorTuning.default().w_aspect)),
            w_fill=float(d.get("w_fill", CandidateSelectorTuning.default().w_fill)),
        ),
        CandidateSelectorTuning.default(),
    )
    return DetectionConfig(
        hsv=hsv, shape_gate=sg, selector=sel,
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
            return from_dict(json.loads(new_path.read_text()))
        except Exception as e:
            # Strict: a corrupt new file is a real bug, not "fall back to
            # defaults". Re-raise so boot fails loudly. Operator can
            # delete the file by hand to force re-migration / default.
            raise RuntimeError(
                f"detection_config.json corrupt at {new_path}: {e}"
            ) from e

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
