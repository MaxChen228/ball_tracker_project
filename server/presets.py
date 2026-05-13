"""Disk-backed preset library.

A preset is a named, immutable snapshot of the detection-config pair
(HSVRange + ShapeGate). It exists so switching between research configs
is one atomic operation that produces a reproducible, recall-able set of
parameters, and so a frozen pitch can be tagged with the preset that
generated it.

Layout: every preset is a single JSON file under
`<data_dir>/presets/<name>.json`. There is no in-memory registry — disk
is the single source of truth. `seed_builtins` writes the built-in
tennis / blue_ball seeds on first boot if the corresponding file is
absent; an operator deleting a built-in file and restarting recreates
it. Custom presets created at runtime persist across restart and never
get rewritten by the seed step.

Selector cost weights are not preset-controlled — they're locked as
`_W_ASPECT` / `_W_FILL` module constants in `candidate_selector` (the
selector retirement). Presets carry HSV + shape gate only.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import algorithms
from detection import HSVRange, ShapeGate

logger = logging.getLogger("ball_tracker")


@dataclass(frozen=True)
class Preset:
    """Disk-backed named detection config. The canonical shape is
    `{name, label, algorithm_id, params}` where `params` is opaque to
    Preset itself — round-tripped through the registered detector's
    `params_schema` at load time, same way `DetectionConfigSnapshotPayload`
    handles per-algorithm params.

    For v11_hsv_cc, `params = {"hsv": {h_min, ...}, "shape_gate":
    {aspect_min, fill_min}}`. The two convenience properties (`hsv`,
    `shape_gate`) project the v11 params dict into typed
    `HSVRange` / `ShapeGate` instances so existing v11-aware callers
    (dashboard slider, chroma_alignment_check, detection_config apply)
    keep working unchanged. Reading `.hsv` / `.shape_gate` on a non-v11
    preset raises `AttributeError` at the boundary."""
    name: str
    label: str
    algorithm_id: str
    # Algorithm-specific params blob. v11 puts `{hsv, shape_gate}` here;
    # future detectors put whatever their params_schema declares.
    params: dict

    @property
    def hsv(self) -> HSVRange:
        """v11-only convenience accessor. Raises `AttributeError` for
        any other algorithm — caller must dispatch on `algorithm_id`
        first when handling a non-v11 preset."""
        if self.algorithm_id != algorithms.V11_HSV_CC:
            raise AttributeError(
                f"preset {self.name!r} is for algorithm "
                f"{self.algorithm_id!r}; .hsv is v11-only — read "
                f".params instead"
            )
        h = self.params["hsv"]
        return HSVRange(
            h_min=h["h_min"], h_max=h["h_max"],
            s_min=h["s_min"], s_max=h["s_max"],
            v_min=h["v_min"], v_max=h["v_max"],
        )

    @property
    def shape_gate(self) -> ShapeGate:
        """v11-only convenience accessor. See `hsv` for the dispatch
        contract."""
        if self.algorithm_id != algorithms.V11_HSV_CC:
            raise AttributeError(
                f"preset {self.name!r} is for algorithm "
                f"{self.algorithm_id!r}; .shape_gate is v11-only — "
                f"read .params instead"
            )
        sg = self.params["shape_gate"]
        return ShapeGate(
            aspect_min=sg["aspect_min"],
            fill_min=sg["fill_min"],
        )

    @classmethod
    def for_v11(
        cls, *, name: str, label: str,
        hsv: HSVRange, shape_gate: ShapeGate,
    ) -> "Preset":
        """Construct a v11_hsv_cc preset from typed runtime objects.
        Use for built-in seeds and any caller that already has typed
        `HSVRange` / `ShapeGate` instances on hand."""
        return cls(
            name=name, label=label,
            algorithm_id=algorithms.V11_HSV_CC,
            params={
                "hsv": {
                    "h_min": hsv.h_min, "h_max": hsv.h_max,
                    "s_min": hsv.s_min, "s_max": hsv.s_max,
                    "v_min": hsv.v_min, "v_max": hsv.v_max,
                },
                "shape_gate": {
                    "aspect_min": shape_gate.aspect_min,
                    "fill_min": shape_gate.fill_min,
                },
            },
        )


_PRESETS_DIRNAME = "presets"
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")


# Built-in seeds. Written to disk on boot if the file does not yet exist;
# never overwritten thereafter. To restore a built-in after editing or
# deletion, remove the corresponding file in `data/presets/` and restart.
_BUILTIN_SEEDS: dict[str, Preset] = {
    "tennis": Preset.for_v11(
        name="tennis",
        label="Tennis",
        # Bound to module defaults so the headless / first-boot fallback
        # in `HSVRange.default()` stays in lockstep with the seeded
        # tennis preset. After seeding, disk is authoritative — later
        # tweaks to `.default()` will NOT propagate to an existing seed
        # file (this is intentional: the operator's on-disk preset
        # survives library changes).
        hsv=HSVRange.default(),
        shape_gate=ShapeGate.default(),
    ),
    "blue_ball": Preset.for_v11(
        name="blue_ball",
        label="Blue ball",
        # Project ball — deep-blue hardball. h tightened to 105-112 on
        # 2026-04-29 to filter background blue; v_min ≥ 40 required
        # because the ball's shaded underside drops to V~80 and lifting
        # v_min carves the mask into a crescent that fails aspect.
        hsv=HSVRange(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
        # Tighter aspect (0.75 vs default 0.70): the project ball is
        # rounder than a tennis ball — minimal motion blur ellipsing on
        # the rig — so we can afford a stricter circularity floor that
        # rejects more clutter (0.63-0.70 fill range observed at p50;
        # see CLAUDE.md tuning baselines).
        shape_gate=ShapeGate(aspect_min=0.75, fill_min=0.55),
    ),
    "hybrid_28d_blue_ball": Preset(
        name="hybrid_28d_blue_ball",
        label="Hybrid 28d (blue ball)",
        algorithm_id=algorithms.HYBRID_28D,
        # Two HSV cubes + two shape gates, matching the lab/research
        # PR #112 winner. PROD = the existing blue_ball seed values
        # (tight, high-precision-low-recall); V11 loose = wider hue
        # band + lower aspect/fill gates that cover the rescue subset
        # PROD misses. neigh_half / match_px are 28d_hybrid.py
        # constants — physics-derived (50ms motion window @ 240fps,
        # CC centroid noise ≈ 5px on this rig).
        params={
            "prod_hsv": {
                "h_min": 105, "h_max": 112,
                "s_min": 140, "s_max": 255,
                "v_min": 40, "v_max": 255,
            },
            "prod_shape": {"aspect_min": 0.75, "fill_min": 0.55},
            # PROD inherits v11_hsv_cc's 20-px floor (ball-sized).
            "prod_area_min": 20,
            "v11_hsv": {
                "h_min": 103, "h_max": 118,
                "s_min": 120, "s_max": 255,
                "v_min": 30, "v_max": 255,
            },
            "v11_shape": {"aspect_min": 0.40, "fill_min": 0.35},
            # V11 floor drops to 3 px so the rescue path can emit
            # micro-blobs PROD's tight floor would silently drop.
            # Matches lab `28d_hybrid.py` `V11["area"]=(3, 150_000)`.
            "v11_area_min": 3,
            "v11_close_kernel": 3,
            "neigh_half": 6,
            "match_px": 5.0,
        },
    ),
}


def _validate_builtin_seeds_against_registry() -> None:
    """Boot drift guard: every `_BUILTIN_SEEDS` entry must round-trip
    through its algorithm's `params_schema`. Catches the case where a
    detector schema gains a new required field but the seed literal
    here wasn't updated — fails at module import (before seed_builtins
    persists invalid JSON to disk)."""
    for name, preset in _BUILTIN_SEEDS.items():
        try:
            entry = algorithms.get(preset.algorithm_id)
        except KeyError:
            raise RuntimeError(
                f"_BUILTIN_SEEDS[{name!r}].algorithm_id="
                f"{preset.algorithm_id!r} not in algorithms._REGISTRY"
            )
        try:
            entry.detector.params_schema.model_validate(preset.params)
        except Exception as exc:
            raise RuntimeError(
                f"_BUILTIN_SEEDS[{name!r}] params drift vs "
                f"{preset.algorithm_id} schema: {exc}"
            ) from exc


_validate_builtin_seeds_against_registry()


def presets_dir(data_dir: Path) -> Path:
    return data_dir / _PRESETS_DIRNAME


def _preset_path(data_dir: Path, name: str) -> Path:
    return presets_dir(data_dir) / f"{name}.json"


def validate_slug(name: str) -> None:
    """Raise `ValueError` if `name` isn't a valid preset slug. Slug is
    used directly as a filename (`<name>.json`); the regex restricts to
    `[a-z0-9_]{1,32}` so paths stay portable and unambiguous. Free-form
    operator-facing labels go in `Preset.label`, not the slug."""
    if not _SLUG_RE.match(name):
        raise ValueError(
            f"invalid preset name {name!r}: must match [a-z0-9_]{{1,32}}"
        )


def _to_dict(preset: Preset) -> dict:
    """Disk-canonical shape: `{algorithm_id, name, label, params}`.
    `params` is opaque per-algorithm — for v11 it carries the same
    `{hsv, shape_gate}` content that used to be at the top level."""
    return {
        "algorithm_id": preset.algorithm_id,
        "name": preset.name,
        "label": preset.label,
        "params": preset.params,
    }


def _from_dict(d: dict) -> Preset:
    """Strict load — every required key must be present. Per CLAUDE.md
    no-silent-fallback: a corrupt preset file raises rather than masking
    a config bug as a defaults-restore.

    Disk shape: `{algorithm_id, name, label, params}`. The `params`
    dict is round-tripped through the algorithm's `Detector.params_schema`
    so a malformed payload is rejected at load with a Pydantic
    ValidationError — same enforcement as `DetectionConfigSnapshotPayload`.

    Pre-`params` files (`hsv` + `shape_gate` at top level) are
    collapsed by `_read_with_migration` before reaching here."""
    algorithm_id = d["algorithm_id"]
    algorithms.validate_runnable_id(algorithm_id)
    params = dict(d["params"])
    # Round-trip through the registered detector's params schema so
    # corrupt fields fail fast, not at first detection run.
    entry = algorithms.get(algorithm_id)
    entry.detector.params_schema.model_validate(params)
    return Preset(
        name=str(d["name"]),
        label=str(d["label"]),
        algorithm_id=algorithm_id,
        params=params,
    )


def _read_with_migration(
    path: Path,
    *,
    atomic_write: Callable[[Path, str], None] | None,
) -> Preset:
    """Read one preset file, migrating older shapes inline.

    Disk shape evolution:
    - **pre-algorithm-id** (oldest): `{name, label, hsv, shape_gate}` —
      no `algorithm_id` field. Default-stamped to v11_hsv_cc.
    - **flat-keys** (Phase-1-of-platform-widening era): `{algorithm_id,
      name, label, hsv, shape_gate}` — `hsv` and `shape_gate` at top
      level. Collapsed into `params`.
    - **canonical** (current): `{algorithm_id, name, label, params}`
      where `params` is opaque per-algorithm.

    `atomic_write=None` is a read-only mode for offline tools — they
    still get a working `Preset` but the file stays in pre-migration
    shape. Every runtime caller (State, route handlers) must supply
    `atomic_write` so migration converges within one boot."""
    raw = json.loads(path.read_text())
    migrated = False
    if "algorithm_id" not in raw:
        raw["algorithm_id"] = algorithms.DEFAULT_ALGORITHM_ID
        migrated = True
    if "params" not in raw:
        # Flat-keys shape: collapse v11-shaped `hsv` + `shape_gate`
        # into the `params` dict. The `_from_dict` validator will
        # reject the result if either key is missing — we don't
        # synthesise empty defaults (no silent fallback).
        params: dict = {}
        if "hsv" in raw:
            params["hsv"] = raw.pop("hsv")
        if "shape_gate" in raw:
            params["shape_gate"] = raw.pop("shape_gate")
        raw["params"] = params
        migrated = True
    preset = _from_dict(raw)
    if atomic_write is not None and migrated:
        atomic_write(path, json.dumps(_to_dict(preset), indent=2))
        logger.info(
            "preset %s: migrated to canonical params shape (algorithm=%s)",
            path.name, preset.algorithm_id,
        )
    return preset


def seed_builtins(
    data_dir: Path,
    *,
    atomic_write: Callable[[Path, str], None],
) -> None:
    """Write any missing built-in preset files. Idempotent: existing
    files (whether unedited seed copies or operator-modified) are left
    alone. Must run before anything reads presets — `State.__init__`
    calls this immediately before `detection_config.load_or_migrate`,
    which depends on `tennis` being present for its boot default."""
    pdir = presets_dir(data_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    for name, preset in _BUILTIN_SEEDS.items():
        path = _preset_path(data_dir, name)
        if not path.exists():
            atomic_write(path, json.dumps(_to_dict(preset), indent=2))


def load_preset(
    data_dir: Path,
    name: str,
    *,
    atomic_write: Callable[[Path, str], None] | None = None,
) -> Preset:
    """Read a single preset by slug. Raises `KeyError(name)` if missing
    — callers at the API boundary translate to HTTP 400/404.

    When `atomic_write` is supplied (the runtime call path), files
    pre-dating the `algorithm_id` field are rewritten in canonical
    shape on read. Read-only callers (offline tools) omit it."""
    path = _preset_path(data_dir, name)
    if not path.exists():
        raise KeyError(name)
    return _read_with_migration(path, atomic_write=atomic_write)


def preset_exists(data_dir: Path, name: str) -> bool:
    return _preset_path(data_dir, name).exists()


def list_presets(
    data_dir: Path,
    *,
    atomic_write: Callable[[Path, str], None] | None = None,
) -> list[Preset]:
    """All presets sorted by slug. Empty list if the directory does not
    yet exist (caller treats as fresh install — `seed_builtins` would
    typically have run already at boot).

    When `atomic_write` is supplied (the runtime call path), files
    pre-dating the `algorithm_id` field are rewritten in canonical
    shape on read."""
    pdir = presets_dir(data_dir)
    if not pdir.exists():
        return []
    out: list[Preset] = []
    for path in sorted(pdir.glob("*.json")):
        out.append(_read_with_migration(path, atomic_write=atomic_write))
    return out


def save_preset(
    data_dir: Path,
    preset: Preset,
    *,
    atomic_write: Callable[[Path, str], None],
) -> None:
    """Atomic single-file write. Validates the slug + algorithm_id
    before touching disk. Overwrites any existing file with the same
    name — name-collision policy (409 vs PUT semantics) is enforced at
    the route layer, not here."""
    validate_slug(preset.name)
    algorithms.validate_runnable_id(preset.algorithm_id)
    presets_dir(data_dir).mkdir(parents=True, exist_ok=True)
    atomic_write(
        _preset_path(data_dir, preset.name),
        json.dumps(_to_dict(preset), indent=2),
    )


def delete_preset(data_dir: Path, name: str) -> None:
    """Unlink the preset file. Raises `KeyError(name)` if missing."""
    path = _preset_path(data_dir, name)
    if not path.exists():
        raise KeyError(name)
    path.unlink()


def hsv_as_dict(preset: Preset) -> dict[str, int]:
    """Wire / render shape: dict of the 6 HSV ints. Used by the dashboard
    HSV card buttons (rendered as `data-*` attributes for the JS slider
    sync) and any JSON-emitting endpoint."""
    return {
        "h_min": preset.hsv.h_min,
        "h_max": preset.hsv.h_max,
        "s_min": preset.hsv.s_min,
        "s_max": preset.hsv.s_max,
        "v_min": preset.hsv.v_min,
        "v_max": preset.hsv.v_max,
    }
