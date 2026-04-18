"""Purge old ball_tracker session data from the server's data directory.

Long-running servers accumulate per-session pitch JSONs, triangulation results,
and H.264 clips under data/. This standalone script groups every file under
`<data-dir>/{pitches,results,videos}` by session id, and deletes sessions whose
youngest file mtime is older than `--days` days ago.

Usage:
    uv run python cleanup_old_sessions.py --days 7
    uv run python cleanup_old_sessions.py --days 30 --data-dir /srv/ball_tracker/data
    uv run python cleanup_old_sessions.py --days 7 --dry-run

Data-dir precedence: CLI flag > $BALL_TRACKER_DATA_DIR > "data" (cwd-relative).

This script is intentionally dependency-free (stdlib only). It never imports
the FastAPI server, so it is safe to run against a stopped server, or via cron
on a schedule independent of the main process.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# session_<sid>_<cam>.<ext> | session_<sid>.json
# sid matches the server's Session.id generator: "s_" + hex chars.
_FILENAME_RE = re.compile(
    r"^session_(s_[0-9a-f]{4,32})(?:_([A-Za-z0-9_-]+))?\.(json|mov|mp4|m4v)$"
)

_SUBDIRS: tuple[str, ...] = ("pitches", "results", "videos")


@dataclass
class SessionGroup:
    """All files belonging to one session, across pitches/results/videos."""

    session_id: str
    files: list[Path] = field(default_factory=list)

    def total_bytes(self) -> int:
        total = 0
        for p in self.files:
            try:
                total += p.stat().st_size
            except OSError:
                # File vanished between scan and size-read; ignore.
                pass
        return total

    def youngest_mtime(self) -> float:
        """Latest mtime across all files (i.e. "most recently touched")."""
        latest = 0.0
        for p in self.files:
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > latest:
                latest = m
        return latest


def _extract_session_id(basename: str) -> str | None:
    """Return the session id embedded in a filename, or None if unrecognized."""
    m = _FILENAME_RE.match(basename)
    if m is None:
        return None
    return m.group(1)


def _scan_data_dir(data_dir: Path) -> dict[str, SessionGroup]:
    """Walk pitches/, results/, videos/ (top-level only) and group by session id."""
    groups: dict[str, SessionGroup] = defaultdict(lambda: SessionGroup(session_id=""))
    for sub in _SUBDIRS:
        sub_path = data_dir / sub
        if not sub_path.is_dir():
            continue
        try:
            entries = list(sub_path.iterdir())
        except OSError as e:
            print(f"warning: cannot read {sub_path}: {e}", file=sys.stderr)
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            sid = _extract_session_id(entry.name)
            if sid is None:
                continue
            group = groups[sid]
            group.session_id = sid
            group.files.append(entry)
    return dict(groups)


def _format_bytes(n: int) -> str:
    """Human-readable byte count (base-1024)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"  # unreachable but keeps type-checker happy


def _find_expired(
    groups: Iterable[SessionGroup], cutoff_epoch: float
) -> list[SessionGroup]:
    """Return groups whose youngest file is at or before `cutoff_epoch`."""
    expired: list[SessionGroup] = []
    for g in groups:
        if not g.files:
            continue
        youngest = g.youngest_mtime()
        if youngest == 0.0:
            # All stat() calls failed; skip defensively rather than delete blind.
            continue
        if youngest <= cutoff_epoch:
            expired.append(g)
    expired.sort(key=lambda g: g.session_id)
    return expired


def _delete_group(group: SessionGroup) -> tuple[int, int]:
    """Unlink every file in the group. Returns (files_removed, bytes_removed)."""
    files_removed = 0
    bytes_removed = 0
    for p in group.files:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"warning: failed to delete {p}: {e}", file=sys.stderr)
            continue
        files_removed += 1
        bytes_removed += size
    return files_removed, bytes_removed


def _resolve_data_dir(cli_value: str | None) -> Path:
    """Precedence: --data-dir > $BALL_TRACKER_DATA_DIR > 'data'."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("BALL_TRACKER_DATA_DIR")
    if env:
        return Path(env)
    return Path("data")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cleanup_old_sessions.py",
        description=(
            "Purge ball_tracker session data older than N days. "
            "Groups files by session id across pitches/results/videos, "
            "uses the youngest file mtime per session as the age signal."
        ),
    )
    parser.add_argument(
        "--days",
        type=float,
        default=30.0,
        help="Delete sessions whose youngest file is older than N days (default: 30).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Data root (default: $BALL_TRACKER_DATA_DIR, else 'data').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.days <= 0:
        print(
            f"error: --days must be positive, got {args.days}",
            file=sys.stderr,
        )
        return 2

    data_dir = _resolve_data_dir(args.data_dir).resolve()
    prefix = "[dry-run] " if args.dry_run else ""

    if not data_dir.is_dir():
        print(
            f"{prefix}data dir does not exist: {data_dir} (nothing to do)",
            file=sys.stderr,
        )
        # Missing dir is not a usage error — exit cleanly so cron stays quiet.
        return 0

    now = time.time()
    cutoff_epoch = now - args.days * 86400.0
    cutoff_human = datetime.fromtimestamp(cutoff_epoch).strftime("%Y-%m-%d %H:%M:%S")

    print(
        f"{prefix}scanning {data_dir} with --days {args.days} "
        f"(cutoff = {cutoff_human})"
    )

    groups = _scan_data_dir(data_dir)
    expired = _find_expired(groups.values(), cutoff_epoch)

    total_files = 0
    total_bytes = 0
    for group in expired:
        file_count = len(group.files)
        byte_count = group.total_bytes()
        if args.dry_run:
            print(
                f"[dry-run] would delete session_{group.session_id}: "
                f"{file_count} files, {_format_bytes(byte_count)}"
            )
            total_files += file_count
            total_bytes += byte_count
        else:
            removed_files, removed_bytes = _delete_group(group)
            print(
                f"deleted session_{group.session_id}: "
                f"{removed_files} files, {_format_bytes(removed_bytes)}"
            )
            total_files += removed_files
            total_bytes += removed_bytes

    session_count = len(expired)
    if args.dry_run:
        suffix = " (not deleted — remove --dry-run to apply)"
    else:
        suffix = ""
    print(
        f"{prefix}total: {session_count} sessions, {total_files} files, "
        f"{_format_bytes(total_bytes)}{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
