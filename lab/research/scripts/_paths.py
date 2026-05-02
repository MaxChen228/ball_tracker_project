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
