#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP adapter for the local BtWriter phone-touch service.

This process does not open COM4 or emit G-code. It only calls the existing
loopback controller at 127.0.0.1:8768 and returns correlated camera frames.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import urllib.error
import urllib.request
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent
from pydantic import BaseModel, Field


CONTROL_BASE = "http://127.0.0.1:8768"
DEFAULT_DWELL_MS = 140
_ACTION_LOCK = threading.RLock()


class ScreenPoint(BaseModel):
    """A normalized phone-screen point; optional per-point press duration."""

    u: Annotated[float, Field(ge=0.0, le=1.0, description="Left-to-right screen fraction")]
    v: Annotated[float, Field(ge=0.0, le=1.0, description="Top-to-bottom screen fraction")]
    dwell_ms: Annotated[int | None, Field(ge=40, le=1500)] = None


def _request(path: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> tuple[bytes, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(
        CONTROL_BASE + path,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ToolError(f"写字机控制服务返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(
            "无法连接本机写字机控制服务 http://127.0.0.1:8768；"
            "请先启动“启动写字机控制服务.bat”，确认设备已连接。"
        ) from exc


def _get_json(path: str) -> dict[str, Any]:
    raw, _headers = _request(path)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"控制服务返回了无效 JSON：{path}") from exc
    if not isinstance(result, dict):
        raise ToolError(f"控制服务返回了非对象 JSON：{path}")
    return result


def _require_ready() -> dict[str, Any]:
    status = _get_json("/api/status")
    if not status.get("origin_valid"):
        raise ToolError("机器原点未确认；请先让笔尖对准屏幕中心并手动设零，再操作。")
    if str(status.get("state", "")).casefold() != "idle":
        raise ToolError(f"机器当前状态是 {status.get('state')}；等它回到 Idle 后再操作。")
    return status


def _validate_uv(u: float, v: float) -> None:
    if not (math.isfinite(u) and math.isfinite(v) and 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        raise ToolError("屏幕坐标 u、v 必须是 0 到 1 之间的有限数字。")


def _camera_image() -> tuple[bytes, Any]:
    return _request("/api/camera.jpg", timeout=20.0)


def _action_with_frame(result: dict[str, Any]) -> list[TextContent | Image]:
    jpeg, headers = _camera_image()
    expected = str(result.get("action_id", ""))
    actual = str(headers.get("X-Last-Action-Id", ""))
    frame_sequence = headers.get("X-Frame-Sequence", "unknown")
    frame_age_ms = headers.get("X-Frame-Age-Ms", "unknown")
    frame_check = "匹配" if expected and expected == actual else "不匹配；请检查画面后再决定下一步"
    metadata = {
        "action": result,
        "camera_frame": {
            "sequence": frame_sequence,
            "age_ms": frame_age_ms,
            "action_id_check": frame_check,
        },
    }
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False, indent=2)),
        Image(data=jpeg, format="jpeg"),
    ]


def _perform_action(path: str, payload: dict[str, Any], timeout: float) -> list[TextContent | Image]:
    # Keep action completion and its confirming frame together for HTTP MCP
    # clients that may issue concurrent tool calls.
    with _ACTION_LOCK:
        _require_ready()
        result = _post_json(path, payload, timeout=timeout)
        return _action_with_frame(result)


server = MCPServer(
    name="btwriter-phone",
    title="BtWriter 手机触控",
    version="1.0.0",
    description="通过本机 Grbl 写字机和摄像头操作手机屏幕。",
    instructions=(
        "先调用 phone_status，确认 origin_valid 为 true 且 state 为 Idle。"
        "屏幕坐标 u/v 是归一化坐标：左上 (0,0)，右下 (1,1)。"
        "tap、tap_sequence、swipe 会在完成后返回一张新摄像头画面；"
        "必须先查看画面确认触控结果，再规划下一步。"
        "屏幕画面被遮挡、坐标不确定、状态不是 Idle 或原点无效时停止，不要猜测。"
        "本服务不提供任意 G-code、笔尖抬升、设零或机器点动工具。"
    ),
)


@server.tool(name="phone_status", title="查看手机触控设备状态")
def phone_status() -> str:
    """Check the local controller, Grbl state, and whether the screen origin is valid."""
    health = _get_json("/health")
    status = _get_json("/api/status")
    return json.dumps({"service": health, "device": status}, ensure_ascii=False, indent=2)


@server.tool(name="phone_camera", title="查看手机摄像头画面")
def phone_camera() -> list[TextContent | Image]:
    """Return a fresh camera image so the AI can inspect the phone screen."""
    jpeg, headers = _camera_image()
    info = {
        "frame_sequence": headers.get("X-Frame-Sequence", "unknown"),
        "frame_age_ms": headers.get("X-Frame-Age-Ms", "unknown"),
    }
    return [
        TextContent(type="text", text=json.dumps(info, ensure_ascii=False)),
        Image(data=jpeg, format="jpeg"),
    ]


@server.tool(name="phone_tap", title="点按手机屏幕")
def phone_tap(
    u: Annotated[float, Field(ge=0.0, le=1.0, description="屏幕横坐标比例，左侧 0，右侧 1")],
    v: Annotated[float, Field(ge=0.0, le=1.0, description="屏幕纵坐标比例，顶部 0，底部 1")],
    dwell_ms: Annotated[int, Field(ge=40, le=1500, description="按压时长，毫秒")] = DEFAULT_DWELL_MS,
) -> list[TextContent | Image]:
    """Tap once, release the spring-loaded pen, then return a fresh camera frame."""
    _validate_uv(u, v)
    return _perform_action(
        "/api/tap", {"u": u, "v": v, "dwell_ms": dwell_ms}, timeout=30.0,
    )


@server.tool(name="phone_tap_sequence", title="连续点按手机屏幕")
def phone_tap_sequence(
    points: Annotated[list[ScreenPoint], Field(min_length=1, max_length=50)],
) -> list[TextContent | Image]:
    """Perform 1–50 separate screen taps, park once, then return a fresh camera frame."""
    normalized = [point.model_dump(exclude_none=True) for point in points]
    for point in normalized:
        _validate_uv(float(point["u"]), float(point["v"]))
    return _perform_action("/api/tap-sequence", {"points": normalized}, timeout=180.0)


@server.tool(name="phone_swipe", title="滑动手机屏幕")
def phone_swipe(
    u1: Annotated[float, Field(ge=0.0, le=1.0, description="起点横坐标比例")],
    v1: Annotated[float, Field(ge=0.0, le=1.0, description="起点纵坐标比例")],
    u2: Annotated[float, Field(ge=0.0, le=1.0, description="终点横坐标比例")],
    v2: Annotated[float, Field(ge=0.0, le=1.0, description="终点纵坐标比例")],
    duration_ms: Annotated[int, Field(ge=100, le=5000, description="滑动时长，毫秒")] = 600,
) -> list[TextContent | Image]:
    """Drag between two screen points with pen-down synchronized to movement."""
    _validate_uv(u1, v1)
    _validate_uv(u2, v2)
    return _perform_action(
        "/api/swipe",
        {"u1": u1, "v1": v1, "u2": u2, "v2": v2, "duration_ms": duration_ms},
        timeout=45.0,
    )


@server.tool(name="phone_stop", title="紧急停止手机触控")
def phone_stop() -> str:
    """Send Grbl feed hold to stop ongoing machine movement."""
    result = _post_json("/api/stop", {}, timeout=5.0)
    return json.dumps(result, ensure_ascii=False)


def _post_json(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw, _headers = _request(path, payload, timeout=timeout)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"控制服务返回了无效 JSON：{path}") from exc
    if not isinstance(result, dict):
        raise ToolError(f"控制服务返回了非对象 JSON：{path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http",
        action="store_true",
        help="在本机 127.0.0.1:8769 启动 Streamable HTTP MCP；默认使用 MCP stdio。",
    )
    parser.add_argument("--port", type=int, default=8769, help="HTTP MCP 端口，默认 8769。")
    args = parser.parse_args()
    if args.http:
        server.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=args.port,
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
