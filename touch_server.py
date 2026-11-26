#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
touch_server.py —— 手机触摸传感器（网页版，免装 App、免 adb）

电脑起一个局域网网页，手机浏览器打开后，页面把每一次触摸的
像素坐标 + 时间戳实时推回电脑，写进 TouchLog.jsonl。

用法：
  python touch_server.py                 # 只起服务，打印访问地址和二维码
  python touch_server.py --port 8765
"""

import argparse
import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "TouchLog.jsonl")

TOUCHES = []
LOCK = threading.Lock()
READY = {"ok": False}
TARGET = {"active": False, "u": 0.5, "v": 0.5, "label": ""}
FULLSCREEN_EXIT_SERIAL = 0

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>触摸传感器</title>
<style>
  html,body{margin:0;padding:0;height:100%;background:#0b0b0d;color:#8ef;overflow:hidden;
            touch-action:none;user-select:none;-webkit-user-select:none;overscroll-behavior:none;}
  #wrap{position:fixed;inset:0;}
  #ring{position:absolute;width:34px;height:34px;border:3px solid #3cf;border-radius:50%;
        transform:translate(-50%,-50%);opacity:0;transition:opacity .35s;pointer-events:none;}
  #target{position:absolute;display:none;width:56px;height:56px;box-sizing:border-box;
        transform:translate(-50%,-50%);border:3px solid #fff;border-radius:50%;
        background:rgba(255,45,85,.22);box-shadow:0 0 0 5px rgba(255,45,85,.28),
        0 0 24px 10px rgba(255,45,85,.95);pointer-events:none;z-index:5;}
  #target:before,#target:after{content:"";position:absolute;background:#fff;left:50%;top:50%;
        transform:translate(-50%,-50%);box-shadow:0 0 8px #ff2d55;}
  #target:before{width:78px;height:2px;} #target:after{width:2px;height:78px;}
  #target-label{position:absolute;left:50%;top:64px;transform:translateX(-50%);white-space:nowrap;
        padding:4px 9px;border-radius:12px;background:#ff2d55;color:white;font:bold 13px sans-serif;
        box-shadow:0 2px 12px #000;}
  #hud{position:absolute;left:0;right:0;bottom:0;padding:10px;font:14px/1.5 monospace;
       background:rgba(0,0,0,.45);}
  #state{position:absolute;left:0;right:0;top:0;padding:10px;font:14px/1.5 monospace;}
  #fullscreen{position:absolute;right:10px;top:10px;z-index:8;padding:8px 12px;border:0;border-radius:9px;
        color:#061018;background:#8ef;font:bold 13px sans-serif;box-shadow:0 0 14px #3cf;}
</style>
</head>
<body>
<div id="wrap">
  <div id="ring"></div>
  <div id="target"><div id="target-label"></div></div>
  <div id="state">等待连接…</div>
  <button id="fullscreen">进入全屏</button>
  <div id="hud">未触摸</div>
</div>
<script>
const st = document.getElementById('state');
const hud = document.getElementById('hud');
const ring = document.getElementById('ring');
const target = document.getElementById('target');
const targetLabel = document.getElementById('target-label');
const fullscreenButton = document.getElementById('fullscreen');
let wakeLock = null;
let lastFullscreenExitSerial = 0;

async function keepAwake(){
  try{ wakeLock = await navigator.wakeLock.request('screen'); st.textContent='传感器在线 · 常亮'; }
  catch(e){ st.textContent='传感器在线 · 常亮不可用'; }
}
document.addEventListener('visibilitychange', () => { if(document.visibilityState==='visible') keepAwake(); });

function hello(){
  fetch('/hello', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({w:innerWidth, h:innerHeight, dpr:devicePixelRatio, fullscreen:!!document.fullscreenElement,
                          sw:screen.width, sh:screen.height, ua:navigator.userAgent})});
}
function syncFullscreenButton(){ fullscreenButton.textContent=document.fullscreenElement?'退出全屏':'进入全屏'; }
fullscreenButton.addEventListener('click', async () => {
  try{
    if(document.fullscreenElement){ await document.exitFullscreen(); st.textContent='已退出全屏'; }
    else { await document.documentElement.requestFullscreen(); st.textContent='全屏已开启'; }
  }
  catch(e){ st.textContent='全屏切换失败，请检查浏览器权限'; }
  setTimeout(hello, 200);
});
document.addEventListener('fullscreenchange', () => { syncFullscreenButton(); setTimeout(hello, 150); });
async function syncFullscreenExit(){
  try{
    const response = await fetch('/fullscreen-state', {cache:'no-store'});
    const data = await response.json();
    if(data.serial > lastFullscreenExitSerial){
      lastFullscreenExitSerial = data.serial;
      if(document.fullscreenElement){
        await document.exitFullscreen();
        st.textContent='已退出全屏（桌面控制台请求）';
        setTimeout(hello, 150);
      }
    }
  }catch(e){}
}
async function syncTarget(){
  try{
    const response = await fetch('/target', {cache:'no-store'});
    const data = await response.json();
    if(data.active){
      target.style.display='block';
      target.style.left=(data.u*innerWidth)+'px';
      target.style.top=(data.v*innerHeight)+'px';
      targetLabel.textContent=data.label||'标定点';
    }else target.style.display='none';
  }catch(e){}
}
setInterval(syncTarget, 250); syncTarget();
setInterval(syncFullscreenExit, 350); syncFullscreenExit();
window.addEventListener('resize', () => setTimeout(hello, 150));

function report(type, x, y, force, sx, sy){
  fetch('/touch', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({type:type, x:x, y:y, force:force,
                          dpr:devicePixelRatio, w:innerWidth, h:innerHeight, sw:screen.width, sh:screen.height,
                          sx:sx, sy:sy})
  }).catch(()=>{});
  hud.textContent = type + '  x=' + x.toFixed(1) + '  y=' + y.toFixed(1);
}

function show(x,y){
  ring.style.left = x + 'px'; ring.style.top = y + 'px';
  ring.style.opacity = 1;
  clearTimeout(show._t); show._t = setTimeout(()=>{ ring.style.opacity = 0; }, 320);
}

window.addEventListener('pointerdown', e => {
  if(!wakeLock) keepAwake();
  show(e.clientX, e.clientY);
  report('down', e.clientX, e.clientY, e.pressure||0, e.screenX, e.screenY);
});
window.addEventListener('pointerup', e => { report('up', e.clientX, e.clientY, 0, e.screenX, e.screenY); });

hello(); keepAwake();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(PAGE)
        elif path == "/log":
            with LOCK:
                self._send("\n".join(json.dumps(t, ensure_ascii=False) for t in TOUCHES),
                           "text/plain; charset=utf-8")
        elif path == "/status":
            with LOCK:
                status = dict(READY)
                status["ready"] = bool(READY.get("ok"))
                status["touches"] = len(TOUCHES)
                self._send(json.dumps(status, ensure_ascii=False), "application/json")
        elif path == "/target":
            with LOCK:
                self._send(json.dumps(dict(TARGET), ensure_ascii=False), "application/json")
        elif path == "/fullscreen-state":
            with LOCK:
                self._send(json.dumps({"serial": FULLSCREEN_EXIT_SERIAL}), "application/json")
        else:
            self._send("not found", "text/plain", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            self._send("request too large", "text/plain; charset=utf-8", 413)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send("invalid JSON", "text/plain; charset=utf-8", 400)
            return
        if not isinstance(payload, dict):
            self._send("JSON body must be an object", "text/plain; charset=utf-8", 400)
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/hello":
            try:
                dimensions = {key: float(payload[key]) for key in ("w", "h", "dpr", "sw", "sh")}
            except (KeyError, TypeError, ValueError):
                self._send("missing or invalid viewport dimensions", "text/plain; charset=utf-8", 400)
                return
            if any(value <= 0 or value > 10000 for value in dimensions.values()):
                self._send("viewport dimensions are out of range", "text/plain; charset=utf-8", 400)
                return
            payload.update(dimensions)
            payload["ua"] = str(payload.get("ua", ""))[:256]
            with LOCK:
                READY["ok"] = True
                READY.update(payload)
            print(f"[READY] 手机已连上：{payload.get('sw')}x{payload.get('sh')} dpr={payload.get('dpr')}", flush=True)
            self._send("ok", "text/plain")
        elif path == "/target":
            try:
                u, v = float(payload.get("u", 0.5)), float(payload.get("v", 0.5))
            except (TypeError, ValueError):
                self._send("invalid target", "text/plain", 400)
                return
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                self._send("target coordinates must be between 0 and 1", "text/plain", 400)
                return
            with LOCK:
                TARGET.update({"active": bool(payload.get("active", True)), "u": u, "v": v,
                               "label": str(payload.get("label", "标定点"))[:80]})
            self._send("ok", "text/plain")
        elif path == "/fullscreen-exit":
            global FULLSCREEN_EXIT_SERIAL
            with LOCK:
                FULLSCREEN_EXIT_SERIAL += 1
                serial = FULLSCREEN_EXIT_SERIAL
            self._send(json.dumps({"ok": True, "serial": serial}), "application/json")
        elif path == "/touch":
            kind = payload.get("type")
            try:
                x, y = float(payload["x"]), float(payload["y"])
                width, height = float(payload["w"]), float(payload["h"])
            except (KeyError, TypeError, ValueError):
                self._send("missing or invalid touch coordinates", "text/plain; charset=utf-8", 400)
                return
            if kind not in {"down", "up"} or not (0 <= x <= width and 0 <= y <= height):
                self._send("touch event is out of range", "text/plain; charset=utf-8", 400)
                return
            entry = dict(payload)
            entry.update({"x": x, "y": y, "w": width, "h": height})
            entry["t"] = time.time()
            with LOCK:
                TOUCHES.append(entry)
            if entry.get("type") == "down":
                print(f"[TOUCH] x={entry.get('x'):.1f} y={entry.get('y'):.1f} "
                      f"force={entry.get('force')} t={entry['t']:.3f}", flush=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._send("ok", "text/plain")
        else:
            self._send("not found", "text/plain", 404)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def make_qr(url, out_path):
    try:
        import qrcode
    except ImportError:
        os.system(f"{sys.executable} -m pip install qrcode pillow --quiet --disable-pip-version-check")
        try:
            import qrcode
        except ImportError:
            return None
    img = qrcode.make(url)
    img.save(out_path)
    return out_path


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port=8765):
    srv = Server(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--qr", default=os.path.join(BASE, "sensor_qr.png"))
    args = ap.parse_args()

    srv = start(args.port)
    url = f"http://{lan_ip()}:{args.port}/"
    print("=" * 46)
    print(" 手机浏览器打开：", url)
    print("=" * 46, flush=True)
    qr = make_qr(url, args.qr)
    if qr:
        print(" 二维码：", qr, flush=True)
        try:
            os.startfile(qr)   # 在电脑上弹出二维码，手机直接扫
        except Exception:
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
