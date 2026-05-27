"""Offline replay: ball_tracker SessionResult → Godot baseball sim.

Wire:
  ball_tracker world frame (X=plate L/R, Y=pitcher→catcher +, Z=up)
    → swap Y↔Z →
  Godot frame (X=L/R, Y=up, -Z=toward pitcher)

Sends `{"trajectory": [{"t","x","y","z"}, ...]}` as one UDP datagram to
`sender.py` listener (default 127.0.0.1:8888), which extracts hit
features and forwards a HitDataConfig payload to Baseball.cs:9999.

Usage:
  uv run python server/godot_bridge.py --list
  uv run python server/godot_bridge.py --session s_xxx
  uv run python server/godot_bridge.py --session s_xxx --host 192.168.50.50
  uv run python server/godot_bridge.py --session s_xxx --dry-run
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
from pathlib import Path

from schemas import SessionResult


def results_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "results"


def session_path(sid: str) -> Path:
    return results_dir() / f"session_{sid}.json"


def list_sessions() -> int:
    d = results_dir()
    if not d.exists():
        print(f"results dir does not exist: {d}", file=sys.stderr)
        return 1
    files = sorted(d.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"(no sessions in {d})")
        return 0
    print(f"{'session_id':<14}  {'points':>6}  {'duration':>8}  modified")
    print("-" * 60)
    for f in files:
        sid = f.stem.removeprefix("session_")
        try:
            r = SessionResult.model_validate_json(f.read_text())
            n = len(r.points)
            dur = (r.points[-1].t_rel_s - r.points[0].t_rel_s) if n >= 2 else 0.0
            dur_s = f"{dur:.2f}s"
        except Exception as e:
            n = -1
            dur_s = f"ERR:{type(e).__name__}"
        mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{sid:<14}  {n:>6}  {dur_s:>8}  {mtime}")
    return 0


def to_godot_trajectory(result: SessionResult) -> list[dict]:
    return [
        {
            "t": p.t_rel_s,
            "x": p.x_m,
            "y": p.z_m,
            "z": p.y_m,
        }
        for p in result.points
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="session id, e.g. s_c8d36fe2")
    ap.add_argument("--list", action="store_true", help="list available sessions and exit")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--dry-run", action="store_true", help="print payload, do not send")
    args = ap.parse_args()

    if args.list:
        return list_sessions()
    if not args.session:
        ap.error("--session is required (or use --list)")

    path = session_path(args.session)
    if not path.exists():
        print(f"session result not found: {path}", file=sys.stderr)
        return 1

    result = SessionResult.model_validate_json(path.read_text())
    trajectory = to_godot_trajectory(result)
    if not trajectory:
        print(f"session {args.session}: empty points[] — nothing to send", file=sys.stderr)
        return 2

    payload = {"trajectory": trajectory}
    blob = json.dumps(payload).encode("utf-8")
    print(f"session {args.session}: {len(trajectory)} points, {len(blob)} bytes")

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(blob, (args.host, args.port))
    print(f"sent → udp://{args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
