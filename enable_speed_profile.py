"""Enable reviewed speed settings after the operator closes the old service."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8768"

def get(path):
    return json.loads(urllib.request.urlopen(URL+path, timeout=3).read())

def main():
    print("读取现有坐标。整个切换期间请勿手动移动机头或关闭机器电源。", flush=True)
    state = get("/api/status")
    if state.get("state") != "Idle" or not state.get("origin_valid"):
        raise RuntimeError("机器必须空闲且原点有效，未修改配置")
    current = state["position_xy"]
    if any(abs(float(n)) > 205 for n in current):
        raise RuntimeError("当前位置超出已知范围")
    print("已记录当前位置：", current, flush=True)
    print("请关闭旧的写字机控制服务窗口；关闭后在这里按回车继续。", flush=True)
    input()
    try:
        get("/health")
    except Exception:
        pass
    else:
        raise RuntimeError("旧服务仍在运行，未修改配置。请关闭后重新运行本程序。")
    cfg_path = ROOT/"tapper_config.json"
    original = cfg_path.read_text(encoding="utf-8")
    cfg = json.loads(original)
    profile = json.loads((ROOT/"speed_profile.json").read_text(encoding="utf-8"))
    for key in ("camera_park_xy_mm", "camera_park_candidates_xy_mm", "camera_clear_settle_seconds", "dwell_ms"):
        cfg[key] = profile[key]
    cfg["resume_xy"] = current
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    try:
        subprocess.run([sys.executable, str(ROOT/"grbl_service.py")], cwd=str(ROOT), check=True)
    finally:
        # Do not leave an unconsumed resume point for a later unrelated boot.
        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        if "resume_xy" in after:
            after.pop("resume_xy", None)
            cfg_path.write_text(json.dumps(after, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("启用未完成：", exc)
        input("按回车关闭")
