"""
remote_browser.py - share YOUR Chrome window through a link (Windows, single file)
pip install flask flask-sock websocket-client

Fixed target: https://web.whatsapp.com  (no URL bar)
"""

import base64
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import websocket
from flask import Flask, Response, request, make_response
from flask_sock import Sock

TARGET_URL = "https://web.whatsapp.com"

DEBUG_PORT = 9222
PORT = 5000
DEVICE_W, DEVICE_H = 1280, 800
TOKEN = secrets.token_urlsafe(18)

# False = any link works. True = links must carry the token.
REQUIRE_TOKEN = False

USE_REAL_PROFILE = False
REAL_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            r"Google\Chrome\User Data")
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome-profile")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"Google\Chrome\Application\chrome.exe"),
]

STATE = {"proc": None, "url": None, "viewers": 0}
LATEST = {"img": None}


# ---------------------------------------------------------------- chrome ----
def find_chrome():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    found = shutil.which("chrome.exe")
    if found:
        return found
    sys.exit("Chrome not found. Install Google Chrome and retry.")


def launch_chrome(url):
    profile = REAL_PROFILE if USE_REAL_PROFILE else PROFILE_DIR
    subprocess.Popen([
        find_chrome(),
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={profile}",
        f"--window-size={DEVICE_W},{DEVICE_H}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate,MediaRouter",
        url,
    ], close_fds=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1):
                return
        except Exception:
            time.sleep(0.5)
    sys.exit("Chrome debugging port never opened. Close all Chrome windows and retry.")


# ------------------------------------------------------------------- cdp ----
class CDP:
    def __init__(self):
        ws_url = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/list", timeout=2) as r:
                    targets = [t for t in json.load(r) if t.get("type") == "page"]
                if targets:
                    ws_url = targets[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            sys.exit("Could not attach to Chrome tab.")

        self.ws = websocket.create_connection(
            ws_url, timeout=60, max_size=1 << 27, suppress_origin=True)
        self.lock = threading.Lock()
        self.msg_id = 0
        self.frames = queue.Queue(maxsize=4)
        self.pending = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, method, params=None):
        with self.lock:
            self.msg_id += 1
            mid = self.msg_id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return mid

    def call(self, method, params=None, timeout=6):
        mid = self.send(method, params)
        ev = threading.Event()
        self.pending[mid] = (ev, {})
        if ev.wait(timeout):
            return self.pending.pop(mid)[1]
        self.pending.pop(mid, None)
        return {}

    def _reader(self):
        while True:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            method = msg.get("method")
            if method == "Page.screencastFrame":
                p = msg["params"]
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put((p["data"], p["sessionId"]))
            elif method == "Page.javascriptDialogOpening":
                self.send("Page.handleJavaScriptDialog", {"accept": True})
            elif "id" in msg and msg["id"] in self.pending:
                ev, box = self.pending[msg["id"]]
                box["result"] = msg
                ev.set()

    def start(self):
        self.send("Page.enable")
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": DEVICE_W, "height": DEVICE_H,
            "deviceScaleFactor": 1, "mobile": False})
        self.send("Browser.grantPermissions",
                  {"permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]})
        self.send("Page.startScreencast", {
            "format": "jpeg", "quality": 62,
            "maxWidth": DEVICE_W, "maxHeight": DEVICE_H,
            "everyNthFrame": 1})


# ----------------------------------------------------------------- flask ----
app = Flask(__name__)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)
cdp: CDP = None

CLIENTS = set()
CLIENTS_LOCK = threading.Lock()


def broadcaster():
    while True:
        data, sid = cdp.frames.get()
        cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        LATEST["img"] = data
        payload = json.dumps({"img": data})
        with CLIENTS_LOCK:
            targets = list(CLIENTS)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


def authed():
    return (request.args.get("t") == TOKEN
            or request.cookies.get("s") == TOKEN
            or not REQUIRE_TOKEN)


def is_local():
    return request.remote_addr in ("127.0.0.1", "::1")


@app.before_request
def gate():
    if request.path == "/favicon.ico":
        return None
    if request.path.startswith("/admin"):
        return None if (is_local() or request.args.get("t") == TOKEN) else Response("Forbidden", 403)
    if authed():
        return None
    return Response("Unauthorized - add ?t=TOKEN", 401)


# ------------------------------------------------------------- shared js ----
INPUT_JS = r"""
const cvs=document.getElementById('scr'), ctx=cvs.getContext('2d'), W=%W%, H=%H%;
let down=false, lastMove=0, ws=null, fails=0, pollTimer=null, fc=0;
function matchBg(){
  try{
    const l=ctx.getImageData(1, Math.floor(cvs.height/2), 1, 1).data;
    const r=ctx.getImageData(cvs.width-2, Math.floor(cvs.height/2), 1, 1).data;
    const mix=[Math.round((l[0]+r[0])/2),Math.round((l[1]+r[1])/2),Math.round((l[2]+r[2])/2)];
    document.body.style.background='rgb('+mix[0]+','+mix[1]+','+mix[2]+')';
  }catch(e){}
}
function draw(b64){const im=new Image();im.onload=()=>{
  if(cvs.width!==im.width||cvs.height!==im.height){cvs.width=im.width;cvs.height=im.height;}
  ctx.drawImage(im,0,0);
  if((++fc & 7)===0) matchBg();};im.src='data:image/jpeg;base64,'+b64;}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
function startPoll(){if(pollTimer)return;
  const tick=async()=>{try{const r=await fetch('/frame?t=%T%');
    if(r.ok){const j=await r.json(); if(j.img) draw(j.img);}}catch(e){}};
  tick(); pollTimer=setInterval(tick,110);}
function startWS(){stopPoll(); const p=location.protocol==='https:'?'wss':'ws';
  try{ws=new WebSocket(p+'://'+location.host+'/ws?t=%T%');}catch(e){startPoll();return;}
  ws.onmessage=e=>{fails=0; draw(JSON.parse(e.data).img);};
  ws.onclose=()=>{fails++; if(fails>3) startPoll(); else setTimeout(startWS,900);};
  ws.onerror=()=>{try{ws.close();}catch(e){}};}
function pos(e){const r=cvs.getBoundingClientRect();
  const sx=cvs.width/r.width, sy=cvs.height/r.height;
  return [Math.round((e.clientX-r.left)*sx), Math.round((e.clientY-r.top)*sy)];}
function post(d){fetch('/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});}
cvs.addEventListener('pointerdown',e=>{down=true;cvs.setPointerCapture(e.pointerId);const[x,y]=pos(e);post({type:'mouse',action:'pressed',x,y,button:0,clickCount:1});});
cvs.addEventListener('pointerup',e=>{down=false;const[x,y]=pos(e);post({type:'mouse',action:'released',x,y,button:0,clickCount:1});});
cvs.addEventListener('pointermove',e=>{const now=Date.now(); if(!down&&now-lastMove<70)return; lastMove=now;
  const[x,y]=pos(e); post({type:'mouse',action:'moved',x,y,button:down?0:-1});});
cvs.addEventListener('wheel',e=>{e.preventDefault();const[x,y]=pos(e);post({type:'wheel',x,y,dx:e.deltaX,dy:e.deltaY});},{passive:false});
const KEYS={Enter:[13,'Enter','\r'],Backspace:[8,'Backspace'],Tab:[9,'Tab'],Escape:[27,'Escape'],Delete:[46,'Delete'],ArrowLeft:[37,'ArrowLeft'],ArrowUp:[38,'ArrowUp'],ArrowRight:[39,'ArrowRight'],ArrowDown:[40,'ArrowDown'],Home:[36,'Home'],End:[35,'End'],PageUp:[33,'PageUp'],PageDown:[34,'PageDown']};
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  const mods=(e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0);
  if(KEYS[e.key]){post({type:'key',name:KEYS[e.key][1],code:KEYS[e.key][0],text:KEYS[e.key][2]||'',mods});e.preventDefault();}
  else if(e.key.length===1&&!e.ctrlKey&&!e.metaKey&&!e.altKey){post({type:'text',text:e.key});e.preventDefault();}
  else if(e.key.length===1){post({type:'key',name:e.key.toUpperCase(),code:e.key.toUpperCase().charCodeAt(0),text:'',mods});e.preventDefault();}});
function sendText(){const t=document.getElementById('txt');if(!t.value)return;post({type:'text',text:t.value});t.value='';}
function nav(a){post({type:'nav',action:a});}
async function paste(){const r=await fetch('/clipboard?t=%T%');const t=await r.text();if(t)post({type:'text',text:t});}
function toggleFit(){const f=cvs.style.objectFit==='fill';cvs.style.objectFit=f?'contain':'fill';}
startWS();
"""

CSS = """
html,body{margin:0;height:100%;overflow:hidden;background:#fff;color:#dfe3e8;font:14px system-ui;overscroll-behavior:none}
body{transition:background-color .25s linear}
#scr{position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;display:block;
     cursor:crosshair;-webkit-user-select:none;user-select:none;touch-action:none}
#bar{display:flex;gap:6px;padding:6px;background:#1e2126;flex-wrap:wrap;align-items:center}
#bar button,#bar input{background:#2b2f36;color:#dfe3e8;border:1px solid #3a3f48;border-radius:6px;padding:6px 9px;font:inherit}
#url{flex:1 1 240px;min-width:120px} #txt{flex:1 1 200px}
#bars{position:fixed;inset:0 0 auto 0;z-index:8;display:none;flex-direction:column;
      box-shadow:0 2px 10px #0008}
#bars.show{display:flex}
#hud{position:fixed;top:6px;right:6px;z-index:9;width:26px;height:26px;padding:0;
     border:0;border-radius:50%;background:#0009;color:#fff9;font:15px/26px system-ui;
     text-align:center;cursor:pointer;opacity:.22;transition:opacity .2s}
#hud:hover{opacity:.85}
#adm{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:8px;
     background:#182028;border-bottom:1px solid #2b3a46}
#adm button{background:#2b6c3f;border:0;color:#fff;border-radius:6px;padding:8px 12px;font:inherit;cursor:pointer}
#adm button.stop{background:#8c2f2f} #adm button.quit{background:#4a4a55}
#status{font:12px ui-monospace,monospace;color:#9fb4c4;padding:4px 10px 8px}
#link{flex:1 1 260px;background:#0f1418;color:#9fb4c4;border:1px solid #2b3a46;border-radius:6px;padding:7px}
"""

TOOLBARS = """
<div id="bar">
 <button onclick="nav('back')">&#8592;</button>
 <button onclick="nav('forward')">&#8594;</button>
 <button onclick="nav('reload')">&#8635;</button>
</div>
<div id="bar">
 <input id="txt" placeholder="type here to fill the focused field, Enter sends"
   onkeydown="if(event.key==='Enter'){sendText();event.preventDefault();}">
 <button onclick="sendText()">Send</button>
 <button onclick="paste()">Paste</button>
 <button onclick="toggleFit()">Fill</button>
</div>
"""

HUD = "<button id='hud' onclick=\"document.getElementById('bars').classList.toggle('show')\">&#9662;</button>"

VIEWER = ("<!doctype html><html><head><meta charset='utf-8'>"
          "<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1,"
          "user-scalable=no'><title>Browser</title><style>" + CSS + "</style></head><body>"
          "<div id='bars'>" + TOOLBARS + "</div>" + HUD +
          "<canvas id='scr' width='1280' height='800'></canvas>"
          "<script>" + INPUT_JS + "</script></body></html>")

ADMIN = ("<!doctype html><html><head><meta charset='utf-8'>"
         "<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1,"
         "user-scalable=no'><title>Browser</title><style>" + CSS + "</style></head><body>"
         "<div id='bars'>"
         "<div id='adm'>"
         "<button onclick=\"startT()\">Start tunnel</button>"
         "<button class='stop' onclick=\"stopT()\">Stop tunnel</button>"
         "<button class='quit' onclick=\"quitT()\">Quit tool</button>"
         "<input id='link' readonly onclick='this.select()'>"
         "</div>"
         "<div id='status'>loading...</div>" + TOOLBARS + "</div>" + HUD +
         "<canvas id='scr' width='1280' height='800'></canvas>"
         "<script>" + INPUT_JS + """
async function poll(){
  try{const r=await fetch('/admin/status'); const j=await r.json();
    document.getElementById('status').textContent =
      'tunnel: '+(j.url?'RUNNING':'stopped')+'   viewers: '+j.viewers+'   chrome: '+(j.chrome?'up':'down');
    document.getElementById('link').value = j.url || 'no public link';}catch(e){}
  setTimeout(poll,2000);}
async function startT(){await fetch('/admin/tunnel/start',{method:'POST'}); poll();}
async function stopT(){await fetch('/admin/tunnel/stop',{method:'POST'}); poll();}
async function quitT(){await fetch('/admin/quit',{method:'POST'});}
poll();
</script></body></html>""")


@app.route("/")
def index():
    resp = make_response(VIEWER.replace("%W%", str(DEVICE_W))
                               .replace("%H%", str(DEVICE_H))
                               .replace("%T%", TOKEN))
    resp.set_cookie("s", TOKEN, max_age=86400, samesite="Lax")
    return resp


@app.route("/admin")
def admin():
    return make_response(ADMIN.replace("%W%", str(DEVICE_W))
                              .replace("%H%", str(DEVICE_H))
                              .replace("%T%", TOKEN))


@app.route("/admin/status")
def admin_status():
    chrome_ok = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1):
            chrome_ok = True
    except Exception:
        pass
    p = STATE["proc"]
    running = bool(p and p.poll() is None)
    return {"url": STATE["url"] if running else None,
            "viewers": len(CLIENTS), "chrome": chrome_ok}


@app.route("/admin/tunnel/stop", methods=["POST"])
def admin_stop():
    stop_tunnel()
    return {"ok": True}


@app.route("/admin/tunnel/start", methods=["POST"])
def admin_start():
    if not (STATE["proc"] and STATE["proc"].poll() is None):
        threading.Thread(target=start_tunnel, daemon=True).start()
    return {"ok": True}


@app.route("/admin/quit", methods=["POST"])
def admin_quit():
    def bye():
        time.sleep(0.4)
        stop_tunnel()
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


@sock.route("/ws")
def ws_feed(ws):
    q = queue.Queue(maxsize=2)
    with CLIENTS_LOCK:
        CLIENTS.add(q)
    STATE["viewers"] = len(CLIENTS)
    if LATEST["img"]:
        try:
            ws.send(json.dumps({"img": LATEST["img"]}))
        except Exception:
            pass
    try:
        while True:
            ws.send(q.get(timeout=30))
    except Exception:
        pass
    finally:
        with CLIENTS_LOCK:
            CLIENTS.discard(q)
        STATE["viewers"] = len(CLIENTS)


@app.route("/frame")
def frame():
    return {"img": LATEST["img"]}


@app.route("/clipboard")
def clipboard():
    res = cdp.call("Runtime.evaluate",
                   {"expression": "navigator.clipboard.readText()",
                    "awaitPromise": True, "userGesture": True})
    try:
        text = res["result"]["result"]["value"] or ""
    except Exception:
        text = ""
    return Response(text, mimetype="text/plain")


@app.route("/input", methods=["POST"])
def input_route():
    d = request.get_json(force=True, silent=True) or {}
    kind = d.get("type")
    if kind == "mouse":
        cdp.send("Input.dispatchMouseEvent", {
            "type": d.get("action", "moved"), "x": d.get("x", 0), "y": d.get("y", 0),
            "button": "left" if d.get("button") == 0 else "none",
            "buttons": 1 if d.get("button") == 0 else 0,
            "clickCount": d.get("clickCount", 1)})
    elif kind == "wheel":
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": d.get("x", 0), "y": d.get("y", 0),
            "deltaX": d.get("dx", 0), "deltaY": d.get("dy", 0)})
    elif kind == "text":
        cdp.send("Input.insertText", {"text": d.get("text", "")})
    elif kind == "key":
        base = {"key": d.get("name", ""), "code": d.get("name", ""),
                "windowsVirtualKeyCode": d.get("code", 0),
                "nativeVirtualKeyCode": d.get("code", 0),
                "modifiers": d.get("mods", 0)}
        if d.get("text"):
            base["text"] = d["text"]
            base["unmodifiedText"] = d["text"]
        cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
    elif kind == "nav":
        act = d.get("action")
        if act == "back":
            cdp.send("Runtime.evaluate", {"expression": "history.back()"})
        elif act == "forward":
            cdp.send("Runtime.evaluate", {"expression": "history.forward()"})
        elif act == "reload":
            cdp.send("Page.reload", {"ignoreCache": False})
        elif d.get("url"):
            cdp.send("Page.navigate", {"url": d["url"]})
    return Response("ok")


# ---------------------------------------------------------------- tunnel ----
def ensure_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe")
    if os.path.isfile(local):
        return local
    print("[*] Downloading cloudflared (one time)...")
    urllib.request.urlretrieve(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
        local)
    return local


def start_tunnel():
    if STATE["proc"] and STATE["proc"].poll() is None:
        return
    try:
        exe = ensure_cloudflared()
    except Exception as e:
        print(f"[!] cloudflared unavailable ({e}) - LAN only")
        return
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    STATE["proc"] = proc
    deadline = time.time() + 40
    while time.time() < deadline and proc.poll() is None:
        line = proc.stdout.readline()
        if not line:
            continue
        if "trycloudflare.com" in line:
            for word in line.split():
                if word.startswith("https://"):
                    STATE["url"] = word.strip()
                    print("\n[+] PUBLIC LINK: " + STATE["url"])
                    print("[+] ADMIN LINK : " + STATE["url"] + "/admin?t=" + TOKEN + "\n")
                    return
    print("[!] tunnel did not start")


def stop_tunnel():
    p = STATE["proc"]
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    STATE["proc"] = None
    STATE["url"] = None
    print("[*] tunnel stopped")


# ------------------------------------------------------------------ main ----
def main():
    global cdp
    target = TARGET_URL

    launch_chrome(target)
    cdp = CDP()
    cdp.start()
    threading.Thread(target=broadcaster, daemon=True).start()

    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False),
        daemon=True).start()
    time.sleep(1)

    local = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    admin = f"http://127.0.0.1:{PORT}/admin"
    print("\n" + "=" * 62)
    print(" ADMIN PANEL (open this, tap the corner dot for controls):\n " + admin)
    print(" LOCAL VIEW :\n " + local)
    print("=" * 62 + "\n")

    webbrowser.open(admin)
    threading.Thread(target=start_tunnel, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_tunnel()
        print("\nStopped. Your Chrome window stays open and logged in.")


if __name__ == "__main__":
    main()