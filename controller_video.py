from flask import Flask, render_template_string, Response
from flask_sock import Sock
import json, time, threading
import cv2
import numpy as np
import pyhula

app = Flask(__name__)
sock = Sock(app)

# ─────────────────────────────────────────
#  Drone connection
# ─────────────────────────────────────────
api = pyhula.UserApi()
connected = False
flying = False

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
#  RC state — updated by WS, consumed by tick loop
# ─────────────────────────────────────────
rc_state = {"forward": 0, "right": 0, "up": 0, "yaw": 0}
rc_lock  = threading.Lock()

TICK_MS  = 50
STEP_MAX = 20
DEADZONE = 8

def rc_tick_loop():
    while True:
        time.sleep(TICK_MS / 1000)
        if not flying or not connected:
            continue
        with rc_lock:
            fwd = rc_state["forward"]
            rgt = rc_state["right"]
            up  = rc_state["up"]
            yaw = rc_state["yaw"]

        def scale(v):
            if abs(v) < DEADZONE: return 0
            return max(1, int(abs(v) / 100 * STEP_MAX))

        try:
            if abs(fwd) >= DEADZONE:
                if fwd > 0: api.single_fly_forward(scale(fwd))
                else:       api.single_fly_back(scale(fwd))
            if abs(rgt) >= DEADZONE:
                if rgt > 0: api.single_fly_right(scale(rgt))
                else:       api.single_fly_left(scale(rgt))
            if abs(up) >= DEADZONE:
                if up > 0:  api.single_fly_up(scale(up))
                else:       api.single_fly_down(scale(up))
            if abs(yaw) >= DEADZONE:
                if yaw > 0: api.single_fly_turnright(scale(yaw))
                else:       api.single_fly_turnleft(scale(yaw))
        except Exception as e:
            print(f"[RC] Error: {e}")

threading.Thread(target=rc_tick_loop, daemon=True).start()

# ─────────────────────────────────────────
#  Camera — single shared frame buffer
#
#  Design:
#  - camera_loop() runs in one thread, always writes latest BGR frame
#  - gen_mjpeg() runs per-client (one generator each), just reads latest frame
#  - No queue = no memory buildup on slow connections
#  - Adaptive JPEG quality: measures real send time, adjusts up/down
#    to stay near TARGET_MS per frame (~8 fps baseline)
# ─────────────────────────────────────────
latest_frame   = None
frame_lock     = threading.Lock()
camera_running = False

CAM_WIDTH   = 320
CAM_HEIGHT  = 240
QUALITY_MIN = 15
QUALITY_MAX = 60
TARGET_MS   = 120   # ~8 fps target

def camera_loop():
    global latest_frame, camera_running
    camera_running = True
    print("[CAM] Initialising stream...")
    try:
        api.Plane_cmd_swith_rtp(0)
        time.sleep(1)
        api.single_fly_flip_rtp()
        time.sleep(1)
        print("[CAM] Stream active.")
    except Exception as e:
        print(f"[CAM] Init error: {e}")
        camera_running = False
        return

    while camera_running:
        try:
            frame = api.get_image_array()
            if frame is None:
                time.sleep(0.05)
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = cv2.resize(frame, (CAM_WIDTH, CAM_HEIGHT),
                               interpolation=cv2.INTER_LINEAR)
            with frame_lock:
                latest_frame = frame
        except Exception as e:
            print(f"[CAM] Frame error: {e}")
            time.sleep(0.1)

    print("[CAM] Stopped.")

def gen_mjpeg():
    """One generator instance per HTTP client. Adaptive JPEG quality."""
    quality = 35

    while True:
        t0 = time.time()

        with frame_lock:
            frame = latest_frame

        if frame is None:
            # Black placeholder with NO SIGNAL text until camera warms up
            placeholder = np.zeros((CAM_HEIGHT, CAM_WIDTH, 3), dtype=np.uint8)
            cv2.putText(placeholder, "NO SIGNAL", (55, 125),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 2)
            frame = placeholder

        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            time.sleep(0.05)
            continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n"
               + buf.tobytes() + b"\r\n")

        elapsed_ms = (time.time() - t0) * 1000

        # Adapt quality based on real throughput
        if elapsed_ms > TARGET_MS * 1.4 and quality > QUALITY_MIN:
            quality = max(QUALITY_MIN, quality - 4)
        elif elapsed_ms < TARGET_MS * 0.6 and quality < QUALITY_MAX:
            quality = min(QUALITY_MAX, quality + 2)

        # Pace loop — don't spin faster than drone can supply frames
        sleep_s = max(0, (TARGET_MS / 1000) - (time.time() - t0))
        time.sleep(sleep_s)

# Auto-start camera if drone connected
if connected:
    threading.Thread(target=camera_loop, daemon=True).start()

# ─────────────────────────────────────────
#  HTML UI
# ─────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>DRONE//CTRL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:      #080c10;
    --panel:   #0d1520;
    --border:  #1a2e45;
    --accent:  #00e5ff;
    --danger:  #ff2d55;
    --ok:      #00ff88;
    --dim:     #2a4060;
    --text:    #cde8ff;
    --mono:    'Share Tech Mono', monospace;
    --display: 'Orbitron', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }

  /* Scanline */
  body::before {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,0.07) 2px, rgba(0,0,0,0.07) 4px
    );
    pointer-events: none; z-index: 999;
  }

  /* Grid */
  body::after {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(rgba(0,229,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,229,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none; z-index: 0;
  }

  header {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 18px 24px 10px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border);
  }

  .logo {
    font-family: var(--display);
    font-size: 1.3rem; font-weight: 900;
    letter-spacing: 4px;
    color: var(--accent);
    text-shadow: 0 0 20px var(--accent);
  }
  .logo span { color: var(--danger); }

  #conn-status {
    font-size: 0.7rem; letter-spacing: 2px;
    padding: 4px 12px; border-radius: 2px;
    border: 1px solid var(--dim); color: var(--dim);
    transition: all 0.3s;
  }
  #conn-status.live { border-color: var(--ok); color: var(--ok); text-shadow: 0 0 8px var(--ok); box-shadow: 0 0 12px rgba(0,255,136,0.15); }
  #conn-status.dead { border-color: var(--danger); color: var(--danger); }

  #telem {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 8px 24px;
    display: flex; gap: 20px; flex-wrap: wrap;
    font-size: 0.68rem; letter-spacing: 1px;
    color: var(--dim);
    border-bottom: 1px solid var(--border);
  }
  #telem span { color: var(--accent); }
  #fly-state { margin-left: auto; }
  #fly-state.airborne { color: var(--ok); text-shadow: 0 0 8px var(--ok); }

  main {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 16px;
    display: flex; flex-direction: column; align-items: center;
    gap: 16px;
  }

  /* ── CAMERA ── */
  .cam-wrap {
    position: relative;
    width: 100%; max-width: 640px;
    background: #000;
    border: 1px solid var(--border);
    border-radius: 2px;
    overflow: hidden;
    aspect-ratio: 4/3;
  }
  .cam-wrap img {
    width: 100%; height: 100%;
    object-fit: cover; display: block;
  }
  .cam-hud {
    position: absolute; inset: 0;
    pointer-events: none;
  }
  /* HUD corner brackets */
  .cam-hud::before, .cam-hud::after {
    content: '';
    position: absolute;
    width: 20px; height: 20px;
    border-color: var(--accent); border-style: solid;
    opacity: 0.55;
  }
  .cam-hud::before { top: 8px;    left: 8px;   border-width: 2px 0 0 2px; }
  .cam-hud::after  { bottom: 8px; right: 8px;  border-width: 0 2px 2px 0; }

  .cam-label {
    position: absolute; top: 8px; right: 12px;
    font-size: 0.6rem; letter-spacing: 2px;
    color: var(--accent); opacity: 0.7;
  }
  #fps-display {
    position: absolute; bottom: 8px; left: 12px;
    font-size: 0.58rem; color: var(--ok);
    letter-spacing: 1px; opacity: 0.85;
  }
  /* Crosshair */
  .crosshair {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 22px; height: 22px; opacity: 0.3;
  }
  .crosshair::before, .crosshair::after {
    content: ''; position: absolute; background: var(--accent);
  }
  .crosshair::before { width: 1px; height: 100%; left: 50%; top: 0; }
  .crosshair::after  { height: 1px; width: 100%; top: 50%; left: 0; }

  /* ── BUTTONS ── */
  .action-row { display: flex; gap: 16px; width: 100%; max-width: 500px; }

  .btn {
    flex: 1; padding: 14px;
    border: 1px solid; background: transparent;
    font-family: var(--display);
    font-size: 0.75rem; font-weight: 700; letter-spacing: 3px;
    cursor: pointer; border-radius: 2px;
    transition: all 0.15s;
    position: relative; overflow: hidden;
  }
  .btn::after { content: ''; position: absolute; inset: 0; background: currentColor; opacity: 0; transition: opacity 0.15s; }
  .btn:active::after { opacity: 0.15; }

  .btn-takeoff { border-color: var(--ok);    color: var(--ok);    box-shadow: 0 0 16px rgba(0,255,136,0.1); }
  .btn-takeoff:hover { background: rgba(0,255,136,0.08); box-shadow: 0 0 24px rgba(0,255,136,0.25); }
  .btn-land    { border-color: var(--danger); color: var(--danger); box-shadow: 0 0 16px rgba(255,45,85,0.1); }
  .btn-land:hover    { background: rgba(255,45,85,0.08);  box-shadow: 0 0 24px rgba(255,45,85,0.25); }

  /* ── STICKS ── */
  .sticks { display: flex; justify-content: center; align-items: center; gap: 40px; width: 100%; }
  .stick-wrap { display: flex; flex-direction: column; align-items: center; gap: 10px; }
  .stick-label { font-size: 0.6rem; letter-spacing: 2px; color: var(--dim); }
  .stick-label span { color: var(--accent); font-size: 0.65rem; }

  .joystick-zone {
    width: 150px; height: 150px;
    border-radius: 50%;
    background: var(--panel);
    border: 1px solid var(--border);
    position: relative;
    touch-action: none;
    box-shadow: inset 0 0 30px rgba(0,0,0,0.5), 0 0 0 1px rgba(0,229,255,0.05), 0 4px 24px rgba(0,0,0,0.4);
  }
  .joystick-zone::before, .joystick-zone::after { content: ''; position: absolute; background: var(--border); }
  .joystick-zone::before { width: 1px; height: 100%; left: 50%; top: 0; }
  .joystick-zone::after  { height: 1px; width: 100%; top: 50%; left: 0; }

  .knob {
    width: 50px; height: 50px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #1a3a5a, #0a1a2a);
    border: 2px solid var(--accent);
    box-shadow: 0 0 12px rgba(0,229,255,0.4), 0 0 0 4px rgba(0,229,255,0.08);
    position: absolute; top: 50px; left: 50px;
    pointer-events: none; z-index: 2;
  }
  .joystick-zone.active .knob {
    box-shadow: 0 0 20px rgba(0,229,255,0.7), 0 0 0 6px rgba(0,229,255,0.15);
  }

  /* ── RC READOUT ── */
  .rc-readout { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; width: 100%; max-width: 500px; }
  .rc-cell { background: var(--panel); border: 1px solid var(--border); border-radius: 2px; padding: 8px; text-align: center; }
  .rc-cell-label { font-size: 0.55rem; letter-spacing: 2px; color: var(--dim); margin-bottom: 3px; }
  .rc-cell-val   { font-family: var(--display); font-size: 0.95rem; color: var(--accent); }

  .corner-dec { position: relative; padding: 4px; }
  .corner-dec::before, .corner-dec::after { content: ''; position: absolute; width: 10px; height: 10px; border-color: var(--accent); border-style: solid; opacity: 0.4; }
  .corner-dec::before { top: 0; left: 0; border-width: 1px 0 0 1px; }
  .corner-dec::after  { bottom: 0; right: 0; border-width: 0 1px 1px 0; }

  @media (max-width: 480px) {
    .joystick-zone { width: 125px; height: 125px; }
    .knob { width: 42px; height: 42px; top: 41px; left: 41px; }
    .sticks { gap: 16px; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">DRONE<span>//</span>CTRL</div>
  <div id="conn-status">OFFLINE</div>
</header>

<div id="telem">
  <div>FWD: <span id="t-fwd">0</span></div>
  <div>RGT: <span id="t-rgt">0</span></div>
  <div>UP:  <span id="t-up">0</span></div>
  <div>YAW: <span id="t-yaw">0</span></div>
  <div id="fly-state">GROUNDED</div>
</div>

<main>

  <!-- CAMERA FEED — pure <img> pointing to /video MJPEG stream -->
  <div class="cam-wrap">
    <img id="cam" src="/video" alt="FPV feed"
         onerror="setTimeout(()=>{ this.src='/video?t='+Date.now(); }, 2000)">
    <div class="cam-hud">
      <div class="cam-label">LIVE · FPV</div>
      <div class="crosshair"></div>
      <div id="fps-display">-- fps</div>
    </div>
  </div>

  <div class="action-row">
    <button class="btn btn-takeoff" id="btn-takeoff">▲ TAKEOFF</button>
    <button class="btn btn-land"    id="btn-land">▼ LAND</button>
  </div>

  <div class="sticks">
    <div class="stick-wrap">
      <div class="corner-dec">
        <div class="joystick-zone" id="left-zone"><div class="knob" id="left-knob"></div></div>
      </div>
      <div class="stick-label">THROTTLE · <span>YAW</span></div>
    </div>
    <div class="stick-wrap">
      <div class="corner-dec">
        <div class="joystick-zone" id="right-zone"><div class="knob" id="right-knob"></div></div>
      </div>
      <div class="stick-label">PITCH · <span>ROLL</span></div>
    </div>
  </div>

  <div class="rc-readout">
    <div class="rc-cell"><div class="rc-cell-label">FORWARD</div><div class="rc-cell-val" id="v-fwd">0</div></div>
    <div class="rc-cell"><div class="rc-cell-label">RIGHT</div>  <div class="rc-cell-val" id="v-rgt">0</div></div>
    <div class="rc-cell"><div class="rc-cell-label">UP</div>     <div class="rc-cell-val" id="v-up">0</div></div>
    <div class="rc-cell"><div class="rc-cell-label">YAW</div>    <div class="rc-cell-val" id="v-yaw">0</div></div>
  </div>

</main>

<script>
// ── Control WebSocket ──
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(proto + "//" + location.host + "/ws");
const statusEl = document.getElementById("conn-status");
const flyEl    = document.getElementById("fly-state");

ws.onopen  = () => { statusEl.textContent = "LIVE";    statusEl.className = "live"; };
ws.onclose = () => { statusEl.textContent = "OFFLINE"; statusEl.className = "dead"; };

function send(obj) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
ws.onmessage = e => {
  const msg = JSON.parse(e.data);
  if (msg.flying !== undefined) {
    flyEl.textContent = msg.flying ? "AIRBORNE" : "GROUNDED";
    flyEl.className   = msg.flying ? "airborne" : "";
  }
};

// ── FPS counter — measured from actual image load events ──
const camEl = document.getElementById("cam");
const fpsEl = document.getElementById("fps-display");
let fTimes  = [];

camEl.addEventListener("load", () => {
  const now = performance.now();
  fTimes.push(now);
  if (fTimes.length > 12) fTimes.shift();
  if (fTimes.length >= 2) {
    const span = fTimes[fTimes.length - 1] - fTimes[0];
    fpsEl.textContent = ((fTimes.length - 1) / span * 1000).toFixed(1) + " fps";
  }
});

// ── Buttons ──
document.getElementById("btn-takeoff").onclick = () => send({ cmd: "takeoff" });
document.getElementById("btn-land").onclick    = () => send({ cmd: "land"    });

// ── RC ──
const rc = { forward: 0, right: 0, up: 0, yaw: 0 };
function updateTelem() {
  ["fwd","rgt","up","yaw"].forEach((k, i) => {
    const val = [rc.forward, rc.right, rc.up, rc.yaw][i];
    document.getElementById("t-" + k).textContent = val;
    document.getElementById("v-" + k).textContent = val;
  });
}
function sendRC() { send({ cmd: "rc", ...rc }); updateTelem(); }

// ── Joystick factory ──
function makeJoystick(zoneId, knobId, onMove) {
  const zone = document.getElementById(zoneId);
  const knob = document.getElementById(knobId);
  const R = zone.offsetWidth / 2;
  const cx = R, cy = R, limit = R - 26;
  let active = false, pid = null;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function move(ex, ey) {
    const rect = zone.getBoundingClientRect();
    let dx = ex - rect.left - cx, dy = ey - rect.top - cy;
    const dist = Math.hypot(dx, dy);
    if (dist > limit) { dx *= limit / dist; dy *= limit / dist; }
    knob.style.left = (cx + dx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy + dy - knob.offsetHeight / 2) + "px";
    onMove(
      Math.round(clamp(dx / limit * 100, -100, 100)),
      Math.round(clamp(dy / limit * 100, -100, 100))
    );
  }
  function reset() {
    knob.style.left = (cx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy - knob.offsetHeight / 2) + "px";
    onMove(0, 0);
  }

  zone.addEventListener("pointerdown", e => {
    active = true; pid = e.pointerId;
    zone.setPointerCapture(e.pointerId);
    zone.classList.add("active");
    move(e.clientX, e.clientY);
  });
  zone.addEventListener("pointermove", e => {
    if (active && e.pointerId === pid) move(e.clientX, e.clientY);
  });
  ["pointerup","pointercancel"].forEach(ev =>
    zone.addEventListener(ev, () => { active = false; zone.classList.remove("active"); reset(); })
  );
  reset();
}

makeJoystick("left-zone",  "left-knob",  (x, y) => { rc.yaw = x; rc.up = -y; sendRC(); });
makeJoystick("right-zone", "right-knob", (x, y) => { rc.right = x; rc.forward = -y; sendRC(); });
</script>
</body>
</html>
"""

# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/video")
def video():
    """MJPEG stream — completely separate from WS so it can't block controls."""
    return Response(
        gen_mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@sock.route("/ws")
def websocket(ws):
    global flying
    print("[WS] Client connected")
    ws.send(json.dumps({"flying": flying}))

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
            api.single_fly_takeoff()
            flying = True
            time.sleep(2)
            ws.send(json.dumps({"flying": True}))

        elif cmd == "land" and flying and connected:
            print("[CMD] Land")
            api.single_fly_touchdown()
            flying = False
            with rc_lock:
                for k in rc_state: rc_state[k] = 0
            ws.send(json.dumps({"flying": False}))

        elif cmd == "rc":
            with rc_lock:
                rc_state["forward"] = int(msg.get("forward", 0))
                rc_state["right"]   = int(msg.get("right",   0))
                rc_state["up"]      = int(msg.get("up",      0))
                rc_state["yaw"]     = int(msg.get("yaw",     0))

    print("[WS] Client disconnected")
    with rc_lock:
        for k in rc_state: rc_state[k] = 0

if __name__ == "__main__":
    print("🌐 Bridge server on http://0.0.0.0:8080")
    print("   Tunnel: cloudflared tunnel --url http://localhost:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)