"""Execute one or a grouped phone action, then capture one correlated fresh frame."""
import hashlib,json,sys,time,urllib.request
from pathlib import Path
from PIL import Image
from io import BytesIO
BASE="http://127.0.0.1:8768";ROOT=Path(__file__).resolve().parent;CONFIG=ROOT/"tapper_config.json"
def request(path,payload=None,timeout=180):
 data=None if payload is None else json.dumps(payload).encode()
 req=urllib.request.Request(BASE+path,data=data,headers={"Content-Type":"application/json"} if data else {},method="POST" if data else "GET")
 with urllib.request.urlopen(req,timeout=timeout) as response:
  raw=response.read();return (json.loads(raw) if not response.headers.get("Content-Type","").startswith("image/") else raw),dict(response.headers)
def main():
 args=json.loads(sys.argv[1]);action=args.get("path");health,_=request("/health")
 if health.get("profile")!="speed-v1":raise RuntimeError("speed-v1 service is not active")
 result=None
 if action:
  before=CONFIG.read_text(encoding="utf-8");cfg=json.loads(before);cfg["z_contact_margin_mm"]=float(args.get("margin",.9))
  try:
   CONFIG.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");request("/api/reload-calibration",{})
   if action=="/session/taps":action="/api/tap-sequence"
   result,_=request(action,args.get("body",{}));print(json.dumps(result,ensure_ascii=False))
  finally:
   CONFIG.write_text(before,encoding="utf-8");request("/api/reload-calibration",{})
 start=time.perf_counter();image,headers=request("/api/camera.jpg",timeout=12)
 action_id=None if result is None else str(result.get("action_id"));seen_id=headers.get("X-Last-Action-Id")
 if action_id is not None and action_id!=seen_id:raise RuntimeError(f"Fresh-frame/action mismatch: action={action_id}, frame={seen_id}")
 frame=Image.open(BytesIO(image));full=ROOT/"session_latest_full.jpg";frame.save(full)
 frame.crop((60,0,830,1330)).resize((650,1123)).save(ROOT/"session_latest_phone.jpg",quality=90)
 metadata={"action_id":action_id,"frame_action_id":seen_id,"frame_sequence":headers.get("X-Frame-Sequence"),"frame_age_ms":headers.get("X-Frame-Age-Ms"),"frame_request_ms":round((time.perf_counter()-start)*1000,1),"frame_sha256":hashlib.sha256(image).hexdigest()}
 (ROOT/"session_latest_metadata.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
 print(full);print(json.dumps(metadata,ensure_ascii=False))
if __name__=="__main__":main()
