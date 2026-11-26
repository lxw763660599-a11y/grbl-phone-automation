#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local visual console for the BtWriter phone tapper."""

from __future__ import annotations

import json
import math
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
import numpy as np
from btwriter_paths import app_base_dir

from PIL import Image, ImageTk

BASE = app_base_dir()
API = "http://127.0.0.1:8768"
SENSOR = "http://127.0.0.1:8765"
MANUAL_TARGETS = [
    (0.50, 0.50, "中心"),
    (0.20, 0.20, "左上"),
    (0.80, 0.20, "右上"),
    (0.80, 0.80, "右下"),
    (0.20, 0.80, "左下"),
]
# A camera-verified interior point from the current phone mounting; it lets
# the operator start from the configured camera-clear park position safely.
MANUAL_APPROACH_UV = (0.465, 0.528)
BG = "#0b1020"
CARD = "#131b2e"
CARD_2 = "#18233a"
TEXT = "#edf2ff"
MUTED = "#9aa9c4"
ACCENT = "#64d2ff"
GREEN = "#53d6a0"
RED = "#ff6b79"


def request(path: str, payload: dict | None = None, timeout: float = 60.0, base: str = API):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.dumps(json.loads(detail), ensure_ascii=False)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("控制服务未连接。请点击右上角“启动 / 连接设备”。") from exc
    return json.loads(body.decode("utf-8")) if content_type.startswith("application/json") else body


class TapperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("写字机 · 手机触控台")
        self.root.geometry("1240x820+40+40")
        self.root.minsize(1020, 700)
        self.root.configure(bg=BG)
        self.busy = False
        self.status_polling = False
        self.camera_polling = False
        self.last_image: Image.Image | None = None
        self.photo = None
        self.manual_samples: list[dict] = []
        self.manual_index = 0
        self.manual_viewport: tuple[float, float] | None = None
        self.manual_active = False
        self._setup_style()
        self._build()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.after(500, self._poll_status)
        self.root.after(900, self._poll_camera)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 19, "bold"))
        style.configure("SubTitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        style.configure("Value.TLabel", background=CARD, foreground=ACCENT, font=("Consolas", 11, "bold"))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=(11, 8), background=CARD_2, foreground=TEXT, borderwidth=0)
        style.map("TButton", background=[("active", "#263653"), ("disabled", "#20293a")], foreground=[("disabled", "#68758d")])
        style.configure("Accent.TButton", background="#176b8b", foreground="white")
        style.map("Accent.TButton", background=[("active", "#2186a9")])
        style.configure("Danger.TButton", background="#792c3a", foreground="white")
        style.map("Danger.TButton", background=[("active", "#a83e50")])
        style.configure("TEntry", fieldbackground="#0e1728", foreground=TEXT, insertcolor=TEXT, bordercolor="#2a3953", padding=7)
        style.configure("TSpinbox", fieldbackground="#0e1728", foreground=TEXT, arrowcolor=TEXT, bordercolor="#2a3953", padding=5)

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = tk.Frame(self.root, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 12))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="写字机 · 手机触控台", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="摄像头观察  /  Grbl 控制  /  单步复核", style="SubTitle.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.connection_label = tk.Label(header, text="● 未连接", bg=BG, fg=RED, font=("Segoe UI", 10, "bold"))
        self.connection_label.grid(row=0, column=1, sticky="e", padx=(0, 12))
        ttk.Button(header, text="启动 / 连接设备", style="Accent.TButton", command=self.connect_service).grid(row=0, column=2, rowspan=2, sticky="e")

        body = ttk.Frame(self.root)
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 12))
        body.columnconfigure(0, weight=6, uniform="body")
        body.columnconfigure(1, weight=5, uniform="body")
        body.rowconfigure(0, weight=1)

        self._build_camera(body)
        self._build_controls(body)

        footer = ttk.Frame(self.root)
        footer.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 16))
        footer.columnconfigure(0, weight=1)
        self.message = tk.StringVar(value="先确认画面与笔尖位置；每次动作会等待机器让位后再刷新摄像头。")
        tk.Label(footer, textvariable=self.message, bg=BG, fg=MUTED, anchor="w", font=("Segoe UI", 9)).grid(row=0, column=0, sticky="ew")
        ttk.Button(footer, text="急停 / 暂停", style="Danger.TButton", command=self.stop_machine).grid(row=0, column=1, sticky="e")

    def _card(self, parent, title: str, row: int, column: int, **grid_options):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=14)
        frame.grid(row=row, column=column, sticky="nsew", **grid_options)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        return frame

    def _build_camera(self, parent):
        card = self._card(parent, "实时摄像头", 0, 0, padx=(0, 10))
        card.rowconfigure(1, weight=1)
        self.camera = tk.Label(card, bg="#060a12", fg=MUTED, text="等待摄像头画面…", font=("Segoe UI", 11))
        self.camera.grid(row=1, column=0, sticky="nsew")
        self.camera.bind("<Configure>", lambda _event: self._render_image())
        camera_footer = tk.Frame(card, bg=CARD)
        camera_footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tk.Label(camera_footer, text="点击 / 滑动完成后，机器会先移动到清晰位，再更新此画面。", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(camera_footer, text="退出手机全屏", command=self.exit_phone_fullscreen).pack(side="right", padx=(6, 0))
        ttk.Button(camera_footer, text="刷新画面", command=self.refresh_camera).pack(side="right")

    def _build_controls(self, parent):
        outer = ttk.Frame(parent)
        outer.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        outer.columnconfigure(0, weight=1)
        for row in range(3):
            outer.rowconfigure(row, weight=1 if row in (1, 3) else 0)

        status = self._card(outer, "设备状态", 0, 0, pady=(0, 9))
        status.columnconfigure(1, weight=1)
        self.state_value = tk.StringVar(value="等待连接")
        self.origin_value = tk.StringVar(value="未设零")
        self.machine_xy = tk.StringVar(value="—")
        self._status_row(status, 1, "控制器", self.state_value)
        self._status_row(status, 2, "屏幕中心零点", self.origin_value)
        self._status_row(status, 3, "当前坐标", self.machine_xy)
        status.rowconfigure(4, weight=0)
        ttk.Button(status, text="设当前点为屏幕中心零点", style="Accent.TButton", command=self.set_zero).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(status, text="朝摄像头清晰位移动 10 mm", command=self.park_step).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        jog = self._card(outer, "相对点动 · 对准笔尖", 1, 0, pady=9)
        jog.columnconfigure(0, weight=1)
        jog.columnconfigure(1, weight=1)
        jog.columnconfigure(2, weight=1)
        self.step = tk.StringVar(value="1")
        tk.Label(jog, text="步长 mm", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        ttk.Spinbox(jog, textvariable=self.step, values=("0.5", "1", "2", "5"), width=7).grid(row=1, column=1, sticky="w", pady=(0, 8))
        ttk.Button(jog, text="+X ↑", command=lambda: self.jog(1, 0)).grid(row=2, column=1, sticky="ew", padx=4, pady=3)
        ttk.Button(jog, text="−Y ←", command=lambda: self.jog(0, -1)).grid(row=3, column=0, sticky="ew", padx=4, pady=3)
        tk.Label(jog, text="Z 释放靠弹簧\n只校正坐标、不上移", bg=CARD, fg=ACCENT, justify="center", font=("Segoe UI", 8)).grid(row=3, column=1, sticky="ew", padx=4, pady=3)
        ttk.Button(jog, text="+Y →", command=lambda: self.jog(0, 1)).grid(row=3, column=2, sticky="ew", padx=4, pady=3)
        ttk.Button(jog, text="−X ↓", command=lambda: self.jog(-1, 0)).grid(row=4, column=1, sticky="ew", padx=4, pady=3)
        tk.Label(jog, text="设零前每步最多 5 mm；按一步、看一帧的节奏对准中心。", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

        tap = self._card(outer, "屏幕操作 · 归一化坐标 0–1", 2, 0, pady=9)
        for col in range(5):
            tap.columnconfigure(col, weight=1)
        self.u = tk.StringVar(value="0.50")
        self.v = tk.StringVar(value="0.50")
        self.u1 = tk.StringVar(value="0.50")
        self.v1 = tk.StringVar(value="0.80")
        self.u2 = tk.StringVar(value="0.50")
        self.v2 = tk.StringVar(value="0.20")
        self._entry(tap, 1, 0, "点按 U", self.u)
        self._entry(tap, 1, 2, "点按 V", self.v)
        ttk.Button(tap, text="点按并让位", style="Accent.TButton", command=self.tap).grid(row=1, column=4, sticky="ew", padx=(10, 0), pady=(4, 7))
        self._entry(tap, 2, 0, "滑动起点 U", self.u1)
        self._entry(tap, 2, 1, "起点 V", self.v1)
        self._entry(tap, 2, 2, "终点 U", self.u2)
        self._entry(tap, 2, 3, "终点 V", self.v2)
        ttk.Button(tap, text="滑动并让位", command=self.swipe).grid(row=3, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        ttk.Button(tap, text="开始显示亮点", style="Accent.TButton",
                   command=self.manual_start).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(7, 0), padx=(0, 4))
        ttk.Button(tap, text="点按并记录此点", command=self.manual_record).grid(
            row=4, column=2, columnspan=2, sticky="ew", pady=(7, 0), padx=4)
        ttk.Button(tap, text="拟合保存", command=self.manual_solve).grid(
            row=4, column=4, sticky="ew", pady=(7, 0), padx=(4, 0))
        self.manual_status = tk.StringVar(value="先启动标定，手机会显示亮点")
        ttk.Label(tap, textvariable=self.manual_status, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Button(tap, text="撤销上个标定点", command=self.manual_undo).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        tk.Label(tap, text="XY 点动对准手机亮点，再点按记录。建议完成 5 个点。",
                 bg=CARD, fg=MUTED, justify="left", wraplength=410,
                 font=("Segoe UI", 8)).grid(row=6, column=2, columnspan=3, sticky="w", pady=(4, 0))

    def _status_row(self, parent, row, label, variable):
        tk.Label(parent, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(parent, textvariable=variable, style="Value.TLabel").grid(row=row, column=1, sticky="e", pady=2)

    def _entry(self, parent, row, col, label, var):
        box = tk.Frame(parent, bg=CARD)
        box.grid(row=row, column=col, sticky="ew", padx=(0, 7), pady=(0, 7))
        tk.Label(box, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")
        ttk.Entry(box, textvariable=var, width=8).pack(fill="x", pady=(3, 0))

    def _set_busy(self, value: bool, text: str | None = None):
        self.busy = value
        if text:
            self.message.set(text)

    def _run_worker(self, work, done, busy_text="机器执行中…"):
        if self.busy:
            return
        self._set_busy(True, busy_text)

        def worker():
            try:
                result = work()
                error = None
            except Exception as exc:  # show service/network errors in the UI
                result, error = None, str(exc)
            self.root.after(0, lambda: self._finish_worker(done, result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_worker(self, done, result, error):
        self._set_busy(False)
        if error:
            self.message.set(error)
            self._log(error, error=True)
        else:
            done(result)
        self._poll_status()

    def connect_service(self):
        def work():
            try:
                request("/health", timeout=1.5)
            except Exception:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                with (BASE / "grbl_service.log").open("a", encoding="utf-8") as log:
                    command = ([sys.executable, "--controller"] if getattr(sys, "frozen", False)
                               else [sys.executable, str(BASE / "grbl_service.py")])
                    subprocess.Popen(command, cwd=BASE,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=flags, close_fds=True)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    time.sleep(0.4)
                    try:
                        request("/health", timeout=1)
                        break
                    except Exception:
                        pass
                else:
                    raise RuntimeError("控制服务未能启动；查看 grbl_service.log，确认 USB 串口驱动正常且未被其他程序占用。")

            status = request("/api/status", timeout=2)
            mcp_ready = self._ensure_mcp_service()
            return status, mcp_ready

        def connected(result):
            status, mcp_ready = result
            port = status.get("port", "USB 串口")
            message = f"设备已连接（{port}）。"
            if mcp_ready:
                message += " 本机 AI MCP 已就绪：127.0.0.1:8769/mcp。"
            self._log(message)
            self.refresh_camera()

        self._run_worker(work, connected, "正在连接 USB 写字机…")

    def _ensure_mcp_service(self):
        def port_open():
            try:
                with socket.create_connection(("127.0.0.1", 8769), timeout=0.25):
                    return True
            except OSError:
                return False

        if port_open():
            return True

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with (BASE / "phone_mcp_service.log").open("a", encoding="utf-8") as log:
            command = ([sys.executable, "--mcp-http"] if getattr(sys, "frozen", False)
                       else [sys.executable, str(BASE / "phone_mcp_server.py"), "--http"])
            subprocess.Popen(command, cwd=BASE, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT,
                             creationflags=flags, close_fds=True)

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if port_open():
                return True
            time.sleep(0.2)
        return False

    def exit_phone_fullscreen(self):
        def work():
            return request("/fullscreen-exit", {}, timeout=3, base=SENSOR)
        self._run_worker(work, lambda result: self._log(f"已请求手机退出全屏（请求 {result.get('serial')}）。"),
                         "正在请求手机退出全屏…")

    def _poll_status(self):
        if self.status_polling:
            self.root.after(1200, self._poll_status)
            return
        self.status_polling = True

        def worker():
            try:
                result, error = request("/api/status", timeout=2.5), None
            except Exception as exc:
                result, error = None, str(exc)
            self.root.after(0, lambda: self._accept_status(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _accept_status(self, status, error):
        self.status_polling = False
        if error:
            self.connection_label.configure(text="● 未连接", fg=RED)
            self.state_value.set("服务离线")
            self.origin_value.set("—")
            self.machine_xy.set("—")
        else:
            self.connection_label.configure(text=f"● 已连接 · {status.get('port', 'USB 串口')}", fg=GREEN)
            self.state_value.set(f"{status.get('state', '?')}  ·  {status.get('baud', '?')} baud")
            self.origin_value.set("已设定" if status.get("origin_valid") else "未设定")
            xy = status.get("position_xy")
            self.machine_xy.set("未设零" if xy is None else f"X {xy[0]:.2f}   Y {xy[1]:.2f} mm")
        self.root.after(1200, self._poll_status)

    def _poll_camera(self):
        if self.busy or self.camera_polling:
            self.root.after(1800, self._poll_camera)
            return
        self.camera_polling = True

        def worker():
            try:
                data = request("/api/camera.jpg", timeout=5)
                from io import BytesIO
                image = Image.open(BytesIO(data)).convert("RGB")
                error = None
            except Exception as exc:
                image, error = None, str(exc)
            self.root.after(0, lambda: self._accept_camera(image, error))

        threading.Thread(target=worker, daemon=True).start()

    def _accept_camera(self, image, error):
        self.camera_polling = False
        if image is not None:
            self.last_image = image
            self._render_image()
        elif not self.last_image:
            self.camera.configure(text="摄像头未连接\n" + (error or ""))
        self.root.after(1800, self._poll_camera)

    def _render_image(self):
        if self.last_image is None:
            return
        width, height = max(100, self.camera.winfo_width()), max(100, self.camera.winfo_height())
        image = self.last_image.copy()
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.camera.configure(image=self.photo, text="")

    def refresh_camera(self):
        if self.camera_polling:
            return
        self.camera_polling = True

        def worker():
            try:
                data = request("/api/camera.jpg", timeout=8)
                from io import BytesIO
                image = Image.open(BytesIO(data)).convert("RGB")
                error = None
            except Exception as exc:
                image, error = None, str(exc)
            self.root.after(0, lambda: self._accept_camera(image, error))

        threading.Thread(target=worker, daemon=True).start()

    def _action(self, path, payload, label):
        self._run_worker(lambda: request(path, payload, timeout=180),
                         lambda result: (self._log(f"{label}完成：{json.dumps(result, ensure_ascii=False)}"), self.refresh_camera()),
                         label + "执行中；完成后机器会让位…")

    def jog(self, dx_factor, dy_factor):
        try:
            step = float(self.step.get())
            if not 0 < step <= 5:
                raise ValueError
        except ValueError:
            messagebox.showerror("步长无效", "请输入大于 0 且不超过 5 mm 的步长。")
            return
        self._action("/api/jog", {"dx": dx_factor * step, "dy": dy_factor * step, "feed": 800}, "相对点动")

    def set_zero(self):
        if not messagebox.askyesno("设定屏幕中心零点", "请确认笔尖正对手机屏幕中心。\n\n设零只记录当前位置，不会移动机器。继续吗？"):
            return
        self._action("/api/zero", {}, "屏幕中心设零")

    def park_step(self):
        self._action("/api/park-step", {"step_mm": 10}, "清晰位点动")

    def _coordinate(self, variable, name):
        value = float(variable.get())
        if not 0 <= value <= 1:
            raise ValueError(f"{name} 必须在 0 到 1 之间")
        return value

    def tap(self):
        try:
            u, v = self._coordinate(self.u, "U"), self._coordinate(self.v, "V")
        except ValueError as exc:
            messagebox.showerror("坐标无效", str(exc))
            return
        self._action("/api/tap", {"u": u, "v": v}, "点按")

    def swipe(self):
        try:
            values = [self._coordinate(var, name) for var, name in (
                (self.u1, "起点 U"), (self.v1, "起点 V"), (self.u2, "终点 U"), (self.v2, "终点 V"))]
        except ValueError as exc:
            messagebox.showerror("坐标无效", str(exc))
            return
        u1, v1, u2, v2 = values
        self._action("/api/swipe", {"u1": u1, "v1": v1, "u2": u2, "v2": v2, "duration_ms": 600}, "滑动")

    def calibrate(self):
        if not messagebox.askyesno("运行九点标定", "请先在手机浏览器打开触摸传感器页面，并确认：\n• 笔尖在屏幕中心\n• 已设定屏幕中心零点\n• Grbl 状态为 Idle\n\n标定会在中心周围 ±10 mm 打 9 个轻点。开始吗？"):
            return

        def work():
            completed = subprocess.run([sys.executable, str(BASE / "auto_calib.py"), "--run"],
                                       cwd=BASE, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=900)
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode:
                raise RuntimeError(output.strip() or f"标定失败，退出码 {completed.returncode}")
            return output.strip()

        self._run_worker(work, lambda output: (self._log("九点标定完成。\n" + output), self.refresh_camera()), "九点标定进行中…")

    def manual_start(self):
        def work():
            status = request("/api/status", timeout=3)
            sensor = request("/status", timeout=3, base=SENSOR)
            if status.get("state", "").casefold() != "idle" or not status.get("origin_valid"):
                raise RuntimeError("先让设备处于 Idle 并设好零点，再开始手动标定。")
            if not sensor.get("ready") or float(sensor.get("w", 0)) <= 0 or float(sensor.get("h", 0)) <= 0:
                raise RuntimeError("手机传感器页未连接；请让传感器页保持在前台。")
            if not sensor.get("fullscreen"):
                raise RuntimeError("先刷新手机传感器页，再点右上角“全屏标定”；然后回这里点开始。")
            first = MANUAL_TARGETS[0]
            request("/target", {"active": True, "u": first[0], "v": first[1], "label": first[2]},
                    timeout=3, base=SENSOR)
            # Move off the camera-clear park point to a camera-verified phone
            # interior point. The operator aligns the displayed marker with
            # the live jog controls before recording.
            request("/api/goto", {"u": MANUAL_APPROACH_UV[0], "v": MANUAL_APPROACH_UV[1]}, timeout=30)
            return {"w": float(sensor["w"]), "h": float(sensor["h"])}

        def done(result):
            self.manual_samples = []
            self.manual_index = 0
            self.manual_viewport = (result["w"], result["h"])
            self.manual_active = True
            self.manual_status.set("第 1/5 点：中心；用 XY 点动让笔尖对准手机亮点。")
            self._log("亮点已显示。对准后按“点按并记录此点”；每次只采一个点。")

        self._run_worker(work, done, "正在手机上显示第一个标定亮点…")

    def _manual_set_target(self, index: int):
        if not self.manual_active or not 0 <= index < len(MANUAL_TARGETS):
            return
        u, v, label = MANUAL_TARGETS[index]
        request("/target", {"active": True, "u": u, "v": v, "label": f"{index + 1}/5 {label}"},
                timeout=3, base=SENSOR)
        self.manual_status.set(
            f"第 {index + 1}/5 点：{label}（U={u:.2f}, V={v:.2f}） · 已记录 {len(self.manual_samples)} 点")

    def manual_record(self):
        if not self.manual_active or self.manual_index >= len(MANUAL_TARGETS):
            self._log("先点“开始 / 显示亮点”。", error=True)
            return
        target_u, target_v, label = MANUAL_TARGETS[self.manual_index]

        def work():
            status = request("/api/status", timeout=3)
            if status.get("state", "").casefold() != "idle" or not status.get("origin_valid"):
                raise RuntimeError("设备不在 Idle，未记录。")
            xy = status.get("position_xy")
            if not xy or len(xy) != 2:
                raise RuntimeError("控制器没有有效 XY 坐标，未记录。")
            before = request("/log", timeout=3, base=SENSOR)
            before_downs = sum(1 for line in before.splitlines()
                               if line.strip() and json.loads(line).get("type") == "down")
            request("/api/pen", {"state": "down"}, timeout=5)
            try:
                time.sleep(0.22)
            finally:
                request("/api/pen", {"state": "up"}, timeout=5)

            deadline = time.monotonic() + 2.0
            event = None
            while time.monotonic() < deadline:
                text = request("/log", timeout=3, base=SENSOR)
                downs = [json.loads(line) for line in text.splitlines()
                         if line.strip() and json.loads(line).get("type") == "down"]
                if len(downs) > before_downs:
                    event = downs[-1]
                    break
                time.sleep(0.08)
            if event is None:
                raise RuntimeError("没收到手机触点反馈，这个点未记录。确认传感器页在前台、笔尖确实碰到亮点后再按。")
            w, h = float(event.get("w", 0)), float(event.get("h", 0))
            if w <= 0 or h <= 0:
                raise RuntimeError("触点事件缺少视口尺寸，这个点未记录。")
            if self.manual_viewport and (abs(w - self.manual_viewport[0]) > 1 or abs(h - self.manual_viewport[1]) > 1):
                raise RuntimeError("标定期间手机视口尺寸变了，这个点未记录；请保持页面工具栏状态不变后重新开始。")
            u, v = float(event["x"]) / w, float(event["y"]) / h
            error_px = math.hypot((u - target_u) * w, (v - target_v) * h)
            if error_px > 28.0:
                raise RuntimeError(f"实际触点离亮点 {error_px:.0f}px，这个点未记录；用 XY 点动对准亮点后再按。")
            return {"x_mm": float(xy[0]), "y_mm": float(xy[1]), "u": u, "v": v,
                    "w": w, "h": h, "label": label, "error_px": error_px}

        def done(point):
            self.manual_samples.append(point)
            self.manual_index += 1
            self._log(f"已记录 {point['label']}：XY=({point['x_mm']:.2f},{point['y_mm']:.2f}) mm，"
                      f"触点误差 {point['error_px']:.1f}px。")
            if self.manual_index < len(MANUAL_TARGETS):
                self._manual_set_target(self.manual_index)
            else:
                self.manual_status.set("5/5 点已记录；点击“拟合并保存”。")

        self._run_worker(work, done, f"正在点按并记录第 {self.manual_index + 1}/5 点…")

    def manual_undo(self):
        if not self.manual_samples:
            self._log("当前没有可撤销的标定点。")
            return
        self.manual_samples.pop()
        self.manual_index = max(0, self.manual_index - 1)
        try:
            self._manual_set_target(self.manual_index)
        except Exception as exc:
            self._log(str(exc), error=True)

    def manual_solve(self):
        if len(self.manual_samples) < 3:
            self._log("至少记录三个不共线点，建议完成 5 个点。", error=True)
            return
        try:
            matrix = np.asarray([[p["x_mm"], p["y_mm"], 1.0] for p in self.manual_samples], dtype=float)
            if np.linalg.matrix_rank(matrix) < 3:
                raise ValueError("标定点机器坐标共线；请采集分散的屏幕点。")
            width, height = self.manual_viewport or (0.0, 0.0)
            css_x = np.asarray([p["u"] * width for p in self.manual_samples], dtype=float)
            css_y = np.asarray([p["v"] * height for p in self.manual_samples], dtype=float)
            coef_x, *_ = np.linalg.lstsq(matrix, css_x, rcond=None)
            coef_y, *_ = np.linalg.lstsq(matrix, css_y, rcond=None)
            predicted_x, predicted_y = matrix @ coef_x, matrix @ coef_y
            errors = np.hypot(predicted_x - css_x, predicted_y - css_y)
            max_error = float(errors.max())
            det = float(coef_x[0] * coef_y[1] - coef_x[1] * coef_y[0])
            if not math.isfinite(det) or abs(det) < 1e-6:
                raise ValueError("标定矩阵不可逆；请重新采集分散点。")
            if max_error > max(width, height) * 0.035:
                raise ValueError(f"拟合误差过大（{max_error:.1f}px）；请撤销偏差点并重新对准。")
            path = BASE / "tapper_config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["affine"] = [list(map(float, coef_x)), list(map(float, coef_y))]
            config["screen_css_w"] = width
            config["screen_css_h"] = height
            config["calibration_points"] = len(self.manual_samples)
            config["calibration_max_error_px"] = max_error
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
            request("/api/reload-calibration", {}, timeout=5)
            request("/target", {"active": False}, timeout=3, base=SENSOR)
            self.manual_active = False
            self.manual_status.set(f"标定通过：{len(self.manual_samples)} 点，最大误差 {max_error:.1f}px。")
            self._log(f"手动标定已保存并应用；{len(self.manual_samples)} 点，最大拟合误差 {max_error:.1f}px。")
        except Exception as exc:
            self._log(str(exc), error=True)

    def stop_machine(self):
        self.message.set("正在发送暂停…")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        try:
            result = request("/api/stop", {}, timeout=3)
            message, error = f"已发送暂停：{result}", False
        except Exception as exc:
            message, error = str(exc), True
        self.root.after(0, lambda: self._log(message, error=error))

    def _log(self, text, error=False):
        self.message.set(text)
        if error:
            self.connection_label.configure(fg=RED)


def main():
    root = tk.Tk()
    TapperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
