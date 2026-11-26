#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI client for the persistent BtWriter Grbl service.

Start grbl_service.py once, then use these commands. Keeping the serial port
open preserves the manually established G92 screen origin between actions.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:8768"


def request(api: str, path: str, payload: dict | None = None, timeout: float = 60.0):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            detail = json.loads(body.decode("utf-8"))
        except Exception:
            detail = body.decode("utf-8", "replace")
        raise RuntimeError(f"Control service returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cannot reach the persistent controller. Start grbl_service.py first; "
            "it owns COM4 and keeps the origin valid."
        ) from exc
    if content_type.startswith("application/json"):
        return json.loads(body.decode("utf-8"))
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Control a BtWriter phone tapper via Grbl")
    parser.add_argument("--api", default=DEFAULT_API, help="local Grbl service URL")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="read controller and origin status")
    sub.add_parser("set-zero", help="set the current pen position as screen center")
    sub.add_parser("stop", help="send Grbl feed hold")

    goto = sub.add_parser("goto", help="raise pen and move to normalized screen coordinates")
    goto.add_argument("u", type=float)
    goto.add_argument("v", type=float)

    jog = sub.add_parser("jog", help="move a small relative distance while pen is raised; before set-zero max 5 mm per axis")
    jog.add_argument("dx", type=float)
    jog.add_argument("dy", type=float)
    jog.add_argument("--feed", type=float, default=1000)

    tap = sub.add_parser("tap", help="tap normalized screen coordinates")
    tap.add_argument("u", type=float)
    tap.add_argument("v", type=float)
    tap.add_argument("--dwell", type=float)

    swipe = sub.add_parser("swipe", help="swipe between normalized screen coordinates")
    swipe.add_argument("u1", type=float)
    swipe.add_argument("v1", type=float)
    swipe.add_argument("u2", type=float)
    swipe.add_argument("v2", type=float)
    swipe.add_argument("--duration", type=float, default=600)

    pen = sub.add_parser("pen", help="raise or lower the pen at the current location")
    pen.add_argument("state", choices=("up", "down"))

    args = parser.parse_args()
    if args.action == "status":
        result = request(args.api, "/api/status")
    elif args.action == "set-zero":
        result = request(args.api, "/api/zero", {})
    elif args.action == "stop":
        result = request(args.api, "/api/stop", {})
    elif args.action == "goto":
        result = request(args.api, "/api/goto", {"u": args.u, "v": args.v})
    elif args.action == "jog":
        result = request(args.api, "/api/jog", {"dx": args.dx, "dy": args.dy, "feed": args.feed})
    elif args.action == "tap":
        result = request(args.api, "/api/tap", {"u": args.u, "v": args.v, "dwell_ms": args.dwell})
    elif args.action == "swipe":
        result = request(args.api, "/api/swipe", {
            "u1": args.u1, "v1": args.v1, "u2": args.u2, "v2": args.v2,
            "duration_ms": args.duration,
        })
    else:
        result = request(args.api, "/api/pen", {"state": args.state})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
