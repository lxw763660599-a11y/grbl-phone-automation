#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay reviewed phone workflows with camera/OCR checkpoints and safe resume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

BASE = Path(__file__).resolve().parent
API = "http://127.0.0.1:8768"
RUNS = BASE / "workflow_runs"


class WorkflowError(RuntimeError):
    pass


class Client:
    def __init__(self, api: str):
        self.api = api.rstrip("/")

    def request(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 45.0) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.api + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise WorkflowError(f"控制服务返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WorkflowError("无法连接写字机服务；先启动 grbl_service.py") from exc
        if content_type.startswith("image/"):
            return body
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"控制服务返回了无法解析的数据: {body[:120]!r}") from exc


@contextmanager
def contact_profile(client: "Client"):
    """Apply the previously phone-verified contact margin for one action only."""
    config_path = BASE / "tapper_config.json"
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8"))
    config["z_contact_margin_mm"] = 0.9
    temp_path = config_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        temp_path.replace(config_path)
        client.request("/api/reload-calibration", {})
        yield
    finally:
        temp_path.write_bytes(original)
        temp_path.replace(config_path)
        client.request("/api/reload-calibration", {})


def touch_request(client: "Client", path: str, payload: dict[str, Any], timeout: float = 45.0):
    with contact_profile(client):
        return client.request(path, payload, timeout=timeout)


class ScreenReader:
    """Offline OCR for named UI checkpoints; never chooses actions from OCR."""

    def __init__(self, client: Client):
        self.client = client
        self._engine = None

    def read(self, roi: list[float] | None = None) -> list[str]:
        try:
            import cv2
            import numpy as np
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise WorkflowError("缺少本地 OCR 依赖；按 requirements.txt 安装 rapidocr、opencv-python 和 numpy") from exc
        if self._engine is None:
            self._engine = RapidOCR()
        data = self.client.request("/api/camera.jpg")
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise WorkflowError("无法解码摄像头画面")
        if roi is not None:
            if len(roi) != 4:
                raise WorkflowError("OCR 区域必须是 [left, top, right, bottom]")
            height, width = frame.shape[:2]
            left, top, right, bottom = roi
            x1, y1 = int(left * width), int(top * height)
            x2, y2 = int(right * width), int(bottom * height)
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise WorkflowError(f"OCR 区域越界: {roi}")
            frame = frame[y1:y2, x1:x2]
        result = self._engine(frame)
        return [str(text).strip() for text in (result.txts or []) if str(text).strip()]

    def contains(self, text: str, roi: list[float] | None = None) -> bool:
        normalized = self._normalize(text)
        return any(normalized in self._normalize(line) for line in self.read(roi))

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text).casefold()


class AIPhoneAgent:
    """Use the installed local GUI model to plan and review one action at a time."""

    T9_KEYS = {
        **{char: (0.47, 0.73) for char in "abc"},
        **{char: (0.69, 0.73) for char in "def"},
        **{char: (0.26, 0.79) for char in "ghi"},
        **{char: (0.47, 0.79) for char in "jkl"},
        **{char: (0.69, 0.79) for char in "mno"},
        **{char: (0.26, 0.86) for char in "pqrs"},
        **{char: (0.47, 0.86) for char in "tuv"},
        **{char: (0.69, 0.86) for char in "wxyz"},
    }

    def __init__(self, client: Client):
        project = Path(os.environ.get("PHONE_AUTOMATION_HOME", "")).expanduser()
        if not project.is_dir():
            raise WorkflowError(
                "AI 半自动模式需要可选的 phone_automation 组件（本地 GUI 模型 + OCR + 透视矫正）。"
                "请设置环境变量 PHONE_AUTOMATION_HOME 指向它，或改用固定流程 run 模式。"
            )
        sys.path.insert(0, str(project))
        try:
            import cv2
            import numpy as np
            import yaml as yaml_module
            from phone_automation.ime import ime_chunks
            from phone_automation.local_gui_model import LocalGuiModel, task_requests_external_effect
            from phone_automation.ocr import LocalOcr
            from phone_automation.vision import (
                ScreenCalibration,
                encode_jpeg,
                resize_long_edge,
                warp_phone_screen,
            )
        except ImportError as exc:
            raise WorkflowError(
                f"AI 组件依赖未安装；先安装 {project / 'requirements.txt'} 中的依赖"
            ) from exc
        config = yaml_module.safe_load((project / "config.yaml").read_text(encoding="utf-8")) or {}
        model_config = dict(config.get("local_gui_model") or {})
        for key in ("executable", "cache_dir"):
            value = Path(str(model_config.get(key, "")))
            if value and not value.is_absolute():
                model_config[key] = str((project / value).resolve())
        runtime_dir = project / str((config.get("paths") or {}).get("runtime", "runtime"))
        self.model = LocalGuiModel(model_config, runtime_dir)
        calibration_config = config.get("screen_calibration") or {}
        calibration_path = project / str(calibration_config.get("file", "runtime/screen_calibration.json"))
        raw_calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        self.calibration = ScreenCalibration.from_dict(raw_calibration)
        self.cv2 = cv2
        self.np = np
        self.ime_chunks = ime_chunks
        self.LocalOcr = LocalOcr
        self.ocr = LocalOcr()
        self.encode_jpeg = encode_jpeg
        self.resize_long_edge = resize_long_edge
        self.warp_phone_screen = warp_phone_screen
        self.task_requests_external_effect = task_requests_external_effect
        self.client = client
        self.minimum_confidence = float(model_config.get("min_confidence", 0.75))
        self.inferred_confidence = float(model_config.get("inferred_confidence", 0.82))
        self.max_han_chars = int((config.get("ime") or {}).get("max_han_chars", 6))

    def close(self) -> None:
        self.model.stop()

    def screen(self) -> tuple[bytes, Any, str]:
        camera_jpeg = self.client.request("/api/camera.jpg")
        frame = self.cv2.imdecode(self.np.frombuffer(camera_jpeg, dtype=self.np.uint8), self.cv2.IMREAD_COLOR)
        if frame is None:
            raise WorkflowError("无法解码摄像头画面")
        height, width = frame.shape[:2]
        if (width, height) != (self.calibration.source_width, self.calibration.source_height):
            raise WorkflowError(
                f"摄像头分辨率从 {self.calibration.source_width}×{self.calibration.source_height} "
                f"变为 {width}×{height}；先重新标定相机画面"
            )
        screen = self.warp_phone_screen(frame, self.calibration)
        model_frame = self.resize_long_edge(screen, 1280)
        jpeg = self.encode_jpeg(model_frame, quality=86)
        small = self.cv2.cvtColor(self.cv2.resize(screen, (48, 96)), self.cv2.COLOR_BGR2GRAY)
        digest = hashlib.sha1(small.tobytes()).hexdigest()
        return jpeg, screen, digest

    def _type_chinese(self, text: str) -> dict[str, Any]:
        chunks = self.ime_chunks(text, self.max_han_chars)
        if any(not chunk.needs_candidate for chunk in chunks):
            raise WorkflowError("AI 输入动作目前只接受中文拼音输入，ASCII 内容请使用已标定工作流")
        completed = 0
        for chunk in chunks:
            points = []
            for letter in chunk.keys:
                if letter not in self.T9_KEYS:
                    raise WorkflowError(f"拼音字母不在九宫格键位内: {letter}")
                u, v = self.T9_KEYS[letter]
                points.append({"u": u, "v": v})
            touch_request(self.client, "/api/tap-sequence", {"points": points}, timeout=180)
            _camera_jpeg, screen, _digest = self.screen()
            height, width = screen.shape[:2]
            lines = self.ocr.read(screen, roi=(0.0, 0.60, 1.0, 0.75), min_confidence=0.25)
            match = self.LocalOcr.find_in_lines(lines, chunk.target)
            if match is None:
                raise WorkflowError(f"输入法候选区没有识别到「{chunk.target}」，已停下避免选错字")
            u = min(0.98, max(0.02, match.center[0] / width))
            v = min(0.98, max(0.02, match.center[1] / height))
            touch_request(self.client, "/api/tap", {"u": u, "v": v})
            completed += len(chunk.target)
        return {"action": "type", "text": text, "completed_chars": completed}

    def _execute(self, decision: Any) -> dict[str, Any]:
        if decision.action == "click":
            if decision.position is None:
                raise WorkflowError("AI 点击动作没有屏幕坐标")
            u, v = decision.position[0] / 1000.0, decision.position[1] / 1000.0
            return touch_request(self.client, "/api/tap", {"u": u, "v": v})
        if decision.action == "swipe":
            if decision.position is None or decision.end_position is None:
                raise WorkflowError("AI 滑动动作缺少起点或终点")
            return touch_request(self.client, "/api/swipe", {
                "u1": decision.position[0] / 1000.0,
                "v1": decision.position[1] / 1000.0,
                "u2": decision.end_position[0] / 1000.0,
                "v2": decision.end_position[1] / 1000.0,
                "duration_ms": 650,
            })
        if decision.action == "type":
            if decision.position is not None:
                touch_request(self.client, "/api/tap", {
                    "u": decision.position[0] / 1000.0,
                    "v": decision.position[1] / 1000.0,
                })
                time.sleep(0.7)
            return self._type_chinese(decision.text)
        if decision.action == "wait":
            time.sleep(1.0)
            return {"action": "wait", "seconds": 1}
        raise WorkflowError(f"不能执行 AI 动作: {decision.action}")

    def _apply_confidence_fallback(self, decision: Any, metadata: dict[str, Any], task: str) -> Any:
        raw_output = str(metadata.get("raw_output", ""))
        if re.search(r'''["']?confidence["']?\s*:''', raw_output, re.IGNORECASE):
            return decision
        if decision.action == "stop":
            return decision
        fallback = self.inferred_confidence
        known_apps = ("微信", "小红书", "抖音", "剪映", "qq", "支付宝", "淘宝", "拼多多", "微博")
        requested_apps = [name for name in known_apps if name in task.casefold()]
        evidence = f"{decision.screen} {decision.target} {decision.reason}".casefold()
        if requested_apps and not any(name in evidence for name in requested_apps):
            fallback = min(fallback, max(0.0, self.minimum_confidence - 0.01))
        metadata["confidence_inferred"] = True
        return replace(decision, confidence=fallback)

    def run(self, task: str, *, max_steps: int) -> int:
        history: list[dict[str, Any]] = []
        verification = ""
        previous_digest: str | None = None
        previous_signature: str | None = None
        unchanged_repeats = 0
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-ai-" + uuid.uuid4().hex[:6]
        RUNS.mkdir(parents=True, exist_ok=True)
        report_path = RUNS / f"{run_id}.json"
        report: dict[str, Any] = {"run_id": run_id, "mode": "ai_assisted", "task": task, "steps": []}
        print(f"AI 半自动任务：{task}\n运行 ID：{run_id}")
        try:
            for index in range(1, max_steps + 1):
                jpeg, _screen, digest = self.screen()
                screenshot = RUNS / f"{run_id}_step_{index:02d}.jpg"
                screenshot.write_bytes(jpeg)
                if previous_digest is not None and digest == previous_digest:
                    unchanged_repeats += 1
                    verification = "上一步后画面没有可见变化；不要重复同一动作，先换一种可验证策略或停止。"
                else:
                    unchanged_repeats = 0
                    verification = "上一步后画面已变化，请核对是否达到目标。"
                decision, metadata = self.model.analyze(
                    jpeg,
                    task,
                    history=history,
                    verification=verification,
                )
                decision = self._apply_confidence_fallback(decision, metadata, task)
                item = {
                    "step": index,
                    "screen_path": str(screenshot),
                    "decision": decision.to_dict(),
                    "confidence_inferred": bool(metadata.get("confidence_inferred", False)),
                    "elapsed_seconds": metadata.get("elapsed_seconds"),
                }
                report["steps"].append(item)
                print(f"[{index}/{max_steps}] {decision.screen}: {decision.action} → {decision.target} ({decision.confidence:.0%})")
                if decision.action == "stop":
                    report.update(status="completed", reason=decision.reason)
                    persist_state(report_path, report)
                    print(f"AI 已确认任务结束：{decision.reason}\n记录：{report_path}")
                    return 0
                if decision.confidence < self.minimum_confidence:
                    raise WorkflowError(f"AI 置信度 {decision.confidence:.0%} 低于阈值 {self.minimum_confidence:.0%}；已保存 {screenshot}")
                if unchanged_repeats >= 2 and decision.action not in {"wait", "stop"}:
                    raise WorkflowError(f"连续两次动作后画面仍无可见变化，已停止避免盲点：{screenshot}")
                # The user's task is the task-level authorization. Do not ask
                # for confirmation at every tap; refuse an external action
                # only when the task itself did not request one.
                if decision.external_effect and not self.task_requests_external_effect(task):
                    report.update(status="awaiting_external_authorization", blocked_decision=decision.to_dict())
                    persist_state(report_path, report)
                    print(f"AI 识别到外部动作「{decision.target}」，但任务描述没有明确要求该动作。已停下并保存画面与计划：{report_path}")
                    return 3
                signature = json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True)
                if unchanged_repeats >= 1 and signature == previous_signature:
                    raise WorkflowError(f"屏幕未变化且 AI 再次建议同一动作，已停止避免重复点击：{screenshot}")
                before_digest = digest
                result = self._execute(decision)
                previous_digest = before_digest
                previous_signature = signature
                item["execution"] = result
                item["verification"] = verification
                history.append({"decision": decision.to_dict(), "execution": result, "verification": verification})
                report["last_completed_step"] = index
                persist_state(report_path, report)
            report.update(status="step_limit_reached")
            persist_state(report_path, report)
            raise WorkflowError(f"达到 {max_steps} 步上限，AI 尚未确认任务完成；记录：{report_path}")
        except Exception as exc:
            report.update(status="stopped", error=str(exc))
            persist_state(report_path, report)
            raise


def save_screenshot(client: Client, run_id: str, label: str) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:40] or "frame"
    path = RUNS / f"{run_id}_{safe_label}.jpg"
    path.write_bytes(client.request("/api/camera.jpg"))
    return path


def check_text(reader: ScreenReader, check: dict[str, Any]) -> bool:
    found = reader.contains(str(check["text"]), check.get("roi"))
    return found if check.get("present", True) else not found


def describe_check(check: dict[str, Any]) -> str:
    return f"{'应有' if check.get('present', True) else '应无'}「{check.get('text', '')}」"


def load_workflow(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        resolved.relative_to((BASE / "flows").resolve())
    except ValueError as exc:
        raise WorkflowError("工作流文件必须放在 flows 目录内") from exc
    workflow = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
        raise WorkflowError("工作流格式错误：需要包含 steps 列表")
    return workflow


def persist_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def run(args: argparse.Namespace) -> int:
    client = Client(args.api)
    reader = ScreenReader(client)
    if args.resume_run:
        run_id = args.resume_run
        state_path = RUNS / f"{run_id}.json"
        if not state_path.exists():
            raise WorkflowError(f"找不到待恢复的运行记录: {run_id}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        workflow_path = BASE / "flows" / state["workflow_file"]
        workflow = load_workflow(workflow_path)
        if state.get("status") == "action_in_progress":
            raise WorkflowError(
                f"第 {state.get('in_progress_step', 0) + 1} 步中断，动作结果不确定。请检查截图后重新开始，避免重复发送。"
            )
        start_at = int(state.get("next_step", 0))
    else:
        workflow_path = args.workflow.resolve()
        workflow = load_workflow(workflow_path)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        state_path = RUNS / f"{run_id}.json"
        state = {
            "run_id": run_id,
            "workflow_file": workflow_path.name,
            "workflow": workflow.get("name", workflow_path.stem),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "next_step": 0,
            "status": "running",
        }
        start_at = 0

    status = client.request("/api/status")
    if status.get("state", "").casefold() != "idle" or not status.get("origin_valid"):
        raise WorkflowError("寫字機必須處於 Idle，且屏幕中心原點有效；先人工確認設備狀態")

    checks = workflow.get("preconditions", [])
    for check in checks:
        if not check_text(reader, check):
            screenshot = save_screenshot(client, run_id, "precondition_failed")
            raise WorkflowError(f"前置檢查失敗：{describe_check(check)}；畫面已保存到 {screenshot}")

    steps = workflow["steps"]
    if any(step.get("type") in {"ai_review", "tap_sequence"} for step in steps):
        health = client.request("/health")
        if health.get("profile") != "speed-v1":
            raise WorkflowError("当前服务尚未启用加速版，请先按加速启用说明重启服务")
    print(f"工作流：{workflow.get('title', workflow.get('name', workflow_path.stem))}")
    print(f"運行 ID：{run_id}")
    for index in range(start_at, len(steps)):
        step = steps[index]
        kind = step.get("type")
        label = step.get("label", f"step {index + 1}")
        print(f"[{index + 1}/{len(steps)}] {label}")
        if kind == "ai_review":
            token = getattr(args, "review_token", None)
            pending = state.get("pending_review", {})
            if (token and args.resume_run and pending.get("step") == index
                    and pending.get("token") == token):
                screenshot_path = Path(pending["screenshot"])
                if (not screenshot_path.is_file() or
                    hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != token):
                    raise WorkflowError("审核截图已变化，不能继续复用旧的审核结果")
                if time.time() - float(pending.get("created_at", 0)) > 300:
                    raise WorkflowError("审核截图已超过五分钟，请重新开始本流程")
                state.setdefault("reviews", []).append(dict(pending, reviewed_by="agent"))
                state.pop("pending_review", None)
                state.update(status="running", next_step=index + 1)
                persist_state(state_path, state)
                args.review_token = None
                continue
            screenshot = save_screenshot(client, run_id, f"review_{index + 1}")
            token = hashlib.sha256(screenshot.read_bytes()).hexdigest()
            state.update(status="awaiting_ai_review", next_step=index,
                         pending_review={"step": index, "token": token,
                                         "screenshot": str(screenshot),
                                         "instruction": step.get("instruction", label),
                                         "created_at": time.time()})
            persist_state(state_path, state)
            print(f"AI 审核节点：{step.get('instruction', label)}")
            print(f"审核截图：{screenshot}")
            print(f"AI 确认通过后继续：python workflow_runner.py run --resume-run {run_id} --review-token {token}"
                  + (" --approve-send" if args.approve_send else ""))
            return 4
        elif kind == "check_text":
            if not check_text(reader, step):
                screenshot = save_screenshot(client, run_id, f"failed_{index + 1}")
                state.update(status="stopped_at_checkpoint", next_step=index, last_failure=describe_check(step))
                persist_state(state_path, state)
                raise WorkflowError(f"畫面檢查失敗：{describe_check(step)}；已停下并保存 {screenshot}")
            state.update(next_step=index + 1, status="running")
            persist_state(state_path, state)
        elif kind == "ensure_keyboard":
            marker = str(step.get("marker", "ABC"))
            roi = step.get("roi")
            if not reader.contains(marker, roi):
                result = touch_request(client, "/api/tap", {"u": float(step["focus_u"]), "v": float(step["focus_v"])})
                if not result.get("ok"):
                    raise WorkflowError(f"打開輸入法失敗: {result}")
                time.sleep(float(step.get("settle_seconds", 0.6)))
            if not reader.contains(marker, roi):
                screenshot = save_screenshot(client, run_id, f"keyboard_not_ready_{index + 1}")
                state.update(status="stopped_at_checkpoint", next_step=index, last_failure="keyboard not visible")
                persist_state(state_path, state)
                raise WorkflowError(f"輸入法未確認打開；已停下并保存 {screenshot}")
            state.update(next_step=index + 1, status="running")
            persist_state(state_path, state)
        elif kind == "wait":
            time.sleep(float(step.get("seconds", 0.5)))
            state.update(next_step=index + 1, status="running")
            persist_state(state_path, state)
        elif kind == "tap_sequence":
            state.update(status="action_in_progress", in_progress_step=index)
            persist_state(state_path, state)
            result = touch_request(client, "/api/tap-sequence", {"points": step["points"]}, timeout=180)
            print(f"  已完成 {result.get('count', 0)} 次点按；已让位")
            state.update(status="running", next_step=index + 1)
            state.pop("in_progress_step", None)
            persist_state(state_path, state)
        elif kind == "tap":
            if step.get("approval") == "send" and not args.approve_send:
                state.update(status="awaiting_approval", next_step=index)
                persist_state(state_path, state)
                screenshot = save_screenshot(client, run_id, "ready_to_send")
                print(f"已停在发送前，草稿画面：{screenshot}")
                print(f"确认后执行：python workflow_runner.py run --resume-run {run_id} --approve-send")
                return 3
            if step.get("approval") == "send":
                for check in step.get("verify_before", []):
                    if not check_text(reader, check):
                        screenshot = save_screenshot(client, run_id, "send_guard_failed")
                        state.update(status="stopped_at_checkpoint", next_step=index, last_failure=describe_check(check))
                        persist_state(state_path, state)
                        raise WorkflowError(f"发送保护检查失败：{describe_check(check)}；已保存 {screenshot}")
            state.update(status="action_in_progress", in_progress_step=index)
            persist_state(state_path, state)
            result = touch_request(client, "/api/tap", {
                "u": float(step["u"]), "v": float(step["v"]),
                "dwell_ms": step.get("dwell_ms"),
            })
            if not result.get("ok"):
                raise WorkflowError(f"点按失败: {result}")
            state.update(status="running", next_step=index + 1)
            state.pop("in_progress_step", None)
            persist_state(state_path, state)
        else:
            raise WorkflowError(f"不支持的步骤类型: {kind}")

    screenshot = save_screenshot(client, run_id, "complete")
    state.update(status="complete", next_step=len(steps), completed_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    state["final_screenshot"] = str(screenshot)
    persist_state(state_path, state)
    print(f"完成。复核画面：{screenshot}")
    return 0


def run_ai_task(args: argparse.Namespace) -> int:
    client = Client(args.api)
    agent = AIPhoneAgent(client)
    try:
        status = client.request("/api/status")
        if status.get("state", "").casefold() != "idle" or not status.get("origin_valid"):
            raise WorkflowError("写字机必须处于 Idle 且原点有效；先在界面确认设备位置")
        return agent.run(args.task, max_steps=args.max_steps)
    finally:
        agent.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="通过摄像头与写字机执行手机工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute = subparsers.add_parser("run", help="重放已验证的固定工作流")
    execute.add_argument("workflow", type=Path, nargs="?", default=BASE / "flows" / "wechat_send_nihao.yaml")
    execute.add_argument("--api", default=API)
    execute.add_argument("--resume-run", help="恢复指定运行 ID；会先检查联系人和草稿")
    execute.add_argument("--review-token", help="由审核此运行截图的 AI 传回，只通过当前一个审核节点")
    execute.add_argument("--approve-send", action="store_true", help="允许执行工作流中标记为 send 的外发动作")
    ai_task = subparsers.add_parser("ai-task", help="AI 观察画面、规划动作、执行并逐步复核")
    ai_task.add_argument("task", help="本次任务，例如：在当前微信聊天给文件传输助手发送你好")
    ai_task.add_argument("--api", default=API)
    ai_task.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args()
    try:
        if args.command == "ai-task":
            if not 1 <= args.max_steps <= 100:
                raise WorkflowError("--max-steps 必须在 1 到 100 之间")
            return run_ai_task(args)
        return run(args)
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"工作流已停止：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
