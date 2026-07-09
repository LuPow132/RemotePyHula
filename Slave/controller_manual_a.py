# ========================================================
# 🚀 FORCE NETWORK INTERFACE (ดัก IP ให้ Manual Control)
# ========================================================
import socket
import struct
import sys

# When launched by the agent, stdout is a pipe (not a console), so Python falls back to
# the system codepage (cp874 on Thai Windows) and every emoji print below would raise
# UnicodeEncodeError and kill the process at import. Force UTF-8 so that can't happen.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
from collections import deque
import cv2
import numpy as np
import pyhula

# Low-latency decode: don't let ffmpeg sit on a big analysis/reorder buffer.
# (Old value used 50MB/50s probe windows, which adds startup + steady-state buffering.)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "fflags;nobuffer|flags;low_delay|analyzeduration;1000000|probesize;500000|timeout;2000"
)

app = Flask(__name__)
sock = Sock(app)

# ─────────────────────────────────────────
#  Drone connection
# ─────────────────────────────────────────
api = pyhula.UserApi()
connected = False
battery_level = "--"

# "landed" | "flying" | "unknown"
# UNKNOWN means the link dropped mid-flight, so our cached idea of whether the drone is
# airborne can no longer be trusted (most drones auto-land on link loss). pyhula exposes
# no flight-state query, so we must NOT guess: RC is frozen and the pilot resolves it.
flight_state = "landed"

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
#  (Battery polling + link health-check now live in the Connection Manager
#   further down, so a mid-session drop triggers an automatic reconnect.)
# ─────────────────────────────────────────

# ─────────────────────────────────────────
#  Camera Thread
# ─────────────────────────────────────────
latest_frame    = None
latest_frame_id = 0
frame_lock      = threading.Lock()
camera_running  = False
cam_fps         = 0.0       # measured capture rate — the hard ceiling on stream fps
CAM_WIDTH   = 960
CAM_HEIGHT  = 640
TARGET_FPS  = 15
FRAME_INTERVAL = 1.0 / TARGET_FPS

def camera_loop():
    global latest_frame, latest_frame_id, camera_running, cam_fps
    camera_running = True
    last_cap = 0.0
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

            # Measure the true capture rate — no amount of network tuning can exceed it.
            now_cap = time.time()
            if last_cap:
                dt = now_cap - last_cap
                if dt > 0:
                    inst = 1.0 / dt
                    cam_fps = inst if not cam_fps else cam_fps * 0.9 + inst * 0.1
            last_cap = now_cap
        except:
            time.sleep(0.1)

# ─────────────────────────────────────────
#  Shared JPEG encoder — encode each frame ONCE and reuse it for every viewer
#  (pilot + /stream + OBS), instead of re-encoding per WebSocket client.
# ─────────────────────────────────────────
_enc_lock     = threading.Lock()
_enc_frame_id = -1
_enc_map      = {}          # (quality, scale) -> jpeg bytes, for the current frame only

def encode_frame(frame, frame_id, quality, scale):
    global _enc_frame_id, _enc_map
    key = (quality, scale)
    with _enc_lock:
        if frame_id == _enc_frame_id and key in _enc_map:
            return _enc_map[key]

    img = frame
    if scale < 0.999:
        h, w = frame.shape[:2]
        img = cv2.resize(frame, (max(2, int(w * scale)), max(2, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    data = buf.tobytes()

    with _enc_lock:
        if frame_id != _enc_frame_id:
            _enc_frame_id, _enc_map = frame_id, {}
        _enc_map[key] = data
    return data

def start_camera():
    """Launch the camera pipeline thread if it isn't already running."""
    global camera_running
    if not camera_running:
        camera_running = True
        threading.Thread(target=camera_loop, daemon=True).start()

if connected:
    start_camera()

# ─────────────────────────────────────────
#  Connection Manager — polls battery as a health check and AUTO-RECONNECTS the
#  drone link if it drops mid-session (also connects late if the drone wasn't
#  available at startup).
# ─────────────────────────────────────────
def connection_manager():
    global connected, battery_level, camera_running, flight_state, cam_fps
    fails = 0
    while True:
        time.sleep(3)
        if connected:
            # Reading battery doubles as a liveness probe for the drone link.
            try:
                if hasattr(api, 'get_electic'):  val = api.get_electic()
                elif hasattr(api, 'get_battery'): val = api.get_battery()
                else: val = "OK"
                if val in (None, "", -1):
                    raise RuntimeError("empty battery reading")
                battery_level = str(val)
                fails = 0
            except Exception:
                fails += 1
                if fails >= 3:                    # ~9s of failed reads → assume the link is down
                    print("⚠️ [LINK] Drone connection lost — auto-reconnecting...")
                    connected = False
                    camera_running = False        # stop the now-dead camera loop
                    battery_level = "--"
                    cam_fps = 0.0

                    # We can no longer trust our flight state: the drone may have
                    # auto-landed while the link was down. Freeze the sticks and make
                    # the pilot tell us, rather than firing RC at a grounded drone
                    # (which errors out) with TAKEOFF locked behind flying==True.
                    if flight_state == "flying":
                        flight_state = "unknown"
                        print("⚠️ [LINK] Lost link while FLYING — flight state is now UNKNOWN.")
                    with rc_lock:
                        for k in rc_state:
                            rc_state[k] = 0
        else:
            # Keep hammering connect() until the drone comes back.
            try:
                ok = api.connect()
            except Exception:
                ok = False
            if ok:
                print("✅ [LINK] Drone reconnected.")
                connected = True
                fails = 0
                start_camera()                    # relaunch the camera pipeline
            else:
                print("📡 [LINK] Reconnect attempt failed, retrying...")

threading.Thread(target=connection_manager, daemon=True).start()

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
        # Only steer when we KNOW it's airborne. "unknown" freezes the sticks.
        if flight_state != "flying" or not connected:
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

  /* Flight-state recovery prompt after a mid-flight link loss */
  #recover {
      position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
      z-index: 50; display: none; text-align: center; pointer-events: auto;
      background: rgba(10,0,6,0.92); border: 2px solid var(--danger); border-radius: 8px;
      padding: 18px 26px; box-shadow: 0 0 40px rgba(255,45,85,0.5);
  }
  #recover.show { display: block; }
  #recover h3 { font-family:'Orbitron',sans-serif; color:var(--danger); font-size:0.95rem;
                letter-spacing:2px; margin-bottom:6px; }
  #recover p  { font-size:0.68rem; color:#ffd0d8; margin-bottom:14px; letter-spacing:1px; }
  #recover .rbtns { display:flex; gap:12px; justify-content:center; }
  #recover button {
      font-family:'Orbitron',sans-serif; font-weight:900; font-size:0.72rem; letter-spacing:1px;
      padding:11px 16px; border-radius:6px; cursor:pointer; background:transparent; color:#fff;
      border:2px solid;
  }
  #btn-ground { border-color: var(--ok);     color: var(--ok); }
  #btn-air    { border-color: var(--accent); color: var(--accent); }

  /* LOW-LATENCY vs SMOOTH video toggle */
  #vmode-btn {
      font-family: 'Orbitron', sans-serif; font-size: 0.55rem; font-weight: 700; letter-spacing: 1px;
      padding: 3px 8px; border-radius: 3px; cursor: pointer; background: transparent;
      border: 1px solid var(--accent); color: var(--accent);
  }
  #vmode-btn.smooth { border-color: var(--ok); color: var(--ok); }
  #vmode-btn:active { transform: scale(0.92); }

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
            <button id="vmode-btn" title="LOW = freshest frame · SMOOTH = even cadence">LOW</button>
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

    <div id="recover">
        <h3>LINK RECOVERED &mdash; STATE UNKNOWN</h3>
        <p>The link dropped mid-flight. Sticks are frozen.<br>Where is the drone right now?</p>
        <div class="rbtns">
            <button id="btn-ground">ON GROUND</button>
            <button id="btn-air">STILL IN AIR</button>
        </div>
    </div>

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
const statusEl = document.getElementById("conn-status");
const batEl = document.getElementById("battery-status");
const camEl = document.getElementById("cam");
const fpsEl = document.getElementById("fps-display");

// Self-healing WebSocket: reconnects after wifi blips, phone sleep, or server restarts.
let ws = null;
let reconnectTimer = null;
let reconnectDelay = 500;   // backoff, grows up to a cap

function connectWS() {
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.onopen = () => {
    statusEl.textContent = "LINK ACTIVE"; statusEl.className = "live";
    reconnectDelay = 500;   // reset backoff after a healthy connect
    ws.send(JSON.stringify({ cmd: "mode", video: videoMode }));   // re-announce after reconnect
  };
  ws.onclose = () => {
    statusEl.textContent = "RECONNECTING"; statusEl.className = "warn";
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };   // force onclose → reconnect
  ws.onmessage = handleMessage;
}

function scheduleReconnect() {
  if (reconnectTimer) return;               // one pending attempt at a time
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWS();
  }, reconnectDelay);
  reconnectDelay = Math.min(Math.round(reconnectDelay * 1.6), 5000);
}

// Reconnect instantly when the network returns or the tab/phone wakes back up.
function ensureConnected() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  reconnectDelay = 500;
  connectWS();
}
window.addEventListener("online", ensureConnected);
document.addEventListener("visibilitychange", () => { if (!document.hidden) ensureConnected(); });

let fTimes = [];
let lastFrameUrl = null;
let lastRtt = 0, lastWin = 0, lastKbps = 0, lastCfps = 0;   // server-measured link stats
const recoverEl = document.getElementById("recover");

// Resolve an UNKNOWN flight state after a mid-flight reconnect. The server freezes the
// sticks until this is answered, so we can never fire RC at a drone that auto-landed.
document.getElementById("btn-ground").onclick = () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "set_state", state: "landed" }));
  recoverEl.classList.remove("show");
};
document.getElementById("btn-air").onclick = () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cmd: "set_state", state: "flying" }));
  recoverEl.classList.remove("show");
};

// Video delivery mode. The pilot wants the freshest frame; the broadcast audience
// wants even cadence and doesn't care about a few hundred ms of lag — so they differ
// by default. Choice is per-viewer and remembered.
const vmodeBtn = document.getElementById("vmode-btn");
const FRAME_MS = 1000 / 15;      // matches the server's TARGET_FPS
const JITTER_MAX = 3;            // max queued frames in SMOOTH; caps the added lag
let frameQ = [];
let renderTimer = null;
let videoMode = (function () {
  try { return localStorage.getItem("videoMode") || (MODE === "stream" ? "smooth" : "low"); }
  catch (_) { return MODE === "stream" ? "smooth" : "low"; }
})();

function handleMessage(e) {
  if (typeof e.data === "string") {
    const msg = JSON.parse(e.data);
    if (msg.rtt  !== undefined) lastRtt  = msg.rtt;
    if (msg.win  !== undefined) lastWin  = msg.win;
    if (msg.kbps !== undefined) lastKbps = msg.kbps;
    if (msg.cfps !== undefined) lastCfps = msg.cfps;

    // Mid-flight link loss leaves the flight state unresolved — ask the pilot.
    if (msg.state !== undefined) {
      const unknown = (msg.state === "unknown");
      recoverEl.classList.toggle("show", unknown && MODE === "control");
      if (unknown && MODE === "stream") {
        statusEl.textContent = "STATE UNKNOWN"; statusEl.className = "warn";
      }
    }
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

  if (videoMode === "smooth") {
    // Queue it; a fixed-rate timer paints it. ACK on RECEIPT so the pipe stays full —
    // the jitter buffer, not the network, decides cadence.
    frameQ.push(url);
    while (frameQ.length > JITTER_MAX) URL.revokeObjectURL(frameQ.shift());  // bound the lag
    ackNow();
  } else {
    // LOW: paint immediately, ACK only once painted. That ACK is the backpressure
    // signal, so frames can never queue up ahead of the renderer.
    paint(url, ackNow);
  }
}

function ackNow() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('{"cmd":"ack"}');
}

function paint(url, after) {
  const prevUrl = lastFrameUrl;
  const done = () => {
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    countFps();
    if (after) after();
  };
  camEl.onload  = done;
  camEl.onerror = done;          // never stall the stream on a bad frame
  camEl.src = url;
  lastFrameUrl = url;
}

function countFps() {
  const now = performance.now();
  fTimes.push(now);
  if (fTimes.length > 12) fTimes.shift();
  if (fTimes.length < 2) return;
  const span = fTimes[fTimes.length - 1] - fTimes[0];
  fpsEl.textContent = ((fTimes.length - 1) / span * 1000).toFixed(1) + " FPS"
                    + (lastCfps ? " / cam " + lastCfps : "")
                    + (lastRtt  ? " · " + lastRtt + "ms" : "")
                    + (lastWin  ? " · w" + lastWin : "")
                    + (lastKbps ? " · " + lastKbps + "kbps" : "");
}

// ─── SMOOTH mode: drain the jitter buffer, aligned to the display's refresh ───
// A setInterval(66.7ms) timer beats against a 16.7ms vsync, so frames land on uneven
// refreshes and you see judder even at a "correct" fps. Driving it from rAF snaps each
// paint to a real refresh, which is most of what "smooth" actually means.
let rafId = null, nextDue = 0;
function renderTick(ts) {
  if (!renderTimer) return;
  rafId = requestAnimationFrame(renderTick);
  if (!frameQ.length) return;
  if (ts < nextDue) return;                       // hold cadence at ~FRAME_MS
  nextDue = (nextDue && ts - nextDue < FRAME_MS) ? nextDue + FRAME_MS : ts + FRAME_MS;
  paint(frameQ.shift(), null);
}
function startRenderLoop() {
  if (renderTimer) return;
  renderTimer = true;
  nextDue = 0;
  rafId = requestAnimationFrame(renderTick);
}
function stopRenderLoop() {
  renderTimer = null;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  while (frameQ.length) URL.revokeObjectURL(frameQ.shift());
}

function setVideoMode(m, tell) {
  videoMode = (m === "smooth") ? "smooth" : "low";
  try { localStorage.setItem("videoMode", videoMode); } catch (_) {}
  vmodeBtn.textContent = videoMode === "smooth" ? "SMOOTH" : "LOW";
  vmodeBtn.classList.toggle("smooth", videoMode === "smooth");
  if (videoMode === "smooth") startRenderLoop(); else stopRenderLoop();
  if (tell && ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ cmd: "mode", video: videoMode }));
}

vmodeBtn.onclick = () => setVideoMode(videoMode === "smooth" ? "low" : "smooth", true);
setVideoMode(videoMode, false);   // apply the saved/default choice before connecting

// Open the first connection (auto-reconnect takes over from here)
connectWS();

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

# Port 8080 is published to the public internet by the Tailscale Funnel, so a remote
# LAND endpoint MUST be authenticated — otherwise any player could ground the fleet.
# The agent injects this into the controller's environment when it spawns us. Empty
# (i.e. run standalone) disables the endpoint entirely.
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")

@app.route("/land", methods=["POST"])
def land_now():
    """Remote emergency land, used by the master dashboard's ALL LAND."""
    global flight_state
    if not CONTROL_TOKEN or request.headers.get("X-Token") != CONTROL_TOKEN:
        return {"ok": False, "error": "unauthorized"}, 401
    if not connected:
        return {"ok": False, "error": "drone not connected"}, 409
    if flight_state == "landed":
        return {"ok": True, "already_landed": True, "state": flight_state}

    aid = start_action("LAND")           # allowed from "flying" AND "unknown"
    try:
        api.single_fly_touchdown()
        finish_action(aid, "DONE")
    except Exception as ex:
        finish_action(aid, "FAIL")
        return {"ok": False, "error": str(ex)}, 500

    flight_state = "landed"
    with rc_lock:
        for k in rc_state: rc_state[k] = 0
    return {"ok": True, "state": flight_state}

@app.route("/health")
def health():
    # Localhost-only status probe consumed by the slave agent / master dashboard.
    return {
        "connected": connected,
        "flying": flight_state == "flying",   # kept for the agent/dashboard
        "flight_state": flight_state,
        "battery": battery_level,
        "camera": camera_running,
        "cam_fps": round(cam_fps, 1),
    }

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

# Frames in flight are limited by a self-tuning window, because throughput is
#     fps = min(camera_fps, window / RTT)
# The window grows while RTT stays near its floor (latency-bound: the pipe is idle, so
# widening costs nothing) and shrinks when RTT inflates (bandwidth-bound: our own frames
# are queueing). It finds the bandwidth-delay product on its own.
#
# To hit 15 fps you need window >= RTT * 15, so a 1s relay needs 15. The old cap of 8
# silently throttled high-RTT links to 8 fps and made delivery bursty.
WINDOW_MIN         = 2
WINDOW_MAX_LOW     = 10   # LOW LATENCY mode: freshest frame wins
WINDOW_MAX_SMOOTH  = 24   # SMOOTH mode: keep the pipe full, client jitter-buffers
ACK_STALL_SECS     = 1.5  # if a client never acks (old page), resume anyway
# Sliding RTT floor (~10s at TARGET_FPS). Long enough that a slowly-building queue can't
# drag the floor up with it (which would hide the bloat), short enough that a genuinely
# slower network re-baselines instead of being flagged as congested forever.
RTT_FLOOR_SAMPLES  = 150
BLOAT_STREAK       = 3    # need sustained inflation before shrinking (ignore paint jitter)

# camera_loop runs uncapped, so latest_frame_id can advance far faster than TARGET_FPS.
# Pace releases so a wide window doesn't dump several captures back-to-back.
PACE_INTERVAL = 1.0 / TARGET_FPS

@sock.route("/ws")
def websocket(ws):
    global flight_state
    print("[WS] Client connected")

    # Small writes (telemetry JSON) interleaved with big binary frames let Nagle's
    # algorithm sit on data for ~40-200ms waiting to coalesce. Turn it off.
    try:
        raw = getattr(ws, "sock", None) or getattr(getattr(ws, "ws", None), "sock", None)
        if raw is not None:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("[WS] TCP_NODELAY enabled")
    except Exception:
        pass

    # Per-connection backpressure state, updated by the ACKs read in the main loop below.
    st = {
        "inflight": 0, "sent_ts": deque(), "last_ack": time.time(),
        "rtt": 0.0, "min_rtt": 0.0, "window": 2, "grow": 0, "bad": 0,
        "rtt_hist": deque(maxlen=RTT_FLOOR_SAMPLES),
        "bytes": 0, "rate": 0.0, "rate_ts": time.time(), "acks": 0,
        "vmode": "low",     # "low" = freshest frame; "smooth" = even cadence (jitter-buffered)
    }
    st_lock = threading.Lock()

    def stream_video():
        quality, scale = 60, 1.0
        MIN_Q, MAX_Q = 30, 80
        last_sent_id = -1
        last_telemetry = 0.0
        last_battery   = 0.0
        next_send      = 0.0

        while True:
            now = time.time()

            # ---- Telemetry: small + independent of the video backpressure ----
            if now - last_telemetry > FRAME_INTERVAL:
                with rc_lock:
                    current_rc = dict(rc_state)
                with action_lock:
                    current_actions = [dict(a) for a in action_log]
                telemetry = {"rc": current_rc, "actions": current_actions,
                             "flying": flight_state == "flying", "state": flight_state,
                             "rtt": int(st["rtt"] * 1000), "win": st["window"],
                             "kbps": int(st["rate"] * 8 / 1000), "vmode": st["vmode"],
                             "cfps": round(cam_fps, 1)}
                if now - last_battery > 1.0:
                    telemetry["battery"] = battery_level
                    last_battery = now
                try:
                    ws.send(json.dumps(telemetry))
                except Exception:
                    break
                last_telemetry = now

            # ---- Backpressure gate: bounded by the self-tuning window ----
            with st_lock:
                if st["inflight"] and (now - st["last_ack"]) > ACK_STALL_SECS:
                    st["inflight"] = 0          # client isn't acking — don't deadlock
                    st["sent_ts"].clear()
                inflight, window = st["inflight"], st["window"]
            if inflight >= window:
                time.sleep(0.003)
                continue

            # ---- Pacing: release frames evenly instead of in bursts ----
            if now < next_send:
                time.sleep(0.002)
                continue

            with frame_lock:
                frame, frame_id = latest_frame, latest_frame_id
            if frame is None:
                placeholder = np.zeros((CAM_HEIGHT, CAM_WIDTH, 3), dtype=np.uint8)
                cv2.putText(placeholder, "NO SIGNAL", (120, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 80), 3)
                frame, frame_id = placeholder, -1

            # Nothing new to show — always send the FRESHEST frame, never a stale queued one.
            if frame_id == last_sent_id:
                time.sleep(0.004)
                continue

            data = encode_frame(frame, frame_id, quality, scale)
            if data is None:
                continue
            try:
                ws.send(data)               # binary frame — no base64/JSON overhead
            except Exception:
                break
            last_sent_id = frame_id
            sent_at = time.time()
            next_send = sent_at + PACE_INTERVAL
            with st_lock:
                st["inflight"] += 1
                st["sent_ts"].append(sent_at)     # FIFO: acks arrive in send order
                st["bytes"] += len(data)
                if sent_at - st["rate_ts"] >= 1.0:
                    st["rate"]  = st["bytes"] / (sent_at - st["rate_ts"])   # bytes/sec
                    st["bytes"], st["rate_ts"] = 0, sent_at

            # ---- Adapt picture quality to the REAL link (ack RTT, not buffer writes) ----
            rtt, floor = st["rtt"], st["min_rtt"]
            bloated = floor and rtt > max(floor * 1.8, floor + 0.12)
            if bloated:                       # frames are queueing → shed bytes
                quality = max(MIN_Q, quality - 6)
                if rtt > max(floor * 3.0, floor + 0.35):
                    scale = max(0.5, round(scale - 0.1, 2))
            elif rtt and not bloated:         # link has headroom → creep back up
                quality = min(MAX_Q, quality + 2)
                scale   = min(1.0, round(scale + 0.05, 2))

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

        # Browser painted a frame → release the next one. Each ack pairs FIFO with the
        # frame that produced it, giving a true per-frame round-trip.
        if cmd == "ack":
            with st_lock:
                now = time.time()
                if st["inflight"] > 0:
                    st["inflight"] -= 1
                if not st["sent_ts"]:
                    st["last_ack"] = now
                    continue
                sample = now - st["sent_ts"].popleft()
                st["rtt"]     = sample if not st["rtt"] else st["rtt"] * 0.7 + sample * 0.3
                st["last_ack"] = now
                st["acks"] += 1

                # RTT floor over a SLIDING window. An all-time minimum gets poisoned by
                # one lucky early sample (e.g. the tiny "NO SIGNAL" placeholder frame),
                # after which every normal frame looks "bloated" and the window collapses.
                st["rtt_hist"].append(sample)
                floor = min(st["rtt_hist"])
                st["min_rtt"] = floor

                wmax = WINDOW_MAX_SMOOTH if st["vmode"] == "smooth" else WINDOW_MAX_LOW

                # Window control. Shrink only on SUSTAINED inflation, so a single slow
                # browser paint can't ratchet us down (shrink is per-ack, growth is
                # per-round-trip, so a naive rule is biased downward).
                if sample > max(floor * 1.5, floor + 0.10):
                    st["bad"] += 1
                    if st["bad"] >= BLOAT_STREAK:
                        st["window"] = max(WINDOW_MIN, st["window"] - 1)
                        st["bad"], st["grow"] = 0, 0
                else:
                    st["bad"] = 0
                    st["grow"] += 1
                    if st["grow"] >= st["window"]:       # at most +1 per round-trip
                        st["window"] = min(wmax, st["window"] + 1)
                        st["grow"] = 0
                st["window"] = min(st["window"], wmax)   # clamp if the mode just changed
            continue

        # Viewer chose LOW-LATENCY vs SMOOTH — only affects this one connection.
        if cmd == "mode":
            want = "smooth" if msg.get("video") == "smooth" else "low"
            with st_lock:
                st["vmode"] = want
                st["window"] = min(st["window"],
                                   WINDOW_MAX_SMOOTH if want == "smooth" else WINDOW_MAX_LOW)
            print(f"[WS] video mode -> {want}")
            continue

        # Pilot resolves an UNKNOWN state after a mid-flight reconnect.
        if cmd == "set_state":
            want = msg.get("state")
            if want in ("landed", "flying") and flight_state == "unknown":
                flight_state = want
                print(f"[CMD] Pilot resolved flight state -> {want}")
                with rc_lock:
                    for k in rc_state: rc_state[k] = 0
            continue

        if cmd == "takeoff" and flight_state == "landed" and connected:
            print("[CMD] Takeoff")
            aid = start_action("TAKEOFF")
            api.single_fly_takeoff()
            finish_action(aid, "DONE")
            flight_state = "flying"
            time.sleep(2)

        # LAND is allowed from "unknown" too — it's the safe recovery action.
        elif cmd == "land" and flight_state in ("flying", "unknown") and connected:
            print("[CMD] Land")
            aid = start_action("LAND")
            api.single_fly_touchdown()
            finish_action(aid, "DONE")
            flight_state = "landed"
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