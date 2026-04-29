"""Offline dry-run: quantify drift between `frames_live` (iOS WS stream
detection) and `frames_server_post` (server PyAV decode + re-detection)
on the same MOV.

Front-line diagnostic for *silent* divergence — e.g. s_97655fc6 where
the live ROI locked onto a stationary plant and missed the ball, while
server_post on the same footage detected it cleanly. Currently the only
way to spot that is hand-eye in the viewer.

Pairs frames by `timestamp_s` (both paths share the iOS master clock
since `video_start_pts_s` is added in `pipeline.detect_pitch`), within
a `--window-ms` tolerance (default 4 ms = half a 240 fps frame).

Outputs a markdown summary to stdout and a JSON detail report under
`data/alignment_reports/<sid>.json`.

Usage:
    uv run python dry_run_live_vs_server.py --session s_xxxx
    uv run python dry_run_live_vs_server.py --all
    uv run python dry_run_live_vs_server.py --since 2026-04-25
    uv run python dry_run_live_vs_server.py --session s_xxxx --window-ms 8
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
PITCH_DIR = DATA_DIR / "pitches"
REPORT_DIR = DATA_DIR / "alignment_reports"

EXIT_OK = 0
EXIT_NO_SERVER_POST = 2
EXIT_NO_LIVE = 3
EXIT_NOT_FOUND = 4


# ----------------------------- core data types -------------------------------


@dataclass
class FrameLite:
    """Just the bits we need from FramePayload — keeping this script
    independent of the schema avoids pulling in pydantic for an offline
    audit tool."""

    frame_index: int
    timestamp_s: float
    px: float | None
    py: float | None
    ball_detected: bool
    n_candidates: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FrameLite":
        cands = d.get("candidates")
        n = len(cands) if isinstance(cands, list) else 0
        return cls(
            frame_index=int(d["frame_index"]),
            timestamp_s=float(d["timestamp_s"]),
            px=(float(d["px"]) if d.get("px") is not None else None),
            py=(float(d["py"]) if d.get("py") is not None else None),
            ball_detected=bool(d.get("ball_detected", False)),
            n_candidates=n,
        )


@dataclass
class CamReport:
    session_id: str
    camera_id: str
    n_live: int
    n_server: int
    paired: int  # frames where a pairing exists within the window
    only_live: int  # paired and live detected, server didn't
    only_server: int  # paired and server detected, live didn't
    both: int  # paired and both detected
    neither: int  # paired and neither detected
    unmatched_live: int  # live frames with no server-side counterpart in window
    unmatched_server: int  # server frames with no live counterpart in window
    centroid_dx: list[float] = field(default_factory=list)
    centroid_dy: list[float] = field(default_factory=list)
    centroid_dist: list[float] = field(default_factory=list)
    cand_delta_hist: dict[int, int] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)


# ----------------------------- pairing logic ---------------------------------


def pair_frames(
    live: list[FrameLite], server: list[FrameLite], window_s: float
) -> tuple[list[tuple[FrameLite, FrameLite]], list[FrameLite], list[FrameLite]]:
    """Greedy nearest-neighbour pairing within ±window_s.

    Both lists are sorted by timestamp; for each `live` frame we find
    the closest unpaired `server` frame within the window. Stable and
    deterministic. Returns (paired, unmatched_live, unmatched_server).
    """
    live_sorted = sorted(live, key=lambda f: f.timestamp_s)
    server_sorted = sorted(server, key=lambda f: f.timestamp_s)
    server_t = [f.timestamp_s for f in server_sorted]
    server_used = [False] * len(server_sorted)

    pairs: list[tuple[FrameLite, FrameLite]] = []
    unmatched_live: list[FrameLite] = []

    for f_live in live_sorted:
        lo = bisect.bisect_left(server_t, f_live.timestamp_s - window_s)
        hi = bisect.bisect_right(server_t, f_live.timestamp_s + window_s)
        best_j = -1
        best_dt = math.inf
        for j in range(lo, hi):
            if server_used[j]:
                continue
            dt = abs(server_sorted[j].timestamp_s - f_live.timestamp_s)
            if dt < best_dt:
                best_dt = dt
                best_j = j
        if best_j < 0:
            unmatched_live.append(f_live)
        else:
            server_used[best_j] = True
            pairs.append((f_live, server_sorted[best_j]))

    unmatched_server = [
        s for s, used in zip(server_sorted, server_used) if not used
    ]
    return pairs, unmatched_live, unmatched_server


def build_cam_report(
    session_id: str,
    camera_id: str,
    pitch_obj: dict[str, Any],
    window_s: float,
    top_n_disagreements: int = 10,
) -> CamReport:
    raw_live = pitch_obj.get("frames_live")
    raw_server = pitch_obj.get("frames_server_post")
    if raw_live is None or raw_server is None:
        raise KeyError(
            f"{session_id}/{camera_id}: pitch JSON missing frames_live or "
            f"frames_server_post key"
        )

    live = [FrameLite.from_dict(d) for d in raw_live]
    server = [FrameLite.from_dict(d) for d in raw_server]

    pairs, unmatched_live, unmatched_server = pair_frames(live, server, window_s)

    rep = CamReport(
        session_id=session_id,
        camera_id=camera_id,
        n_live=len(live),
        n_server=len(server),
        paired=len(pairs),
        only_live=0,
        only_server=0,
        both=0,
        neither=0,
        unmatched_live=len(unmatched_live),
        unmatched_server=len(unmatched_server),
    )

    for f_l, f_s in pairs:
        delta = f_l.n_candidates - f_s.n_candidates
        rep.cand_delta_hist[delta] = rep.cand_delta_hist.get(delta, 0) + 1

        if f_l.ball_detected and f_s.ball_detected:
            rep.both += 1
            if (
                f_l.px is not None
                and f_l.py is not None
                and f_s.px is not None
                and f_s.py is not None
            ):
                dx = f_l.px - f_s.px
                dy = f_l.py - f_s.py
                rep.centroid_dx.append(dx)
                rep.centroid_dy.append(dy)
                rep.centroid_dist.append(math.hypot(dx, dy))
            # if a side claims detected but lacks pixel coords we
            # silently skip the centroid Δ — but still count the both.
        elif f_l.ball_detected and not f_s.ball_detected:
            rep.only_live += 1
            rep.disagreements.append(
                {
                    "timestamp_s": f_l.timestamp_s,
                    "live_frame_index": f_l.frame_index,
                    "server_frame_index": f_s.frame_index,
                    "detected_in": "live",
                    "live_px": f_l.px,
                    "live_py": f_l.py,
                    "server_n_candidates": f_s.n_candidates,
                }
            )
        elif f_s.ball_detected and not f_l.ball_detected:
            rep.only_server += 1
            rep.disagreements.append(
                {
                    "timestamp_s": f_s.timestamp_s,
                    "live_frame_index": f_l.frame_index,
                    "server_frame_index": f_s.frame_index,
                    "detected_in": "server",
                    "server_px": f_s.px,
                    "server_py": f_s.py,
                    "live_n_candidates": f_l.n_candidates,
                }
            )
        else:
            rep.neither += 1

    # keep top-N most extreme by absolute timestamp ordering — but the
    # caller likely wants the *first* divergences chronologically since
    # that's where the ROI lock-on tends to start. Sort by timestamp
    # asc and truncate.
    rep.disagreements.sort(key=lambda r: r["timestamp_s"])
    rep.disagreements = rep.disagreements[:top_n_disagreements]
    return rep


# ----------------------------- stats helpers ---------------------------------


def _stats(xs: list[float]) -> dict[str, float | None]:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "p95": None, "abs_max": None}
    s = sorted(xs)
    n = len(s)

    def _pct(p: float) -> float:
        if n == 1:
            return s[0]
        idx = (n - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return s[lo]
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)

    return {
        "n": n,
        "mean": sum(s) / n,
        "median": _pct(0.5),
        "p95": _pct(0.95),
        "abs_max": max(abs(x) for x in s),
    }


def _fmt_float(x: float | None, fmt: str = "{:+.2f}") -> str:
    if x is None:
        return "N/A"
    return fmt.format(x)


# ----------------------------- rendering -------------------------------------


def render_markdown(rep: CamReport) -> str:
    dx_s = _stats(rep.centroid_dx)
    dy_s = _stats(rep.centroid_dy)
    d_s = _stats(rep.centroid_dist)

    lines = []
    lines.append(f"### {rep.session_id} cam {rep.camera_id}")
    lines.append("")
    lines.append(
        f"frames_live={rep.n_live}  frames_server_post={rep.n_server}  "
        f"paired={rep.paired}  unmatched_live={rep.unmatched_live}  "
        f"unmatched_server={rep.unmatched_server}"
    )
    lines.append("")
    lines.append("| outcome (paired only)  | count |")
    lines.append("|------------------------|------:|")
    lines.append(f"| both detected          | {rep.both} |")
    lines.append(f"| only live detected     | {rep.only_live} |")
    lines.append(f"| only server detected   | {rep.only_server} |")
    lines.append(f"| neither detected       | {rep.neither} |")
    lines.append("")
    lines.append("| centroid Δ (px, both detected) | dx | dy | dist |")
    lines.append("|--------------------------------|----|----|------|")
    lines.append(
        f"| n                              | {dx_s['n']} | {dy_s['n']} | {d_s['n']} |"
    )
    lines.append(
        f"| mean                           | {_fmt_float(dx_s['mean'])} | "
        f"{_fmt_float(dy_s['mean'])} | {_fmt_float(d_s['mean'], '{:.2f}')} |"
    )
    lines.append(
        f"| median                         | {_fmt_float(dx_s['median'])} | "
        f"{_fmt_float(dy_s['median'])} | {_fmt_float(d_s['median'], '{:.2f}')} |"
    )
    lines.append(
        f"| p95                            | {_fmt_float(dx_s['p95'])} | "
        f"{_fmt_float(dy_s['p95'])} | {_fmt_float(d_s['p95'], '{:.2f}')} |"
    )
    lines.append(
        f"| abs max                        | {_fmt_float(dx_s['abs_max'], '{:.2f}')} | "
        f"{_fmt_float(dy_s['abs_max'], '{:.2f}')} | {_fmt_float(d_s['abs_max'], '{:.2f}')} |"
    )
    lines.append("")
    if rep.cand_delta_hist:
        lines.append("| Δ candidates (live - server) | count |")
        lines.append("|------------------------------|------:|")
        for k in sorted(rep.cand_delta_hist):
            lines.append(f"| {k:+d}                            | {rep.cand_delta_hist[k]} |")
        lines.append("")
    if rep.disagreements:
        lines.append(f"top {len(rep.disagreements)} ball-detection disagreements (chronological):")
        lines.append("")
        lines.append("| t (s)   | side    | frame_l | frame_s | extra |")
        lines.append("|---------|---------|--------:|--------:|-------|")
        for d in rep.disagreements:
            extra_bits = []
            if d["detected_in"] == "live":
                extra_bits.append(
                    f"live=({_fmt_float(d.get('live_px'), '{:.1f}')},"
                    f"{_fmt_float(d.get('live_py'), '{:.1f}')}) "
                    f"server_cands={d.get('server_n_candidates')}"
                )
            else:
                extra_bits.append(
                    f"server=({_fmt_float(d.get('server_px'), '{:.1f}')},"
                    f"{_fmt_float(d.get('server_py'), '{:.1f}')}) "
                    f"live_cands={d.get('live_n_candidates')}"
                )
            lines.append(
                f"| {d['timestamp_s']:.4f} | {d['detected_in']:7s} | "
                f"{d['live_frame_index']:7d} | {d['server_frame_index']:7d} | "
                f"{' '.join(extra_bits)} |"
            )
        lines.append("")
    return "\n".join(lines)


def report_to_json(rep: CamReport) -> dict[str, Any]:
    return {
        "session_id": rep.session_id,
        "camera_id": rep.camera_id,
        "counts": {
            "n_live": rep.n_live,
            "n_server": rep.n_server,
            "paired": rep.paired,
            "unmatched_live": rep.unmatched_live,
            "unmatched_server": rep.unmatched_server,
            "both": rep.both,
            "only_live": rep.only_live,
            "only_server": rep.only_server,
            "neither": rep.neither,
        },
        "centroid_delta_px": {
            "dx": _stats(rep.centroid_dx),
            "dy": _stats(rep.centroid_dy),
            "dist": _stats(rep.centroid_dist),
        },
        "candidates_delta_histogram": dict(
            sorted(rep.cand_delta_hist.items())
        ),
        "disagreements": rep.disagreements,
    }


# ----------------------------- session discovery -----------------------------


def find_session_pitches(session_id: str) -> dict[str, Path]:
    """Return {cam_id: path} for cameras present on disk for this session."""
    out: dict[str, Path] = {}
    if not PITCH_DIR.exists():
        return out
    for p in PITCH_DIR.glob(f"session_{session_id}_*.json"):
        # filename: session_<sid>_<cam>.json
        stem = p.stem  # session_s_xxx_A
        cam = stem.split("_")[-1]
        out[cam] = p
    return out


def discover_sessions(args: argparse.Namespace) -> list[str]:
    if args.session:
        return [s if s.startswith("s_") else f"s_{s}" for s in args.session]

    if not PITCH_DIR.exists():
        return []
    paths = sorted(PITCH_DIR.glob("session_*.json"))

    cutoff: float | None = None
    if args.since:
        cutoff = _parse_since(args.since).timestamp()

    sids: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if cutoff is not None and p.stat().st_mtime < cutoff:
            continue
        # session_<sid_with_underscores>_<cam>.json — sid starts with s_
        # so the prefix is session_s_<hex>; strip "session_" then drop trailing _<cam>
        name = p.stem
        if not name.startswith("session_s_"):
            continue
        body = name[len("session_") :]  # s_<hex>_<cam>
        # cam is always the trailing token after the last underscore
        sid = body.rsplit("_", 1)[0]
        if sid not in seen:
            seen.add(sid)
            sids.append(sid)
    return sids


def _parse_since(s: str) -> datetime:
    if s == "today":
        now = datetime.now().astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        d = datetime.fromisoformat(s)
    except ValueError as exc:
        raise SystemExit(f"bad --since value: {s!r}") from exc
    if d.tzinfo is None:
        d = d.astimezone()
    return d


# ----------------------------- top-level run ---------------------------------


def run_session(session_id: str, window_s: float) -> tuple[int, list[CamReport]]:
    """Return (exit_code, [reports]). Exit code != 0 if any cam is unusable."""
    cam_paths = find_session_pitches(session_id)
    if not cam_paths:
        print(
            f"[{session_id}] no pitch JSON found under {PITCH_DIR} "
            f"— cannot audit. Skipping.",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND, []

    reports: list[CamReport] = []
    rc = EXIT_OK
    for cam_id in sorted(cam_paths):
        path = cam_paths[cam_id]
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"[{session_id}/{cam_id}] bad JSON: {exc}", file=sys.stderr)
            rc = max(rc, EXIT_NOT_FOUND)
            continue

        n_server = len(obj.get("frames_server_post") or [])
        n_live = len(obj.get("frames_live") or [])
        if n_server == 0:
            print(
                f"[{session_id}/{cam_id}] frames_server_post is empty — "
                f"server_post not run on this session yet. Re-run with "
                f"`Run server` (dashboard) or `reprocess_sessions.py "
                f"--session {session_id}` first.",
                file=sys.stderr,
            )
            rc = max(rc, EXIT_NO_SERVER_POST)
            continue
        if n_live == 0:
            print(
                f"[{session_id}/{cam_id}] frames_live is empty — no live WS "
                f"detections were captured for this cam. Nothing to compare.",
                file=sys.stderr,
            )
            rc = max(rc, EXIT_NO_LIVE)
            continue

        rep = build_cam_report(session_id, cam_id, obj, window_s=window_s)
        reports.append(rep)
        print(render_markdown(rep))

    if reports:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORT_DIR / f"{session_id}.json"
        out_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "window_ms": window_s * 1000.0,
                    "cameras": [report_to_json(r) for r in reports],
                },
                indent=2,
            )
        )
        print(f"\n[{session_id}] wrote {out_path}")
    return rc, reports


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Quantify drift between frames_live (iOS WS detection) and "
            "frames_server_post (server PyAV decode) on the same MOV."
        )
    )
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--session",
        action="append",
        help="session id, e.g. s_97655fc6 (repeat for multiple)",
    )
    sel.add_argument(
        "--all",
        action="store_true",
        help="audit every session under data/pitches/",
    )
    sel.add_argument(
        "--since",
        help="audit sessions whose pitch JSON mtime ≥ this date "
        "(YYYY-MM-DD or 'today')",
    )
    p.add_argument(
        "--window-ms",
        type=float,
        default=4.0,
        help="±tolerance for live↔server timestamp pairing in ms "
        "(default 4 ms = half a 240 fps frame)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    window_s = args.window_ms / 1000.0
    sessions = discover_sessions(args)
    if not sessions:
        print("no matching sessions found", file=sys.stderr)
        return EXIT_NOT_FOUND

    overall = EXIT_OK
    for sid in sessions:
        rc, _ = run_session(sid, window_s=window_s)
        overall = max(overall, rc)
    return overall


if __name__ == "__main__":
    sys.exit(main())
