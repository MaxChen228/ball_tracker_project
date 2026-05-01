from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8876"


def _fail(method: str, url: str, exc: BaseException) -> None:
    print(f"{method} {url} failed: {exc}", file=sys.stderr)
    sys.exit(1)


def _request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers = {"Accept": accept}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        print(f"{method} {url} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        _fail(method, url, e.reason)
    except OSError as e:
        _fail(method, url, e)


def cmd_list(args: argparse.Namespace) -> None:
    _, body, _ = _request("GET", f"{args.server}/api/items")
    items = json.loads(body)
    for item in items:
        print(f"{item.get('slug')}\t{item.get('status', '?')}")


def cmd_trim(args: argparse.Namespace) -> None:
    url = f"{args.server}/api/items/{args.slug}/trim"
    _, body, _ = _request("POST", url, body={"in_frame": args.in_frame, "out_frame": args.out_frame})
    print(body.decode("utf-8", "replace"))


def cmd_seed(args: argparse.Namespace) -> None:
    try:
        x_str, y_str = args.point.split(",")
        x, y = int(x_str), int(y_str)
    except ValueError:
        print("--point must be 'X,Y' integers", file=sys.stderr)
        sys.exit(2)
    url = f"{args.server}/api/items/{args.slug}/seed"
    _, body, _ = _request(
        "POST", url, body={"frame_index": args.frame, "x": x, "y": y}, accept="image/png"
    )
    if args.out:
        Path(args.out).write_bytes(body)
    else:
        print(f"{len(body)} bytes mask received", file=sys.stderr)


def cmd_propagate(args: argparse.Namespace) -> None:
    url = f"{args.server}/api/items/{args.slug}/propagate"
    _request("POST", url)
    if not args.watch:
        return
    events_url = f"{args.server}/api/items/{args.slug}/events"
    req = urllib.request.Request(events_url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req) as r:
            event = None
            for raw in r:
                line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload) if payload else {}
                    except json.JSONDecodeError:
                        data = {"raw": payload}
                    if event == "mask":
                        frame = data.get("frame")
                        print(f"frame {frame} done")
                    elif event == "done":
                        return
                    elif event == "error":
                        print(f"propagate error: {data}", file=sys.stderr)
                        sys.exit(1)
                elif line == "":
                    event = None
    except urllib.error.URLError as e:
        _fail("GET", events_url, e.reason)
    except OSError as e:
        _fail("GET", events_url, e)


def cmd_overlay(args: argparse.Namespace) -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    clip_url = f"{args.server}/clip/{args.slug}.mp4"
    tmp_clip = Path(args.out).with_suffix(".src.mp4")
    try:
        with urllib.request.urlopen(clip_url) as r:
            tmp_clip.write_bytes(r.read())
    except urllib.error.URLError as e:
        _fail("GET", clip_url, e.reason)

    cap = cv2.VideoCapture(str(tmp_clip))
    if not cap.isOpened():
        print(f"cannot open downloaded clip {tmp_clip}", file=sys.stderr)
        sys.exit(1)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(args.out), fourcc, fps, (w, h))

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        mask_url = f"{args.server}/mask/{args.slug}/{idx:05d}.png"
        try:
            with urllib.request.urlopen(mask_url) as r:
                buf = np.frombuffer(r.read(), dtype=np.uint8)
                mask = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                mask = None
            else:
                print(f"GET {mask_url} -> HTTP {e.code}", file=sys.stderr)
                sys.exit(1)
        except urllib.error.URLError as e:
            _fail("GET", mask_url, e.reason)

        if mask is not None and mask.shape[:2] == (h, w):
            green = np.zeros_like(frame)
            green[:, :, 1] = 255
            sel = mask > 127
            frame[sel] = cv2.addWeighted(frame, 0.5, green, 0.5, 0)[sel]
        writer.write(frame)
        idx += 1

    cap.release()
    writer.release()
    tmp_clip.unlink(missing_ok=True)
    print(f"wrote {args.out} ({idx} frames)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=DEFAULT_SERVER)

    p = argparse.ArgumentParser(
        prog="lab/cli.py", description="Headless labeller client.", parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="List items", parents=[common])
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("trim", help="Trim clip to [in,out] frame range", parents=[common])
    sp.add_argument("--slug", required=True)
    sp.add_argument("--in", dest="in_frame", type=int, required=True)
    sp.add_argument("--out", dest="out_frame", type=int, required=True)
    sp.set_defaults(func=cmd_trim)

    sp = sub.add_parser("seed", help="Send a seed point, receive PNG mask", parents=[common])
    sp.add_argument("--slug", required=True)
    sp.add_argument("--frame", type=int, required=True)
    sp.add_argument("--point", required=True, help="X,Y pixel coords")
    sp.add_argument("--out", help="Write mask PNG to this path")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser(
        "propagate", help="Run SAM2 propagation; --watch streams SSE", parents=[common]
    )
    sp.add_argument("--slug", required=True)
    sp.add_argument("--watch", action="store_true")
    sp.set_defaults(func=cmd_propagate)

    sp = sub.add_parser("overlay", help="Compose mask overlay video", parents=[common])
    sp.add_argument("--slug", required=True)
    sp.add_argument("--out", required=True, help="Output mp4 path")
    sp.set_defaults(func=cmd_overlay)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
