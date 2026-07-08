# ========================================================
# 🚀 FORCE NETWORK INTERFACE (ดัก IP ให้ Manual Control)
# ========================================================
import socket
import struct
TARGET_IP   = "192.168.100.99"  # ถูกต้องแล้วตาม ipconfig
IFACE_NAME  = "Wi-Fi"  # 👈 เปลี่ยนเป็นชื่อเต็มตรงนี้!

# Both drones sit on the same 192.168.100.0/24 subnet/gateway, so Windows'
# routing table alone can't tell the two Wi-Fi adapters apart for outbound
# traffic — a plain bind() to a local source IP isn't enough. IP_UNICAST_IF
# pins a socket's egress to a specific physical interface, overriding the
# ambiguous route lookup.
IP_UNICAST_IF = 31  # Windows-specific IPPROTO_IP option
try:
    _IFACE_INDEX = socket.if_nametoindex(IFACE_NAME)
except OSError:
    _IFACE_INDEX = None
    print(f"⚠️ Could not resolve interface '{IFACE_NAME}' — check the name against `ipconfig`.")

def _pin_interface(sock):
    if _IFACE_INDEX is not None:
        try:
            sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack('I', socket.htonl(_IFACE_INDEX)))
        except OSError:
            pass

_orig_bind = socket.socket.bind
_orig_connect = socket.socket.connect
_orig_sendto = socket.socket.sendto

def patched_bind(self, address):
    ip, port = address
    if port not in (8080, 8081) and ip not in ("127.0.0.1", "localhost"):
        address = (TARGET_IP, port)
        try:
            self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        _pin_interface(self)
    return _orig_bind(self, address)

def patched_connect(self, address):
    ip, port = address
    if ip not in ("127.0.0.1", "localhost"):
        try: _orig_bind(self, (TARGET_IP, 0))
        except: pass
        _pin_interface(self)
    return _orig_connect(self, address)

def patched_sendto(self, *args, **kwargs):
    try: _orig_bind(self, (TARGET_IP, 0))
    except: pass
    _pin_interface(self)
    return _orig_sendto(self, *args, **kwargs)

socket.socket.bind = patched_bind
socket.socket.connect = patched_connect
socket.socket.sendto = patched_sendto
# ========================================================

from flask import Flask, render_template_string, request
from flask_sock import Sock
import json, time, threading
import os
import cv2
import numpy as np
import pyhula

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "analyzeduration;50000000|probesize;50000000|timeout;2000"

app = Flask(__name__)
sock = Sock(app)

# ─────────────────────────────────────────
#  Drone connection
# ─────────────────────────────────────────
api = pyhula.UserApi()
connected = False
flying = False
battery_level = "--"

# เชื่อมต่อแค่ 5 รอบตอนเริ่มโปรแกรม (เหมือน Block Code)
for i in range(5):
    print(f"📡 Connection attempt {i+1}/5...")
    if api.connect():
        print("✅ Connected to drone!")
        connected = True
        break
    time.sleep(1)

if not connected:
    print("❌ Could not connect to drone. Running in mock mode.")

# ─────────────────────────────────────────
#  Battery Checker Thread (เช็คแบตทุกๆ 5 วินาที)
# ─────────────────────────────────────────
def battery_loop():
    global battery_level
    while connected:
        time.sleep(5)
        try:
            if hasattr(api, 'get_electic'): battery_level = str(api.get_electic())
            elif hasattr(api, 'get_battery'): battery_level = str(api.get_battery())
            else: battery_level = "OK" 
        except:
            battery_level = "--"

if connected:
    threading.Thread(target=battery_loop, daemon=True).start()

# ─────────────────────────────────────────
#  Camera Thread
# ─────────────────────────────────────────
latest_frame    = None
latest_frame_id = 0
frame_lock      = threading.Lock()
camera_running  = False
CAM_WIDTH   = 960
CAM_HEIGHT  = 640
TARGET_FPS  = 15
FRAME_INTERVAL = 1.0 / TARGET_FPS

def camera_loop():
    global latest_frame, latest_frame_id, camera_running
    camera_running = True
    try:
        api.Plane_cmd_swith_rtp(0)
        time.sleep(2)
        api.single_fly_flip_rtp()
        time.sleep(4)
    except:
        camera_running = False
        return

    while camera_running:
        try:
            frame = api.get_image_array()
            if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
                time.sleep(0.05)
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = frame.shape[:2]
            if w > CAM_WIDTH or h > CAM_HEIGHT:
                frame = cv2.resize(frame, (CAM_WIDTH, CAM_HEIGHT), interpolation=cv2.INTER_AREA)
            with frame_lock:
                latest_frame = frame
                latest_frame_id += 1
        except:
            time.sleep(0.1)

if connected:
    threading.Thread(target=camera_loop, daemon=True).start()

# ─────────────────────────────────────────
#  RC state (🚀 โหมดลื่นไหลขั้นสุด Dominant Axis Control)
# ─────────────────────────────────────────
rc_state = {"forward": 0, "right": 0, "up": 0, "yaw": 0}
rc_lock  = threading.Lock()

TICK_MS  = 200 # จังหวะส่งคำสั่ง 5 ครั้ง/วินาที
DEADZONE = 15

# ─────────────────────────────────────────
#  Action feed — what the drone is physically doing RIGHT NOW.
#  These moves are discrete blocking commands (not velocity), so we expose each
#  one's EXEC → DONE lifecycle to the UI to drive the "combo" overlay.
# ─────────────────────────────────────────
action_log  = []            # rolling list of recent moves: {id, label, status}
action_lock = threading.Lock()
_action_seq = 0

def start_action(label):
    """Register a move as executing; returns its id so we can finish it later."""
    global _action_seq
    with action_lock:
        _action_seq += 1
        aid = _action_seq
        action_log.append({"id": aid, "label": label, "status": "EXEC"})
        if len(action_log) > 12:
            del action_log[:len(action_log) - 12]
    return aid

def finish_action(aid, status="DONE"):
    with action_lock:
        for ev in action_log:
            if ev["id"] == aid:
                ev["status"] = status
                break

def rc_tick_loop():
    while True:
        time.sleep(TICK_MS / 1000.0)
        if not flying or not connected:
            continue
            
        with rc_lock:
            axes = {
                "fwd": rc_state["forward"],
                "rgt": rc_state["right"],
                "up":  rc_state["up"],
                "yaw": rc_state["yaw"]
            }

        active_axes = {k: v for k, v in axes.items() if abs(v) > DEADZONE}
        
        if not active_axes:
            continue 
            
        dom_axis = max(active_axes, key=lambda k: abs(active_axes[k]))
        val = active_axes[dom_axis]
        
        speed = max(1, min(120, int(abs(val) / 100 * 80)))
        speedYaw = max(1, min(30, int(abs(val) / 100 * 20)))
        # speed = 150
        # speedYaw = 25

        # Resolve the dominant axis to a human label + the blocking API call to run.
        if dom_axis == "fwd":
            label, action = ("FORWARD", lambda: api.single_fly_forward(speed)) if val > 0 \
                       else ("BACK",    lambda: api.single_fly_back(speed))
        elif dom_axis == "rgt":
            label, action = ("RIGHT",   lambda: api.single_fly_right(speed)) if val > 0 \
                       else ("LEFT",    lambda: api.single_fly_left(speed))
        elif dom_axis == "up":
            label, action = ("UP",      lambda: api.single_fly_up(speed)) if val > 0 \
                       else ("DOWN",    lambda: api.single_fly_down(speed))
        else:  # yaw
            label, action = ("YAW RIGHT", lambda: api.single_fly_turnright(speedYaw)) if val > 0 \
                       else ("YAW LEFT",  lambda: api.single_fly_turnleft(speedYaw))

        # EXEC now, run the (blocking) move to completion, then mark DONE/FAIL.
        aid = start_action(label)
        try:
            action()
            finish_action(aid, "DONE")
        except Exception:
            finish_action(aid, "FAIL")

threading.Thread(target=rc_tick_loop, daemon=True).start()

# ─────────────────────────────────────────
#  HTML UI (Overlay Interface + Battery)
# ─────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, orientation=landscape">
<title>DRONE//CTRL - {{ mode|upper }}</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --accent:  #00e5ff;
    --danger:  #ff2d55;
    --ok:      #00ff88;
    --panel:   rgba(10, 20, 30, 0.6);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  
  body, html { 
      width: 100%; height: 100%; overflow: hidden; background: #000; 
      font-family: 'Share Tech Mono', monospace; user-select: none; -webkit-user-select: none;
  }

  .cam-container {
      position: absolute; top: 0; left: 0; width: 100vw; height: 100vh; z-index: 1;
  }
  .cam-container img {
      width: 100%; height: 100%; object-fit: cover; filter: brightness(0.85); 
  }

  .ui-layer {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 10;
      display: flex; flex-direction: column; justify-content: space-between; padding: 15px;
      pointer-events: none; 
  }

  .header {
      display: flex; justify-content: space-between; align-items: flex-start; pointer-events: auto;
  }
  .logo { font-family: 'Orbitron', sans-serif; font-size: 1.2rem; font-weight: 900; color: var(--accent); text-shadow: 0 0 10px var(--accent); }
  .logo span { color: var(--danger); }
  .kb-hint { font-size: 0.55rem; color: rgba(255,255,255,0.4); letter-spacing: 1px; margin-top: 4px; text-shadow: none; }
  
  .status-box {
      background: var(--panel); border: 1px solid var(--accent); padding: 5px 10px; border-radius: 4px;
      backdrop-filter: blur(4px); display: flex; flex-direction: column; align-items: flex-end; gap: 5px;
  }
  
  .status-line { display: flex; gap: 10px; align-items: center;}
  #battery-status { font-size: 0.7rem; font-weight: bold; color: #f39c12; letter-spacing: 1px; }
  #conn-status { font-size: 0.7rem; font-weight: bold; color: var(--danger); letter-spacing: 1px; }
  #conn-status.live { color: var(--ok); }
  #conn-status.warn { color: #f39c12; }
  #fps-display { font-size: 0.6rem; color: var(--accent); }

  .telemetry {
      position: absolute; top: 15px; left: 50%; transform: translateX(-50%);
      background: var(--panel); border: 1px solid rgba(0, 229, 255, 0.3); padding: 8px 15px; border-radius: 4px;
      backdrop-filter: blur(4px); display: flex; gap: 15px; font-size: 0.7rem; color: #fff; pointer-events: auto;
  }
  .telemetry span { color: var(--accent); font-weight: bold; }

  .controls {
      display: flex; justify-content: space-between; align-items: flex-end; width: 100%; pointer-events: auto;
  }

  .stick-wrap { display: flex; flex-direction: column; align-items: center; gap: 8px; }
  .stick-label { font-size: 0.65rem; color: rgba(255,255,255,0.7); font-weight: bold; letter-spacing: 1px; text-shadow: 1px 1px 2px #000; }
  .stick-label span { color: var(--accent); }
  
  .joystick-zone { 
      width: 140px; height: 140px; border-radius: 50%; 
      background: rgba(0, 0, 0, 0.3); border: 2px solid rgba(255, 255, 255, 0.2); 
      position: relative; touch-action: none; box-shadow: inset 0 0 20px rgba(0,0,0,0.5);
      backdrop-filter: blur(2px);
  }
  .joystick-zone::before, .joystick-zone::after {
      content: ''; position: absolute; background: rgba(255,255,255,0.2);
  }
  .joystick-zone::before { width: 1px; height: 100%; left: 50%; top: 0; }
  .joystick-zone::after { height: 1px; width: 100%; top: 50%; left: 0; }

  .knob { 
      width: 46px; height: 46px; border-radius: 50%; 
      background: rgba(0, 229, 255, 0.8); border: 2px solid #fff; 
      position: absolute; top: 47px; left: 47px; pointer-events: none; z-index: 2; 
      box-shadow: 0 0 15px var(--accent);
  }

  .action-buttons {
      display: flex; gap: 15px; margin-bottom: 20px;
  }
  .btn { 
      padding: 15px 25px; border-radius: 50px; font-family: 'Orbitron', sans-serif; font-size: 0.8rem; font-weight: 900;
      cursor: pointer; border: 2px solid; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px);
      text-transform: uppercase; letter-spacing: 2px; transition: 0.2s; color: #fff;
  }
  .btn:active { transform: scale(0.9); }
  .btn-takeoff { border-color: var(--ok); text-shadow: 0 0 5px var(--ok); box-shadow: inset 0 0 10px rgba(0,255,136,0.3); }
  .btn-land { border-color: var(--danger); text-shadow: 0 0 5px var(--danger); box-shadow: inset 0 0 10px rgba(255,45,85,0.3); }

  .center-crosshair {
      position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
      width: 30px; height: 30px; opacity: 0.4; pointer-events: none;
  }
  .center-crosshair::before, .center-crosshair::after { content: ''; position: absolute; background: var(--accent); }
  .center-crosshair::before { width: 2px; height: 100%; left: 14px; top: 0; }
  .center-crosshair::after { height: 2px; width: 100%; top: 14px; left: 0; }
  .center-crosshair .circle {
      position: absolute; top: 5px; left: 5px; width: 20px; height: 20px;
      border: 1px solid var(--accent); border-radius: 50%;
  }

  /* ── Action feed (top-right): game-combo readout of what the drone is doing ── */
  .action-feed {
      position: absolute; top: 78px; right: 15px; z-index: 20;
      display: flex; flex-direction: column; align-items: flex-end; gap: 7px;
      pointer-events: none; max-width: 46vw;
  }
  .combo-badge {
      font-family: 'Orbitron', sans-serif; font-weight: 900; font-size: 0.95rem;
      color: #fff; letter-spacing: 2px; padding: 4px 14px; border-radius: 20px;
      border: 1px solid var(--accent);
      background: linear-gradient(90deg, rgba(255,45,85,0.15), rgba(0,229,255,0.28));
      text-shadow: 0 0 10px var(--accent);
      opacity: 0; transform: scale(0.5) translateX(20px); transition: opacity .15s, transform .15s;
  }
  .combo-badge.show { opacity: 1; transform: scale(1) translateX(0); }
  .combo-badge #combo-count { color: var(--danger); font-size: 1.15rem; }

  .action-list { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
  .action-chip {
      display: flex; align-items: center; gap: 9px; min-width: 150px;
      font-family: 'Orbitron', sans-serif; font-weight: 700; font-size: 0.78rem;
      color: #fff; letter-spacing: 1px; padding: 7px 13px; border-radius: 5px;
      background: var(--panel); backdrop-filter: blur(4px);
      border: 1px solid rgba(255,255,255,0.15); border-right: 3px solid var(--accent);
      animation: chipIn .22s ease-out; transition: opacity .4s, transform .4s;
  }
  .action-chip .ac-icon   { font-size: 1.05rem; line-height: 1; }
  .action-chip .ac-label  { flex: 1; }
  .action-chip .ac-status { margin-left: auto; font-size: 0.95rem; font-weight: 900; }

  .action-chip.exec {
      border-right-color: var(--accent);
      animation: chipIn .22s ease-out, execPulse .9s ease-in-out infinite;
  }
  .action-chip.exec .ac-status { color: var(--accent); }
  .action-chip.done { border-right-color: var(--ok);     color: #cdeee0; }
  .action-chip.done .ac-status { color: var(--ok); }
  .action-chip.fail { border-right-color: var(--danger); color: #ffd0d8; }
  .action-chip.fail .ac-status { color: var(--danger); }
  .action-chip.fade { opacity: 0; transform: translateX(40px); }

  @keyframes chipIn   { from { opacity: 0; transform: translateX(45px); } to { opacity: 1; transform: translateX(0); } }
  @keyframes execPulse { 0%,100% { box-shadow: 0 0 8px rgba(0,229,255,0.35); } 50% { box-shadow: 0 0 20px rgba(0,229,255,0.85); } }

  /* On phones the top-right feed overlaps the right joystick / camera. Move it to the
     middle-top, taking the place of the F/B·L/R telemetry bar (which we hide there). */
  @media (pointer: coarse) and (max-width: 950px) {
      .telemetry { display: none; }

      .action-feed {
          top: 12px; right: auto; left: 50%; transform: translateX(-50%);
          align-items: center; max-width: 80vw;
      }
      .action-list { align-items: center; }
      .action-chip { min-width: 0; }
      /* keep the stack short so it never creeps down over the sticks */
      .action-list .action-chip:nth-child(n+4) { display: none; }
      /* chips now slide in from above rather than the right edge */
      @keyframes chipIn { from { opacity: 0; transform: translateY(-16px); } to { opacity: 1; transform: translateY(0); } }
      .action-chip.fade { opacity: 0; transform: translateY(-16px); }
  }

  {% if mode == 'stream' %}
  /* Broadcast view: purely passive — viewers must never be able to touch the drone */
  .controls, .action-buttons, .joystick-zone { pointer-events: none !important; }
  {% endif %}

  @media (orientation: portrait) {
      .ui-layer::before {
          content: "PLEASE ROTATE YOUR PHONE TO LANDSCAPE";
          position: fixed; inset: 0; background: #000; color: var(--accent);
          display: flex; align-items: center; justify-content: center; z-index: 999;
          font-family: 'Orbitron', sans-serif; font-size: 1.2rem; text-align: center; padding: 20px; pointer-events: auto;
      }
  }
</style>
</head>
<body>

<div class="cam-container">
    <img id="cam" src="" alt="">
</div>

<div class="ui-layer">
    <div class="header">
        <div class="logo">DRONE<span>//</span>CTRL
            <div class="kb-hint">WASD / IJKL &middot; &uarr; TAKEOFF &middot; &darr; LAND</div>
        </div>
        <div class="status-box">
            <div class="status-line">
                <div id="battery-status">BAT: --%</div>
                <div id="conn-status">OFFLINE</div>
            </div>
            <div id="fps-display">-- fps</div>
        </div>
    </div>

    <div class="action-feed">
        <div class="combo-badge" id="combo-badge">COMBO <span id="combo-count">x2</span></div>
        <div class="action-list" id="action-list"></div>
    </div>

    <div class="telemetry">
        <div>F/B: <span id="t-fwd">0</span></div>
        <div>L/R: <span id="t-rgt">0</span></div>
        <div>UP/DN: <span id="t-up">0</span></div>
        <div>YAW: <span id="t-yaw">0</span></div>
    </div>

    <div class="center-crosshair"><div class="circle"></div></div>

    <div class="controls">
        <div class="stick-wrap">
            <div class="joystick-zone" id="left-zone"><div class="knob" id="left-knob"></div></div>
            <div class="stick-label">THROTTLE · <span>YAW</span></div>
        </div>

        <div class="action-buttons">
            <button class="btn btn-takeoff" id="btn-takeoff">TAKEOFF</button>
            <button class="btn btn-land" id="btn-land">LAND</button>
        </div>

        <div class="stick-wrap">
            <div class="joystick-zone" id="right-zone"><div class="knob" id="right-knob"></div></div>
            <div class="stick-label">PITCH · <span>ROLL</span></div>
        </div>
    </div>
</div>

<script>
const MODE = "{{ mode }}";   // "control" = the pilot at "/", "stream" = passive broadcast at "/stream"
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(proto + "//" + location.host + "/ws");
const statusEl = document.getElementById("conn-status");
const batEl = document.getElementById("battery-status");
const camEl = document.getElementById("cam");
const fpsEl = document.getElementById("fps-display");

ws.onopen  = () => { statusEl.textContent = "LINK ACTIVE"; statusEl.className = "live"; };
ws.onclose = () => { statusEl.textContent = "OFFLINE"; statusEl.className = ""; };

let fTimes = [];

let lastFrameUrl = null;

ws.onmessage = e => {
  if (typeof e.data === "string") {
    const msg = JSON.parse(e.data);
    if (msg.battery !== undefined) {
        batEl.innerText = `BAT: ${msg.battery}${msg.battery !== "--" && msg.battery !== "OK" ? "%" : ""}`;
        if (parseInt(msg.battery) <= 20) {
            batEl.style.color = "var(--danger)";
        } else {
            batEl.style.color = "#f39c12";
        }
    }

    // Combo action feed — works in BOTH control and stream mode
    if (msg.actions !== undefined) renderActionFeed(msg.actions);

    // STREAM mode: passively replay the pilot's live joystick/telemetry from the server broadcast
    if (MODE === "stream" && msg.rc !== undefined) {
        document.getElementById("t-fwd").textContent = msg.rc.forward;
        document.getElementById("t-rgt").textContent = msg.rc.right;
        document.getElementById("t-up").textContent  = msg.rc.up;
        document.getElementById("t-yaw").textContent = msg.rc.yaw;

        // Same axis mapping as the live joysticks, reversed to place the knobs
        setStreamKnob("left-zone",  "left-knob",  msg.rc.yaw,   -msg.rc.up);
        setStreamKnob("right-zone", "right-knob", msg.rc.right, -msg.rc.forward);

        if (msg.flying !== undefined) {
            const btnTk = document.getElementById("btn-takeoff");
            const btnLd = document.getElementById("btn-land");
            if (msg.flying) {
                btnTk.style.background = "rgba(0,255,136,0.3)";
                btnLd.style.background = "rgba(0,0,0,0.5)";
            } else {
                btnTk.style.background = "rgba(0,0,0,0.5)";
                btnLd.style.background = "rgba(255,45,85,0.3)";
            }
        }
    }
    return;
  }

  // Binary message = raw JPEG frame
  const url = URL.createObjectURL(e.data);
  const prevUrl = lastFrameUrl;
  camEl.onload = () => { if (prevUrl) URL.revokeObjectURL(prevUrl); };
  camEl.src = url;
  lastFrameUrl = url;

  const now = performance.now();
  fTimes.push(now);
  if (fTimes.length > 12) fTimes.shift();
  if (fTimes.length >= 2) {
    const span = fTimes[fTimes.length - 1] - fTimes[0];
    fpsEl.textContent = ((fTimes.length - 1) / span * 1000).toFixed(1) + " FPS";
  }
};

// ─── Passive knob positioning used by STREAM (broadcast) mode ───
function setStreamKnob(zoneId, knobId, valX, valY) {
    const zone = document.getElementById(zoneId);
    const knob = document.getElementById(knobId);
    if (!zone || !knob) return;
    const limit = (zone.offsetWidth / 2) - 23;
    const cx = zone.offsetWidth / 2, cy = zone.offsetHeight / 2;
    const dx = (valX / 100) * limit;
    const dy = (valY / 100) * limit;
    knob.style.left = (cx + dx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy + dy - knob.offsetHeight / 2) + "px";
}

// ─── Combo action feed: visualise each discrete drone move (EXEC → DONE) ───
const actionListEl = document.getElementById("action-list");
const comboBadgeEl = document.getElementById("combo-badge");
const comboCountEl = document.getElementById("combo-count");
const seenActions  = new Map();   // server id -> { el, done }
let comboCount = 0;
let lastComboTime = 0;

const ACTION_ICON = {
  "FORWARD": "⬆", "BACK": "⬇", "LEFT": "⬅", "RIGHT": "➡",
  "UP": "🔼", "DOWN": "🔽", "YAW LEFT": "↺", "YAW RIGHT": "↻",
  "TAKEOFF": "🚀", "LAND": "🛬"
};

function renderActionFeed(actions) {
  actions.forEach(a => {
    let entry = seenActions.get(a.id);

    if (!entry) {
      // Brand-new move → build a chip and slide it in from the right
      const el = document.createElement("div");
      el.className = "action-chip exec";
      el.innerHTML = '<span class="ac-icon"></span><span class="ac-label"></span><span class="ac-status">▶</span>';
      el.querySelector(".ac-icon").textContent  = ACTION_ICON[a.label] || "●";
      el.querySelector(".ac-label").textContent = a.label;
      actionListEl.prepend(el);            // newest on top
      entry = { el, done: false };
      seenActions.set(a.id, entry);

      // Combo counter: chain moves that fire in quick succession
      const now = performance.now();
      comboCount = (now - lastComboTime < 1600) ? comboCount + 1 : 1;
      lastComboTime = now;
      updateCombo();

      while (actionListEl.children.length > 6) actionListEl.lastElementChild.remove();
    }

    // Transition EXEC → DONE / FAIL once, then fade the chip out
    if (a.status !== "EXEC" && !entry.done) {
      entry.done = true;
      entry.el.classList.remove("exec");
      entry.el.classList.add(a.status.toLowerCase());   // "done" | "fail"
      entry.el.querySelector(".ac-status").textContent = (a.status === "DONE") ? "✓" : "✕";
      const el = entry.el;
      setTimeout(() => { el.classList.add("fade"); setTimeout(() => el.remove(), 400); }, 1200);
    }
  });

  // Forget ids the server has already aged out of its buffer (prevents leaks/dupes)
  if (actions.length) {
    const minId = actions[0].id;   // action_log is oldest-first
    for (const id of seenActions.keys()) if (id < minId) seenActions.delete(id);
  }
}

function updateCombo() {
  if (comboCount >= 2) {
    comboCountEl.textContent = "x" + comboCount;
    comboBadgeEl.classList.add("show");
  } else {
    comboBadgeEl.classList.remove("show");
  }
}

// Drop the combo badge once the pilot pauses
setInterval(() => {
  if (comboCount > 0 && performance.now() - lastComboTime > 1600) {
    comboCount = 0;
    comboBadgeEl.classList.remove("show");
  }
}, 400);

// ═══ Active control wiring — ONLY for the pilot at "/". Never runs in stream mode ═══
if (MODE === "control") {
document.getElementById("btn-takeoff").onclick = () => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "takeoff" })); };
document.getElementById("btn-land").onclick    = () => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "land" })); };

const rc = { forward: 0, right: 0, up: 0, yaw: 0 };
let lastSentRC = JSON.stringify(rc);

function updateTelem() {
  document.getElementById("t-fwd").textContent = rc.forward;
  document.getElementById("t-rgt").textContent = rc.right;
  document.getElementById("t-up").textContent = rc.up;
  document.getElementById("t-yaw").textContent = rc.yaw;
}

setInterval(() => {
    const currentRC = JSON.stringify(rc);
    if (currentRC !== lastSentRC || (rc.forward !== 0 || rc.right !== 0 || rc.up !== 0 || rc.yaw !== 0)) {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "rc", ...rc }));
        lastSentRC = currentRC;
    }
}, 80);

function makeJoystick(zoneId, knobId, onMove) {
  const zone = document.getElementById(zoneId);
  const knob = document.getElementById(knobId);
  const R = zone.offsetWidth / 2;
  const cx = R, cy = R, limit = R - 23; 
  let active = false, pid = null;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  let ticking = false;

  function move(ex, ey) {
    const rect = zone.getBoundingClientRect();
    let dx = ex - rect.left - cx, dy = ey - rect.top - cy;
    const dist = Math.hypot(dx, dy);
    if (dist > limit) { dx *= limit / dist; dy *= limit / dist; }
    
    if (!ticking) {
        window.requestAnimationFrame(() => {
            knob.style.left = (cx + dx - knob.offsetWidth  / 2) + "px";
            knob.style.top  = (cy + dy - knob.offsetHeight / 2) + "px";
            ticking = false;
        });
        ticking = true;
    }
    onMove(Math.round(clamp(dx / limit * 100, -100, 100)), Math.round(clamp(dy / limit * 100, -100, 100)));
  }
  
  function reset() {
    knob.style.left = (cx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy - knob.offsetHeight / 2) + "px";
    onMove(0, 0);
  }

  zone.addEventListener("pointerdown", e => {
    active = true; pid = e.pointerId;
    zone.setPointerCapture(e.pointerId); move(e.clientX, e.clientY);
  });
  zone.addEventListener("pointermove", e => { if (active && e.pointerId === pid) move(e.clientX, e.clientY); });
  ["pointerup","pointercancel"].forEach(ev => zone.addEventListener(ev, () => { active = false; reset(); }));
  reset();
}

makeJoystick("left-zone",  "left-knob",  (x, y) => { rc.yaw = x; rc.up = -y; updateTelem(); });
makeJoystick("right-zone", "right-knob", (x, y) => { rc.right = x; rc.forward = -y; updateTelem(); });

// ─── Keyboard Control (Desktop): WASD = move, IJKL = throttle/yaw, ↑/↓ = takeoff/land ───
const leftZoneEl   = document.getElementById("left-zone");
const leftKnobEl   = document.getElementById("left-knob");
const rightZoneEl  = document.getElementById("right-zone");
const rightKnobEl  = document.getElementById("right-knob");
const keysPressed  = new Set();
const MOVE_KEYS     = ["w", "a", "s", "d", "i", "j", "k", "l"];

function setKnobByPercent(zone, knob, xPercent, yPercent) {
  const R = zone.offsetWidth / 2, limit = R - 23;
  const dx = xPercent / 100 * limit, dy = yPercent / 100 * limit;
  knob.style.left = (R + dx - knob.offsetWidth  / 2) + "px";
  knob.style.top  = (R + dy - knob.offsetHeight / 2) + "px";
}

function updateFromKeyboard() {
  const w = keysPressed.has("w"), a = keysPressed.has("a"), s = keysPressed.has("s"), d = keysPressed.has("d");
  const i = keysPressed.has("i"), j = keysPressed.has("j"), k = keysPressed.has("k"), l = keysPressed.has("l");

  rc.forward = w ? 100 : (s ? -100 : 0);
  rc.right   = d ? 100 : (a ? -100 : 0);
  rc.up      = i ? 100 : (k ? -100 : 0);
  rc.yaw     = l ? 100 : (j ? -100 : 0);

  setKnobByPercent(rightZoneEl, rightKnobEl, rc.right, -rc.forward);
  setKnobByPercent(leftZoneEl,  leftKnobEl,  rc.yaw,   -rc.up);
  updateTelem();
}

window.addEventListener("keydown", e => {
  const key = e.key.toLowerCase();
  if (MOVE_KEYS.includes(key)) {
    e.preventDefault();
    if (!keysPressed.has(key)) { keysPressed.add(key); updateFromKeyboard(); }
  } else if (key === "arrowup") {
    e.preventDefault();
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "takeoff" }));
  } else if (key === "arrowdown") {
    e.preventDefault();
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "land" }));
  }
});

window.addEventListener("keyup", e => {
  const key = e.key.toLowerCase();
  if (MOVE_KEYS.includes(key)) {
    e.preventDefault();
    keysPressed.delete(key);
    updateFromKeyboard();
  }
});

window.addEventListener("blur", () => { keysPressed.clear(); updateFromKeyboard(); });
} // ═══ end if (MODE === "control") ═══
</script>
</body>
</html>
"""

# ─────────────────────────────────────────
#  Stream (broadcast) login page
# ─────────────────────────────────────────
STREAM_PASS = "ddm2026"   # unlock via /stream?pass=ddm2026  or the login form (case-insensitive)

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>STREAM LOGIN</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<style>
  body { background: #000; color: #00e5ff; font-family: 'Orbitron', sans-serif; display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100vh; margin: 0; }
  input { padding: 15px; margin: 20px 0; border: 1px solid #00e5ff; background: rgba(0,229,255,0.1); color: #fff; text-align: center; font-family: 'Share Tech Mono', monospace; outline: none; font-size: 1.2rem; }
  button { padding: 15px 40px; background: #00e5ff; color: #000; border: none; font-weight: bold; cursor: pointer; font-size: 1rem; box-shadow: 0 0 15px rgba(0,229,255,0.5); font-family: 'Orbitron', sans-serif;}
  button:hover { background: #fff; box-shadow: 0 0 25px #fff; }
  .error { color: #ff2d55; margin-top: 15px; font-family: 'Share Tech Mono', monospace; letter-spacing: 1px;}
</style>
</head>
<body>
    <form method="POST" style="text-align: center;">
        <h2 style="text-shadow: 0 0 10px #00e5ff; letter-spacing: 2px;">STREAM//ACCESS</h2>
        <input type="password" name="pwd" placeholder="ENTER PASSWORD" autofocus autocomplete="off">
        <br>
        <button type="submit">AUTHENTICATE</button>
        {% if error %}<div class="error">ACCESS DENIED - INVALID PASSCODE</div>{% endif %}
    </form>
</body>
</html>
"""

# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML, mode="control")

@app.route("/stream", methods=["GET", "POST"])
def stream_page():
    # Query-string unlock: /stream?pass=ddm2026  (handy as an OBS browser source)
    q = request.args.get("pass")
    if q is not None and q.lower() == STREAM_PASS:
        return render_template_string(HTML, mode="stream")

    # Login-form unlock (fallback when no valid ?pass= is supplied)
    if request.method == "POST":
        pwd = request.form.get("pwd", "")
        if pwd.lower() == STREAM_PASS:
            return render_template_string(HTML, mode="stream")
        return render_template_string(LOGIN_HTML, error=True)

    return render_template_string(LOGIN_HTML, error=False)

@sock.route("/ws")
def websocket(ws):
    global flying
    print("[WS] Client connected")

    def stream_video():
        quality = 80
        MIN_QUALITY, MAX_QUALITY = 35, 85
        last_sent_id = -1
        last_telemetry_sent = 0.0

        while True:
            loop_start = time.time()

            with frame_lock:
                frame, frame_id = latest_frame, latest_frame_id

            if frame is None:
                placeholder = np.zeros((CAM_HEIGHT, CAM_WIDTH, 3), dtype=np.uint8)
                cv2.putText(placeholder, "NO SIGNAL", (120, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 80), 3)
                frame, frame_id = placeholder, -1

            # Broadcast the pilot's live RC + flight state every tick (~TARGET_FPS Hz) so the
            # /stream page mirrors the joysticks 1-to-1. Battery is bundled in only once a second.
            # Sent before the frame-skip below so mirroring keeps flowing even if video stalls.
            with rc_lock:
                current_rc = dict(rc_state)
            with action_lock:
                current_actions = [dict(a) for a in action_log]
            telemetry = {"rc": current_rc, "flying": flying, "actions": current_actions}
            if loop_start - last_telemetry_sent > 1.0:
                telemetry["battery"] = battery_level
                last_telemetry_sent = loop_start
            try:
                ws.send(json.dumps(telemetry))
            except:
                break

            # Skip re-encoding/re-sending a frame the camera thread hasn't updated yet
            if frame_id == last_sent_id:
                time.sleep(FRAME_INTERVAL)
                continue

            encode_start = time.time()
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            encode_time = time.time() - encode_start

            if ok:
                send_start = time.time()
                try:
                    ws.send(buf.tobytes())  # binary frame — no base64/JSON overhead
                except:
                    break
                send_time = time.time() - send_start
                last_sent_id = frame_id

                # Adaptive quality: back off if encode+send can't keep up with the
                # frame budget, ease back up once the link has headroom again.
                busy_time = encode_time + send_time
                if busy_time > FRAME_INTERVAL * 1.5 and quality > MIN_QUALITY:
                    quality -= 5
                elif busy_time < FRAME_INTERVAL * 0.5 and quality < MAX_QUALITY:
                    quality += 2

            elapsed = time.time() - loop_start
            remaining = FRAME_INTERVAL - elapsed
            if remaining > 0:
                time.sleep(remaining)

    threading.Thread(target=stream_video, daemon=True).start()

    while True:
        data = ws.receive()
        if data is None:
            break
        try:
            msg = json.loads(data)
        except Exception:
            continue

        cmd = msg.get("cmd")

        if cmd == "takeoff" and not flying and connected:
            print("[CMD] Takeoff")
            aid = start_action("TAKEOFF")
            api.single_fly_takeoff()
            finish_action(aid, "DONE")
            flying = True
            time.sleep(2)

        elif cmd == "land" and flying and connected:
            print("[CMD] Land")
            aid = start_action("LAND")
            api.single_fly_touchdown()
            finish_action(aid, "DONE")
            flying = False
            with rc_lock:
                for k in rc_state: rc_state[k] = 0

        elif cmd == "rc":
            with rc_lock:
                rc_state["forward"] = int(msg.get("forward", 0))
                rc_state["right"]   = int(msg.get("right",   0))
                rc_state["up"]      = int(msg.get("up",      0))
                rc_state["yaw"]     = int(msg.get("yaw",     0))

if __name__ == "__main__":
    print("🌐 Manual Control Server running on http://0.0.0.0:8080")
    print("📡 Stream (broadcast) Subpage on   http://0.0.0.0:8080/stream?pass=ddm2026")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
