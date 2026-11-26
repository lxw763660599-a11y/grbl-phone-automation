#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure the minimum spring stroke that registers a touch across the phone."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "tapper_config.json"
CONTROL = "http://127.0.0.1:8768"
SENSOR = "http://127.0.0.1:8765"
GRID = (0.2, 0.5, 0.8)
BASE_DEPTH = 5.3
STEP = 0.2
MAX_DEPTH = 7.5


def request_json(url: str, payload: dict | None = None, timeout: float = 30.0):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def touch_log() -> list[dict]:
    with urllib.request.urlopen(SENSOR + "/log", timeout=3) as response:
        body = response.read().decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def save_config(config: dict) -> None:
    temp = CFG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(CFG_PATH)


def set_depth(config: dict, depth: float) -> None:
    config["z_rest_coordinate"] = round(depth, 3)
    save_config(config)
    request_json(CONTROL + "/api/reload-calibration", {})


def tap_and_wait(u: float, v: float, dwell_ms: int = 500) -> dict | None:
    before = sum(1 for item in touch_log() if item.get("type") == "down")
    request_json(CONTROL + "/api/tap", {"u": u, "v": v, "dwell_ms": dwell_ms})
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        downs = [item for item in touch_log() if item.get("type") == "down"]
        if len(downs) > before:
            return downs[-1]
        time.sleep(0.08)
    return None


def main() -> int:
    sensor = request_json(SENSOR + "/status")
    controller = request_json(CONTROL + "/api/status")
    if not sensor.get("ready"):
        raise RuntimeError("Phone touch page is offline")
    if controller.get("state", "").casefold() != "idle" or not controller.get("origin_valid"):
        raise RuntimeError("Controller must be Idle with a valid screen-center origin")
    if not controller.get("affine") and not json.loads(CFG_PATH.read_text(encoding="utf-8")).get("affine"):
        raise RuntimeError("Screen coordinates must be calibrated before pressure mapping")

    config = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    z_max = float(controller.get("settings", {}).get("z_travel_mm") or 9.0)
    max_depth = min(MAX_DEPTH, z_max)
    if max_depth < BASE_DEPTH:
        raise RuntimeError("Configured Z travel is too short for the measured 5.3 mm contact stroke")

    samples: list[dict] = []
    missing: list[tuple[float, float]] = []
    points = [(u, v) for v in GRID for u in GRID]
    print(f"开始 {len(points)} 点下压力扫描：从 {BASE_DEPTH:.1f} mm 开始，每次加 {STEP:.1f} mm，最多到 {max_depth:.1f} mm。", flush=True)

    for u, v in points:
        contact_depth = None
        depth = BASE_DEPTH
        while depth <= max_depth + 1e-6:
            set_depth(config, depth)
            event = tap_and_wait(u, v)
            if event is not None:
                contact_depth = depth
                print(f"  u={u:.2f} v={v:.2f}: {depth:.1f} mm -> touch ({event['x']:.1f},{event['y']:.1f})", flush=True)
                break
            print(f"  u={u:.2f} v={v:.2f}: {depth:.1f} mm 无信号", flush=True)
            depth = round(depth + STEP, 3)
        if contact_depth is None:
            missing.append((u, v))
        else:
            samples.append({"u": u, "v": v, "z_rest_mm": contact_depth})

    set_depth(config, BASE_DEPTH)
    if missing:
        config.pop("z_contact_map", None)
        save_config(config)
        request_json(CONTROL + "/api/reload-calibration", {})
        missing_text = ", ".join(f"({u:.2f},{v:.2f})" for u, v in missing)
        raise RuntimeError(f"No touch at maximum depth for: {missing_text}; no partial map was activated")

    config["z_contact_map"] = samples
    config["z_contact_margin_mm"] = 0.2
    save_config(config)
    request_json(CONTROL + "/api/reload-calibration", {})
    print(f"已保存 {len(samples)} 点局部下压力图；点击会按位置插值，并在实测触发深度上加 0.2 mm 余量。", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nPressure calibration stopped.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Pressure calibration failed: {exc}")
        raise SystemExit(1)
