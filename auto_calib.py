#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-time, bounded 2D calibration from five central touch samples.

This is not a per-task fullscreen routine. It fits the full machine-XY to
phone-touch affine map from the center and a +/-5 mm cross around the known
screen-center origin. A missing touch aborts immediately; there is no search.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "tapper_config.json"
SENSOR = "http://127.0.0.1:8765"
CONTROL = "http://127.0.0.1:8768"
OFFSETS = [(0.0, 0.0), (5.0, 0.0), (-5.0, 0.0), (0.0, 5.0), (0.0, -5.0)]


def get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: float = 60.0):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_ack(url: str, payload: dict) -> None:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"Request failed: HTTP {response.status}")


def touch_log(sensor_url: str) -> list[dict]:
    with urllib.request.urlopen(sensor_url.rstrip("/") + "/log", timeout=5) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def save_config(config: dict) -> None:
    temporary = CFG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CFG_PATH)


def camera_marker_pixels(control_url: str) -> int:
    import cv2
    import os

    project_value = os.environ.get("PHONE_AUTOMATION_HOME", "")
    if not project_value:
        raise RuntimeError(
            "相机亮点定位需要可选的 phone_automation 组件（透视矫正）；"
            "请设置 PHONE_AUTOMATION_HOME，或改用控制台的手动亮点标定。"
        )
    project = Path(project_value).expanduser()
    sys.path.insert(0, str(project))
    from phone_automation.vision import ScreenCalibration, warp_phone_screen

    with urllib.request.urlopen(control_url.rstrip("/") + "/api/camera.jpg", timeout=10) as response:
        encoded = np.frombuffer(response.read(), dtype=np.uint8)
    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("Cannot decode camera frame")
    path = project / "runtime" / "screen_calibration.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    height, width = frame.shape[:2]
    old_w, old_h = int(raw.get("source_width", 0)), int(raw.get("source_height", 0))
    if old_w < 2 or old_h < 2 or abs((width / height) / (old_w / old_h) - 1.0) > 0.01:
        raise RuntimeError("Camera framing differs from the saved phone-screen calibration")
    sx, sy = width / old_w, height / old_h
    raw["corners"] = [[x * sx, y * sy] for x, y in raw["corners"]]
    raw["source_width"], raw["source_height"] = width, height
    screen = warp_phone_screen(frame, ScreenCalibration.from_dict(raw))
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.asarray([145, 60, 60]), np.asarray([179, 255, 255]))
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask)
    return max((int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, count)), default=0)


def wait_for_touch(sensor_url: str, previous_count: int, viewport: tuple[float, float]):
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        downs = [row for row in touch_log(sensor_url) if row.get("type") == "down"]
        if len(downs) > previous_count:
            event = downs[-1]
            event_viewport = (float(event.get("w", 0)), float(event.get("h", 0)))
            if any(abs(a - b) > 1 for a, b in zip(event_viewport, viewport)):
                raise RuntimeError("Touch viewport changed during calibration")
            return event, len(downs)
        time.sleep(0.08)
    return None, previous_count


def fit_affine(samples: list[tuple[float, float, float, float]]):
    machine = np.asarray([[x, y, 1.0] for x, y, _, _ in samples], dtype=float)
    if np.linalg.matrix_rank(machine) < 3:
        raise RuntimeError("Calibration samples do not span both machine axes")
    px = np.asarray([sx for _, _, sx, _ in samples], dtype=float)
    py = np.asarray([sy for _, _, _, sy in samples], dtype=float)
    cx, *_ = np.linalg.lstsq(machine, px, rcond=None)
    cy, *_ = np.linalg.lstsq(machine, py, rcond=None)
    affine = np.asarray([cx, cy], dtype=float)
    errors = np.hypot(machine @ cx - px, machine @ cy - py)
    if abs(float(np.linalg.det(affine[:, :2]))) < 1e-6 or not np.all(np.isfinite(errors)):
        raise RuntimeError("Touch mapping is singular or invalid")
    return affine, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit the full screen map from five bounded central touch samples")
    parser.add_argument("--run", action="store_true", help="take five controlled samples within +/-5 mm")
    parser.add_argument("--control", default=CONTROL)
    parser.add_argument("--sensor", default=SENSOR)
    args = parser.parse_args()
    if not args.run:
        print("预检模式：每次普通任务不必全屏标定；--run 仅在已确认屏幕中心周围 ±5 mm 采 5 点拟合整屏坐标。任一点无触控回报即停，不向外搜索，不修改 Z。")
        return 0

    sensor_url, control_url = args.sensor.rstrip("/"), args.control.rstrip("/")
    sensor = get_json(sensor_url + "/status")
    if not sensor.get("ready") or not sensor.get("fullscreen"):
        raise RuntimeError("触控反馈页需在线并全屏；没有移动机器")
    viewport = (float(sensor.get("w", 0)), float(sensor.get("h", 0)))
    if min(viewport) <= 0:
        raise RuntimeError("触控服务没有有效的屏幕尺寸")
    status = get_json(control_url + "/api/status")
    if status.get("state", "").casefold() != "idle" or not status.get("origin_valid"):
        raise RuntimeError("机器必须 Idle 且有有效零点")
    config = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    original = json.loads(json.dumps(config))
    z_down = float(config["z_down"])
    z_rest = float(config.get("z_rest_coordinate", 3.0))
    z_max = float(status.get("settings", {}).get("z_travel_mm") or 9.0)
    if config.get("pen_mode") != "z" or not (0 <= z_down < z_rest <= z_max):
        raise RuntimeError("当前 Z 配置不合法；本流程不会更改 Z")

    post_ack(sensor_url + "/target", {"active": True, "u": 0.5, "v": 0.5, "label": "屏幕中心"})
    committed = False
    try:
        marker = camera_marker_pixels(control_url)
        if marker < 40:
            raise RuntimeError(f"相机未看到中心标记（{marker}px），未开始触点采样")
        downs = [row for row in touch_log(sensor_url) if row.get("type") == "down"]
        count = len(downs)
        samples = []
        print(f"相机确认中心标记（{marker}px）；开始 5 个中心区域样本。", flush=True)
        for index, (x, y) in enumerate(OFFSETS, start=1):
            live = get_json(sensor_url + "/status")
            machine = get_json(control_url + "/api/status")
            if (not live.get("ready") or not live.get("fullscreen")
                    or machine.get("state", "").casefold() != "idle"
                    or not machine.get("origin_valid")):
                raise RuntimeError("传感器或机器状态变化；立即停止")
            live_viewport = (float(live.get("w", 0)), float(live.get("h", 0)))
            if any(abs(a - b) > 1 for a, b in zip(live_viewport, viewport)):
                raise RuntimeError("手机视口变化；立即停止")
            post_json(control_url + "/api/calibration/tap", {
                "x_mm": x, "y_mm": y, "dwell_ms": 120,
            })
            event, count = wait_for_touch(sensor_url, count, viewport)
            if event is None:
                raise RuntimeError(f"中心周围第 {index} 点 ({x:+.0f},{y:+.0f})mm 无触控回报；停止，不继续探点")
            samples.append((x, y, float(event["x"]), float(event["y"])))
            print(f"点 {index}/5：XY=({x:+.0f},{y:+.0f}) → ({event['x']:.1f},{event['y']:.1f}) px", flush=True)

        affine, errors = fit_affine(samples)
        tolerance = 12.0
        if float(errors.max()) > tolerance:
            raise RuntimeError(f"5 点拟合最大残差 {errors.max():.1f}px 超过 {tolerance:.0f}px；不保存")
        jacobian = affine[:, :2].copy()
        center = np.asarray([viewport[0] / 2, viewport[1] / 2], dtype=float)
        home_xy = np.linalg.solve(jacobian, center - affine[:, 2])
        if np.linalg.norm(home_xy) > 8.0:
            raise RuntimeError(f"测得屏幕中心偏离旧零点 {np.linalg.norm(home_xy):.1f}mm；停止，不移向远处")

        config["affine"] = affine.tolist()
        config["screen_css_w"], config["screen_css_h"] = viewport
        config["calibration_points"] = len(samples)
        config["calibration_max_error_px"] = float(errors.max())
        save_config(config)
        post_json(control_url + "/api/reload-calibration", {})
        home = post_json(control_url + "/api/goto", {"u": 0.5, "v": 0.5})
        actual_home = np.asarray(home.get("xy_mm"), dtype=float)
        if actual_home.shape != (2,) or np.linalg.norm(actual_home) > 8.0:
            raise RuntimeError("抬笔回到拟合出的屏幕中心失败；恢复原配置")
        affine[:, 2] += jacobian @ actual_home
        config["affine"] = affine.tolist()
        save_config(config)
        post_json(control_url + "/api/reload-calibration", {})
        post_json(control_url + "/api/zero", {})
        committed = True
        for _ in range(12):
            parked = post_json(control_url + "/api/park-step", {"step_mm": 10})
            if parked.get("camera_ready"):
                break
        print(f"整屏映射标定完成：5 点，最大拟合残差 {errors.max():.1f}px。原点在屏幕中心，Z 未修改。")
        return 0
    finally:
        if not committed and config != original:
            save_config(original)
            try:
                post_json(control_url + "/api/reload-calibration", {})
            except Exception:
                pass
        post_ack(sensor_url + "/target", {"active": False})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCalibration stopped.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Calibration failed: {exc}")
        raise SystemExit(1)
