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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from detection import HSVRange, ShapeGate


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    hsv: HSVRange
    shape_gate: ShapeGate


_PRESETS_DIRNAME = "presets"
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")


# Built-in seeds. Written to disk on boot if the file does not yet exist;
# never overwritten thereafter. To restore a built-in after editing or
# deletion, remove the corresponding file in `data/presets/` and restart.
_BUILTIN_SEEDS: dict[str, Preset] = {
    "tennis": Preset(
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
    "blue_ball": Preset(
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
}


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
    return {
        "name": preset.name,
        "label": preset.label,
        "hsv": {
            "h_min": preset.hsv.h_min, "h_max": preset.hsv.h_max,
            "s_min": preset.hsv.s_min, "s_max": preset.hsv.s_max,
            "v_min": preset.hsv.v_min, "v_max": preset.hsv.v_max,
        },
        "shape_gate": {
            "aspect_min": preset.shape_gate.aspect_min,
            "fill_min": preset.shape_gate.fill_min,
        },
    }


def _from_dict(d: dict) -> Preset:
    """Strict load — every required key must be present. Per CLAUDE.md
    no-silent-fallback: a corrupt preset file raises rather than masking
    a config bug as a defaults-restore."""
    hsv = d["hsv"]
    sg = d["shape_gate"]
    return Preset(
        name=str(d["name"]),
        label=str(d["label"]),
        hsv=HSVRange(
            h_min=int(hsv["h_min"]), h_max=int(hsv["h_max"]),
            s_min=int(hsv["s_min"]), s_max=int(hsv["s_max"]),
            v_min=int(hsv["v_min"]), v_max=int(hsv["v_max"]),
        ),
        shape_gate=ShapeGate(
            aspect_min=float(sg["aspect_min"]),
            fill_min=float(sg["fill_min"]),
        ),
    )


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


def load_preset(data_dir: Path, name: str) -> Preset:
    """Read a single preset by slug. Raises `KeyError(name)` if missing
    — callers at the API boundary translate to HTTP 400/404."""
    path = _preset_path(data_dir, name)
    if not path.exists():
        raise KeyError(name)
    return _from_dict(json.loads(path.read_text()))


def preset_exists(data_dir: Path, name: str) -> bool:
    return _preset_path(data_dir, name).exists()


def list_presets(data_dir: Path) -> list[Preset]:
    """All presets sorted by slug. Empty list if the directory does not
    yet exist (caller treats as fresh install — `seed_builtins` would
    typically have run already at boot)."""
    pdir = presets_dir(data_dir)
    if not pdir.exists():
        return []
    out: list[Preset] = []
    for path in sorted(pdir.glob("*.json")):
        out.append(_from_dict(json.loads(path.read_text())))
    return out


def save_preset(
    data_dir: Path,
    preset: Preset,
    *,
    atomic_write: Callable[[Path, str], None],
) -> None:
    """Atomic single-file write. Validates the slug before touching disk.
    Overwrites any existing file with the same name — name-collision
    policy (409 vs PUT semantics) is enforced at the route layer, not
    here."""
    validate_slug(preset.name)
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
