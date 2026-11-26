#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BtWriter portable desktop app and local MCP host."""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from btwriter_paths import app_base_dir


BASE = app_base_dir()
CONTROL_HEALTH = "http://127.0.0.1:8768/health"


def _is_controller_online() -> bool:
    try:
        with urllib.request.urlopen(CONTROL_HEALTH, timeout=1.0) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _controller_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--controller"]
    return [sys.executable, str(BASE / "btwriter_app.py"), "--controller"]


def _ensure_controller() -> None:
    if _is_controller_online():
        return
    log_path = BASE / "grbl_service.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            _controller_command(), cwd=BASE, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
        )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _is_controller_online():
            return
        time.sleep(0.25)
    detail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
    raise RuntimeError("写字机控制服务没有启动。检查 USB 串口和驱动。\n" + detail)


def _redirect_child_output(filename: str) -> None:
    stream = (BASE / filename).open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("--") else ""
    if mode == "--controller":
        _redirect_child_output("grbl_service.log")
        import grbl_service

        grbl_service.main()
        return
    if mode == "--mcp-http":
        _redirect_child_output("phone_mcp_service.log")
        _ensure_controller()
        import phone_mcp_server

        sys.argv = [str(BASE / "phone_mcp_server.py"), "--http"]
        phone_mcp_server.main()
        return
    import phone_tapper_gui

    phone_tapper_gui.main()


if __name__ == "__main__":
    main()
