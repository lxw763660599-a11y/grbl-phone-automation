#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent, loopback-only Grbl controller for the BtWriter phone tapper."""

from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from btwriter_paths import app_base_dir

BASE = app_base_dir()
CONFIG_PATH = BASE / "tapper_config.json"


def _load_camera_backend(library_path: str = ""):
    """Return (camera class, jpeg encoder).

    The bundled ``phone_camera`` module is used by default.  If a directory
    holding an external ``phone_automation`` package is configured via
    ``camera_library_path`` it is preferred, which keeps existing rigs working
    when their camera stack lives elsewhere.
    """
    if library_path:
        import sys

        root = Path(library_path)
        if (root / "phone_automation").is_dir():
            sys.path.insert(0, str(root))
            from phone_automation.vision import IvcamCamera, encode_jpeg, VisionError

            return IvcamCamera, encode_jpeg
    from phone_camera import PhoneCamera, encode_jpeg

    return PhoneCamera, encode_jpeg


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as source:
        return json.load(source)


class GrblError(RuntimeError):
    pass


def _resolve_serial_port(config: dict[str, Any]) -> str:
    requested = str(config.get("port", "auto")).strip()
    if requested.casefold() != "auto":
        return requested

    from serial.tools import list_ports

    ports = list(list_ports.comports())
    ch340 = [port for port in ports if port.vid == 0x1A86 and port.pid == 0x7523]
    candidates = ch340
    if not candidates:
        candidates = [
            port for port in ports
            if port.vid is not None
            and any(key in f"{port.description} {port.hwid}".casefold()
                    for key in ("usb-serial", "usb serial", "ch340", "ch341", "cp210", "ft232", "wch"))
        ]
    if not candidates:
        usb_ports = [port for port in ports if port.vid is not None]
        if len(usb_ports) == 1:
            candidates = usb_ports

    if len(candidates) == 1:
        return str(candidates[0].device)

    found = ", ".join(
        f"{port.device} ({port.description})" for port in ports
    ) or "未发现串口"
    if len(candidates) > 1:
        raise GrblError(f"检测到多个 USB 串口设备：{found}。请在 tapper_config.json 中把 port 设为目标 COM 号。")
    raise GrblError(f"没有识别到写字机 USB 串口。检测到：{found}。请安装 CH340 驱动或在 tapper_config.json 中指定 COM 号。")


class Controller:
    def __init__(self, config: dict[str, Any]):
        import serial

        self.config = config
        self.lock = threading.RLock()
        self.origin_valid = False
        self.xy = (0.0, 0.0)
        self.preorigin_jog_xy = (0.0, 0.0)
        self.firmware = ""
        self.settings: dict[int, float] = {}
        self._camera = None
        self._encode_jpeg = None
        self._camera_lock = threading.Lock()
        self._pen_released = False
        self.last_action_id = 0
        self.last_action_completed = 0.0
        config["port"] = _resolve_serial_port(config)
        self.ser = serial.Serial()
        self.ser.port = str(config.get("port", "COM4"))
        self.ser.baudrate = int(config.get("baud", 115200))
        self.ser.timeout = 0.1
        self.ser.write_timeout = 2.0
        self.ser.rtscts = False
        self.ser.dsrdtr = False
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        time.sleep(float(config.get("boot_wait_seconds", 3.2)))
        self.ser.reset_input_buffer()
        self._drain(0.3)
        self.firmware = self._command("$I")
        raw_settings = self._command("$$")
        for line in raw_settings.splitlines():
            if line.startswith("$") and "=" in line:
                key, value = line[1:].split("=", 1)
                try:
                    self.settings[int(key)] = float(value)
                except ValueError:
                    continue
        self._set_modes()
        resume_xy = config.pop("resume_xy", None)
        if isinstance(resume_xy, (list, tuple)) and len(resume_xy) == 2:
            try:
                resume_x, resume_y = (float(value) for value in resume_xy)
                self._command(f"G92 X{resume_x:.3f} Y{resume_y:.3f}")
                self.xy = (resume_x, resume_y)
                self.origin_valid = True
                temp_path = CONFIG_PATH.with_suffix(".json.tmp")
                temp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp_path.replace(CONFIG_PATH)
            except (TypeError, ValueError, GrblError):
                self.origin_valid = False
        self._pen_up()
        self._assert_idle()

    def close(self) -> None:
        with self.lock:
            if self._camera is not None:
                self._camera.close()
                self._camera = None
            if self.ser.is_open:
                self.ser.close()

    def _drain(self, quiet_seconds: float = 0.15) -> str:
        chunks: list[bytes] = []
        deadline = time.monotonic() + quiet_seconds
        while time.monotonic() < deadline:
            data = self.ser.read(max(1, self.ser.in_waiting))
            if data:
                chunks.append(data)
                deadline = time.monotonic() + quiet_seconds
        return b"".join(chunks).decode("utf-8", "replace")

    def _command(self, line: str, timeout: float = 8.0) -> str:
        if "\n" in line or "\r" in line:
            raise GrblError("A Grbl command must be one line")
        self.ser.write((line + "\n").encode("ascii"))
        self.ser.flush()
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            data = self.ser.read(max(1, self.ser.in_waiting))
            if not data:
                continue
            chunks.append(data)
            text = b"".join(chunks).decode("utf-8", "replace")
            lines = [part.strip() for part in text.replace("\r", "\n").split("\n")]
            for response in lines:
                if response.lower().startswith("error:") or response.startswith("ALARM:"):
                    if "ALARM:" in response:
                        self.origin_valid = False
                    raise GrblError(f"Grbl rejected {line!r}: {response}")
                if response.lower() == "ok":
                    return text.strip()
        self.origin_valid = False
        raise GrblError(f"Grbl did not acknowledge {line!r}")

    def status(self) -> dict[str, Any]:
        with self.lock:
            self.ser.write(b"?")
            self.ser.flush()
            deadline = time.monotonic() + 2.0
            text = ""
            while time.monotonic() < deadline:
                data = self.ser.read(max(1, self.ser.in_waiting))
                if not data:
                    continue
                text += data.decode("utf-8", "replace")
                start = text.find("<")
                end = text.find(">", start + 1)
                if start >= 0 and end > start:
                    raw = text[start : end + 1]
                    parts = raw[1:-1].split("|")
                    state = parts[0]
                    if state.casefold() == "alarm":
                        self.origin_valid = False
                    result: dict[str, Any] = {
                        "state": state,
                        "raw": raw,
                        "origin_valid": self.origin_valid,
                        "position_xy": list(self.xy) if self.origin_valid else None,
                        "firmware": self.firmware,
                        "port": self.config.get("port", "COM4"),
                        "baud": self.config.get("baud", 115200),
                    }
                    for item in parts[1:]:
                        if ":" not in item:
                            continue
                        key, value = item.split(":", 1)
                        if key in {"MPos", "WPos"}:
                            try:
                                result[key] = [float(n) for n in value.split(",")]
                            except ValueError:
                                pass
                    result["settings"] = {
                        "soft_limits": int(self.settings.get(20, 0)),
                        "hard_limits": int(self.settings.get(21, 0)),
                        "homing": int(self.settings.get(22, 0)),
                        "x_travel_mm": self.settings.get(130),
                        "y_travel_mm": self.settings.get(131),
                        "z_travel_mm": self.settings.get(132),
                        "z_max_rate_mm_min": self.settings.get(112),
                    }
                    return result
            raise GrblError("Timed out waiting for the Grbl status report")

    def _assert_idle(self) -> dict[str, Any]:
        status = self.status()
        state = str(status["state"]).casefold()
        if state == "alarm":
            self.origin_valid = False
            raise GrblError("Grbl is in ALARM; check the machine and re-establish its origin")
        if state != "idle":
            raise GrblError(f"Grbl must be Idle before a new action (state={status['state']})")
        return status

    def _wait_idle(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            state = str(status["state"]).casefold()
            if state == "idle":
                return
            if state == "alarm":
                self.origin_valid = False
                raise GrblError("Grbl entered ALARM during motion; origin is no longer trusted")
            if state not in {"run", "hold", "jog"}:
                raise GrblError(f"Unexpected Grbl state during motion: {status['state']}")
            time.sleep(0.025)
        self.origin_valid = False
        raise GrblError("Motion did not finish before the timeout")

    def _set_modes(self) -> None:
        self._command("G21 G90")

    def set_zero(self) -> dict[str, Any]:
        with self.lock:
            self._assert_idle()
            self._set_modes()
            self._command("G92 X0 Y0")
            self.xy = (0.0, 0.0)
            self.origin_valid = True
            return {"ok": True, "origin": "screen center", "xy": [0.0, 0.0]}

    def _xy_from_uv(self, u: float, v: float, *, physical: bool = False) -> tuple[float, float]:
        if not all(math.isfinite(n) and 0.0 <= n <= 1.0 for n in (u, v)):
            raise GrblError("u and v must be finite screen fractions in [0, 1]")
        affine = None if physical else self.config.get("affine")
        if affine:
            if not isinstance(affine, list) or len(affine) != 2 or any(len(row) != 3 for row in affine):
                raise GrblError("Saved affine calibration must be two rows of three values")
            a, b, c = (float(n) for n in affine[0])
            d, e, f = (float(n) for n in affine[1])
            det = a * e - b * d
            if abs(det) < 1e-8:
                raise GrblError("Saved screen calibration is singular")
            css_w = float(self.config.get("screen_css_w", 0))
            css_h = float(self.config.get("screen_css_h", 0))
            if css_w <= 0 or css_h <= 0:
                raise GrblError("Saved screen calibration is missing viewport dimensions")
            margin_px = float(self.config.get("screen_margin_px", 12.0))
            if u * css_w < margin_px or (1.0 - u) * css_w < margin_px:
                raise GrblError("Target is too close to the left or right screen edge")
            if v * css_h < margin_px or (1.0 - v) * css_h < margin_px:
                raise GrblError("Target is too close to the top or bottom screen edge")
            tx, ty = u * css_w - c, v * css_h - f
            return ((e * tx - b * ty) / det, (a * ty - d * tx) / det)
        if not physical:
            raise GrblError("Screen mapping is not calibrated; run auto_calib.py before tapping")
        width = float(self.config["screen_w_mm"])
        height = float(self.config["screen_h_mm"])
        return ((u - 0.5) * width, (v - 0.5) * height)

    def _check_xy(self, x: float, y: float, radius_limit: float | None = None) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            raise GrblError("Machine coordinates must be finite")
        # With no homing or limit switches, the manually established screen
        # center is the only reference. Restrict moves to the inner half-bed.
        max_x = max(0.0, float(self.settings.get(130, 300.0)) / 2.0 - 5.0)
        max_y = max(0.0, float(self.settings.get(131, 300.0)) / 2.0 - 5.0)
        limit = radius_limit or float(self.config.get("max_screen_radius_mm", 85.5))
        if math.hypot(x, y) > limit:
            raise GrblError(f"Target is outside the calibrated screen envelope ({limit:.1f} mm radius)")
        if abs(x) > max_x or abs(y) > max_y:
            raise GrblError(f"Target exceeds the conservative +/-{max_x:.1f}/+/-{max_y:.1f} mm travel around screen center")

    def _require_origin(self) -> None:
        if not self.origin_valid:
            raise GrblError("Manually place the pen at screen center, then set /api/zero before moving")

    def _z_rest_at(self, x: float | None = None, y: float | None = None) -> float:
        """Interpolate measured spring-rest coordinates across the phone surface."""
        fallback = float(self.config.get("z_rest_coordinate", 3.0))
        points = self.config.get("z_contact_map", [])
        affine = self.config.get("affine")
        css_w = float(self.config.get("screen_css_w", 0.0))
        css_h = float(self.config.get("screen_css_h", 0.0))
        if not points or not affine or css_w <= 0 or css_h <= 0:
            return fallback
        x = self.xy[0] if x is None else x
        y = self.xy[1] if y is None else y
        a, b, c = (float(value) for value in affine[0])
        d, e, f = (float(value) for value in affine[1])
        u, v = (a * x + b * y + c) / css_w, (d * x + e * y + f) / css_h
        samples: list[tuple[float, float, float]] = []
        for point in points:
            try:
                pu, pv = float(point["u"]), float(point["v"])
                depth = float(point["z_rest_mm"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 <= pu <= 1.0 and 0.0 <= pv <= 1.0 and math.isfinite(depth):
                samples.append((pu, pv, depth))
        if not samples:
            return fallback
        distances = [math.hypot(u - pu, v - pv) for pu, pv, _ in samples]
        nearest = min(range(len(samples)), key=distances.__getitem__)
        if distances[nearest] < 1e-6:
            depth = samples[nearest][2]
        else:
            weights = [1.0 / max(distance, 0.02) ** 2 for distance in distances]
            depth = sum(weight * sample[2] for weight, sample in zip(weights, samples)) / sum(weights)
        margin = float(self.config.get("z_contact_margin_mm", 0.2))
        return min(float(self.settings.get(132, 9.0)), depth + margin)

    def _pen_up(self) -> None:
        if self._pen_released:
            return
        mode = self.config.get("pen_mode", "z")
        if mode == "z":
            # The mechanism returns by spring. G92 changes the logical work
            # coordinate only; it does not drive Z and avoids a repeated G1
            # target becoming a no-op after the spring has physically reset.
            rest = self._z_rest_at()
            if not 0.0 <= rest <= float(self.settings.get(132, 9.0)):
                raise GrblError("Configured spring-rest coordinate is outside Z travel")
            delay = float(self.config.get("z_release_settle_seconds", 0.35))
            if not 0.0 <= delay <= 3.0:
                raise GrblError("z_release_settle_seconds must be between 0 and 3")
            time.sleep(delay)
            self._command(f"G92 Z{rest:.3f}")
            self._pen_released = True
            return
        elif mode == "servo":
            self._command(str(self.config.get("pen_up_cmd", "M5")))
            self._pen_released = True
        else:
            raise GrblError("pen_mode must be 'z' or 'servo'")

    def _pen_down(self, wait_for_idle: bool = True) -> None:
        self._pen_released = False
        mode = self.config.get("pen_mode", "z")
        if mode == "z":
            z = float(self.config.get("z_down", 0.0))
            if not 0.0 <= z <= float(self.settings.get(132, 9.0)):
                raise GrblError("Configured pen-down position is outside the Z travel")
            rest = self._z_rest_at()
            if not z < rest <= float(self.settings.get(132, 9.0)):
                raise GrblError("Local spring-rest coordinate must be above the pen-down coordinate and within Z travel")
            # Re-label the spring-returned Z at this XY so a non-flat phone
            # gets its own measured contact depth without an upward move.
            self._command(f"G92 Z{rest:.3f}")
            requested_feed = float(self.config.get("z_down_feed", 1200.0))
            if not math.isfinite(requested_feed) or requested_feed <= 0:
                raise GrblError("z_down_feed must be a positive finite number")
            feed = min(requested_feed, float(self.settings.get(112, requested_feed)))
            self._command(f"G1 Z{z:.3f} F{feed:.0f}")
            if wait_for_idle:
                self._wait_idle()
        elif mode == "servo":
            self._command(str(self.config.get("pen_down_cmd", "M3 S1000")))
        else:
            raise GrblError("pen_mode must be 'z' or 'servo'")

    def _goto_xy(self, x: float, y: float, radius_limit: float | None = None) -> None:
        self._check_xy(x, y, radius_limit)
        self._set_modes()
        self._pen_up()
        self._command(f"G0 X{x:.3f} Y{y:.3f}")
        self._wait_idle()
        self.xy = (x, y)

    def _park_for_camera(self) -> list[float] | None:
        """Move the lifted toolhead clear before the caller captures a frame."""
        target = self.config.get("camera_park_xy_mm")
        if not self.config.get("park_after_action", True) or target is None:
            return None
        if not isinstance(target, list) or len(target) != 2:
            raise GrblError("camera_park_xy_mm must be [x_mm, y_mm]")
        x, y = (float(value) for value in target)
        candidates = self.config.get("camera_park_candidates_xy_mm", [])
        if candidates:
            choices = [(x, y)]
            for candidate in candidates:
                if not isinstance(candidate, list) or len(candidate) != 2:
                    raise GrblError("Camera park candidates must be XY pairs")
                choices.append((float(candidate[0]), float(candidate[1])))
            radius = float(self.config.get("camera_park_radius_mm", 145.0))
            for cx, cy in choices:
                self._check_xy(cx, cy, radius)
            x, y = min(choices, key=lambda point: math.hypot(point[0]-self.xy[0], point[1]-self.xy[1]))
        radius = float(self.config.get("camera_park_radius_mm", 145.0))
        self._goto_xy(x, y, radius)
        delay = float(self.config.get("camera_clear_settle_seconds", 0.4))
        if not math.isfinite(delay) or not 0 <= delay <= 3:
            raise GrblError("camera_clear_settle_seconds must be between 0 and 3")
        time.sleep(delay)
        return [x, y]

    def park_step(self, step_mm: float = 10.0) -> dict[str, Any]:
        """Advance toward the configured camera-clear point in bounded steps."""
        with self.lock:
            self._require_origin()
            self._assert_idle()
            if not math.isfinite(step_mm) or not 1.0 <= step_mm <= 10.0:
                raise GrblError("Park step must be between 1 and 10 mm per axis")
            target = self.config.get("camera_park_xy_mm")
            if not isinstance(target, list) or len(target) != 2:
                raise GrblError("camera_park_xy_mm must be [x_mm, y_mm]")
            tx, ty = (float(value) for value in target)

            def advance(current: float, goal: float) -> float:
                delta = goal - current
                return current + math.copysign(min(abs(delta), step_mm), delta) if delta else current

            x, y = advance(self.xy[0], tx), advance(self.xy[1], ty)
            radius = float(self.config.get("camera_park_radius_mm", 145.0))
            self._goto_xy(x, y, radius)
            finished = math.isclose(x, tx, abs_tol=1e-6) and math.isclose(y, ty, abs_tol=1e-6)
            if finished:
                delay = float(self.config.get("camera_clear_settle_seconds", 0.4))
                time.sleep(delay)
            return {"ok": True, "action": "park_step", "xy_mm": [x, y],
                    "target_xy_mm": [tx, ty], "camera_ready": finished}

    def goto(self, u: float, v: float) -> dict[str, Any]:
        with self.lock:
            self._require_origin()
            self._assert_idle()
            x, y = self._xy_from_uv(u, v)
            self._goto_xy(x, y)
            return {"ok": True, "action": "goto", "uv": [u, v], "xy_mm": [x, y]}

    def jog(self, dx: float, dy: float, feed: float = 1000.0) -> dict[str, Any]:
        with self.lock:
            self._assert_idle()
            if not all(math.isfinite(n) for n in (dx, dy, feed)):
                raise GrblError("Jog values must be finite")
            if not 100 <= feed <= 3000:
                raise GrblError("Jog feed must be between 100 and 3000 mm/min")
            if self.origin_valid:
                if max(abs(dx), abs(dy)) > 10:
                    raise GrblError("Referenced jog must be no larger than 10 mm per axis")
                x, y = self.xy[0] + dx, self.xy[1] + dy
                # Jogging is the operator's manual calibration control. Keep
                # per-axis Grbl travel guards, but allow the wider radius needed
                # when the temporary screen origin is not at bed center.
                jog_radius = float(self.config.get("jog_limit_radius_mm", 205.0))
                self._check_xy(x, y, radius_limit=jog_radius)
            else:
                # Before set-zero, only permit short relative moves. The
                # machine has no homing/limits, so absolute coordinates are
                # not trustworthy yet.
                if max(abs(dx), abs(dy)) > 5:
                    raise GrblError("Before set-zero, jog is limited to 5 mm per axis per step")
                x = self.preorigin_jog_xy[0] + dx
                y = self.preorigin_jog_xy[1] + dy
                limit = float(self.config.get("preorigin_jog_radius_mm", 30.0))
                if math.hypot(x, y) > limit:
                    raise GrblError(f"Before set-zero, cumulative jog is limited to {limit:g} mm from startup")
            self._set_modes()
            self._pen_up()
            if self.origin_valid:
                self._command(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
                self._wait_idle()
                self.xy = (x, y)
                return {"ok": True, "action": "jog", "referenced": True, "xy_mm": [x, y]}
            try:
                self._command("G91")
                self._command(f"G1 X{dx:.3f} Y{dy:.3f} F{feed:.0f}")
                self._wait_idle()
            finally:
                self._command("G90")
            self.preorigin_jog_xy = (x, y)
            return {"ok": True, "action": "jog", "referenced": False,
                    "relative_step_mm": [dx, dy], "cumulative_mm": [x, y]}

    def tap(self, u: float, v: float, dwell_ms: float | None = None, *, physical: bool = False) -> dict[str, Any]:
        with self.lock:
            self._require_origin()
            self._assert_idle()
            x, y = self._xy_from_uv(u, v, physical=physical)
            dwell = float(self.config.get("dwell_ms", 120) if dwell_ms is None else dwell_ms)
            return self._tap_xy(x, y, dwell, u=u, v=v)

    def tap_sequence(self, points: list[dict[str, Any]]) -> dict[str, Any]:
        """Perform a bounded series of separate taps, parking only once at the end."""
        if not isinstance(points, list) or not 1 <= len(points) <= 50:
            raise GrblError("Tap sequence must contain between 1 and 50 points")
        with self.lock:
            self._require_origin()
            self._assert_idle()
            prepared: list[tuple[float, float, float, float, float]] = []
            default_dwell = float(self.config.get("dwell_ms", 120))
            # Validate the entire sequence before moving, so one bad coordinate
            # cannot leave a partially typed string on the phone.
            for point in points:
                if not isinstance(point, dict):
                    raise GrblError("Each tap sequence point must be an object")
                u, v = float(point["u"]), float(point["v"])
                x, y = self._xy_from_uv(u, v)
                dwell = float(point.get("dwell_ms", default_dwell))
                self._check_xy(x, y)
                if not math.isfinite(dwell) or not 40 <= dwell <= 1500:
                    raise GrblError("Tap dwell must be between 40 and 1500 ms")
                prepared.append((u, v, x, y, dwell))
            results = [
                self._tap_xy(x, y, dwell, u=u, v=v, park_after=False)
                for u, v, x, y, dwell in prepared
            ]
            parked = self._park_for_camera()
            return {
                "ok": True,
                "action": "tap_sequence",
                "count": len(results),
                "points": results,
                "parked_xy_mm": parked,
                "camera_ready": parked is not None,
            }

    def _tap_xy(self, x: float, y: float, dwell: float, *, radius_limit: float | None = None,
                u: float | None = None, v: float | None = None,
                park_after: bool = True) -> dict[str, Any]:
        self._check_xy(x, y, radius_limit)
        if not math.isfinite(dwell) or not 40 <= dwell <= 1500:
            raise GrblError("Tap dwell must be between 40 and 1500 ms")
        self._goto_xy(x, y, radius_limit)
        try:
            self._pen_down()
            time.sleep(dwell / 1000.0)
        finally:
            self._pen_up()
        parked = self._park_for_camera() if park_after else None
        result: dict[str, Any] = {"ok": True, "action": "tap", "xy_mm": [x, y], "dwell_ms": dwell}
        if parked is not None:
            result["parked_xy_mm"] = parked
            result["camera_ready"] = True
        if u is not None and v is not None:
            result["uv"] = [u, v]
        return result

    def calibration_tap(self, x: float, y: float, dwell_ms: float = 180.0) -> dict[str, Any]:
        with self.lock:
            self._require_origin()
            self._assert_idle()
            if not all(math.isfinite(n) for n in (x, y)) or max(abs(x), abs(y)) > 10.0:
                raise GrblError("Calibration taps are limited to a +/-10 mm square around screen center")
            # The calibration sensor logs touches itself; keep this wizard's
            # movements local and do not travel to the camera park point.
            return self._tap_xy(float(x), float(y), float(dwell_ms), radius_limit=15.0,
                                park_after=False)

    def swipe(self, u1: float, v1: float, u2: float, v2: float, duration_ms: float = 600) -> dict[str, Any]:
        with self.lock:
            self._require_origin()
            self._assert_idle()
            x1, y1 = self._xy_from_uv(u1, v1)
            x2, y2 = self._xy_from_uv(u2, v2)
            self._check_xy(x1, y1)
            self._check_xy(x2, y2)
            duration = float(duration_ms)
            if not math.isfinite(duration) or not 100 <= duration <= 5000:
                raise GrblError("Swipe duration must be between 100 and 5000 ms")
            distance = math.hypot(x2 - x1, y2 - y1)
            feed = max(300.0, min(5000.0, distance / (duration / 1000.0) * 60.0))
            self._goto_xy(x1, y1)
            try:
                if self.config.get("pen_mode", "z") == "z":
                    self._pen_released = False
                    z = float(self.config.get("z_down", 0.0))
                    rest = self._z_rest_at(x1, y1)
                    if not z < rest <= float(self.settings.get(132, 9.0)):
                        raise GrblError("Local spring-rest coordinate must be above pen-down and within Z travel")
                    self._command(f"G92 Z{rest:.3f}")
                    # Start XY while Z is descending. This prevents a stationary
                    # press on the screen from turning the drag into a long press.
                    # Move far enough, fast enough, during the descent to get
                    # beyond Android's long-press slop as soon as contact starts.
                    approach = min(10.0, distance * 0.12)
                    ratio = approach / distance if distance > 0 else 0.0
                    xa, ya = x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio
                    self._command(f"G1 X{xa:.3f} Y{ya:.3f} Z{z:.3f} F900")
                    self._command(f"G1 X{x2:.3f} Y{y2:.3f} F{feed:.0f}")
                else:
                    self._pen_down()
                    self._command(f"G1 X{x2:.3f} Y{y2:.3f} F{feed:.0f}")
                self._wait_idle()
                self.xy = (x2, y2)
            finally:
                self._pen_up()
            parked = self._park_for_camera()
            result: dict[str, Any] = {"ok": True, "action": "swipe", "from": [u1, v1], "to": [u2, v2], "feed": feed}
            if parked is not None:
                result["parked_xy_mm"] = parked
                result["camera_ready"] = True
            return result

    def pen(self, up: bool) -> dict[str, Any]:
        with self.lock:
            self._require_origin()
            self._assert_idle()
            (self._pen_up if up else self._pen_down)()
            return {"ok": True, "pen": "up" if up else "down"}

    def stop(self) -> dict[str, Any]:
        # Grbl's realtime hold byte must bypass the motion lock so another
        # HTTP thread can stop a long tap/swipe while it owns that lock.
        self.ser.write(b"!")
        self.ser.flush()
        return {"ok": True, "action": "feed_hold"}

    def reload_calibration(self) -> dict[str, Any]:
        with self.lock:
            saved = load_config()
            if str(saved.get("port")) != str(self.config.get("port")) or int(saved.get("baud", 0)) != int(self.config.get("baud", 0)):
                raise GrblError("Port settings changed; restart the service to apply them")
            for key in ("affine", "screen_css_w", "screen_css_h", "calibration_points", "calibration_max_error_px",
                        "z_rest_coordinate", "z_contact_map", "z_contact_margin_mm", "jog_limit_radius_mm",
                        "max_screen_radius_mm", "camera_park_xy_mm", "camera_park_candidates_xy_mm",
                        "camera_clear_settle_seconds"):
                if key in saved:
                    self.config[key] = saved[key]
                else:
                    self.config.pop(key, None)
            rest = float(self.config.get("z_rest_coordinate", 3.0))
            if not 0.0 < rest <= float(self.settings.get(132, 9.0)):
                raise GrblError("Configured spring-rest coordinate is outside Z travel")
            for point in self.config.get("z_contact_map", []):
                u, v = float(point["u"]), float(point["v"])
                depth = float(point["z_rest_mm"])
                if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 and 0.0 < depth <= float(self.settings.get(132, 9.0))):
                    raise GrblError("Z contact-map samples must be inside the screen and Z travel")
            return {"ok": True, "calibrated": bool(self.config.get("affine"))}

    def camera_jpeg(self) -> bytes:
        return self.camera_snapshot()[0]

    def camera_snapshot(self) -> tuple[bytes, dict[str, Any]]:
        with self._camera_lock:
            if self._camera is None:
                CameraClass, self._encode_jpeg = _load_camera_backend(
                    str(self.config.get("camera_library_path", "")).strip()
                )
                self._camera = CameraClass(
                    index=int(self.config.get("camera_index", 0)),
                    width=int(self.config.get("camera_width", 1280)),
                    height=int(self.config.get("camera_height", 720)),
                    rotate_clockwise=bool(self.config.get("camera_rotate_clockwise", True)),
                    warmup_frames=3,
                )
            frame = self._camera.frame()
            return self._encode_jpeg(frame), dict(self._camera.last_frame_metadata)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: Controller):
        super().__init__(address, Handler)
        self.controller = controller


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def controller(self) -> Controller:
        return self.server.controller  # type: ignore[attr-defined,return-value]

    def log_message(self, *_: object) -> None:
        pass

    def _reply(self, code: int, body: Any, content_type: str = "application/json",
               frame_metadata: dict[str, Any] | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if frame_metadata is not None:
            metadata = frame_metadata
            self.send_header("X-Frame-Sequence", str(metadata.get("sequence", 0)))
            self.send_header("X-Frame-Read-Started", str(metadata.get("read_started_monotonic", 0)))
            self.send_header("X-Frame-Age-Ms", str(metadata.get("frame_age_ms", 0)))
            self.send_header("X-Last-Action-Id", str(self.controller.last_action_id))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 2 or length > 4096:
            raise GrblError("Request body must be JSON and at most 4 KB")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise GrblError("Request JSON must be an object")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                self._reply(200, self.controller.status())
            elif path == "/api/camera.jpg":
                jpeg, metadata = self.controller.camera_snapshot()
                self._reply(200, jpeg, "image/jpeg", metadata)
            elif path == "/health":
                self._reply(200, {"ok": True, "service": "btwriter-grbl", "profile": "speed-v1",
                                  "features": ["latest-frame", "tap-sequence", "near-park"]})
            else:
                self._reply(404, {"error": "not found"})
        except Exception as exc:
            self._reply(503, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            action_started = time.monotonic()
            body = self._body()
            if path == "/api/zero":
                result = self.controller.set_zero()
            elif path == "/api/goto":
                result = self.controller.goto(float(body["u"]), float(body["v"]))
            elif path == "/api/jog":
                result = self.controller.jog(float(body["dx"]), float(body["dy"]), float(body.get("feed", 1000)))
            elif path == "/api/tap":
                result = self.controller.tap(
                    float(body["u"]), float(body["v"]), body.get("dwell_ms"),
                    physical=bool(body.get("physical", False)),
                )
            elif path == "/api/tap-sequence":
                result = self.controller.tap_sequence(body.get("points"))
            elif path == "/api/calibration/tap":
                result = self.controller.calibration_tap(
                    float(body["x_mm"]), float(body["y_mm"]), float(body.get("dwell_ms", 180)),
                )
            elif path == "/api/park-step":
                result = self.controller.park_step(float(body.get("step_mm", 10)))
            elif path == "/api/swipe":
                result = self.controller.swipe(
                    float(body["u1"]), float(body["v1"]), float(body["u2"]), float(body["v2"]),
                    float(body.get("duration_ms", 600)),
                )
            elif path == "/api/pen":
                if body.get("state") not in {"up", "down"}:
                    raise GrblError("state must be 'up' or 'down'")
                result = self.controller.pen(body["state"] == "up")
            elif path == "/api/stop":
                result = self.controller.stop()
            elif path == "/api/reload-calibration":
                result = self.controller.reload_calibration()
            else:
                self._reply(404, {"error": "not found"})
                return
            if path in {"/api/tap", "/api/tap-sequence", "/api/swipe", "/api/goto", "/api/jog", "/api/pen"}:
                self.controller.last_action_id += 1
                self.controller.last_action_completed = time.monotonic()
                result["action_id"] = self.controller.last_action_id
                result["completed_monotonic"] = self.controller.last_action_completed
                result["elapsed_ms"] = round((time.monotonic() - action_started) * 1000, 1)
            self._reply(200, result)
        except (GrblError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:
            self._reply(500, {"error": str(exc)})


def main() -> None:
    config = load_config()
    controller = Controller(config)
    host = str(config.get("service_host", "127.0.0.1"))
    port = int(config.get("service_port", 8768))
    server = Server((host, port), controller)
    status = controller.status()
    print(f"BtWriter Grbl service listening at http://{host}:{port}", flush=True)
    print(f"{status['port']} {status['state']} · origin={'set' if status['origin_valid'] else 'unset'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BtWriter service failed: {exc}")
        raise SystemExit(1)
